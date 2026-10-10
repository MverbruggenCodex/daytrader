"""Paper broker: position sizing, stop/target handling, fees and account rules."""
from __future__ import annotations

from typing import List, Optional, Tuple

from .models import Candle, Trade
from .sessions import ams_date


class Broker:
    def __init__(self, state: dict, acfg: dict):
        self.state = state
        self.acfg = acfg
        self.trades: List[Trade] = [Trade.from_dict(t) for t in state.get("trades", [])]

    # ---- account -------------------------------------------------------------------
    def realized(self) -> float:
        return sum(t.pnl_eur or 0.0 for t in self.trades if t.status == "closed")

    def balance(self) -> float:
        return round(self.acfg["start_balance"] + self.realized(), 2)

    def open_trades(self, market: Optional[str] = None) -> List[Trade]:
        return [t for t in self.trades if t.status == "open" and (market is None or t.market == market)]

    def can_open(self, ts: int) -> Tuple[bool, str]:
        if len(self.open_trades()) >= self.acfg["max_open_trades"]:
            return False, f"al {self.acfg['max_open_trades']} trades open"
        day = ams_date(ts)
        today = sum(t.pnl_eur or 0.0 for t in self.trades if t.status == "closed" and t.closed_t and ams_date(t.closed_t) == day)
        start_of_day = self.balance() - today
        if start_of_day > 0 and today <= -self.acfg["daily_stop_pct"] / 100 * start_of_day:
            return False, f"daglimiet van -{self.acfg['daily_stop_pct']}% bereikt"
        if self.balance() <= 0:
            return False, "account leeg"
        return True, ""

    def next_id(self) -> str:
        nums = [int(t.id[1:]) for t in self.trades if t.id[1:].isdigit()]
        return f"T{(max(nums) + 1 if nums else 1):03d}"

    # ---- orders --------------------------------------------------------------------
    def open(self, market: str, side: str, entry: float, stop: float, target: float, ts: int,
             eurusd: float, **extra) -> Trade:
        risk_eur = round(self.balance() * self.acfg["risk_pct"] / 100, 2)
        dist = abs(entry - stop)
        size = risk_eur * eurusd / dist  # all markets are quoted in USD
        fee_eur = self.acfg["fee_pct"] / 100 * entry * size / eurusd
        t = Trade(id=self.next_id(), market=market, side=side, entry=entry, stop=stop, target=target,
                  size=round(size, 8), risk_eur=risk_eur, opened_t=ts, fees_eur=round(fee_eur, 4), **extra)
        self.trades.append(t)
        return t

    def close(self, t: Trade, price: float, ts: int, reason: str, eurusd: float) -> Trade:
        pnl_usd = (price - t.entry) * t.size * t.dir
        exit_fee_eur = self.acfg["fee_pct"] / 100 * price * t.size / eurusd
        fees = t.fees_eur + exit_fee_eur
        t.exit = price
        t.closed_t = ts
        t.exit_reason = reason
        t.status = "closed"
        t.fees_eur = round(fees, 4)
        t.pnl_eur = round(pnl_usd / eurusd - fees, 2)
        t.r_result = round(t.pnl_eur / t.risk_eur, 2) if t.risk_eur else None
        return t

    def manage(self, market: str, c: Candle, eurusd: float, candle_ms: int) -> List[Trade]:
        """Apply one new candle to the open trades of a market. Stop wins if both are hit."""
        closed = []
        for t in self.open_trades(market):
            if c.t < t.opened_t:
                continue
            if t.close_at is not None and c.t >= t.close_at:
                closed.append(self.close(t, c.o, c.t, "sessie-einde", eurusd))
                continue
            if t.side == "long":
                if c.l <= t.stop:
                    closed.append(self.close(t, min(c.o, t.stop), c.t + candle_ms, "stop loss", eurusd))
                elif c.h >= t.target:
                    closed.append(self.close(t, max(c.o, t.target), c.t + candle_ms, "take profit", eurusd))
            else:
                if c.h >= t.stop:
                    closed.append(self.close(t, max(c.o, t.stop), c.t + candle_ms, "stop loss", eurusd))
                elif c.l <= t.target:
                    closed.append(self.close(t, min(c.o, t.target), c.t + candle_ms, "take profit", eurusd))
        return closed

    def open_pnl(self, prices: dict, eurusd: float) -> float:
        total = 0.0
        for t in self.open_trades():
            p = prices.get(t.market)
            if p:
                total += (p - t.entry) * t.size * t.dir / eurusd
        return round(total, 2)

    def dump(self) -> list:
        return [t.to_dict() for t in self.trades]
