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
from . import swing as swing_mod
from .structure import aggregate, atr, pivots, trend
from .zones import detect_displacement, evaluate_touch, make_zone, zone_is_valid

KEEP_CANDLES = 900
KEEP_EVENTS = 400
KEEP_DAYS_MS = 4 * 86_400_000
KEEP_HTF = 400


class Engine:
    def __init__(self, cfg: dict, state: dict, now_ms: int, live_from: Optional[int] = None):
        self.cfg = cfg
        self.state = state
        self.base_scfg = cfg["strategy"]
        self.scfg = self.base_scfg
        # a market can run several strategies: at most one session based (S&D or ORB) plus swing modes
        self.market_scfgs = {k: [dict(self.base_scfg, **(x or {})) for x in
                                 (v.get("strategy") if isinstance(v.get("strategy"), list) else [v.get("strategy")])]
                             for k, v in cfg["markets"].items()}
        self.market_scfg = {k: next((x for x in v if x.get("mode") != "swing"), v[0])
                            for k, v in self.market_scfgs.items()}
        self.acfg = cfg["account"]
        self.markets = {k: v for k, v in cfg["markets"].items() if v.get("enabled")}
        self.candle_ms = self.scfg["candle_minutes"] * 60_000
        self.now = now_ms
        if state.get("started_t") is None:
            state["started_t"] = now_ms if live_from is None else live_from
        self.live_from = state["started_t"] if live_from is None else live_from
        self.broker = Broker(state, self.acfg)
        self.zones: List[Zone] = [Zone.from_dict(z) for z in state.get("zones", [])]
        self.zones_by_key: Dict[str, List[Zone]] = {}
        for z in self.zones:
            self.zones_by_key.setdefault(z.session_key, []).append(z)
        self.new_events: List[dict] = []
        self.order_changes: List[str] = []
        state.setdefault("orders", [])
        state.setdefault("order_seq", 0)
        self._trend_cache: Dict[tuple, str] = {}
        self._htf_cache: Dict[str, List[Candle]] = {}

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
        for m, rows in self._htf_cache.items():
            self.state.setdefault("htf", {})[m] = [r.to_list() for r in rows[-KEEP_HTF:]]
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
        if not mcfg.get("trade", True):
            return
        for sc in self.market_scfgs[m]:
            self.scfg = sc
            if sc.get("mode") == "swing":
                self._swing_candle(m, cs, i)
                continue
            a = atr(cs[max(0, i - sc["atr_period"]):i + 1], sc["atr_period"])
            for w in windows_at(c.t, mcfg, self.cfg["sessions"]):
                self._session_candle(m, cs, i, a, w)
        self.scfg = self.base_scfg

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
        if self.scfg.get("mode", "sd") == "orb":
            self._orb_candle(m, cs, i, a, w, ss)
            return

        # 1) existing zones of this session
        limit_mode = self.scfg.get("entry_mode", "close") == "limit"
        for z in [z for z in self.zones_by_key.get(key, []) if z.status in ("fresh", "touched") and z.created_t < c.t]:
            if limit_mode:
                self._limit_check(m, z, cs, i, a, w, ss)
                continue
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
        for z in self.zones_by_key.get(key, []):
            if z.origin_t == origin and z.side == side:
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
        self.zones_by_key.setdefault(key, []).append(z)
        if limit_mode:
            zc = det["zone_candle"]
            body_top, body_bot = max(zc.o, zc.c), min(zc.o, zc.c)
            limit = (body_top + body_bot) / 2 if body_top > body_bot else (z.top + z.bottom) / 2
            z.checks["limit"] = limit
            plan = self._plan(z, cs, i, a, limit)
            problems = self._plan_problems(plan)
            if problems:
                z.status, z.status_t, z.note = "skipped", c.t, "; ".join(problems)
                if c.t >= self.live_from:
                    self._event(m, c, "Overgeslagen", f"{self._zname(z)} zonder limietorder", "Zone gevonden, maar: "
                                + "; ".join(problems) + ".", "Geen order", "")
                return
            self._new_zone_intent(z, plan, limit, w, c)
        if c.t >= self.live_from:
            self._event(m, c, "Nieuwe zone", f"Nieuwe {self._zname(z).lower()} na uitbraak {w.label}",
                        f"{det['run_len']} grote candles {'omhoog' if side == 'long' else 'omlaag'} (gem. {det['avg_body_x']}x normale grootte) "
                        f"met fair value gap en break of structure, door de openingsrange heen en mee met de 15m-trend. "
                        f"Zone {self._p(m, z.bottom)} – {self._p(m, z.top)}. Wacht op een terugval naar de zone en een slot weer erbuiten.",
                        "Zone actief, wacht op entry", f"slot {'onder' if side == 'long' else 'boven'} {self._p(m, z.bottom if side == 'long' else z.top)}")

    # ---- entries -------------------------------------------------------------------
    def _plan(self, z: Zone, cs: List[Candle], i: int, a: float, entry: float) -> dict:
        """Stop, target and R:R for an entry at `entry` from zone z."""
        long = z.side == "long"
        buf = self.scfg["stop_buffer_atr"] * a
        stop = z.bottom - buf if long else z.top + buf
        risk = abs(entry - stop)
        if self.scfg.get("target_mode", "leg") == "r":
            tr = self.scfg.get("target_r", 2.0)
            target = entry + tr * risk if long else entry - tr * risk
        else:
            seg = [x for x in cs[max(0, i - 300):i + 1] if x.t >= z.origin_t]
            target = max([z.leg_extreme] + [x.h for x in seg]) if long else min([z.leg_extreme] + [x.l for x in seg])
        rr = ((target - entry) if long else (entry - target)) / risk if risk > 0 else 0.0
        return {"stop": stop, "target": target, "rr": rr, "risk": risk,
                "stop_pct": risk / entry * 100 if entry else 0.0}

    def _plan_problems(self, plan: dict) -> List[str]:
        why = []
        if plan["risk"] <= 0:
            why.append("geen ruimte tussen entry en stop")
        if plan["stop_pct"] < self.scfg.get("min_stop_pct", 0.0):
            why.append(f"stop te krap ({plan['stop_pct']:.3f}% < {self.scfg['min_stop_pct']}%, kosten te hoog)")
        if self.scfg.get("target_mode", "leg") != "r" and plan["rr"] < self.acfg["min_rr"]:
            why.append(f"R:R 1:{plan['rr']:.2f} onder minimum 1:{self.acfg['min_rr']}")
        return why

    def _try_entry(self, m: str, z: Zone, cs: List[Candle], i: int, a: float, w: Window, ss: dict) -> None:
        """Pat's normal entry: the candle that tapped the zone closed back outside it."""
        c = cs[i]
        self._enter(m, z, cs, i, a, w, ss, entry=c.c, opened_t=c.t + self.candle_ms, fill_candle=None,
                    how="De koers tikte de zone aan en sloot er weer buiten")

    def _limit_check(self, m: str, z: Zone, cs: List[Candle], i: int, a: float, w: Window, ss: dict) -> None:
        """Pat's aggressive entry: a resting limit in the middle of the zone candle's body."""
        c = cs[i]
        limit = z.checks.get("limit")
        if limit is None:
            return
        long = z.side == "long"
        if (long and c.l > limit) or (not long and c.h < limit):
            return
        prev = cs[i - 1] if i > 0 else None
        if prev is not None and a > 0 and prev.body > self.scfg["falling_knife_atr"] * a and \
                ((long and prev.bear) or (not long and prev.bull)):
            z.status, z.status_t, z.note = "skipped", c.t, "vallend mes: limiet geannuleerd"
            self._event(m, c, "Overgeslagen", f"{self._zname(z)}: vallend mes",
                        "Een grote candle dook richting de zone; de limietorder wordt geannuleerd (Pat: geen vallend mes vangen).",
                        "Order annuleren", "")
            return
        fill = min(limit, c.o) if long else max(limit, c.o)
        self._enter(m, z, cs, i, a, w, ss, entry=fill, opened_t=c.t + self.candle_ms, fill_candle=c,
                    how="De limietorder in de zone werd gevuld")

    def _enter(self, m: str, z: Zone, cs: List[Candle], i: int, a: float, w: Window, ss: dict, entry: float,
               opened_t: int, fill_candle: Optional[Candle], how: str) -> None:
        c = cs[i]
        long = z.side == "long"
        plan = self._plan(z, cs, i, a, entry)
        stop, target, rr = plan["stop"], plan["target"], plan["rr"]

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
        why += self._plan_problems(plan)
        if self.scfg.get("prefer_extreme_zone"):
            reach = self.scfg["extreme_zone_atr"] * a
            deeper = [o for o in self.zones_by_key.get(z.session_key, []) if o is not z and o.side == z.side
                      and o.status == "fresh" and ((o.top < z.bottom and z.bottom - o.top <= reach) if long
                                                   else (o.bottom > z.top and o.bottom - z.top <= reach))]
            if deeper:
                why.append("een diepere zone ligt dichtbij (Pat: de laagste demand / hoogste supply is de sterkste)")
        if why:
            z.status, z.status_t, z.note = "skipped", c.t, "; ".join(why)
            self._event(m, c, "Overgeslagen", f"Entry-signaal {self._zname(z).lower()} niet genomen",
                        f"{how}, maar: {'; '.join(why)}.", "Geen trade", "")
            return

        discount = entry < (z.bottom + target) / 2 if long else entry > (z.top + target) / 2
        idx0 = next((k for k in range(max(0, i - 300), i + 1) if cs[k].t >= z.origin_t), i)
        prior = pivots(cs[max(0, idx0 - 120):idx0], self.scfg["swing_pivot"])
        flips = [p[2] for p in prior if p[1] == ("H" if long else "L") and z.bottom <= p[2] <= z.top]
        checks = dict(z.checks)
        checks.update({"vers": True, "discount": discount, "level_stack": bool(flips), "rr": round(rr, 2),
                       "risk0": plan["risk"]})
        if self.scfg.get("breakeven_r"):
            checks["be_r"] = self.scfg["breakeven_r"]
        t = self.broker.open(
            m, z.side, entry, stop, target, opened_t,
            setup=f"ORB {w.label} + {'demand' if long else 'supply'}-zone", session_key=z.session_key,
            close_at=w.close, zone_id=z.id, checks=checks,
            reason=(f"{how}. Zone {self._p(m, z.bottom)} – {self._p(m, z.top)}. "
                    f"Trend {self._trend_nl(checks.get('trend_15m', ''))}; FVG {'ja' if checks.get('fvg') else 'nee'}; "
                    f"BOS {'ja' if checks.get('bos') else 'nee'}; discount {'ja' if discount else 'nee'}; level stack {'ja' if flips else 'nee'}."))
        z.status, z.status_t = "entered", c.t
        ss["trades"] += 1
        if self.scfg.get("entry_mode", "close") == "close":
            self._new_intent(t, w)
        self._event(m, c, "Entry", f"{'Long' if long else 'Short'} {m} ({t.id})",
                    t.reason, f"{'Long' if long else 'Short'} op {self._p(m, entry)} · stop {self._p(m, stop)} · doel {self._p(m, target)} (1:{rr:.2f})",
                    f"stop {self._p(m, stop)} of sessie-einde")
        if fill_candle is not None and ((long and fill_candle.l <= stop) or (not long and fill_candle.h >= stop)):
            self.broker.close(t, stop, fill_candle.t + self.candle_ms, "stop loss")
            self._event_exit(t)

    # ---- classic opening range breakout --------------------------------------------
    def _orb_candle(self, m: str, cs: List[Candle], i: int, a: float, w: Window, ss: dict) -> None:
        """Breakout of the opening range. entry 'close': first 5m close outside the range;
        'retest': after the breakout, the first candle that touches the broken edge and closes back outside."""
        c = cs[i]
        hi, lo = ss["or_high"], ss["or_low"]
        if ss["trades"] >= self.scfg["max_trades_per_session"] or ss.get("orb_done"):
            return
        rng = hi - lo
        if rng <= 0:
            return
        side = ss.get("orb_side")
        if side is None:
            if c.c > hi:
                side = "long"
            elif c.c < lo:
                side = "short"
            else:
                return
            ss["orb_side"] = side
            if self.scfg.get("orb_entry", "close") == "retest":
                return  # wait for the retest
        else:
            if self.scfg.get("orb_entry", "close") != "retest":
                return
            if side == "long" and not (c.l <= hi and c.c > hi):
                if c.c < lo:
                    ss["orb_done"] = True
                return
            if side == "short" and not (c.h >= lo and c.c < lo):
                if c.c > hi:
                    ss["orb_done"] = True
                return
        ss["orb_done"] = True
        long = side == "long"
        why = []
        if self.scfg.get("require_trend", True):
            tr = self._trend(m, cs, i)
            if tr != ("up" if long else "down"):
                why.append(f"trend is {self._trend_nl(tr)}")
        if self.scfg.get("min_range_atr") and a > 0 and rng < self.scfg["min_range_atr"] * a:
            why.append("openingsrange te klein")
        if self.scfg.get("max_range_atr") and a > 0 and rng > self.scfg["max_range_atr"] * a:
            why.append("openingsrange te groot")
        entry = c.c
        if self.scfg.get("orb_stop", "opposite") == "mid":
            stop = (hi + lo) / 2
        else:
            stop = lo if long else hi
        risk = abs(entry - stop)
        if risk <= 0 or (long and stop >= entry) or (not long and stop <= entry):
            why.append("geen ruimte voor de stop")
        elif risk / entry * 100 < self.scfg.get("min_stop_pct", 0.0):
            why.append("stop te krap (kosten)")
        if self.broker.open_trades(m):
            why.append("er staat al een positie open in deze markt")
        ok, reason = self.broker.can_open(c.t)
        if not ok:
            why.append(reason)
        bo = self._blackout(m, c.t)
        if bo:
            why.append(f"nieuwsfilter: {bo}")
        if c.t < self.live_from:
            return
        if why:
            self._event(m, c, "Overgeslagen", f"ORB-uitbraak {w.label} niet genomen",
                        f"Slot {'boven' if long else 'onder'} de openingsrange, maar: {'; '.join(why)}.", "Geen trade", "")
            return
        tr_r = self.scfg.get("target_r", 2.0)
        target = entry + tr_r * risk if long else entry - tr_r * risk
        checks = {"orb_high": hi, "orb_low": lo, "risk0": risk, "rr": tr_r}
        if self.scfg.get("breakeven_r"):
            checks["be_r"] = self.scfg["breakeven_r"]
        t = self.broker.open(m, side, entry, stop, target, c.t + self.candle_ms,
                             setup=f"ORB-uitbraak {w.label}", strategy="ORB-uitbraak", session_key=f"{m}|{w.key}",
                             close_at=w.close, checks=checks,
                             reason=f"5m-slot {'boven' if long else 'onder'} de openingsrange {self._p(m, lo)} – {self._p(m, hi)}.")
        ss["trades"] += 1
        self._new_intent(t, w)
        self._event(m, c, "Entry", f"{'Long' if long else 'Short'} {m} ({t.id}), ORB-uitbraak", t.reason,
                    f"{'Long' if long else 'Short'} op {self._p(m, entry)} · stop {self._p(m, stop)} · doel {self._p(m, target)} (1:{tr_r})",
                    f"stop {self._p(m, stop)} of sessie-einde")

    # ---- Trade with Pat swing (1h / 4h) -----------------------------------------------
    def ingest_htf(self, market: str, candles: List[Candle], tf_minutes: int) -> int:
        """Merge closed higher-timeframe candles (e.g. Liquid 1h) into state['htf']."""
        tf_ms = tf_minutes * 60_000
        key = f"{market}@{tf_minutes}"
        store = self.state.setdefault("htf", {})
        cache = {int(r[0]): r for r in store.get(key, [])}
        before = len(cache)
        for c in candles:
            if c.t % tf_ms == 0 and c.t + tf_ms <= self.now:
                cache[c.t] = c.to_list()
        store[key] = sorted(cache.values(), key=lambda r: r[0])[-KEEP_HTF:]
        self._htf_cache.pop(key, None)
        return len(cache) - before

    def _htf(self, m: str, cs: List[Candle], i: int, tf_ms: int) -> Optional[List[Candle]]:
        """Higher-timeframe candles up to and including the bucket that closes with cs[i]."""
        c = cs[i]
        if (c.t + self.candle_ms) % tf_ms:
            return None
        key = c.t + self.candle_ms - tf_ms
        need = tf_ms // self.candle_ms
        skey = f"{m}@{tf_ms // 60_000}"
        store = self.state.setdefault("htf", {})
        rows = self._htf_cache.get(skey)
        if rows is None:
            rows = [Candle.from_list(r) for r in store.get(skey, [])]
        if not rows or rows[-1].t < key:
            g = [x for x in cs[max(0, i - need + 1):i + 1] if x.t >= key]
            if len(g) == need:
                rows.append(Candle(key, g[0].o, max(x.h for x in g), min(x.l for x in g), g[-1].c, sum(x.v for x in g)))
        rows = rows[-KEEP_HTF:]
        self._htf_cache[skey] = rows
        if not rows or rows[-1].t != key:
            return None
        return rows

    def _swing_candle(self, m: str, cs: List[Candle], i: int) -> None:
        p = swing_mod.params(self.scfg)
        tf_ms = p["tf_minutes"] * 60_000
        h = self._htf(m, cs, i, tf_ms)
        if not h:
            return
        c = cs[i]
        setup = swing_mod.find_setup(h, p)
        if not setup:
            return
        used = self.state.setdefault("swing_used", {}).setdefault(f"{m}@{p['tf_minutes']}", [])
        if setup["impulse_t"] in used:
            return
        used.append(setup["impulse_t"])
        del used[:-50]
        long = setup["side"] == "long"
        tf = f"{p['tf_minutes'] // 60}u" if p["tf_minutes"] >= 60 else f"{p['tf_minutes']}m"
        why = []
        if self.broker.open_trades(m):
            why.append("er staat al een positie open in deze markt")
        ok, reason = self.broker.can_open(c.t)
        if not ok:
            why.append(reason)
        bo = self._blackout(m, c.t)
        if bo:
            why.append(f"nieuwsfilter: {bo}")
        risk = abs(setup["entry"] - setup["stop"])
        if risk / setup["entry"] * 100 < self.scfg.get("min_stop_pct", 0.0):
            why.append("stop te krap (kosten)")
        if c.t < self.live_from:
            return
        if why:
            self._event(m, c, "Overgeslagen", f"Swing-setup {m} ({tf}) niet genomen",
                        f"Pullback met bevestiging, maar: {'; '.join(why)}.", "Geen trade", "")
            return
        ts = c.t + self.candle_ms
        close_at = ts + p["max_hold_hours"] * 3_600_000 if p["max_hold_hours"] else None
        lvl = f", steun/weerstand links op {self._p(m, setup['level'])}" if setup.get("level") is not None else ""
        reason = (f"{tf}-trend {'boven' if long else 'onder'} de EMA {p['ema_len']}; pullback van minstens {p['min_red']} "
                  f"{'rode' if long else 'groene'} candles tot voorbij 50% van de beweging "
                  f"{self._p(m, setup['swing_low'])} – {self._p(m, setup['swing_high'])}{lvl}; "
                  f"{'bullish' if long else 'bearish'} engulfing als bevestiging.")
        checks = {"risk0": risk, "rr": setup["rr"], "level": setup.get("level"), "impulse_t": setup["impulse_t"]}
        t = self.broker.open(m, setup["side"], setup["entry"], setup["stop"], setup["target"], ts,
                             setup=f"Swing {tf} (Pat)", strategy="Swing", session_key=f"{m}|swing|{ts}",
                             close_at=close_at, checks=checks, reason=reason)
        w = Window(name="swing", label=f"Swing {tf}", day=ams_hm(ts), open=ts, range_end=ts,
                   entry_until=ts + self.acfg["intent_valid_minutes"] * 60_000,
                   close=close_at or ts + 30 * 86_400_000)
        self._new_intent(t, w)
        self._event(m, c, "Entry", f"{'Long' if long else 'Short'} {m} ({t.id}), swing {tf}", reason,
                    f"{'Long' if long else 'Short'} op {self._p(m, t.entry)} · stop {self._p(m, t.stop)} · "
                    f"doel {self._p(m, t.target)} (1:{setup['rr']})",
                    f"stop {self._p(m, t.stop)} of na {p['max_hold_hours']} uur")

    def _new_zone_intent(self, z: Zone, plan: dict, limit: float, w: Window, c: Candle) -> None:
        """Limit-entry mode: the Liquid paper order is prepared as soon as the zone exists."""
        risk_usd = round(self.broker.sizing_equity(c.t) * self.acfg["risk_pct"] / 100, 2)
        t = Trade(id=z.id, market=z.market, side=z.side, entry=limit, stop=plan["stop"], target=plan["target"],
                  size=0.0, risk_usd=risk_usd, opened_t=c.t + self.candle_ms, zone_id=z.id,
                  reason=f"Limietorder in de {self._zname(z).lower()} {self._p(z.market, z.bottom)} – {self._p(z.market, z.top)} "
                         f"(midden van de body van de zone-candle), Pats agressieve entry.")
        self._new_intent(t, w)

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
        key = (m, tm, bucket)
        if key not in self._trend_cache:
            higher = aggregate(cs[max(0, i - 3 * 120):i + 1], self.scfg["trend_minutes"], self.scfg["candle_minutes"])
            self._trend_cache[key] = trend(higher[-80:], self.scfg["swing_pivot"])
        return self._trend_cache[key]

    def trend_now(self, m: str) -> str:
        cs = self.history(m)
        self.scfg = self.market_scfg.get(m, self.base_scfg)
        try:
            return self._trend(m, cs, len(cs) - 1) if cs else "range"
        finally:
            self.scfg = self.base_scfg

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
            "bias": self._trend_nl(self._trend_cache.get((m, self.scfg['trend_minutes'] * 60_000, c.t - c.t % (self.scfg['trend_minutes'] * 60_000)), "")),
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
