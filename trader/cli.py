"""Command line: python -m trader <command>."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

from . import data, export, state as st_mod
from .engine import Engine
from .models import Trade
from .orders import ACTIVE, housekeeping, sync_positions
from .sessions import iso

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDLES_PER_RUN_MAX = 200  # Liquid's show_chart limit


def load_cfg(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_time(s: str) -> int:
    if s.isdigit():
        return int(s)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        from zoneinfo import ZoneInfo
        dt = dt.replace(tzinfo=ZoneInfo("Europe/Amsterdam"))
    return int(dt.timestamp() * 1000)


def summary(engine: Engine) -> dict:
    prices = {m: (engine.history(m)[-1].c if engine.history(m) else None) for m in engine.markets}
    b = engine.broker
    closed = [t for t in b.trades if t.status == "closed"]
    wins = [t for t in closed if (t.pnl_usd or 0) > 0]
    return {
        "time": iso(engine.now),
        "model_balance_usd": b.balance(),
        "open_pnl_usd": b.open_pnl({k: v for k, v in prices.items() if v}),
        "open_trades": [{"id": t.id, "market": t.market, "side": t.side, "entry": t.entry, "stop": t.stop,
                         "target": t.target} for t in b.open_trades()],
        "closed_trades": len(closed),
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "active_zones": [{"id": z.id, "market": z.market, "side": z.side, "bottom": z.bottom, "top": z.top}
                         for z in engine.zones if z.status in ("fresh", "touched")],
        "last_candle": {m: (iso(engine.history(m)[-1].t) if engine.history(m) else None) for m in engine.markets},
        "prices": prices,
        "liquid": engine.state.get("liquid"),
        "orders_attention": [o for o in engine.state.get("orders", []) if o["status"] in ACTIVE],
        "order_changes": engine.order_changes,
    }


def cmd_init(a, cfg):
    path = a.state
    if os.path.exists(path) and not a.force:
        print(f"{path} bestaat al (gebruik --force om opnieuw te beginnen)")
        return 1
    state = st_mod.empty_state()
    st_mod.save(path, state)
    print(f"Nieuwe staat geschreven naar {path}")
    return 0


def cmd_fetch_plan(a, cfg):
    state = st_mod.load(a.state)
    now = now_ms()
    plan = []
    for key, m in cfg["markets"].items():
        if not m.get("enabled"):
            continue
        last = state["last_ts"].get(key)
        if last is None:
            limit = CANDLES_PER_RUN_MAX
        else:
            limit = min(CANDLES_PER_RUN_MAX, max(12, int((now - last) / (cfg["strategy"]["candle_minutes"] * 60_000)) + 6))
        plan.append({"market": key, "symbol": m["symbol"], "interval": f"{cfg['strategy']['candle_minutes']}m",
                     "limit": limit, "inbox_file": f"inbox/{key}.json"})
    print(json.dumps(plan, indent=1))
    return 0


def _collect(a, cfg, engine: Engine, days: float = 0.0) -> dict:
    counts = {}
    if a.source == "inbox":
        got = data.load_inbox(a.inbox, cfg["markets"])
        for key, cs in got.items():
            if key in engine.markets:
                counts[key] = engine.ingest(key, cs)
    else:
        span = days * 86_400_000 if days else 0
        for key, m in engine.markets.items():
            last = engine.state["last_ts"].get(key)
            start = engine.now - (span or (16 * 3_600_000 if last is None else max(3_600_000, engine.now - last + 1_800_000)))
            cs = []
            cursor = start
            while cursor < engine.now:  # Hyperliquid returns at most 5000 candles per call
                chunk = data.fetch_hyperliquid(m["symbol"], cursor, engine.now)
                if not chunk:
                    break
                cs.extend(chunk)
                if chunk[-1].t <= cursor:
                    break
                cursor = chunk[-1].t + 1
            counts[key] = engine.ingest(key, cs)
    return counts


def cmd_run(a, cfg):
    while True:
        state = st_mod.load(a.state)
        engine = Engine(cfg, state, now_ms())
        counts = _collect(a, cfg, engine)
        events = engine.run()
        st_mod.save(a.state, state)
        export.write(engine, a.export)
        if a.source == "inbox" and not a.keep_inbox:
            data.clear_inbox(a.inbox)
        out = {"new_candles": counts, "new_events": events, "summary": summary(engine)}
        print(json.dumps(out, indent=1, ensure_ascii=False))
        if not a.loop:
            return 0
        time.sleep(a.interval)


def cmd_status(a, cfg):
    state = st_mod.load(a.state)
    engine = Engine(cfg, state, now_ms())
    print(json.dumps(summary(engine), indent=1, ensure_ascii=False))
    return 0


def cmd_export(a, cfg):
    state = st_mod.load(a.state)
    engine = Engine(cfg, state, now_ms())
    print(json.dumps(export.write(engine, a.export), indent=1))
    return 0


def cmd_backtest(a, cfg):
    state = st_mod.empty_state()
    now = now_ms()
    engine = Engine(cfg, state, now, live_from=0)
    from . import engine as eng_mod
    eng_mod.KEEP_CANDLES = 100_000
    _collect(a, cfg, engine, days=a.days)
    engine.run()
    export.write(engine, a.export)
    closed = [t for t in engine.broker.trades if t.status == "closed"]
    wins = [t for t in closed if (t.pnl_usd or 0) > 0]
    eq, peak, dd = cfg["account"]["start_balance"], cfg["account"]["start_balance"], 0.0
    for t in sorted(closed, key=lambda t: t.closed_t):
        eq += t.pnl_usd
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak if peak else 0)
    res = {
        "markets": {m: len(engine.history(m)) for m in engine.markets},
        "trades": len(closed),
        "open_at_end": len(engine.broker.open_trades()),
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "avg_r": round(sum(t.r_result or 0 for t in closed) / len(closed), 2) if closed else None,
        "net_eur": round(sum(t.pnl_usd for t in closed), 2),
        "end_balance": engine.broker.balance(),
        "max_drawdown_pct": round(dd * 100, 1),
        "zones": len(engine.zones),
        "by_market": {m: round(sum(t.pnl_usd for t in closed if t.market == m), 2) for m in engine.markets},
        "trades_list": [{"id": t.id, "market": t.market, "side": t.side, "opened": iso(t.opened_t), "r": t.r_result,
                         "exit": t.exit_reason} for t in closed],
    }
    print(json.dumps(res, indent=1, ensure_ascii=False))
    return 0


def cmd_blackout(a, cfg):
    state = st_mod.load(a.state)
    state.setdefault("blackouts", []).append({
        "start": parse_time(a.start), "end": parse_time(a.end),
        "markets": a.markets.split(",") if a.markets else ["all"], "reason": a.reason,
    })
    st_mod.save(a.state, state)
    print(f"Nieuwsfilter toegevoegd: {a.reason}")
    return 0


def cmd_note(a, cfg):
    """Add Claude's own analysis (news, context) to the analysis log."""
    state = st_mod.load(a.state)
    state["event_seq"] = state.get("event_seq", 0) + 1
    m = a.market
    cs = state["candles"].get(m) or []
    state["events"].append({
        "id": f"E{state['event_seq']:05d}", "t": now_ms(), "market": m, "price": cs[-1][4] if cs else None,
        "bias": a.bias or "—", "trigger": a.trigger, "title": a.title, "text": a.text, "action": a.action or "",
        "invalidation": a.invalidation or "",
    })
    st_mod.save(a.state, state)
    print("Notitie toegevoegd")
    return 0


