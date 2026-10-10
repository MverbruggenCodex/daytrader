"""Pat's supply & demand zones.

Step 1 of the strategy: a zone is only valid after *displacement* — several big candles in a
row in one direction, with at least one fair value gap and a break of structure. The zone is
the last opposite-coloured candle before that move (wick to wick; body only when too wide).
"""
from __future__ import annotations

from typing import Optional, Sequence

from .models import Candle, Zone
from .structure import has_fvg, last_swing, median_body


def detect_displacement(candles: Sequence[Candle], i: int, scfg: dict, atr_value: float) -> Optional[dict]:
    """Check whether candle i completes a displacement leg. Returns details or None."""
    c = candles[i]
    d = c.direction()
    if d == 0:
        return None
    side = "long" if d == 1 else "short"
    j = i
    while j - 1 >= 0 and candles[j - 1].direction() == d:
        j -= 1
    run = candles[j:i + 1]
    if len(run) < scfg["min_run"]:
        return None

    base = candles[max(0, j - scfg["body_lookback"]):j]
    if len(base) < 10:
        return None
    med = median_body(base)
    if med <= 0:
        return None
    avg_body = sum(x.body for x in run) / len(run)
    big = avg_body >= scfg["body_mult"] * med

    fvg = has_fvg(candles, j + 1, i, side)

    swing = last_swing(candles[max(0, j - 150):j], "H" if side == "long" else "L", scfg["swing_pivot"])
    if swing is None:
        bos = False
    elif side == "long":
        bos = any(x.c > swing for x in run)
    else:
        bos = any(x.c < swing for x in run)

    # zone candle: last opposite-coloured candle just before the run
    zc = None
    for m in range(j - 1, max(-1, j - 5), -1):
        if candles[m].direction() == -d:
            zc = candles[m]
            break
    if zc is None:
        if j - 1 < 0:
            return None
        zc = candles[j - 1]

    top, bottom, body_only = zc.h, zc.l, False
    if atr_value > 0 and (top - bottom) > scfg["zone_max_atr"] * atr_value:
        top, bottom, body_only = max(zc.o, zc.c), min(zc.o, zc.c), True
    if top <= bottom:
        return None

    leg_extreme = max(x.h for x in run) if side == "long" else min(x.l for x in run)
    return {
        "side": side,
        "run_start": j,
        "run_len": len(run),
        "zone_candle": zc,
        "top": top,
        "bottom": bottom,
        "body_only": body_only,
        "leg_extreme": leg_extreme,
        "big_candles": big,
        "fvg": fvg,
        "bos": bos,
        "swing": swing,
        "avg_body_x": round(avg_body / med, 2),
    }


def zone_is_valid(det: dict, scfg: dict) -> bool:
    if not det["big_candles"]:
        return False
    if scfg.get("require_fvg", True) and not det["fvg"]:
        return False
    if scfg.get("require_bos", True) and not det["bos"]:
        return False
    return True


def make_zone(det: dict, market: str, session_key: str, created_t: int, seq: int) -> Zone:
    zc: Candle = det["zone_candle"]
    return Zone(
        id=f"Z{seq:04d}",
        market=market,
        session_key=session_key,
        side=det["side"],
        top=det["top"],
        bottom=det["bottom"],
        origin_t=zc.t,
        created_t=created_t,
        leg_extreme=det["leg_extreme"],
        checks={
            "displacement": f"{det['run_len']} candles, gem. {det['avg_body_x']}x normale body",
            "fvg": det["fvg"],
            "bos": det["bos"],
            "body_only": det["body_only"],
        },
    )


def evaluate_touch(zone: Zone, c: Candle, atr_value: float, scfg: dict) -> str:
    """Pat's normal entry + dead-zone and falling-knife rules for one new candle.

    Returns one of: 'none', 'touched', 'entry', 'dead', 'knife'.
    """
    if zone.side == "long":
        if zone.status == "fresh":
            if c.c < zone.bottom:
                return "dead"
            if c.l > zone.top:
                return "none"
            if c.bear and atr_value > 0 and c.body > scfg["falling_knife_atr"] * atr_value:
                return "knife"
        if c.c < zone.bottom:
            return "dead"
        if c.c > zone.top:
            return "entry"
        return "touched"
    else:
        if zone.status == "fresh":
            if c.c > zone.top:
                return "dead"
            if c.h < zone.bottom:
                return "none"
            if c.bull and atr_value > 0 and c.body > scfg["falling_knife_atr"] * atr_value:
                return "knife"
        if c.c > zone.top:
            return "dead"
        if c.c < zone.bottom:
            return "entry"
        return "touched"
