"""Basic data types: candles, zones and trades."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional


@dataclass
class Candle:
    t: int  # open time, ms since epoch (UTC)
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0

    @property
    def body(self) -> float:
        return abs(self.c - self.o)

    @property
    def bull(self) -> bool:
        return self.c > self.o

    @property
    def bear(self) -> bool:
        return self.c < self.o

    def direction(self) -> int:
        return 1 if self.bull else (-1 if self.bear else 0)

    def to_list(self) -> list:
        return [self.t, self.o, self.h, self.l, self.c, self.v]

    @classmethod
    def from_list(cls, row: list) -> "Candle":
        t, o, h, l, c = row[:5]
        v = row[5] if len(row) > 5 else 0.0
        return cls(int(t), float(o), float(h), float(l), float(c), float(v))


class _Dictable:
    def to_dict(self) -> dict:
        return asdict(self)  # type: ignore[arg-type]

    @classmethod
    def from_dict(cls, d: dict):
        names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class Zone(_Dictable):
    """A Pat-style supply (short) or demand (long) zone."""
    id: str
    market: str
    session_key: str
    side: str  # "long" (demand) or "short" (supply)
    top: float
    bottom: float
    origin_t: int  # open time of the zone candle
    created_t: int  # open time of the candle that confirmed the displacement
    leg_extreme: float  # high (long) / low (short) of the displacement leg = first target
    status: str = "fresh"  # fresh | touched | entered | dead | skipped
    status_t: Optional[int] = None
    checks: dict = field(default_factory=dict)
    note: str = ""


@dataclass
class Trade(_Dictable):
    id: str
    market: str
    side: str  # "long" | "short"
    entry: float
    stop: float
    target: float
    size: float  # units of the base asset
    risk_eur: float
    opened_t: int
    status: str = "open"  # open | closed
    strategy: str = "ORB + Supply & Demand"
    setup: str = ""
    session_key: Optional[str] = None
    close_at: Optional[int] = None  # forced session close (ms)
    zone_id: Optional[str] = None
    checks: dict = field(default_factory=dict)
    reason: str = ""
    exit: Optional[float] = None
    closed_t: Optional[int] = None
    exit_reason: Optional[str] = None
    pnl_eur: Optional[float] = None
    r_result: Optional[float] = None
    fees_eur: float = 0.0

    @property
    def dir(self) -> int:
        return 1 if self.side == "long" else -1

    @property
    def rr(self) -> float:
        risk = abs(self.entry - self.stop)
        return round(abs(self.target - self.entry) / risk, 2) if risk else 0.0


def jsonable(x: Any) -> Any:
    if hasattr(x, "to_dict"):
        return x.to_dict()
    return x
