"""Write the JSON files the dashboard reads (account, trades, analyses, markets, zones)."""
from __future__ import annotations

import json
import os
from typing import Dict

from .engine import Engine
from .sessions import iso, windows_at

SIDE_NL = {"long": "Long", "short": "Short"}
STATUS_NL = {"open": "Open", "closed": "Gesloten"}
ZONE_STATUS_NL = {"fresh": "Vers", "touched": "Aangetikt", "entered": "Trade", "dead": "Vervallen", "skipped": "Overgeslagen"}
BIAS_NL = {"up": "Bullish", "down": "Bearish", "range": "Neutraal"}


def build(engine: Engine) -> Dict[str, list]:
    cfg, st = engine.cfg, engine.state
    acfg = cfg["account"]
    now = engine.now
    eurusd = engine.eurusd()

    account = [{
        "start_balance": acfg["start_balance"], "currency": acfg["currency"], "risk_pct": acfg["risk_pct"],
        "max_open_trades": acfg["max_open_trades"], "daily_stop_pct": acfg["daily_stop_pct"],
        "min_rr": acfg["min_rr"], "eurusd": round(eurusd, 5), "rent_goal": acfg["rent_goal"],
        "started_at": iso(st["started_t"]) if st.get("started_t") else None, "updated": iso(now),
        "strategy": "ORB + Trade with Pat supply & demand",
    }]

    trades = []
    for t in engine.broker.trades:
        trades.append({
            "id": t.id, "market": t.market, "side": SIDE_NL.get(t.side, t.side), "status": STATUS_NL.get(t.status, t.status),
            "opened_at": iso(t.opened_t), "entry": t.entry, "stop": round(t.stop, 6), "target": round(t.target, 6),
            "size": t.size, "risk_eur": t.risk_eur, "rr": t.rr, "exit": t.exit,
            "closed_at": iso(t.closed_t) if t.closed_t else None, "pnl_eur": t.pnl_eur, "r_result": t.r_result,
            "setup": t.setup, "reason": t.reason, "strategy": t.strategy, "exit_reason": t.exit_reason,
            "fees_eur": t.fees_eur,
        })

    analyses = []
    for e in st["events"][-150:]:
        analyses.append({
            "id": e["id"], "time": iso(e["t"]), "market": e["market"], "price": e["price"], "bias": e.get("bias") or "—",
            "trigger": e["trigger"], "title": e["title"], "text": e["text"], "action": e["action"],
            "invalidation": e.get("invalidation", ""),
        })

    markets = []
    for key, m in cfg["markets"].items():
        cs = engine.history(key) if m.get("enabled") else []
        last = cs[-1] if cs else None
        day = [c for c in cs if c.t >= now - 86_400_000]
        if not m.get("enabled"):
            status, bias = "Niet beschikbaar", "—"
        else:
            active = windows_at(now, m, cfg["sessions"])
            status = ("Sessie " + active[0].label) if active else "Buiten sessie"
            bias = BIAS_NL.get(engine.trend_now(key), "—")
        markets.append({
            "market": key, "status": status, "price": last.c if last else None,
            "day_open": day[0].o if day else None, "day_high": max(c.h for c in day) if day else None,
            "day_low": min(c.l for c in day) if day else None, "bias": bias,
            "note": m.get("note") or f"{m.get('symbol')} op Liquid · trend 15m",
            "updated": iso(last.t + engine.candle_ms) if last else iso(now),
        })

    zones = []
    for z in sorted(engine.zones, key=lambda z: z.created_t, reverse=True)[:60]:
        zones.append({
            "id": z.id, "market": z.market, "session": z.session_key.split("|")[1] if "|" in z.session_key else z.session_key,
            "side": "Demand" if z.side == "long" else "Supply", "bottom": z.bottom, "top": z.top,
            "status": ZONE_STATUS_NL.get(z.status, z.status), "created": iso(z.created_t + engine.candle_ms),
            "fvg": bool(z.checks.get("fvg")), "bos": bool(z.checks.get("bos")), "note": z.note,
        })

    return {"account": account, "trades": trades, "analyses": analyses, "markets": markets, "zones": zones}


def write(engine: Engine, out_dir: str) -> Dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    for name, rows in build(engine).items():
        p = os.path.join(out_dir, f"{name}.json")
        with open(p, "w") as f:
            json.dump(rows, f, indent=1, ensure_ascii=False)
        paths[name] = p
    return paths
