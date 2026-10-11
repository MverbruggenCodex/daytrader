"""Model broker: simulates every signal of the strategy (sizing, stop/target, fees, account rules).

All amounts are in USD, the currency of the Liquid paper account. Position size is based on
the Liquid paper equity when a recent sync exists, otherwise on the simulated balance.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .models import Candle, Trade
from .sessions import ams_date

LIQUID_FRESH_MS = 6 * 3_600_000


class Broker:
    def __init__(self, state: dict, acfg: dict):
        self.state = state
        self.acfg = acfg
        self.trades: List[Trade] = [Trade.from_dict(t) for t in state.get("trades", [])]

    # ---- account -------------------------------------------------------------------
    def realized(self) -> float:
        return sum(t.pnl_usd or 0.0 for t in self.trades if t.status == "closed")

    def balance(self) -> float:
        return round(self.acfg["start_balance"] + self.realized(), 2)

    def sizing_equity(self, ts: int) -> float:
        lq = self.state.get("liquid") or {}
        if lq.get("paper_mode") and lq.get("equity") and ts - lq.get("updated_t", 0) <= LIQUID_FRESH_MS:
            return float(lq["equity"])
        return self.balance()

    def open_trades(self, market: Optional[str] = None) -> List[Trade]:
        return [t for t in self.trades if t.status == "open" and (market is None or t.market == market)]

    def can_open(self, ts: int) -> Tuple[bool, str]:
        if len(self.open_trades()) >= self.acfg["max_open_trades"]:
            return False, f"al {self.acfg['max_open_trades']} trades open"
        day = ams_date(ts)
        today = sum(t.pnl_usd or 0.0 for t in self.trades if t.status == "closed" and t.closed_t and ams_date(t.closed_t) == day)
        start_of_day = self.balance() - today
        if start_of_day > 0 and today <= -self.acfg["daily_stop_pct"] / 100 * start_of_day:
            return False, f"daglimiet van -{self.acfg['daily_stop_pct']}% bereikt"
        if self.balance() <= 0:
            return False, "account leeg"
        return True, ""

    def _fee_pct(self, maker: bool) -> float:
        if maker:
            return self.acfg.get("fee_maker_pct", self.acfg.get("fee_pct", 0.045))
        return self.acfg.get("fee_taker_pct", self.acfg.get("fee_pct", 0.045))

    def next_id(self) -> str:
        nums = [int(t.id[1:]) for t in self.trades if t.id[1:].isdigit()]
        return f"T{(max(nums) + 1 if nums else 1):03d}"

    # ---- orders --------------------------------------------------------------------
    def open(self, market: str, side: str, entry: float, stop: float, target: float, ts: int, **extra) -> Trade:
        risk_usd = round(self.sizing_equity(ts) * self.acfg["risk_pct"] / 100, 2)
        dist = abs(entry - stop)
        size = risk_usd / dist
        fee = self._fee_pct(maker=True) / 100 * entry * size  # entry is a limit order
        t = Trade(id=self.next_id(), market=market, side=side, entry=entry, stop=stop, target=target,
                  size=round(size, 8), risk_usd=risk_usd, opened_t=ts, fees_usd=round(fee, 4), **extra)
        self.trades.append(t)
        return t

    def close(self, t: Trade, price: float, ts: int, reason: str) -> Trade:
        pnl = (price - t.entry) * t.size * t.dir
        fees = t.fees_usd + self._fee_pct(maker=reason == "take profit") / 100 * price * t.size
        t.exit = price
        t.closed_t = ts
        t.exit_reason = reason
        t.status = "closed"
        t.fees_usd = round(fees, 4)
        t.pnl_usd = round(pnl - fees, 2)
        t.r_result = round(t.pnl_usd / t.risk_usd, 2) if t.risk_usd else None
        return t

    def manage(self, market: str, c: Candle, candle_ms: int) -> List[Trade]:
        """Apply one new candle to the open trades of a market. Stop wins if both are hit."""
        closed = []
        for t in self.open_trades(market):
            if c.t < t.opened_t:
                continue
            if t.close_at is not None and c.t >= t.close_at:
                closed.append(self.close(t, c.o, c.t, "sessie-einde"))
                continue
            stop_reason = "breakeven" if t.checks.get("be_done") else "stop loss"
            if t.side == "long":
                if c.l <= t.stop:
                    closed.append(self.close(t, min(c.o, t.stop), c.t + candle_ms, stop_reason))
                elif c.h >= t.target:
                    closed.append(self.close(t, max(c.o, t.target), c.t + candle_ms, "take profit"))
            else:
                if c.h >= t.stop:
                    closed.append(self.close(t, max(c.o, t.stop), c.t + candle_ms, stop_reason))
                elif c.l <= t.target:
                    closed.append(self.close(t, min(c.o, t.target), c.t + candle_ms, "take profit"))
            be_r = t.checks.get("be_r")
            if t.status == "open" and be_r and not t.checks.get("be_done"):
                r0 = t.checks.get("risk0") or abs(t.entry - t.stop)
                if (t.side == "long" and c.h >= t.entry + be_r * r0) or (t.side == "short" and c.l <= t.entry - be_r * r0):
                    t.stop = t.entry  # applies from the next candle
                    t.checks["be_done"] = True
        return closed

    def open_pnl(self, prices: dict) -> float:
        total = 0.0
        for t in self.open_trades():
            p = prices.get(t.market)
            if p:
                total += (p - t.entry) * t.size * t.dir
        return round(total, 2)

    def dump(self) -> list:
        return [t.to_dict() for t in self.trades]
