import json
import os
import subprocess
import sys

import pytest

from helpers import FIVE, Path, cfg, london_long_setup, ms
from trader import state as st_mod
from trader.broker import Broker
from trader.data import parse_candles
from trader.engine import Engine
from trader.export import build
from trader.models import Candle, Zone
from trader.sessions import window_for, windows_at
from trader.structure import aggregate, has_fvg, pivots, trend
from trader.zones import detect_displacement, evaluate_touch, zone_is_valid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---- sessions --------------------------------------------------------------------------
def test_session_times_follow_daylight_saving():
    c = cfg()
    from datetime import date
    lon = window_for("london", c["sessions"]["london"], date(2026, 10, 7))
    ny = window_for("newyork", c["sessions"]["newyork"], date(2026, 10, 7))
    assert lon.open == ms("2026-10-07T07:00:00")  # 08:00 BST
    assert ny.open == ms("2026-10-07T13:30:00")  # 09:30 EDT
    winter = window_for("newyork", c["sessions"]["newyork"], date(2026, 12, 2))
    assert winter.open == ms("2026-12-02T14:30:00")  # 09:30 EST
    assert lon.range_end - lon.open == 15 * 60_000


def test_weekend_skipped_unless_allowed():
    c = cfg()
    sat = ms("2026-10-10T08:00:00")
    assert windows_at(sat, c["markets"]["BTCUSD"], c["sessions"]) == []
    m = dict(c["markets"]["BTCUSD"], trade_weekends=True)
    assert len(windows_at(sat, m, c["sessions"])) == 1


# ---- structure -------------------------------------------------------------------------
def test_trend_and_pivots():
    p = Path(0, 100)
    for _ in range(6):
        p.move(1.0, 6).move(-0.6, 3)
    assert trend(p.candles) == "up"
    q = Path(0, 100)
    for _ in range(6):
        q.move(-1.0, 6).move(0.6, 3)
    assert trend(q.candles) == "down"
    assert any(k == "H" for _, k, _ in pivots(p.candles))


def test_fvg_and_aggregate():
    p = Path(0, 100, wick=0.1).move(2.0, 3)
    assert has_fvg(p.candles, 2, 2, "long")
    q = Path(0, 100, wick=1.5).move(1.0, 3)
    assert not has_fvg(q.candles, 2, 2, "long")
    agg = aggregate(Path(0, 100).move(1, 7).candles, 15)
    assert len(agg) == 2 and agg[0].o == 100 and agg[0].c == 103


# ---- zones -----------------------------------------------------------------------------
def test_displacement_creates_valid_demand_zone():
    candles, info = london_long_setup()
    c = cfg()
    idx = next(i for i, x in enumerate(candles) if x.h == info["leg_high"])
    det = detect_displacement(candles, idx, c["strategy"], 2.0)
    assert det and det["side"] == "long" and zone_is_valid(det, c["strategy"])
    assert (det["bottom"], det["top"]) == pytest.approx(info["zone"])
    assert det["fvg"] and det["bos"] and det["big_candles"]


def test_entry_dead_and_knife_rules():
    s = cfg()["strategy"]
    z = Zone("Z1", "BTCUSD", "k", "long", top=101, bottom=100, origin_t=0, created_t=0, leg_extreme=110)
    assert evaluate_touch(z, Candle(1, 103, 104, 102, 103.5), 1.0, s) == "none"
    assert evaluate_touch(z, Candle(1, 102, 102, 100.5, 101.5), 1.0, s) == "entry"  # tap + close above
    assert evaluate_touch(z, Candle(1, 102, 102, 100.5, 100.7), 1.0, s) == "touched"
    assert evaluate_touch(z, Candle(1, 102, 102, 99, 99.5), 1.0, s) == "dead"  # close through
    assert evaluate_touch(z, Candle(1, 104.5, 104.6, 100.8, 101.5), 1.0, s) == "knife"  # 3 ATR bearish candle
    short = Zone("Z2", "BTCUSD", "k", "short", top=101, bottom=100, origin_t=0, created_t=0, leg_extreme=90)
    assert evaluate_touch(short, Candle(1, 99, 100.5, 98.8, 99.4), 1.0, s) == "entry"
    assert evaluate_touch(short, Candle(1, 99, 102, 98.8, 101.5), 1.0, s) == "dead"


# ---- broker ----------------------------------------------------------------------------
def test_sizing_risk_and_stop_first():
    acfg = cfg()["account"]
    b = Broker({"trades": []}, acfg)
    t = b.open("BTCUSD", "long", 100.0, 99.0, 103.0, 0)
    assert t.risk_usd == 300 and t.size == pytest.approx(300)
    closed = b.manage("BTCUSD", Candle(FIVE, 100, 104, 98.5, 101), FIVE)  # both hit -> stop
    assert closed[0].exit_reason == "stop loss" and closed[0].r_result < -0.99
    assert b.balance() < 10000


