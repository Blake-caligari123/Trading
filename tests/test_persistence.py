import pytest

from bot.config import Config
from bot.engine import Engine
from bot.events import EventBus


def _engine(tmp_path):
    e = Engine(Config(auto_git_commit=False, symbols=["A", "B", "C"]), EventBus())
    e.state_path = tmp_path / "state.json"
    e.load_active_strategy()
    return e


def test_restart_resumes_exactly_where_it_left_off(tmp_path):
    """A bot that saves + restarts must end up identical to one that never stopped."""
    ref = _engine(tmp_path)
    for _ in range(150):
        ref.tick()

    a = _engine(tmp_path)
    for _ in range(100):
        a.tick()
    a.save_state()

    b = _engine(tmp_path)          # "Codespace restarted"
    b.load_state()
    assert b.ticks == 100 and b.broker.equity() == pytest.approx(a.broker.equity())
    for _ in range(50):
        b.tick()
    assert b.broker.equity() == pytest.approx(ref.broker.equity())
    assert {s: p.qty for s, p in b.broker.positions().items()} == pytest.approx(
        {s: p.qty for s, p in ref.broker.positions().items()})
    assert len(b.trades) == len(ref.trades)


def test_state_for_other_symbols_is_ignored(tmp_path):
    a = _engine(tmp_path)
    a.tick()
    a.save_state()
    b = Engine(Config(auto_git_commit=False, symbols=["X", "Y"]), EventBus())
    b.state_path = tmp_path / "state.json"
    b.load_state()
    assert b.ticks == 0
