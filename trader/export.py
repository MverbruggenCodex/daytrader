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

    lq = st.get("liquid") or {}
    closed = [t for t in engine.broker.trades if t.status == "closed"]
    account = [{
        "venue": acfg.get("venue", "Liquid paper account"), "currency": acfg["currency"],
        "start_balance": acfg["start_balance"], "risk_pct": acfg["risk_pct"],
        "max_open_trades": acfg["max_open_trades"], "daily_stop_pct": acfg["daily_stop_pct"],
        "min_rr": acfg["min_rr"], "eurusd": round(eurusd, 5), "rent_goal_eur": acfg["rent_goal_eur"],
        "model_balance": engine.broker.balance(),
        "model_trades": len(closed),
        "liquid_equity": lq.get("equity"), "liquid_paper_mode": lq.get("paper_mode"),
        "liquid_synced_at": iso(lq["updated_t"]) if lq.get("updated_t") else None,
        "started_at": iso(st["started_t"]) if st.get("started_t") else None, "updated": iso(now),
        "strategy": "ORB + supply & demand + swing (Trade with Pat)",
    }]

    ORDER_NL = {"pending": "Wacht op bevestiging", "placed": "Limietorder geplaatst", "filled": "Positie open",
                "closed": "Gesloten", "declined": "Afgewezen", "expired": "Verlopen", "cancel_needed": "Wordt geannuleerd",
                "close_needed": "Sluiten nodig", "cancelled": "Geannuleerd", "rejected": "Niet mogelijk"}
    by_trade = {o["trade_id"]: o for o in st.get("orders", [])}
    trades = []
    for t in engine.broker.trades:
        trades.append({
            "id": t.id, "market": t.market, "side": SIDE_NL.get(t.side, t.side), "status": STATUS_NL.get(t.status, t.status),
            "opened_at": iso(t.opened_t), "entry": t.entry, "stop": round(t.stop, 6), "target": round(t.target, 6),
            "size": t.size, "risk_usd": t.risk_usd, "rr": t.rr, "exit": t.exit,
            "closed_at": iso(t.closed_t) if t.closed_t else None, "pnl_usd": t.pnl_usd, "r_result": t.r_result,
            "setup": t.setup, "reason": t.reason, "strategy": t.strategy, "exit_reason": t.exit_reason,
            "fees_usd": t.fees_usd,
            "liquid": ORDER_NL.get(by_trade[t.id]["status"], by_trade[t.id]["status"]) if t.id in by_trade else "—",
        })

    orders = []
    for o in sorted(st.get("orders", []), key=lambda o: o["created_t"], reverse=True)[:60]:
        orders.append({
            "id": o["id"], "trade_id": o["trade_id"], "market": o["market"], "symbol": o["symbol"],
            "side": SIDE_NL.get(o["side"], o["side"]), "price": o["price"], "sl": o["sl"], "tp": o["tp"],
            "notional": o.get("notional"), "leverage": o.get("leverage"), "risk_usd": o.get("risk_usd"), "rr": o["rr"],
            "status": ORDER_NL.get(o["status"], o["status"]), "created": iso(o["created_t"]),
            "expires": iso(o["expires_t"]), "note": o.get("note", ""),
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
            "note": m.get("note") or f"{m.get('symbol')} op Liquid, trend 15m",
            "trading": bool(m.get("enabled") and m.get("trade", True)),
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

    sessions = []
    for key, ss in sorted(st.get("sessions", {}).items(), key=lambda kv: kv[1].get("open", 0)):
        if ss.get("or_high") is None or ss.get("open", 0) < now - 3 * 86_400_000:
            continue
        scfg = cfg["sessions"].get(ss.get("session"), {})
        sessions.append({
            "key": key, "market": ss["market"], "session": ss.get("session"), "label": ss.get("label"),
            "open": iso(ss["open"]), "range_end": iso(ss["open"] + scfg.get("range_minutes", 15) * 60_000),
            "or_high": ss["or_high"], "or_low": ss["or_low"], "trades": ss.get("trades", 0),
        })

    zones_chart = []
    for z in engine.zones:
        if z.created_t >= now - 3 * 86_400_000:
            zones_chart.append({"id": z.id, "market": z.market, "side": z.side, "bottom": z.bottom, "top": z.top,
                                "from": iso(z.origin_t), "until": iso(z.status_t) if z.status_t else None,
                                "status": ZONE_STATUS_NL.get(z.status, z.status)})

    return {"account": account, "trades": trades, "analyses": analyses, "markets": markets, "zones": zones,
            "orders": orders, "sessions": sessions, "zones_chart": zones_chart}


EMPTY_COLUMNS = {
    "trades": ["id", "market", "side", "status", "opened_at", "entry", "stop", "target", "size", "risk_usd", "rr", "exit",
               "closed_at", "pnl_usd", "r_result", "setup", "reason", "strategy", "exit_reason", "fees_usd", "liquid"],
    "orders": ["id", "trade_id", "market", "symbol", "side", "price", "sl", "tp", "notional", "leverage", "risk_usd", "rr",
               "status", "created", "expires", "note"],
    "zones": ["id", "market", "session", "side", "bottom", "top", "status", "created", "fvg", "bos", "note"],
    "analyses": ["id", "time", "market", "price", "bias", "trigger", "title", "text", "action", "invalidation"],
}


def write(engine: Engine, out_dir: str) -> Dict[str, str]:
    """The dashboard cannot read a file without rows, so an empty table gets one row marked _empty."""
    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    for name, rows in build(engine).items():
        if not rows and name in EMPTY_COLUMNS:
            rows = [dict({k: None for k in EMPTY_COLUMNS[name]}, id="—", _empty=True)]
        p = os.path.join(out_dir, f"{name}.json")
        with open(p, "w") as f:
            json.dump(rows, f, indent=1, ensure_ascii=False)
        paths[name] = p
    return paths