def _latest_prices(state: dict) -> dict:
    return {m: rows[-1][4] for m, rows in state.get("candles", {}).items() if rows}


def cmd_orders(a, cfg):
    """What Claude has to do on Liquid this round."""
    state = st_mod.load(a.state)
    housekeeping(state.setdefault("orders", []), now_ms(), _latest_prices(state))
    st_mod.save(a.state, state)
    lq = state.get("liquid") or {}
    out = {
        "paper_mode": lq.get("paper_mode"),
        "propose": [o for o in state["orders"] if o["status"] == "pending"],
        "cancel": [o for o in state["orders"] if o["status"] == "cancel_needed"],
        "close": [o for o in state["orders"] if o["status"] == "close_needed"],
        "watch": [o for o in state["orders"] if o["status"] in ("placed", "filled")],
    }
    if lq.get("paper_mode") is not True:
        out["propose"] = []
        out["blocked"] = ("Liquid staat niet (aantoonbaar) in paper modus. Draai eerst liquid-sync met een "
                          "get_portfolio-antwoord waarin paperMode true is. Geen orders voorstellen of plaatsen.")
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


def cmd_order_update(a, cfg):
    state = st_mod.load(a.state)
    for o in state.get("orders", []):
        if o["id"] == a.id:
            o["status"] = a.status
            if a.liquid_id:
                o["liquid_order_id"] = a.liquid_id
            if a.note:
                o["note"] = a.note
            o["updated_t"] = now_ms()
            st_mod.save(a.state, state)
            print(json.dumps(o, indent=1, ensure_ascii=False))
            return 0
    print(f"Order {a.id} niet gevonden")
    return 1


