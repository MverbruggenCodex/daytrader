"""Trading sessions (London / New York) in their own time zones, DST-aware."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import List
from zoneinfo import ZoneInfo

AMS = ZoneInfo("Europe/Amsterdam")


@dataclass
class Window:
    name: str
    label: str
    day: str
    open: int
    range_end: int
    entry_until: int
    close: int

    @property
    def key(self) -> str:
        return f"{self.name}|{self.day}"


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _at(day: date, hhmm: str, tz: ZoneInfo) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, time(h, m), tz)


def window_for(name: str, cfg: dict, day: date) -> Window:
    tz = ZoneInfo(cfg["tz"])
    open_dt = _at(day, cfg["open"], tz)
    return Window(
        name=name,
        label=cfg.get("label", name),
        day=day.isoformat(),
        open=_ms(open_dt),
        range_end=_ms(open_dt + timedelta(minutes=cfg["range_minutes"])),
        entry_until=_ms(_at(day, cfg["entry_until"], tz)),
        close=_ms(_at(day, cfg["close"], tz)),
    )


def windows_at(ts_ms: int, market_cfg: dict, sessions_cfg: dict) -> List[Window]:
    """Session windows of this market that contain ts_ms (open <= ts < close)."""
    out = []
    for name in market_cfg.get("sessions", []):
        cfg = sessions_cfg[name]
        local = datetime.fromtimestamp(ts_ms / 1000, ZoneInfo(cfg["tz"]))
        if local.weekday() >= 5 and not market_cfg.get("trade_weekends", False):
            continue
        w = window_for(name, cfg, local.date())
        if w.open <= ts_ms < w.close:
            out.append(w)
    return out


def ams_date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, AMS).date().isoformat()


def iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ams_hm(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, AMS).strftime("%d-%m %H:%M")
