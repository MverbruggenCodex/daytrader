"""Historical backtest of the engine plus Monte Carlo on its trade results.

The backtest replays the real engine candle by candle (same rules as live), with every signal taken
immediately at the signal price (the "model" account). The Monte Carlo draws the realised R-multiples
with replacement to show the spread of outcomes the same edge could produce.
"""
from __future__ import annotations

import copy
import random
from collections import defaultdict
from statistics import mean, median
from typing import Dict, List, Optional

from . import engine as engine_mod
from . import state as st_mod
from .engine import Engine
from .models import Candle, Trade


def run(cfg: dict, candles: Dict[str, List[Candle]]) -> dict:
    cfg = copy.deepcopy(cfg)
    cfg["markets"] = {k: v for k, v in cfg["markets"].items() if k in candles and v.get("enabled")}
    end = max(cs[-1].t for cs in candles.values() if cs)
    old_keep = engine_mod.KEEP_CANDLES
    engine_mod.KEEP_CANDLES = 10 ** 8
    try:
        state = st_mod.empty_state()
        eng = Engine(cfg, state, now_ms=end + 2 * 60_000 * cfg["strategy"]["candle_minutes"], live_from=0)
        for m, cs in candles.items():
            if m in cfg["markets"]:
                eng.ingest(m, cs)
        eng.run()
    finally:
        engine_mod.KEEP_CANDLES = old_keep
    closed = [t for t in eng.broker.trades if t.status == "closed"]
    first = min(cs[0].t for cs in candles.values() if cs)
    return {"stats": stats(closed, cfg["account"]["start_balance"], first, end),
            "trades": closed, "zones": len(eng.zones),
            "candles": {m: len(cs) for m, cs in candles.items()}, "period": [first, end]}


def _group(trades: List[Trade]) -> dict:
    if not trades:
        return {"trades": 0}
    rs = [t.r_result or 0.0 for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    return {
        "trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 3),
        "avg_r": round(mean(rs), 3),
        "total_r": round(sum(rs), 2),
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "net_usd": round(sum(t.pnl_usd or 0 for t in trades), 2),
    }


def stats(trades: List[Trade], start_balance: float, first_t: int, end_t: int) -> dict:
    trades = sorted(trades, key=lambda t: t.closed_t or 0)
    out = _group(trades)
    eq = peak = start_balance
    dd = 0.0
    streak = worst = 0
    for t in trades:
        eq += t.pnl_usd or 0
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak if peak else 0)
        streak = streak + 1 if (t.pnl_usd or 0) <= 0 else 0
        worst = max(worst, streak)
    weeks = max(1e-9, (end_t - first_t) / (7 * 86_400_000))
    out.update({
        "end_balance": round(eq, 2),
        "return_pct": round((eq / start_balance - 1) * 100, 2),
        "max_drawdown_pct": round(dd * 100, 2),
        "longest_losing_streak": worst,
        "trades_per_week": round(len(trades) / weeks, 2),
        "weeks": round(weeks, 1),
    })
    by = defaultdict(list)
    for t in trades:
        by[("market", t.market)].append(t)
        by[("session", (t.session_key or "?").split("|")[1] if t.session_key and "|" in t.session_key else "?")].append(t)
        by[("side", t.side)].append(t)
        by[("exit", t.exit_reason or "?")].append(t)
    for kind in ("market", "session", "side", "exit"):
        out[f"by_{kind}"] = {k: _group(v) for (g, k), v in sorted(by.items()) if g == kind}
    return out


def monte_carlo(rs: List[float], sims: int = 1000, n_trades: Optional[int] = None, risk_pct: float = 3.0,
                start: float = 10_000.0, seed: int = 7, ruin_pct: float = 50.0) -> dict:
    """Bootstrap the R-multiples: each simulation draws n_trades results with replacement and compounds
    them at risk_pct of equity per trade."""
    if not rs:
        return {"sims": 0}
    n = n_trades or len(rs)
    rng = random.Random(seed)
    risk = risk_pct / 100
    ends, dds, ruins = [], [], 0
    for _ in range(sims):
        eq = peak = start
        mdd = 0.0
        ruined = False
        for _ in range(n):
            eq *= 1 + risk * rng.choice(rs)
            if eq <= 0:
                eq = 0.0
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak if peak else 1.0)
            if eq <= start * (1 - ruin_pct / 100):
                ruined = True
        ends.append(eq)
        dds.append(mdd)
        ruins += ruined
    ends.sort()
    dds.sort()

    def pct(xs, p):
        return xs[min(len(xs) - 1, int(p / 100 * len(xs)))]

    return {
        "sims": sims, "trades_per_sim": n, "risk_pct": risk_pct, "start": start,
        "end_balance": {f"p{p}": round(pct(ends, p), 2) for p in (5, 25, 50, 75, 95)},
        "prob_profit": round(sum(e > start for e in ends) / sims, 3),
        "prob_double": round(sum(e >= 2 * start for e in ends) / sims, 3),
        f"prob_drawdown_over_{int(ruin_pct)}pct": round(ruins / sims, 3),
        "max_drawdown_pct": {f"p{p}": round(pct(dds, p) * 100, 1) for p in (50, 95)},
        "median_r_per_trade": round(median(rs), 3),
        "mean_r_per_trade": round(mean(rs), 3),
    }
