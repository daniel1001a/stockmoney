"""Console actions and the Streamlit app (review finding M4). Offline: the app
runs in streamlit's AppTest against a temp working directory."""
import json
import os
from pathlib import Path

import pytest

from stockmoney.desk import console, run, store
from stockmoney.desk.execution.ledger import Ledger

APP = str(Path(__file__).resolve().parents[2] / "app" / "desk_app.py")


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        run.ensure_schema(c)
        yield c


def changes(con):
    return [dict(r) for r in con.execute("SELECT strategy, param, old, new, reason FROM param_changes ORDER BY id")]


# ------------------------------------------------------------------ actions

def test_parameter_change_is_validated_logged_and_stored_as_int(con):
    done = console.apply_strategy_changes(con, "rotation", {"top_n": 4.0, "keep_rank": 12.0}, True, "fewer names")
    assert len(done) == 2
    assert store.strategy_params(con)["rotation"] == {"top_n": 4, "keep_rank": 12}
    assert json.loads(store.get_setting(con, "strategy_params"))["rotation"]["top_n"] == 4
    assert [c["param"] for c in changes(con)] == ["top_n", "keep_rank"]
    assert all(c["reason"] == "fewer names" for c in changes(con))


@pytest.mark.parametrize(
    "edit",
    [{"top_n": 4.5}, {"top_n": 0}, {"top_n": 12}, {"stop_atr": 0.4}, {"bogus": 1.0}],
)
def test_invalid_edit_writes_nothing(con, edit):
    with pytest.raises(ValueError):
        console.apply_strategy_changes(con, "rotation", {"stop_atr": 5.0, **edit}, True, "reason")
    assert changes(con) == [] and store.strategy_params(con) == {}


def test_reason_required_only_when_something_changes(con):
    assert console.apply_strategy_changes(con, "breakout", {}, True, "") == []  # nothing differs
    with pytest.raises(ValueError, match="理由"):
        console.apply_strategy_changes(con, "breakout", {"stop_atr": 3.0}, True, "  ")
    assert changes(con) == []


def test_turning_a_strategy_off_and_on_is_recorded_with_reason(con):
    console.apply_strategy_changes(con, "pullback", {}, False, "chop regime, pausing")
    assert json.loads(store.get_setting(con, "disabled_strategies")) == ["pullback"]
    console.apply_strategy_changes(con, "pullback", {}, True, "regime back")
    assert json.loads(store.get_setting(con, "disabled_strategies")) == []
    rows = changes(con)
    assert [(r["strategy"], r["param"], r["old"], r["new"], r["reason"]) for r in rows] == [
        ("pullback", "enabled", "true", "false", "chop regime, pausing"),
        ("pullback", "enabled", "false", "true", "regime back"),
    ]
    with pytest.raises(ValueError, match="理由"):
        console.apply_strategy_changes(con, "pullback", {}, False, "")


def test_resuming_new_entries_requires_a_reason_and_is_logged(con):
    console.set_halt(con, True, "")  # pausing never needs a reason
    assert store.get_setting(con, "halt_new_entries") == "1"
    with pytest.raises(ValueError, match="理由"):
        console.set_halt(con, False, "   ")
    assert store.get_setting(con, "halt_new_entries") == "1"
    console.set_halt(con, False, "reconciled with broker, checked fills")
    assert store.get_setting(con, "halt_new_entries") == "0"
    rows = changes(con)
    assert [(r["strategy"], r["param"], r["new"]) for r in rows] == [
        ("desk", "halt_new_entries", "true"), ("desk", "halt_new_entries", "false")]
    assert rows[-1]["reason"] == "reconciled with broker, checked fills"


# ------------------------------------------------------------------ app, no price cache

@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def seed(path):
    with store.connect(path) as c:
        run.ensure_schema(c)
        Ledger(c)
        c.execute("INSERT INTO equity_history VALUES ('2026-10-09', 50500, 50500, 'normal', 1, '2026-10-09T21:00:00')")
        c.execute("INSERT INTO sleeve_positions (symbol, strategy, qty, avg_price, opened_at, stop) "
                  "VALUES ('AAPL','breakout',10,100,'2026-10-08',95)")
        c.execute("INSERT INTO position_meta (symbol, highest_close, initial_stop, entry_date) "
                  "VALUES ('AAPL',101,95,'2026-10-08')")


def open_app(page=None):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(APP, default_timeout=60).run()
    if page:
        at.sidebar.radio[0].set_value(page).run()
    return at


def test_overview_and_positions_survive_a_missing_price_cache(cwd):
    seed(cwd / "data" / "desk" / "desk.sqlite")
    assert not os.path.exists("data/desk/ohlcv.parquet")
    at = open_app()
    assert not at.exception, at.exception
    assert any("價格快取" in w.value for w in at.warning)
    at = open_app("持倉與委託")
    assert not at.exception, at.exception
    assert any("價格快取" in w.value for w in at.warning)


def test_lab_blocks_invalid_parameters_and_logs_valid_ones(cwd):
    seed(cwd / "data" / "desk" / "desk.sqlite")
    at = open_app("策略實驗室")
    assert not at.exception, at.exception
    # pick rotation (third strategy), set top_n to a non-integer, give a reason
    at.selectbox[0].set_value("rotation").run()
    top_n = next(n for n in at.number_input if n.label == "top_n")
    top_n.set_value(4.5)
    next(t for t in at.text_input if t.label.startswith("套用理由")).set_value("try fewer names")
    next(b for b in at.button if b.label == "套用").click().run()
    assert any("whole number" in e.value for e in at.error), [e.value for e in at.error]
    with store.connect(cwd / "data" / "desk" / "desk.sqlite") as c:
        assert store.strategy_params(c) == {}
    # now a valid edit
    next(n for n in at.number_input if n.label == "top_n").set_value(4.0)
    next(b for b in at.button if b.label == "套用").click().run()
    assert not at.exception and not at.error
    with store.connect(cwd / "data" / "desk" / "desk.sqlite") as c:
        assert store.strategy_params(c)["rotation"]["top_n"] == 4
        assert changes(c)[0]["reason"] == "try fewer names"


def test_overview_resume_button_demands_a_reason(cwd):
    path = cwd / "data" / "desk" / "desk.sqlite"
    seed(path)
    with store.connect(path) as c:
        store.set_setting(c, "halt_new_entries", "1")
    at = open_app()
    next(b for b in at.button if b.label == "解除暫停新進場").click().run()
    assert any("理由" in e.value for e in at.error)
    with store.connect(path) as c:
        assert store.get_setting(c, "halt_new_entries") == "1"
    next(t for t in at.text_input if t.label.startswith("理由")).set_value("checked the broker").run()
    next(b for b in at.button if b.label == "解除暫停新進場").click().run()
    with store.connect(path) as c:
        assert store.get_setting(c, "halt_new_entries") == "0"
        assert changes(c)[-1]["reason"] == "checked the broker"
