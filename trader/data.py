"""Candle sources.

* hyperliquid: direct HTTP to Hyperliquid's public info API (the venue behind Liquid).
  Works wherever the machine can reach api.hyperliquid.xyz (e.g. your own computer).
* inbox: JSON files written by Claude from the Liquid connector (`show_chart`).
  Used in Claude Code cloud sessions, where the shell cannot reach the exchange.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Dict, List

from .models import Candle

HL_URL = "https://api.hyperliquid.xyz/info"


def fetch_hyperliquid(symbol: str, start_ms: int, end_ms: int, interval: str = "5m") -> List[Candle]:
    body = json.dumps({"type": "candleSnapshot",
                       "req": {"coin": symbol, "interval": interval, "startTime": start_ms, "endTime": end_ms}}).encode()
    req = urllib.request.Request(HL_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        rows = json.loads(r.read().decode())
    return [Candle(int(x["t"]), float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"]), float(x.get("v", 0)))
            for x in rows]


def parse_candles(payload) -> List[Candle]:
    """Accepts Liquid's show_chart answer, Hyperliquid's candleSnapshot rows, or compact lists."""
    if isinstance(payload, dict):
        if "candles" in payload:
            payload = payload["candles"]
        elif "content" in payload:  # raw MCP envelope
            payload = json.loads("".join(p.get("text", "") for p in payload["content"]))
            return parse_candles(payload)
    out: List[Candle] = []
    for x in payload or []:
        if isinstance(x, (list, tuple)):
            out.append(Candle.from_list(list(x)))
        elif "timestamp" in x:
            out.append(Candle(int(x["timestamp"]), float(x["open"]), float(x["high"]), float(x["low"]),
                              float(x["close"]), float(x.get("volume", 0) or 0)))
        else:
            out.append(Candle(int(x["t"]), float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"]),
                              float(x.get("v", 0) or 0)))
    out.sort(key=lambda c: c.t)
    return out


def load_inbox(path: str, markets: dict) -> Dict[str, List[Candle]]:
    """Read inbox/<MARKET>.json (or <symbol>.json with ':' replaced by '_')."""
    found: Dict[str, List[Candle]] = {}
    if not os.path.isdir(path):
        return found
    by_file = {}
    for key, m in markets.items():
        by_file[f"{key}.json"] = key
        if m.get("symbol"):
            by_file[m["symbol"].replace(":", "_") + ".json"] = key
    for name in sorted(os.listdir(path)):
        key = by_file.get(name)
        if not key:
            continue
        with open(os.path.join(path, name)) as f:
            found[key] = parse_candles(json.load(f))
    return found


def load_inbox_htf(path: str, markets: dict) -> Dict[tuple, List[Candle]]:
    """Read higher-timeframe files inbox/<MARKET>@<minutes>.json, e.g. BTCUSD@60.json."""
    found: Dict[tuple, List[Candle]] = {}
    if not os.path.isdir(path):
        return found
    for name in sorted(os.listdir(path)):
        if "@" not in name or not name.endswith(".json"):
            continue
        key, tf = name[:-5].split("@", 1)
        if key in markets and tf.isdigit():
            with open(os.path.join(path, name)) as f:
                found[(key, int(tf))] = parse_candles(json.load(f))
    return found


def clear_inbox(path: str) -> None:
    if not os.path.isdir(path):
        return
    for name in os.listdir(path):
        if name.endswith(".json"):
            os.remove(os.path.join(path, name))
