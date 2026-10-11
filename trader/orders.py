"""Order intents for the Liquid paper account.

The engine never talks to Liquid itself. For every entry signal it writes an *order intent*:
a limit order at the signal price with stop loss and take profit attached. Claude turns a
pending intent into a Liquid `suggest_trade` preview, and only places it (`execute_order`)
after the user confirms it in chat and the preview's executionMode is "paper".

Life cycle (status):
  pending        -> waiting for the user's confirmation
  placed         -> limit order resting on the Liquid paper account
  filled         -> position open on Liquid
  closed         -> position closed (TP, SL or manual close)
  declined       -> user said no
  expired        -> not confirmed in time, or price already past stop/target
  cancel_needed  -> placed but not filled before the entry window closed: Claude cancels it
  close_needed   -> filled and the session has ended: Claude proposes a close (needs confirmation)
  cancelled / rejected
"""
from __future__ import annotations

import math
from typing import List, Optional

from .models import Trade

ACTIVE = ("pending", "placed", "filled", "cancel_needed", "close_needed")


def sig_round(x: float, sig: int = 5) -> float:
    """Hyperliquid prices: at most 5 significant figures."""
    if x == 0:
        return 0.0
    digits = sig - int(math.floor(math.log10(abs(x)))) - 1
    return round(x, max(0, digits))


def size_order(t: Trade, mcfg: dict, acfg: dict, equity: float) -> Optional[dict]:
    """Notional and leverage for a trade so that a stop-out loses about risk_usd."""
    entry, stop = t.entry, t.stop
    dist_pct = abs(entry - stop) / entry
    if dist_pct <= 0:
        return None
    notional = t.risk_usd / dist_pct
    cap = acfg["collateral_cap_pct"] / 100 * equity
    max_lev = mcfg.get("max_leverage", 5)
    lev = max(1, math.ceil(notional / cap)) if cap > 0 else max_lev
    if lev > max_lev:
        lev = max_lev
        notional = cap * max_lev
    if notional / lev < acfg["min_collateral"]:
        lev = max(1, math.floor(notional / acfg["min_collateral"]))
        if notional / lev < acfg["min_collateral"]:
            return None
    notional = round(notional, 2)
    return {"notional": notional, "leverage": lev, "collateral": round(notional / lev, 2),
            "risk_usd": round(notional * dist_pct, 2)}


def make_intent(t: Trade, symbol: str, mcfg: dict, acfg: dict, equity: float, created_t: int,
                expires_t: int, close_at: int, seq: int) -> dict:
    sz = size_order(t, mcfg, acfg, equity)
    intent = {
        "id": f"O{seq:03d}", "trade_id": t.id, "market": t.market, "symbol": symbol, "side": t.side,
        "type": "limit", "price": sig_round(t.entry), "sl": sig_round(t.stop), "tp": sig_round(t.target),
        "rr": t.rr, "created_t": created_t, "expires_t": expires_t, "close_at": close_at,
        "status": "pending", "liquid_order_id": None, "idempotency_key": f"daytrader-{t.id}-{created_t}",
        "note": "", "reason": t.reason,
    }
    if sz is None:
        intent.update(status="rejected", note="order te klein voor Liquid (minimaal $10 onderpand)")
    else:
        intent.update(sz)
    return intent


def housekeeping(intents: List[dict], now: int, prices: dict) -> List[str]:
    """Time and price based transitions. Returns human-readable changes."""
    changes = []
    for o in intents:
        p = prices.get(o["market"])
        if o["status"] == "pending":
            if now > o["expires_t"]:
                o["status"], o["note"] = "expired", "niet op tijd bevestigd"
            elif p is not None and ((o["side"] == "long" and (p <= o["sl"] or p >= o["tp"]))
                                    or (o["side"] == "short" and (p >= o["sl"] or p <= o["tp"]))):
                o["status"], o["note"] = "expired", f"koers ({p}) al voorbij stop of doel"
            else:
                continue
            changes.append(f"{o['id']} {o['status']}: {o['note']}")
        elif o["status"] == "placed" and now > o["expires_t"]:
            o["status"], o["note"] = "cancel_needed", "limietorder niet gevuld voor het einde van het entry-venster"
            changes.append(f"{o['id']} moet geannuleerd worden")
        elif o["status"] == "filled" and o.get("close_at") and now >= o["close_at"]:
            o["status"], o["note"] = "close_needed", "sessie voorbij: positie sluiten (bevestiging nodig)"
            changes.append(f"{o['id']} moet gesloten worden")
    return changes


def sync_positions(intents: List[dict], positions: List[dict]) -> List[str]:
    """Match Liquid paper positions to placed/filled intents by symbol."""
    held = set()
    for pos in positions or []:
        sym = pos.get("coin") or pos.get("symbol") or pos.get("asset")
        if sym:
            held.add(str(sym))
    changes = []
    for o in intents:
        if o["status"] == "placed" and o["symbol"] in held:
            o["status"], o["note"] = "filled", "limietorder gevuld"
            changes.append(f"{o['id']} gevuld")
        elif o["status"] in ("filled", "close_needed") and o["symbol"] not in held:
            o["status"], o["note"] = "closed", "positie niet meer open op Liquid (TP, SL of handmatig)"
            changes.append(f"{o['id']} gesloten")
    return changes