def test_sizing_uses_fresh_liquid_equity():
    acfg = cfg()["account"]
    st = {"trades": [], "liquid": {"paper_mode": True, "equity": 5000.0, "updated_t": 0}}
    b = Broker(st, acfg)
    assert b.open("BTCUSD", "long", 100.0, 99.0, 103.0, 1000).risk_usd == 150
    assert b.open("BTCUSD", "long", 100.0, 99.0, 103.0, 7 * 3_600_000).risk_usd == 300  # stale sync -> model


def test_daily_stop_blocks_new_trades():
    acfg = cfg()["account"]
    b = Broker({"trades": []}, acfg)
    for k in range(2):
        t = b.open("BTCUSD", "long", 100.0, 99.0, 103.0, k * FIVE)
        b.close(t, 99.0, k * FIVE + FIVE, "stop loss")
    ok, why = b.can_open(3 * FIVE)
    assert not ok and "daglimiet" in why


# ---- liquid orders ---------------------------------------------------------------------
def test_order_sizing_and_rounding():
    from trader.models import Trade
    from trader.orders import make_intent, sig_round, size_order
    acfg = cfg()["account"]
    t = Trade("T001", "BTCUSD", "long", 83046.0, 82000.0, 86000.0, 0.0, 300.0, 0)
    sz = size_order(t, {"max_leverage": 20}, acfg, 10000)
    assert sz["notional"] == pytest.approx(300 / (1046 / 83046), rel=1e-3) and sz["leverage"] == 10
    assert sz["collateral"] <= 2500 + 0.01 and sz["leverage"] <= 20 and sz["risk_usd"] == pytest.approx(300, rel=1e-3)
    capped = size_order(t, {"max_leverage": 2}, acfg, 10000)
    assert capped["leverage"] == 2 and capped["notional"] == 5000 and capped["risk_usd"] < 300  # cap limits the risk
    assert sig_round(83046.37) == 83046 and sig_round(1.120634) == 1.1206 and sig_round(4190.345) == 4190.3
    tiny = Trade("T002", "BTCUSD", "long", 100.0, 50.0, 200.0, 0.0, 1.0, 0)
    o = make_intent(tiny, "BTC", {"max_leverage": 20}, acfg, 10000, 0, 10, 20, 1)
    assert o["status"] == "rejected"


def test_order_housekeeping_and_sync():
    from trader.orders import housekeeping, sync_positions
    base = {"market": "BTCUSD", "symbol": "BTC", "side": "long", "sl": 99.0, "tp": 103.0, "created_t": 0,
            "expires_t": 100, "close_at": 200}
    o1 = dict(base, id="O1", status="pending")
    o2 = dict(base, id="O2", status="pending")
    o3 = dict(base, id="O3", status="placed")
    o4 = dict(base, id="O4", status="filled")
    housekeeping([o1], 150, {"BTCUSD": 100.0})
    housekeeping([o2], 50, {"BTCUSD": 98.0})
    housekeeping([o3, o4], 250, {})
    assert o1["status"] == "expired" and o2["status"] == "expired" and "voorbij" in o2["note"]
    assert o3["status"] == "cancel_needed" and o4["status"] == "close_needed"
    o5 = dict(base, id="O5", status="placed")
    sync_positions([o5, o4], [{"coin": "BTC", "szi": "0.1"}])
    assert o5["status"] == "filled" and o4["status"] == "close_needed"
    sync_positions([o5, o4], [])
    assert o5["status"] == "closed" and o4["status"] == "closed"


# ---- end to end ------------------------------------------------------------------------
def run_engine(candles, c=None, live_from=0):
    c = c or cfg()
    state = st_mod.empty_state()
    eng = Engine(c, state, now_ms=candles[-1].t + FIVE * 2, live_from=live_from)
    eng.ingest("BTCUSD", candles)
    events = eng.run()
    return eng, events


def test_full_london_long_trade_hits_target():
    candles, info = london_long_setup()
    eng, events = run_engine(candles)
    trades = eng.broker.trades
    assert len(trades) == 1, [e["title"] for e in events]
    t = trades[0]
    assert t.side == "long" and t.entry == pytest.approx(info["entry"])
    assert t.stop < info["zone"][0] and t.target >= info["leg_high"]
    assert t.status == "closed" and t.exit_reason == "take profit" and t.pnl_usd > 0
    assert t.rr >= 2
    (o,) = eng.state["orders"]
    assert o["trade_id"] == t.id and o["type"] == "limit" and o["status"] in ("pending", "expired")
    assert o["notional"] > 0 and o["sl"] < o["price"] < o["tp"]
    kinds = [e["trigger"] for e in events]
    assert "Openingsrange" in kinds and "Nieuwe zone" in kinds and "Entry" in kinds and "Exit" in kinds
    files = build(eng)
    assert files["trades"][0]["status"] == "Gesloten"
    assert next(z for z in files["zones"] if z["id"] == t.zone_id)["status"] == "Trade"


def test_no_zone_against_trend():
    candles, _ = london_long_setup()
    # mirror the series: downtrend before London with a bullish displacement is still a long setup
    # against the trend -> no trade
    flipped = [Candle(x.t, 400 - x.o, 400 - x.l, 400 - x.h, 400 - x.c) for x in candles[:96]]
    tail_start = flipped[-1].c
    shift = tail_start - candles[96].o
    tail = [Candle(x.t, x.o + shift, x.h + shift, x.l + shift, x.c + shift) for x in candles[96:]]
    eng, events = run_engine(flipped + tail)
    assert eng.broker.trades == []


