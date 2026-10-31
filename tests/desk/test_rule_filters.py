import json

import pytest

from stockmoney.desk import run, store
from stockmoney.desk.learn import filters
from stockmoney.desk.execution.types import Side
from test_run_safety import WED, desk, plan, market_orders, sent  # noqa: F401


def rule(action="skip", when=None, sleeve="stocks"):
    return {"name": "test_filter", "sleeve": sleeve, "strategies": [],
            "when": when or {"field": "sector", "op": "==", "value": "XLK"}, "action": action}


def activate(con, value, status="active"):
    con.execute("INSERT INTO rule_filters (json,status) VALUES (?,?)", (json.dumps(value), status))


@pytest.mark.parametrize("op,value,observation", [
    ("==", 5, 5), ("!=", 5, 6), ("<", 5, 4), ("<=", 5, 5),
    (">", 5, 6), (">=", 5, 5), ("in", [4, 5], 5), ("not_in", [4, 5], 6)])
def test_each_operator(op, value, observation):
    f = rule(when={"field": "days_to_earnings", "op": op, "value": value})
    assert filters.evaluate(f, {"days_to_earnings": observation}) == (True, 1.0)


def test_combinators_and_missing_never_fire():
    leaf = {"field": "wind", "op": "==", "value": "headwind"}
    f = rule(when={"all": [{"not": leaf}, {"field": "sector", "op": "==", "value": "XLK"}]})
    assert filters.evaluate(f, {"wind": "tailwind", "sector": "XLK"}) == (True, 1.0)
    assert filters.evaluate(f, {"sector": "XLK"}) == (False, 1.0)
    f["when"] = {"any": [leaf, {"field": "vix", "op": ">", "value": 20}]}
    assert filters.evaluate(f, {"wind": "headwind"}) == (False, 1.0)
    assert filters.evaluate(f, {"wind": "headwind", "vix": 10}) == (True, 1.0)
    assert filters.evaluate(f, {"wind": "headwind", "vix": float("nan")}) == (False, 1.0)


@pytest.mark.parametrize("when", [
    {"field": "unknown", "op": "==", "value": 1},
    {"field": "vix", "op": "eval", "value": 1},
    {"field": "vix", "op": "==", "value": True},
    {"field": "wind", "op": ">", "value": "tailwind"},
    {"field": "vix", "op": "in", "value": 3},
    {"all": []},
    {"all": [{"field": "vix", "op": ">", "value": 1}] * 7},
    {"not": {"not": {"not": {"field": "vix", "op": ">", "value": 1}}}},
])
def test_bad_conditions_rejected(when):
    with pytest.raises(ValueError):
        filters.validate(rule(when=when))


@pytest.mark.parametrize("action", ["buy", "shrink:1", "shrink:0.24", "shrink:0.76", "shrink:nan", "shrink:-1"])
def test_no_enlarging_or_other_actions(action):
    with pytest.raises(ValueError):
        filters.validate(rule(action))


def test_active_paper_only_and_size_never_increases(con):
    for action in ("shrink:0.25", "shrink:0.5", "shrink:0.75"):
        activate(con, rule(action))
    activate(con, rule(), "reverted")
    result = filters.active(con, "stocks", "breakout", {"sector": "XLK"}, paper=True)
    assert not result.skip and result.size_factor == 0.25 * 0.5 * 0.75
    assert filters.active(con, "stocks", "breakout", {"sector": "XLK"}, paper=False) == filters.Decision()
    assert filters.active(con, "options", "orb15", {"sector": "XLK"}, paper=True) == filters.Decision()


@pytest.mark.parametrize("action,expected", [("skip", 0), ("shrink:0.5", 25)])
def test_stock_entry_filters_before_risk(desk, action, expected, monkeypatch):
    with store.connect(desk.path) as con:
        activate(con, rule(action))
        cid = plan(con, "AAPL", qty=50)
        seen = []
        original = run.RiskEngine.check

        def check(self, intent, *args, **kwargs):
            if intent.side == Side.BUY:
                seen.append(intent.qty)
            return original(self, intent, *args, **kwargs)

        monkeypatch.setattr(run.RiskEngine, "check", check)
        run.premarket(con, desk.broker, WED, desk.limits)
        buys = market_orders(desk.broker, "AAPL", Side.BUY)
        assert sum(o.qty for o in buys) == expected
        assert seen == ([25] if expected else [])
        if not expected:
            assert "test_filter" in con.execute("SELECT note FROM planned_orders WHERE client_order_id=?", (cid,)).fetchone()[0]


def test_filter_cannot_bypass_risk(desk):
    with store.connect(desk.path) as con:
        activate(con, rule("shrink:0.5"))
        plan(con, "AAPL")
        store.set_setting(con, "halt_new_entries", "1")
        run.premarket(con, desk.broker, WED, desk.limits)
        assert not market_orders(desk.broker, "AAPL", Side.BUY)


def test_nonpaper_stock_entry_does_not_apply_filter(desk):
    desk.broker.is_paper = False
    with store.connect(desk.path) as con:
        activate(con, rule())
        plan(con, "AAPL")
        run.premarket(con, desk.broker, WED, desk.limits)
        assert sum(o.qty for o in market_orders(desk.broker, "AAPL", Side.BUY)) == 50
