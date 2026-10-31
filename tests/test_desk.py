import pytest

from bot.analysis import indicators, resample, rsi
from bot.config import Config, RiskLimits
from bot.engine import Engine
from bot.events import EventBus


def make(**risk):
    cfg = Config(auto_git_commit=False, symbols=["A", "B", "C"], risk=RiskLimits(**risk))
    e = Engine(cfg, EventBus())
    e.load_active_strategy()
    e.tick()
    e.broker.close_all()   # start each test from a flat account
    return e


def set_price(e, sym, px):
    e.feed.bars[sym][-1]["c"] = px


def test_buy_with_stop_fills_and_takes_symbol_from_strategy():
    e = make()
    px = e.feed.last_price("A")
    r = e.desk.order("A", "buy", percent_equity=4, stop_loss=px * 0.97, take_profit=px * 1.05, reason="test", author="ai")
    assert r["status"] == "filled" and r["position_after"] > 0
    assert "A" in e.desk.locked()
    qty = e.broker.positions()["A"].qty
    for _ in range(30):          # strategy must not resize a desk-controlled symbol
        set_price(e, "A", px)    # keep the stop from triggering
        e.tick()
        set_price(e, "A", px)
    assert e.broker.positions()["A"].qty == pytest.approx(qty)
    assert e.desk.journal[-1]["reason"] == "test"


def test_rejections_explain_and_give_max_size():
    e = make(max_position_pct=0.1)
    r = e.desk.order("A", "buy", percent_equity=50, reason="too big")
    assert r["status"] == "rejected" and r["max_allowed_qty"] > 0
    ok = e.desk.order("A", "buy", qty=r["max_allowed_qty"], reason="max")   # the hint itself must be accepted
    assert ok["status"] == "filled"
    e.desk.order("A", "buy", qty=r["max_allowed_qty"], reason="up to the position cap")
    r2 = e.desk.order("A", "buy", percent_equity=5, reason="over cap")
    assert r2["status"] == "rejected"
    if r2["max_allowed_qty"] > 0:
        assert e.desk.order("A", "buy", qty=r2["max_allowed_qty"], reason="hint")["status"] == "filled"
    assert e.desk.order("ZZZ", "buy", qty=1, reason="x")["status"] == "rejected"
    assert e.desk.order("B", "sell", qty=1, reason="short")["status"] == "rejected"  # shorting disabled
    assert e.desk.order("B", "buy", qty=1, notional=100, reason="two sizes")["status"] == "rejected"


def test_dry_run_changes_nothing():
    e = make()
    before = dict(e.broker.positions())
    r = e.desk.order("B", "buy", notional=1000, reason="preview", dry_run=True)
    assert r["status"] == "preview" and r["position_after"] > 0
    assert dict(e.broker.positions()).keys() == before.keys() and not e.desk.journal


def test_stop_loss_and_take_profit_execute():
    e = make()
    e.desk.set_mode("manual")
    px = e.feed.last_price("C")
    e.desk.order("C", "buy", notional=5000, stop_loss=px * 0.98, take_profit=px * 1.1, reason="t")
    e.feed.advance = lambda: None      # freeze the simulated market so we control the price
    set_price(e, "C", px * 0.97)
    e.tick()
    assert "C" not in e.broker.positions()
    last = e.desk.journal[-1]
    assert last["author"] == "auto-exit" and "stop-loss" in last["reason"] and last["realized_pnl"] < 0


def test_kill_switch_and_pause_only_allow_risk_reduction():
    e = make()
    e.desk.order("A", "buy", notional=4000, reason="t")
    e.risk.state.halted, e.risk.state.halt_reason = True, "test"
    assert e.desk.order("A", "buy", notional=500, reason="more")["status"] == "rejected"
    assert e.desk.close("A", 50, "reduce")["status"] == "filled"
    e.risk.state.halted = False
    e.pause("operator")
    assert e.desk.order("B", "buy", notional=500, reason="x")["status"] == "rejected"
    assert e.resume("mcp-ai").startswith("refused")
    assert e.desk.close("A", 100, "flat")["status"] == "filled"
    assert e.resume("operator") == "resumed"


def test_realized_pnl_and_rate_limit():
    e = make(max_manual_orders_per_hour=3)
    e.desk.set_mode("manual")
    e.feed.advance = lambda: None
    set_price(e, "A", 100.0)
    e.desk.order("A", "buy", qty=10, reason="in")
    set_price(e, "A", 110.0)
    r = e.desk.close("A", 100, "out")
    assert r["realized_pnl"] == pytest.approx(100.0, rel=0.01)
    e.desk.order("B", "buy", qty=1, reason="2")
    assert e.desk.order("B", "buy", qty=1, reason="3")["status"] == "rejected"   # 3/hour used (close is exempt)
    assert e.desk.close("B", 100, "reducing is never rate-limited")["status"] == "filled"


def test_manual_mode_stops_strategy_and_release_returns_control():
    e = make()
    e.desk.set_mode("manual")
    n = len(e.trades)
    for _ in range(40):
        e.tick()
    assert len(e.trades) == n
    e.desk.set_mode("auto")
    e.desk.order("A", "buy", notional=1000, reason="t")
    assert e.desk.release("A")["status"] == "ok" and "A" not in e.desk.locked()


def test_desk_state_survives_restart(tmp_path):
    e = make()
    e.state_path = tmp_path / "s.json"
    e.desk.order("A", "buy", notional=2000, stop_loss=1.0, reason="persist")
    e.save_state()
    f = Engine(Config(auto_git_commit=False, symbols=["A", "B", "C"]), EventBus())
    f.state_path = tmp_path / "s.json"
    f.load_state()
    assert f.desk.manual["A"]["stop"] == 1.0 and f.desk.journal[-1]["reason"] == "persist"


def test_indicators_basics():
    up = [{"t": i, "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i, "v": 1000} for i in range(250)]
    assert rsi([b["c"] for b in up]) == 100.0
    ind = indicators(up)
    assert ind["trend"] == "uptrend" and ind["sma"]["20"] == pytest.approx(sum(range(330, 350)) / 20)
    big = resample(up, 12)
    assert len(big) == 250 // 12 and big[-1]["c"] == up[-1]["c"] and big[-1]["v"] == 12000


def test_overview_and_analyze_run():
    e = make()
    ov = e.market_overview()
    assert {r["symbol"] for r in ov["symbols"]} == {"A", "B", "C"}
    a = e.analyze("A", "1Hour")
    assert a["timeframe"] == "1Hour" and "rsi_14" in a
