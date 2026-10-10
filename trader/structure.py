"""Market structure helpers: ATR, swing points, trend, fair value gaps."""
from __future__ import annotations

from statistics import median
from typing import List, Optional, Sequence, Tuple

from .models import Candle


def atr(candles: Sequence[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return candles[-1].h - candles[-1].l if candles else 0.0
    trs = []
    for prev, cur in zip(candles[:-1], candles[1:]):
        trs.append(max(cur.h - cur.l, abs(cur.h - prev.c), abs(cur.l - prev.c)))
    window = trs[-period:]
    return sum(window) / len(window)


def pivots(candles: Sequence[Candle], k: int = 2) -> List[Tuple[int, str, float]]:
    """Confirmed swing points: (index, 'H'|'L', price).

    A swing high needs k candles on both sides with lower highs (left strictly lower),
    so the last k candles can never be a confirmed pivot yet.
    """
    out: List[Tuple[int, str, float]] = []
    n = len(candles)
    for i in range(k, n - k):
        h, l = candles[i].h, candles[i].l
        left = candles[i - k:i]
        right = candles[i + 1:i + 1 + k]
        if all(x.h < h for x in left) and all(x.h <= h for x in right):
            out.append((i, "H", h))
        if all(x.l > l for x in left) and all(x.l >= l for x in right):
            out.append((i, "L", l))
    return out


def last_swing(candles: Sequence[Candle], kind: str, k: int = 2) -> Optional[float]:
    pts = [p for p in pivots(candles, k) if p[1] == kind]
    return pts[-1][2] if pts else None


def trend(candles: Sequence[Candle], k: int = 2) -> str:
    """'up' (higher highs and higher lows), 'down' (lower highs and lower lows) or 'range'."""
    pts = pivots(candles, k)
    highs = [p[2] for p in pts if p[1] == "H"][-2:]
    lows = [p[2] for p in pts if p[1] == "L"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return "range"
    if highs[1] > highs[0] and lows[1] > lows[0]:
        return "up"
    if highs[1] < highs[0] and lows[1] < lows[0]:
        return "down"
    return "range"


def aggregate(candles: Sequence[Candle], minutes: int, base_minutes: int = 5) -> List[Candle]:
    """Group base candles into complete higher-timeframe candles."""
    size = minutes * 60_000
    need = minutes // base_minutes
    groups: dict = {}
    order: list = []
    for c in candles:
        key = c.t - (c.t % size)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(c)
    out = []
    for key in order:
        g = groups[key]
        if len(g) < need:
            continue
        out.append(Candle(key, g[0].o, max(x.h for x in g), min(x.l for x in g), g[-1].c, sum(x.v for x in g)))
    return out


def has_fvg(candles: Sequence[Candle], start: int, end: int, side: str) -> bool:
    """True when a fair value gap exists for some k in [start, end] (k-2 >= 0)."""
    for k in range(max(start, 2), end + 1):
        a, c = candles[k - 2], candles[k]
        if side == "long" and a.h < c.l:
            return True
        if side == "short" and a.l > c.h:
            return True
    return False


def median_body(candles: Sequence[Candle]) -> float:
    bodies = [c.body for c in candles if c.body > 0]
    return median(bodies) if bodies else 0.0
