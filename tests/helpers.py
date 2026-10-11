import copy
import json
import os
from datetime import datetime, timezone

from trader.models import Candle

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIVE = 300_000


def cfg(**strategy_overrides):
    with open(os.path.join(ROOT, "config.json")) as f:
        c = json.load(f)
    c = copy.deepcopy(c)
    c["markets"] = {"BTCUSD": {"symbol": "BTC", "enabled": True, "sessions": ["london"], "trade_weekends": False, "decimals": 2}}
    c["account"]["risk_pct"] = 3  # tests are written for 3% risk, independent of the live setting
    c["strategy"].update(strategy_overrides)
    return c


def ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


class Path:
    """Build a 5m candle series from simple moves."""

    def __init__(self, start_t: int, price: float = 100.0, wick: float = 0.2):
        self.t = start_t
        self.p = price
        self.wick = wick
        self.candles = []

    def move(self, delta: float, n: int = 1, wick: float = None):
        w = self.wick if wick is None else wick
        for _ in range(n):
            o, c = self.p, self.p + delta
            self.candles.append(Candle(self.t, o, max(o, c) + w, min(o, c) - w, c))
            self.p = c
            self.t += FIVE
        return self

    def raw(self, o, h, l, c):
        self.candles.append(Candle(self.t, o, h, l, c))
        self.p = c
        self.t += FIVE
        return self


def london_long_setup():
    """Uptrend before London, quiet opening range, bullish displacement, pullback into demand, rally.

    Wednesday 2026-10-07: London opens 07:00 UTC (BST).
    Returns (candles, info).
    """
    open_t = ms("2026-10-07T07:00:00")
    pre = 96
    path = Path(open_t - pre * FIVE, 100.0)
    # rising zigzag: 12 up, 6 down -> higher highs and higher lows on 15m
    while len(path.candles) < pre:
        path.move(0.6, min(12, pre - len(path.candles)))
        if len(path.candles) < pre:
            path.move(-0.5, min(6, pre - len(path.candles)))
    assert path.t == open_t
    # opening range (3 candles) small
    path.move(0.3).move(-0.3).move(0.2)
    or_high = max(c.h for c in path.candles[-3:])
    # small up, then the bearish zone candle
    path.move(0.2)
    P = path.p
    path.raw(P + 0.3, P + 0.5, P - 0.2, P)  # zone candle (bearish) -> zone [P-0.2, P+0.5]
    zone = (P - 0.2, P + 0.5)
    # displacement: 4 big bullish candles with gaps
    for _ in range(4):
        path.move(4.0)
    leg_high = path.candles[-1].h
    # slow pullback back to the zone
    while path.p - 2.0 > P + 1.0:
        path.move(-2.0)
    path.raw(path.p, path.p + 0.2, P + 0.3, P + 0.8)  # taps zone, closes above it -> entry
    entry_close = path.p
    # rally through the target
    path.move(2.0, 12)
    return path.candles, {"open_t": open_t, "or_high": or_high, "zone": zone, "leg_high": leg_high, "entry": entry_close}
