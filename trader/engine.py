"""The trading engine: ORB + Pat's supply & demand, candle by candle.

For every closed 5-minute candle (all markets merged in time order):
1. open trades are checked against stop, target and session end;
2. during the opening range (first 15 min of London / New York) the range is recorded;
3. after the range, existing zones are checked for Pat's entry (tap + close back outside),
   dead zones (close through) and falling knives;
4. a new zone is created when the candle completes a displacement leg (big candles + FVG +
   break of structure) that breaks the opening range in the direction of the 15m trend.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .broker import Broker
from .orders import ACTIVE, housekeeping, make_intent
from .models import Candle, Trade, Zone
from .sessions import Window, ams_hm, windows_at
from .structure import aggregate, atr, pivots, trend
from .zones import detect_displacement, evaluate_touch, make_zone, zone_is_valid

KEEP_CANDLES = 900
KEEP_EVENTS = 400
KEEP_DAYS_MS = 4 * 86_400_000


class Engine:
    def __init__(self, cfg: dict, state: dict, now_ms: int, live_from: Optional[int] = None):
        self.cfg = cfg
        self.state = state
        self.scfg = cfg["strategy"]
        self.acfg = cfg["account"]
        self.markets = {k: v for k, v in cfg["markets"].items() if v.get("enabled")}
        self.candle_ms = self.scfg["candle_minutes"] * 60_000
        self.now = now_ms
        if state.get("started_t") is None:
            state["started_t"] = now_ms if live_from is None else live_from
        self.live_from = state["started_t"] if live_from is None else live_from
        self.broker = Broker(state, self.acfg)
        self.zones: List[Zone] = [Zone.from_dict(z) for z in state.get("zones", [])]
        self.new_events: List[dict] = []
        self.order_changes: List[str] = []
        state.setdefault("orders", [])
        state.setdefault("order_seq", 0)
        self._trend_cache: Dict[tuple, str] = {}

    # ---- data ----------------------------------------------------------------------
    def ingest(self, market: str, candles: List[Candle]) -> int:
        """Merge closed candles into the cache. Returns how many were new."""
        cache = {int(r[0]): r for r in self.state["candles"].get(market, [])}
        before = len(cache)
        for c in candles:
            if c.t + self.candle_ms <= self.now:
                cache[c.t] = c.to_list()
        rows = sorted(cache.values(), key=lambda r: r[0])[-KEEP_CANDLES:]
        self.state["candles"][market] = rows
        return len(cache) - before

    def history(self, market: str) -> List[Candle]:
        return [Candle.from_list(r) for r in self.state["candles"].get(market, [])]

    def eurusd(self) -> float:
        return float(self.state.get("eurusd") or self.acfg["eurusd_fallback"])

    # ---- main loop -----------------------------------------------------------------
    def run(self) -> List[dict]:
        hist = {m: self.history(m) for m in self.markets}
        order = {m: n for n, m in enumerate(self.markets)}
        queue = []
        for m, cs in hist.items():
            last = self.state["last_ts"].get(m)
            for i, c in enumerate(cs):
                if last is None or c.t > last:
                    queue.append((c.t, order[m], m, i))
        queue.sort()
        for _, _, m, i in queue:
            self.process(m, hist[m], i)
            self.state["last_ts"][m] = hist[m][i].t
        prices = {m: cs[-1].c for m, cs in hist.items() if cs}
        for change in housekeeping(self.state["orders"], self.now, prices):
            self.order_changes.append(change)
        self._persist()
        return self.new_events

    def _persist(self) -> None:
        horizon = self.now - KEEP_DAYS_MS
        self.state["trades"] = self.broker.dump()
        self.state["zones"] = [z.to_dict() for z in self.zones if z.created_t >= horizon]
        self.state["sessions"] = {k: v for k, v in self.state["sessions"].items() if v.get("open", 0) >= horizon}
        self.state["events"] = self.state["events"][-KEEP_EVENTS:]
        self.state["blackouts"] = [b for b in self.state.get("blackouts", []) if b["end"] >= horizon]
        self.state["orders"] = [o for o in self.state["orders"]
                                if o["status"] in ACTIVE or o["created_t"] >= self.now - 30 * 86_400_000]

    # ---- per candle ----------------------------------------------------------------
    def process(self, m: str, cs: List[Candle], i: int) -> None:
        c = cs[i]
        if m == "EURUSD":
            self.state["eurusd"] = c.c
        for t in self.broker.manage(m, c, self.candle_ms):
            self._event_exit(t)

        mcfg = self.markets[m]
        a = atr(cs[max(0, i - self.scfg["atr_period"]):i + 1], self.scfg["atr_period"])
        for w in windows_at(c.t, mcfg, self.cfg["sessions"]):
            self._session_candle(m, cs, i, a, w)

    def _session_candle(self, m: str, cs: List[Candle], i: int, a: float, w: Window) -> None:
        c = cs[i]
        key = f"{m}|{w.key}"
        ss = self.state["sessions"].setdefault(key, {
            "market": m, "session": w.name, "label": w.label, "day": w.day, "open": w.open,
            "or_high": None, "or_low": None, "range_done": False, "trades": 0, "skipped_origins": [],
        })
        if c.t < w.range_end:
            if c.t >= w.open:
                ss["or_high"] = c.h if ss["or_high"] is None else max(ss["or_high"], c.h)
                ss["or_low"] = c.l if ss["or_low"] is None else min(ss["or_low"], c.l)
            return
        if not ss["range_done"]:
            ss["range_done"] = True
            if ss["or_high"] is not None and c.t >= self.live_from - 6 * 3_600_000:
                self._event(m, c, "Openingsrange", f"Openingsrange {w.label} staat",
                            f"{m}: range van het eerste kwartier {self._p(m, ss['or_low'])} – {self._p(m, ss['or_high'])}. "
                            f"Wacht op een uitbraak met displacement (grote candles, FVG en break of structure) in de richting van de 15m-trend.",
                            "Geen trade; zones zoeken", "")
        if ss["or_high"] is None or c.t >= w.entry_until:
            return

        # 1) existing zones of this session
        for z in [z for z in self.zones if z.session_key == key and z.status in ("fresh", "touched") and z.created_t < c.t]:
            res = evaluate_touch(z, c, a, self.scfg)
            if res == "none":
                continue
            if res == "touched":
                if z.status == "fresh":
                    z.status, z.status_t = "touched", c.t
            elif res == "dead":
                z.status, z.status_t, z.note = "dead", c.t, "candle sloot door de zone"
                self._event(m, c, "Zone vervallen", f"{self._zname(z)} vervallen",
                            f"Een candle sloot door de {self._zname(z).lower()} ({self._p(m, z.bottom)} – {self._p(m, z.top)}). "
                            "Volgens Pats regel is een zone dan dood; geen trade.", "Geen trade", "")
            elif res == "knife":
                z.status, z.status_t, z.note = "skipped", c.t, "vallend mes"
                self._event(m, c, "Overgeslagen", f"{self._zname(z)}: vallend mes",
                            "De koers dook met een grote candle de zone in. Pat: geen vallend mes vangen, dus deze zone vervalt.",
                            "Geen trade", "")
            elif res == "entry":
                self._try_entry(m, z, cs, i, a, w, ss)

        # 2) new displacement / zone
        det = detect_displacement(cs, i, self.scfg, a)
        if not det or not zone_is_valid(det, self.scfg):
            return
        origin = det["zone_candle"].t
        side = det["side"]
        for z in self.zones:
            if z.session_key == key and z.origin_t == origin and z.side == side:
                if z.status == "fresh":
                    z.leg_extreme = max(z.leg_extreme, det["leg_extreme"]) if side == "long" else min(z.leg_extreme, det["leg_extreme"])
                return
        if origin in ss["skipped_origins"]:
            return
        tr = self._trend(m, cs, i)
        want = "up" if side == "long" else "down"
        run = cs[det["run_start"]:i + 1]
        broke = any(x.c > ss["or_high"] for x in run) if side == "long" else any(x.c < ss["or_low"] for x in run)
        why = []
        if self.scfg.get("require_trend", True) and tr != want:
            why.append(f"15m-trend is {self._trend_nl(tr)}")
        if self.scfg.get("require_or_break", True) and not broke:
            why.append("beweging breekt de openingsrange niet")
        if why:
            ss["skipped_origins"].append(origin)
            if c.t >= self.live_from:
                self._event(m, c, "Overgeslagen", f"Displacement zonder setup ({'omhoog' if side == 'long' else 'omlaag'})",
                            f"Sterke beweging met FVG en break of structure, maar {', '.join(why)}. Geen zone.", "Geen trade", "")
            return
        self.state["zone_seq"] += 1
        z = make_zone(det, m, key, c.t, self.state["zone_seq"])
        z.checks["trend_15m"] = tr
        z.checks["or_break"] = broke
        self.zones.append(z)
        if c.t >= self.live_from:
            self._event(m, c, "Nieuwe zone", f"Nieuwe {self._zname(z).lower()} na uitbraak {w.label}",
                        f"{det['run_len']} grote candles {'omhoog' if side == 'long' else 'omlaag'} (gem. {det['avg_body_x']}x normale grootte) "
                        f"met fair value gap en break of structure, door de openingsrange heen en mee met de 15m-trend. "
                        f"Zone {self._p(m, z.bottom)} – {self._p(m, z.top)}. Wacht op een terugval naar de zone en een slot weer erbuiten.",
                        "Zone actief, wacht op entry", f"slot {'onder' if side == 'long' else 'boven'} {self._p(m, z.bottom if side == 'long' else z.top)}")

    def _try_entry(self, m: str, z: Zone, cs: List[Candle], i: int, a: float, w: Window, ss: dict) -> None:
        c = cs[i]
        long = z.side == "long"
        entry = c.c
        buf = self.scfg["stop_buffer_atr"] * a
        stop = z.bottom - buf if long else z.top + buf
        seg = [x for x in cs[:i + 1] if x.t >= z.origin_t]
        target = max([z.leg_extreme] + [x.h for x in seg]) if long else min([z.leg_extreme] + [x.l for x in seg])
        risk = abs(entry - stop)
        rr = ((target - entry) if long else (entry - target)) / risk if risk > 0 else 0.0

        why = []
        if c.t < self.live_from:
            z.status, z.status_t, z.note = "skipped", c.t, "voor de start van het account"
            return
        if ss["trades"] >= self.scfg["max_trades_per_session"]:
            why.append("al een trade in deze sessie")
        if self.broker.open_trades(m):
            why.append("er staat al een positie open in deze markt")
        ok, reason = self.broker.can_open(c.t)
        if not ok:
            why.append(reason)
        bo = self._blackout(m, c.t)
        if bo:
            why.append(f"nieuwsfilter: {bo}")
        if rr < self.acfg["min_rr"]:
            why.append(f"R:R 1:{rr:.2f} onder minimum 1:{self.acfg['min_rr']}")
        if self.scfg.get("prefer_extreme_zone"):
            reach = self.scfg["extreme_zone_atr"] * a
            deeper = [o for o in self.zones if o is not z and o.session_key == z.session_key and o.side == z.side
                      and o.status == "fresh" and ((o.top < z.bottom and z.bottom - o.top <= reach) if long
                                                   else (o.bottom > z.top and o.bottom - z.top <= reach))]
            if deeper:
                why.append("een diepere zone ligt dichtbij (Pat: de laagste demand / hoogste supply is de sterkste)")
        if why:
            z.status, z.status_t, z.note = "skipped", c.t, "; ".join(why)
            self._event(m, c, "Overgeslagen", f"Entry-signaal {self._zname(z).lower()} niet genomen",
                        f"De koers tikte de zone aan en sloot er weer buiten, maar: {'; '.join(why)}.", "Geen trade", "")
            return

        discount = entry < (z.bottom + target) / 2 if long else entry > (z.top + target) / 2
        idx0 = next((k for k, x in enumerate(cs) if x.t >= z.origin_t), i)
        prior = pivots(cs[max(0, idx0 - 120):idx0], self.scfg["swing_pivot"])
        flips = [p[2] for p in prior if p[1] == ("H" if long else "L") and z.bottom <= p[2] <= z.top]
        checks = dict(z.checks)
        checks.update({"vers": True, "discount": discount, "level_stack": bool(flips), "rr": round(rr, 2)})
        t = self.broker.open(
            m, z.side, entry, stop, target, c.t + self.candle_ms,
            setup=f"ORB {w.label} + {'demand' if long else 'supply'}-zone", session_key=z.session_key,
            close_at=w.close, zone_id=z.id, checks=checks,
            reason=(f"Zone {self._p(m, z.bottom)} – {self._p(m, z.top)} aangetikt, candle sloot er weer buiten. "
                    f"Trend 15m {self._trend_nl(checks.get('trend_15m', ''))}; FVG {'ja' if checks.get('fvg') else 'nee'}; "
                    f"BOS {'ja' if checks.get('bos') else 'nee'}; discount {'ja' if discount else 'nee'}; level stack {'ja' if flips else 'nee'}."))
        z.status, z.status_t = "entered", c.t
        ss["trades"] += 1
        self._new_intent(t, w)
        self._event(m, c, "Entry", f"Signaal {'long' if long else 'short'} {m} ({t.id}), paper-order klaar ter bevestiging",
                    t.reason + f" Doel = recente {'top' if long else 'bodem'} van de beweging.",
                    f"{'Long' if long else 'Short'} op {self._p(m, entry)} · stop {self._p(m, stop)} · doel {self._p(m, target)} (1:{rr:.2f})",
                    f"stop {self._p(m, stop)} of sessie-einde")

    def _new_intent(self, t: Trade, w: Window) -> None:
        """Order intent for the Liquid paper account (only for live signals)."""
        mcfg = self.markets[t.market]
        if not mcfg.get("symbol") or t.opened_t < self.live_from:
            return
        self.state["order_seq"] += 1
        expires = min(w.entry_until, t.opened_t + self.acfg["intent_valid_minutes"] * 60_000)
        o = make_intent(t, mcfg["symbol"], mcfg, self.acfg, self.broker.sizing_equity(t.opened_t), t.opened_t,
                        expires, w.close, self.state["order_seq"])
        self.state["orders"].append(o)
        self.order_changes.append(f"{o['id']} {o['status']} voor {t.id}")

    # ---- helpers -------------------------------------------------------------------
    def _trend(self, m: str, cs: List[Candle], i: int) -> str:
        tm = self.scfg["trend_minutes"] * 60_000
        bucket = cs[i].t - cs[i].t % tm
        key = (m, bucket)
        if key not in self._trend_cache:
            higher = aggregate(cs[max(0, i - 3 * 120):i + 1], self.scfg["trend_minutes"], self.scfg["candle_minutes"])
            self._trend_cache[key] = trend(higher[-80:], self.scfg["swing_pivot"])
        return self._trend_cache[key]

    def trend_now(self, m: str) -> str:
        cs = self.history(m)
        return self._trend(m, cs, len(cs) - 1) if cs else "range"

    def _blackout(self, m: str, ts: int) -> str:
        for b in self.state.get("blackouts", []):
            if b["start"] <= ts <= b["end"] and ("all" in b.get("markets", ["all"]) or m in b.get("markets", [])):
                return b.get("reason", "nieuws")
        return ""

    def _event_exit(self, t: Trade) -> None:
        ts = (t.closed_t or self.now) - self.candle_ms
        c = Candle(ts, t.exit or 0, t.exit or 0, t.exit or 0, t.exit or 0)
        res = "winst" if (t.pnl_usd or 0) > 0 else "verlies"
        self._event(t.market, c, "Exit", f"{t.id} gesloten: {t.exit_reason} ({res})",
                    f"{t.side.capitalize()} {t.market} van {self._p(t.market, t.entry)} gesloten op {self._p(t.market, t.exit)} "
                    f"via {t.exit_reason}. Modelresultaat ${t.pnl_usd:,.2f} ({t.r_result:+.2f}R).",
                    f"Positie gesloten · ${t.pnl_usd:,.2f}", "")

    def _event(self, m: str, c: Candle, trigger: str, title: str, text: str, action: str, invalidation: str) -> None:
        self.state["event_seq"] += 1
        ev = {
            "id": f"E{self.state['event_seq']:05d}", "t": c.t + self.candle_ms, "market": m, "price": c.c,
            "bias": self._trend_nl(self._trend_cache.get((m, c.t - c.t % (self.scfg['trend_minutes'] * 60_000)), "")),
            "trigger": trigger, "title": title, "text": text, "action": action, "invalidation": invalidation,
            "when": ams_hm(c.t + self.candle_ms),
        }
        self.state["events"].append(ev)
        self.new_events.append(ev)

    def _p(self, m: str, v: Optional[float]) -> str:
        if v is None:
            return "—"
        d = self.cfg["markets"].get(m, {}).get("decimals", 2)
        return f"{v:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")

    @staticmethod
    def _zname(z: Zone) -> str:
        return "Demand-zone" if z.side == "long" else "Supply-zone"

    @staticmethod
    def _trend_nl(tr: str) -> str:
        return {"up": "omhoog", "down": "omlaag", "range": "zijwaarts"}.get(tr, "onbekend")
