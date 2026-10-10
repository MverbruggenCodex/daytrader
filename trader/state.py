"""Persistent JSON state (committed to the repo so cloud runs can continue where they left off)."""
from __future__ import annotations

import json
import os

STATE_VERSION = 1


def empty_state() -> dict:
    return {
        "version": STATE_VERSION,
        "trades": [],
        "zones": [],
        "events": [],
        "sessions": {},
        "last_ts": {},
        "candles": {},
        "eurusd": None,
        "zone_seq": 0,
        "event_seq": 0,
        "blackouts": [],
        "started_t": None,
    }


def load(path: str) -> dict:
    if not os.path.exists(path):
        return empty_state()
    with open(path) as f:
        st = json.load(f)
    base = empty_state()
    base.update(st)
    return base


def save(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)
