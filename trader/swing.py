"""Trade with Pat's swing strategy (video 'How to Start Swing Trading', CtJN2t3Uc0M), on 1h or 4h candles.

Rules, long side (short is the mirror image):
1. Trend: the candle closes above the 50 EMA.
2. Impulse: the last confirmed swing low -> swing high.
3. Pullback: since that swing high at least `min_red` red candles in a row with real size
   (together >= `red_atr` x ATR), and the pullback has not broken the swing low.
4. Discount: the pullback reached below the 50% Fibonacci level of the impulse.
5. Look left: the pullback low sits on an older swing level (support or flip zone), optionally the 50 EMA.
6. Confirmation: a bullish engulfing candle (body engulfs the previous red body), within a few candles of the low.
Entry on the close of that candle, stop below the pullback low, target the swing high (or a fixed R).
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from .models import Candle
from .structure import atr, pivots

DEFAULTS = {
    "tf_minutes": 60,
    "ema_len": 50,
    "swing_k": 3,
    "min_red": 3,
    "red_atr": 1.0,
    "discount": 0.5,
    "look_left": True,
    "level_lookback": 150,
    "level_tol_atr": 0.5,
    "ema_confluence": False,
    "require_engulf": True,
    "max_bars_since_low": 3,
    "stop_buffer_atr": 0.2,
    "target": "swing",  # 'swing' = the swing high/low, or 'r' = target_r x risk
    "target_r": 2.0,
    "min_rr": 1.0,
    "max_hold_hours": 120,
}


def params(scfg: dict) -> dict:
    return {k: scfg.get(k, v) for k, v in DEFAULTS.items()}


def ema_series(values: Sequence[float], n: int) -> List[float]:
    out: List[float] = []
    k = 2 / (n + 1)
    for i, v in enumerate(values):
        out.append(v if i == 0 else v * k + out[-1] * (1 - k))
    return out


def _mirror(cs: Sequence[Candle]) -> List[Candle]:
    return [Candle(c.t, -c.o, -c.l, -c.h, -c.c, c.v) for c in cs]


def find_setup(h: Sequence[Candle], p: dict) -> Optional[dict]:
    """Evaluate the last closed candle of h. Returns a setup dict or None."""
    if len(h) < p["ema_len"] + 10:
        return None
    ema = ema_series([c.c for c in h], p["ema_len"])
    if h[-1].c > ema[-1]:
        res = _long_setup(list(h), ema, p)
        side, sign = "long", 1
    elif h[-1].c < ema[-1]:
        res = _long_setup(_mirror(h), [-e for e in ema], p)
        side, sign = "short", -1
    else:
        return None
    if res is None:
        return None
    out = {k: (v * sign if v is not None and k in ("entry", "stop", "target", "swing_high", "swing_low", "level", "pull_low", "ema") else v)
           for k, v in res.items()}
    out["side"] = side
    return out


def _long_setup(h: List[Candle], ema: List[float], p: dict) -> Optional[dict]:
    n = len(h)
    c, prev = h[-1], h[-2]
    a = atr(h[-15:], 14)
    if a <= 0:
        return None
    if p["require_engulf"]:
        if not (c.bull and prev.bear and c.c >= prev.o and c.o <= prev.c):
            return None
    elif not c.bull:
        return None
    pts = pivots(h[:-1], p["swing_k"])
    highs = [x for x in pts if x[1] == "H"]
    if not highs:
        return None
    jh, _, hi = highs[-1]
    lows = [x for x in pts if x[1] == "L" and x[0] < jh]
    if not lows:
        return None
    jl, _, lo = lows[-1]
    if hi <= lo:
        return None
    seg = h[jh + 1:n]
    if len(seg) < p["min_red"] + 1:
        return None
    pull_low = min(x.l for x in seg)
    j_low = jh + 1 + min(range(len(seg)), key=lambda k: seg[k].l)
    if pull_low <= lo:  # structure broken
        return None
    if n - 1 - j_low > p["max_bars_since_low"]:
        return None
    # discount: below the 50% level of the impulse
    if pull_low > hi - p["discount"] * (hi - lo):
        return None
    # at least min_red red candles in a row with real size
    best = run = 0
    body = best_body = 0.0
    for x in h[jh + 1:n - 1]:
        if x.bear:
            run += 1
            body += x.body
            if run > best or (run == best and body > best_body):
                best, best_body = run, body
        else:
            run, body = 0, 0.0
    if best < p["min_red"] or best_body < p["red_atr"] * a:
        return None
    level = None
    if p["look_left"]:
        tol = p["level_tol_atr"] * a
        start = max(0, jh - p["level_lookback"])
        cands = [x[2] for x in pts if start <= x[0] < jh and abs(pull_low - x[2]) <= tol]
        if not cands:
            return None
        level = min(cands, key=lambda v: abs(pull_low - v))
    if p["ema_confluence"] and pull_low > ema[j_low] + p["level_tol_atr"] * a:
        return None
    entry = c.c
    stop = pull_low - p["stop_buffer_atr"] * a
    risk = entry - stop
    if risk <= 0:
        return None
    target = hi if p["target"] == "swing" else entry + p["target_r"] * risk
    rr = (target - entry) / risk
    if rr < p["min_rr"]:
        return None
    return {"entry": entry, "stop": stop, "target": target, "rr": round(rr, 2), "swing_high": hi, "swing_low": lo,
            "level": level, "pull_low": pull_low, "ema": ema[-1], "impulse_t": h[jh].t, "atr": a}