def cmd_liquid_sync(a, cfg):
    """Store a get_portfolio answer from the Liquid connector and match positions to orders."""
    state = st_mod.load(a.state)
    with open(a.file) as f:
        pf = json.load(f)
    if isinstance(pf, dict) and "content" in pf:
        pf = json.loads("".join(p.get("text", "") for p in pf["content"]))
    acct = pf.get("account") or {}
    positions = pf.get("positions") or []
    state["liquid"] = {
        "paper_mode": pf.get("paperMode") is True,
        "equity": float(acct["equity"]) if acct.get("equity") is not None else None,
        "available": float(acct["available_balance"]) if acct.get("available_balance") is not None else None,
        "margin_used": float(acct["margin_used"]) if acct.get("margin_used") is not None else None,
        "positions": positions,
        "updated_t": now_ms(),
    }
    changes = sync_positions(state.setdefault("orders", []), positions)
    st_mod.save(a.state, state)
    print(json.dumps({"paper_mode": state["liquid"]["paper_mode"], "equity": state["liquid"]["equity"],
                      "positions": len(positions), "changes": changes}, indent=1, ensure_ascii=False))
    return 0 if state["liquid"]["paper_mode"] else 2


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="trader", description="ORB + supply & demand paper trader")
    p.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    p.add_argument("--state", default=os.path.join(ROOT, "state", "state.json"))
    p.add_argument("--export", default=os.path.join(ROOT, "export"))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="nieuwe staat aanmaken")
    s.add_argument("--force", action="store_true")

    sub.add_parser("fetch-plan", help="welke candles Claude via Liquid moet ophalen")

    for name in ("run", "backtest"):
        s = sub.add_parser(name)
        s.add_argument("--source", choices=["hyperliquid", "inbox"], default="hyperliquid")
        s.add_argument("--inbox", default=os.path.join(ROOT, "inbox"))
        if name == "run":
            s.add_argument("--keep-inbox", action="store_true")
            s.add_argument("--loop", action="store_true", help="blijf draaien (lokaal 24/7)")
            s.add_argument("--interval", type=int, default=300)
        else:
            s.add_argument("--days", type=float, default=14)

    sub.add_parser("orders", help="wat er op Liquid moet gebeuren (voorstellen, annuleren, sluiten)")

    s = sub.add_parser("order-update", help="status van een order bijwerken na een Liquid-actie")
    s.add_argument("--id", required=True)
    s.add_argument("--status", required=True, choices=["pending", "placed", "filled", "closed", "declined", "expired",
                                                       "cancel_needed", "close_needed", "cancelled", "rejected"])
    s.add_argument("--liquid-id", default="")
    s.add_argument("--note", default="")

    s = sub.add_parser("liquid-sync", help="get_portfolio-antwoord van Liquid opslaan")
    s.add_argument("--file", required=True)

    sub.add_parser("status")
    sub.add_parser("export")

    s = sub.add_parser("blackout", help="geen entries rond belangrijk nieuws")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--markets", default="")
    s.add_argument("--reason", required=True)

    s = sub.add_parser("note", help="eigen analyse toevoegen aan het logboek")
    s.add_argument("--market", required=True)
    s.add_argument("--title", required=True)
    s.add_argument("--text", required=True)
    s.add_argument("--trigger", default="Analyse")
    s.add_argument("--bias", default="")
    s.add_argument("--action", default="")
    s.add_argument("--invalidation", default="")

    a = p.parse_args(argv)
    cfg = load_cfg(a.config)
    handler = {
        "init": cmd_init, "fetch-plan": cmd_fetch_plan, "run": cmd_run, "backtest": cmd_backtest,
        "status": cmd_status, "export": cmd_export, "blackout": cmd_blackout, "note": cmd_note,
        "orders": cmd_orders, "order-update": cmd_order_update, "liquid-sync": cmd_liquid_sync,
    }[a.cmd]
    return handler(a, cfg) or 0


if __name__ == "__main__":
    sys.exit(main())
