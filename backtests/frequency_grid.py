"""Grid of 'trade more often' variants (12 Oct 2026).

Runs inside Pyodide (browser worker) or locally. Expects the globals CFG_JSON (config.json text) and
DATA_JSON ({market: [[t, o, h, l, c], ...]}). Every variant is backtested per market on its own, with
the real Liquid paper fee (about 0.095% per side, observed on 11 Oct 2026) unless noted.
"""
import copy
import json

from trader import backtest as bt
from trader.models import Candle

BASE = json.loads(CFG_JSON)  # noqa: F821 (provided by the caller)
DATA = {m: [Candle.from_list(r) for r in rows] for m, rows in json.loads(DATA_JSON).items()}  # noqa: F821

REAL_FEES = {"fee_maker_pct": 0.095, "fee_taker_pct": 0.095}
OLD_FEES = {"fee_maker_pct": 0.015, "fee_taker_pct": 0.045}

EXTRA_SESSIONS = {
    "asia": {"label": "Tokio", "tz": "Asia/Tokyo", "open": "09:00", "range_minutes": 15,
             "entry_until": "12:00", "close": "15:00"},
    "london_long": {"label": "Londen (lang)", "tz": "Europe/London", "open": "08:00", "range_minutes": 15,
                    "entry_until": "14:00", "close": "16:00"},
    "newyork_long": {"label": "New York (lang)", "tz": "America/New_York", "open": "09:30",
                     "range_minutes": 15, "entry_until": "15:00", "close": "16:00"},
}
for h in range(0, 24, 4):
    EXTRA_SESSIONS[f"u{h:02d}"] = {"label": f"{h:02d}:00 UTC", "tz": "UTC", "open": f"{h:02d}:00",
                                   "range_minutes": 15, "entry_until": f"{h + 3:02d}:00",
                                   "close": f"{h + 3:02d}:55"}
BLOCKS = [f"u{h:02d}" for h in range(0, 24, 4)]

ORB = {"mode": "orb", "orb_entry": "retest", "orb_stop": "opposite", "target_r": 1.0,
       "require_trend": True, "trend_minutes": 60}

# (id, market, label, sessions, strategy override, base-strategy changes, fees)
VARIANTS = [
    ("B0", "BTCUSD", "Huidig (oude kosten)", ["london", "newyork"], None, {}, OLD_FEES),
    ("B1", "BTCUSD", "Huidig (echte kosten)", ["london", "newyork"], None, {}, REAL_FEES),
    ("B2", "BTCUSD", "+ Tokio", ["asia", "london", "newyork"], None, {}, REAL_FEES),
    ("B3", "BTCUSD", "Langer instappen (tot 14:00/15:00)", ["london_long", "newyork_long"], None, {}, REAL_FEES),
    ("B4", "BTCUSD", "2 trades per sessie", ["london", "newyork"], None, {"max_trades_per_session": 2}, REAL_FEES),
    ("B5", "BTCUSD", "+ Tokio, langer instappen", ["asia", "london_long", "newyork_long"], None, {}, REAL_FEES),
    ("B6", "BTCUSD", "+ Tokio, langer, 2 per sessie", ["asia", "london_long", "newyork_long"], None,
     {"max_trades_per_session": 2}, REAL_FEES),
    ("B7", "BTCUSD", "Elke 4 uur een range (24/7)", BLOCKS, None, {}, REAL_FEES),
    ("B8", "BTCUSD", "Elke 4 uur, trend 60m", BLOCKS, None, {"trend_minutes": 60}, REAL_FEES),
    ("B9", "BTCUSD", "ORB-uitbraak 1R, Londen + NY", ["london", "newyork"], ORB, {}, REAL_FEES),
    ("B10", "BTCUSD", "ORB-uitbraak 1R, elke 4 uur", BLOCKS, ORB, {}, REAL_FEES),
    ("G0", "XAUUSD", "Huidig (oude kosten)", ["newyork"], ORB, {}, OLD_FEES),
    ("G1", "XAUUSD", "Huidig (echte kosten)", ["newyork"], ORB, {}, REAL_FEES),
    ("G2", "XAUUSD", "+ Londen", ["london", "newyork"], ORB, {}, REAL_FEES),
    ("G3", "XAUUSD", "+ Tokio + Londen", ["asia", "london", "newyork"], ORB, {}, REAL_FEES),
    ("G4", "XAUUSD", "NY langer instappen (tot 15:00)", ["newyork_long"], ORB, {}, REAL_FEES),
    ("G5", "XAUUSD", "Londen + NY, doel 1,5R", ["london", "newyork"], dict(ORB, target_r=1.5), {}, REAL_FEES),
    ("G6", "XAUUSD", "Elke 4 uur een range", BLOCKS, ORB, {}, REAL_FEES),
    ("E1", "EURUSD", "ORB 1R, Londen + NY", ["london", "newyork"], ORB, {}, REAL_FEES),
    ("E2", "EURUSD", "ORB 1R, alleen Londen", ["london"], ORB, {}, REAL_FEES),
    ("E3", "EURUSD", "ORB 1,5R, Londen + NY", ["london", "newyork"], dict(ORB, target_r=1.5), {}, REAL_FEES),
]

