import asyncio
import json
import random

import pytest

from bot import evolve as ev
from bot.backtest import walk_forward
from bot.brokers import PaperBroker
from bot.config import Config, RiskLimits
from bot.data import SyntheticFeed
from bot.events import EventBus
from bot.risk import RiskManager
from bot.sandbox import StrategyRejected, load_strategy, validate_source

GOOD = """
import math
PARAMS = {"n": 10}
def generate_signals(history, positions, params):
    return {s: 0.1 for s in history}
"""


@pytest.mark.parametrize("src", [
    "import os\ndef generate_signals(h,p,q): return {}",
    "def generate_signals(h,p,q): return open('/etc/passwd')",
    "def generate_signals(h,p,q): return ().__class__.__bases__",
    "def generate_signals(h,p,q): return getattr(h, 'x')",
    "from subprocess import run\ndef generate_signals(h,p,q): return {}",
    "def generate_signals(h,p,q): return __import__('os')",
    "PARAMS = {}",
])
def test_sandbox_rejects(src):
    with pytest.raises(StrategyRejected):
        validate_source(src)


def test_sandbox_loads_good_strategy():
    ns = load_strategy(GOOD)
    assert ns["generate_signals"]({"A": []}, {}, ns["PARAMS"]) == {"A": 0.1}


def test_seed_strategy_is_valid_and_trades():
    ns = load_strategy(ev.ACTIVE.read_text())
    feed = SyntheticFeed(["A", "B", "C"], warmup=1500)
    res = walk_forward(ns, feed.bars)
    assert "error" not in res["holdout"]
    assert res["holdout"]["bars"] > 100


def test_risk_clamps():
    rm = RiskManager(RiskLimits(max_position_pct=0.2, max_gross_exposure=0.5), ["A", "B", "C"])
    w, notes = rm.sanitize_weights({"A": 0.9, "B": -0.3, "C": float("nan"), "EVIL": 1.0, "D": "x"})
    assert w["A"] == pytest.approx(0.2) and w["B"] == 0.0 and "C" not in w and "EVIL" not in w
    w, _ = rm.sanitize_weights({"A": 0.2, "B": 0.2, "C": 0.2})
    assert sum(w.values()) == pytest.approx(0.5)


def test_kill_switch():
    rm = RiskManager(RiskLimits(max_daily_loss_pct=0.03), ["A"])
    rm.start_day(100_000)
    assert not rm.check_kill_switch(98_000)
    assert rm.check_kill_switch(96_900)


def test_paper_broker_accounting():
    px = {"A": 100.0}
    b = PaperBroker(lambda s: px[s], starting_cash=10_000, slippage_bps=0)
    b.submit_order("A", 10, "buy")
    px["A"] = 110.0
    assert b.equity() == pytest.approx(10_100)
    b.submit_order("A", 10, "sell")
    assert b.positions() == {} and b.cash() == pytest.approx(10_100)


def test_mutation_keeps_strategy_valid():
    src = ev.ACTIVE.read_text()
    for seed in range(10):
        why, new = ev.mutate_params(src, random.Random(seed))
        ns = load_strategy(new)
        assert ns["PARAMS"] != load_strategy(src)["PARAMS"], why


def test_extract_code():
    assert "def f" in ev.extract_code("RATIONALE: x\n```python\ndef f(): pass\n```")


def test_evolution_cycle_offline(tmp_path, monkeypatch):
    """Full loop without an API key: mutation candidates, isolated backtests, journal + promotion."""
    sdir = tmp_path / "strategies"
    (sdir / "versions").mkdir(parents=True)
    (sdir / "active.py").write_text(ev.ACTIVE.read_text())
    monkeypatch.setattr(ev, "ACTIVE", sdir / "active.py")
    monkeypatch.setattr(ev, "VERSIONS", sdir / "versions")
    monkeypatch.setattr(ev, "JOURNAL", sdir / "versions" / "journal.json")
    monkeypatch.setattr(ev, "CANDIDATES", sdir / "candidates")
    cfg = Config(auto_git_commit=False, evolve_min_improvement=-100)  # force promotion of best candidate
    e = ev.Evolver(cfg, EventBus())
    assert e.current_version == 1
    bars = SyntheticFeed(["A", "B"], seed=3, warmup=1200).bars
    res = asyncio.run(e.evolve(bars, {}, []))
    assert res["status"] == "promoted"
    assert e.current_version == res["version"] > 1
    journal = json.loads((sdir / "versions" / "journal.json").read_text())
    assert sum(j["is_active"] for j in journal) == 1
    assert (sdir / "active.py").read_text() == (sdir / "versions" / f"v{res['version']:04d}.py").read_text()
    e.rollback(1)
    assert e.current_version == 1


def test_isolated_backtest_times_out_or_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "CANDIDATES", tmp_path)
    monkeypatch.setattr(ev, "BACKTEST_TIMEOUT", 3)
    e = ev.Evolver.__new__(ev.Evolver)
    loop_src = "def generate_signals(h,p,q):\n    while True:\n        pass\n"
    bars = SyntheticFeed(["A"], warmup=400).bars
    res = asyncio.run(e.evaluate(loop_src, bars, "loop"))
    assert "error" in res