def test_rr_filter_and_warmup():
    candles, _ = london_long_setup()
    c = cfg()
    c["account"]["min_rr"] = 50
    eng, events = run_engine(candles, c)
    assert eng.broker.trades == [] and any("R:R" in e["text"] for e in events)
    eng2, _ = run_engine(candles, cfg(), live_from=candles[-1].t + FIVE)  # everything is warm-up
    assert eng2.broker.trades == []


def test_incremental_runs_match_single_run():
    candles, _ = london_long_setup()
    c = cfg()
    state = st_mod.empty_state()
    for cut in range(60, len(candles) + 1, 7):
        part = candles[:cut]
        eng = Engine(c, state, now_ms=part[-1].t + FIVE, live_from=0)
        eng.ingest("BTCUSD", part[-40:] if cut > 60 else part)
        eng.run()
    eng = Engine(c, state, now_ms=candles[-1].t + 2 * FIVE, live_from=0)
    eng.ingest("BTCUSD", candles[-10:])
    eng.run()
    single, _ = run_engine(candles)
    assert [(t.entry, t.exit) for t in eng.broker.trades] == [(t.entry, t.exit) for t in single.broker.trades]


def test_parse_liquid_and_hyperliquid_formats():
    liquid = {"candles": [{"timestamp": 1000, "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "3"}]}
    hl = [{"t": 1000, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "3"}]
    compact = {"candles": [[1000, 1, 2, 0.5, 1.5]]}
    for payload in (liquid, hl, compact):
        (c,) = parse_candles(payload)
        assert (c.t, c.o, c.h, c.l, c.c) == (1000, 1, 2, 0.5, 1.5)


def test_cli_inbox_run(tmp_path):
    candles, _ = london_long_setup()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "BTCUSD.json").write_text(json.dumps({"candles": [c.to_list() for c in candles]}))
    cfg_path = tmp_path / "config.json"
    c = cfg()
    cfg_path.write_text(json.dumps(c))
    env = dict(os.environ, PYTHONPATH=ROOT)
    base = [sys.executable, "-m", "trader", "--config", str(cfg_path), "--state", str(tmp_path / "state.json"),
            "--export", str(tmp_path / "export")]
    subprocess.run(base + ["init"], check=True, env=env, capture_output=True)
    out = subprocess.run(base + ["run", "--source", "inbox", "--inbox", str(inbox)], check=True, env=env,
                         capture_output=True, text=True)
    res = json.loads(out.stdout)
    assert res["new_candles"]["BTCUSD"] == len(candles)
    assert (tmp_path / "export" / "trades.json").exists() and not list(inbox.glob("*.json"))


def test_cli_orders_require_paper_mode(tmp_path):
    candles, _ = london_long_setup()
    c = cfg()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(c))
    env = dict(os.environ, PYTHONPATH=ROOT)
    base = [sys.executable, "-m", "trader", "--config", str(cfg_path), "--state", str(tmp_path / "state.json"),
            "--export", str(tmp_path / "export")]
    subprocess.run(base + ["init"], check=True, env=env, capture_output=True)
    state = json.loads((tmp_path / "state.json").read_text())
    state["orders"] = [{"id": "O001", "trade_id": "T001", "market": "BTCUSD", "symbol": "BTC", "side": "long",
                        "price": 100.0, "sl": 99.0, "tp": 103.0, "rr": 3.0, "created_t": 0,
                        "expires_t": 10 ** 15, "close_at": 10 ** 15, "status": "pending"}]
    (tmp_path / "state.json").write_text(json.dumps(state))
    out = json.loads(subprocess.run(base + ["orders"], check=True, env=env, capture_output=True, text=True).stdout)
    assert out["propose"] == [] and "blocked" in out
    live = tmp_path / "live.json"
    live.write_text(json.dumps({"paperMode": False, "account": {"equity": "100"}, "positions": []}))
    r = subprocess.run(base + ["liquid-sync", "--file", str(live)], env=env, capture_output=True, text=True)
    assert r.returncode == 2
    paper = tmp_path / "paper.json"
    paper.write_text(json.dumps({"paperMode": True, "account": {"equity": "9989.69", "available_balance": "9989.69",
                                                                "margin_used": "0.00"}, "positions": []}))
    subprocess.run(base + ["liquid-sync", "--file", str(paper)], check=True, env=env, capture_output=True)
    out = json.loads(subprocess.run(base + ["orders"], check=True, env=env, capture_output=True, text=True).stdout)
    assert [o["id"] for o in out["propose"]] == ["O001"] and "blocked" not in out
    subprocess.run(base + ["order-update", "--id", "O001", "--status", "placed", "--liquid-id", "123"], check=True,
                   env=env, capture_output=True)
    out = json.loads(subprocess.run(base + ["orders"], check=True, env=env, capture_output=True, text=True).stdout)
    assert out["propose"] == [] and out["watch"][0]["liquid_order_id"] == "123"