# Trade with Pat swing (video CtJN2t3Uc0M): 24/7, no sessions
SW = {"mode": "swing", "tf_minutes": 60}
SWING = [
    ("1", "Swing 1u (Pat)", dict(SW)),
    ("2", "Swing 1u, doel 2R", dict(SW, target="r", target_r=2.0)),
    ("3", "Swing 1u, min. 1:1,5", dict(SW, min_rr=1.5)),
    ("4", "Swing 1u, zonder 'look left'", dict(SW, look_left=False)),
    ("5", "Swing 1u, zonder engulfing", dict(SW, require_engulf=False)),
    ("6", "Swing 1u, + EMA-confluence", dict(SW, ema_confluence=True)),
    ("7", "Swing 4u (Pat)", dict(SW, tf_minutes=240)),
    ("8", "Swing 4u, doel 2R", dict(SW, tf_minutes=240, target="r", target_r=2.0)),
]
for mk, pre in (("BTCUSD", "SB"), ("XAUUSD", "SG"), ("EURUSD", "SE")):
    for sid, label, strat in SWING:
        VARIANTS.append((pre + sid, mk, label, [], strat, {}, REAL_FEES))


def variant_cfg(market, sessions, strat, base_changes, fees):
    cfg = copy.deepcopy(BASE)
    cfg["sessions"].update(EXTRA_SESSIONS)
    cfg["account"].update(fees)
    cfg["strategy"].update(base_changes)
    mc = cfg["markets"][market]
    mc.update({"enabled": True, "trade": True, "sessions": sessions})
    if strat is None:
        mc.pop("strategy", None)
    else:
        mc["strategy"] = dict(strat)
    cfg["markets"] = {market: mc}
    return cfg


def summarise(vid, market, label, res):
    s = res["stats"]
    first, end = res["period"]
    mid = (first + end) / 2
    tr = res["trades"]
    h1 = [t.r_result or 0 for t in tr if (t.opened_t or 0) < mid]
    h2 = [t.r_result or 0 for t in tr if (t.opened_t or 0) >= mid]
    years = (end - first) / (365.25 * 86_400_000)
    return {
        "id": vid, "market": market, "label": label, "trades": s.get("trades", 0),
        "per_year": round(s.get("trades", 0) / years, 1) if years else None,
        "win_rate": s.get("win_rate"), "avg_r": s.get("avg_r"), "total_r": s.get("total_r"),
        "h1_r": round(sum(h1) / len(h1), 3) if h1 else None, "h1_n": len(h1),
        "h2_r": round(sum(h2) / len(h2), 3) if h2 else None, "h2_n": len(h2),
        "return_pct": s.get("return_pct"), "max_dd_pct": s.get("max_drawdown_pct"),
        "exits": {k: v["trades"] for k, v in (s.get("by_exit") or {}).items()},
        "rs": [t.r_result or 0 for t in sorted(tr, key=lambda t: t.opened_t or 0)],
    }


def run_variant(v):
    vid, market, label, sessions, strat, changes, fees = v
    if market not in DATA:
        return {"id": vid, "market": market, "label": label, "error": "geen data"}
    cfg = variant_cfg(market, sessions, strat, changes, fees)
    return summarise(vid, market, label, bt.run(cfg, {market: DATA[market]}))
