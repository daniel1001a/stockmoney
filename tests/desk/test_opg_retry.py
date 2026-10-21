"""An opening (OPG) entry that ended short gets one DAY market order for the
missing shares at postopen, through the same risk path, before 10:30 ET
only, never twice. Offline only: FakeBroker and a temp SQLite file."""
from __future__ import annotations

import pytest

from stockmoney.desk import run, store
from stockmoney.desk.execution.types import TIF, OrderStatus, Side

from test_order_path_round4 import desk, plan_status, sent  # noqa: F401  (fixtures)
from test_run_safety import WED, market_orders, plan, stops

POSTOPEN = WED.replace(hour=9, minute=50)


@pytest.fixture(autouse=True)
def job_clock(monkeypatch):
    # submitted_at is wall-clock UTC; pin it to the test session (2026-09-09 08:45 ET)
    monkeypatch.setattr(store, "now", lambda: "2026-09-09T12:45:00+00:00")
    monkeypatch.setattr(run, "_sleep", lambda s: None)


def opening_short(con, b, limits, ratio, sym="AAPL", qty=50):
    """Premarket sends the OPG buy; it fills ``ratio`` of the qty, then the
    broker ends it (expired) with the rest unfilled."""
    cid = plan(con, sym, qty=qty)
    b.partial_ratio = ratio
    run.premarket(con, b, WED, limits)
    o = b.get_order(cid)
    assert o.tif == TIF.OPG and o.qty == qty
    b.expire_day_orders()
    b.partial_ratio = None
    return cid


def retries(b, sym="AAPL"):
    return [o for o in market_orders(b, sym, Side.BUY) if o.client_order_id.endswith("-rt")]


def test_partial_fill_retries_only_the_remainder_once(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        cid = opening_short(con, b, desk.limits, 0.4)  # 20 of 50 filled
        run.postopen(con, b, POSTOPEN, desk.limits)
        rt = retries(b)
        assert len(rt) == 1
        o = rt[0]
        assert o.client_order_id == f"{cid}-rt"
        assert (o.qty, o.tif, o.status) == (30, TIF.DAY, OrderStatus.FILLED)
        assert b.positions["AAPL"].qty == 50
        assert run.Ledger(con).position("AAPL").qty == 50
        assert [s.qty for s in stops(b, "AAPL")] == [50]  # the stop covers the whole position
        row = plan_status(con, cid)
        assert row["status"] == "submitted" and "20/50" in row["note"] and f"{cid}-rt" in row["note"]


def test_unfilled_opening_order_retries_the_full_qty(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        cid = opening_short(con, b, desk.limits, 0.0)
        run.postopen(con, b, POSTOPEN, desk.limits)
        assert [(o.qty, o.tif) for o in retries(b)] == [(50, TIF.DAY)]
        assert b.positions["AAPL"].qty == 50
        assert plan_status(con, cid)["status"] == "submitted"


def test_second_postopen_and_recover_never_retry_again(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        cid = opening_short(con, b, desk.limits, 0.4)
        run.postopen(con, b, POSTOPEN, desk.limits)
        b.orders[f"{cid}-rt"].status = OrderStatus.EXPIRED  # even if the retry itself had ended short
        run.postopen(con, b, POSTOPEN.replace(minute=55), desk.limits)
        run.recover(con, b, POSTOPEN.replace(minute=58), desk.limits)
        run.postopen(con, b, POSTOPEN.replace(hour=10, minute=5), desk.limits)
        assert len(retries(b)) == 1
        assert len(market_orders(b, "AAPL", Side.BUY)) == 2  # the opening order and one retry


def test_risk_blocked_retry_is_not_sent_and_the_reason_is_kept(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        cid = opening_short(con, b, desk.limits, 0.0)
        b.prices["AAPL"] = 115.0  # 15% above plan: the existing 10% price band refuses it
        run.postopen(con, b, POSTOPEN, desk.limits)
        assert retries(b) == []
        row = plan_status(con, cid)
        assert row["status"] == "blocked" and "風控擋下" in row["note"]
        b.prices["AAPL"] = 100.0
        run.postopen(con, b, POSTOPEN.replace(minute=55), desk.limits)  # one decision per plan
        assert retries(b) == []


def test_halted_desk_blocks_a_partial_retry_and_keeps_the_held_plan(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        cid = opening_short(con, b, desk.limits, 0.4)
        desk.limits.halt_file.write_text("owner halt")
        run.postopen(con, b, POSTOPEN, desk.limits)
        assert retries(b) == []
        assert b.positions["AAPL"].qty == 20  # the filled part is untouched
        row = plan_status(con, cid)
        assert row["status"] == "submitted" and "未補單" in row["note"] and "20/50" in row["note"]
        assert [s.qty for s in stops(b, "AAPL")] == [20]


def test_no_retry_after_the_entry_cutoff(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        cid = opening_short(con, b, desk.limits, 0.0)
        run.postopen(con, b, WED.replace(hour=10, minute=31), desk.limits)
        assert retries(b) == []
        row = plan_status(con, cid)
        assert row["status"] == "expired" and "10:30" in row["note"] and "作廢" in row["note"]


def test_fully_filled_opening_order_is_not_retried(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        cid = plan(con, "AAPL", qty=50)
        run.premarket(con, b, WED, desk.limits)
        run.postopen(con, b, POSTOPEN, desk.limits)
        assert retries(b) == []
        assert plan_status(con, cid)["status"] == "submitted"


def test_no_retry_while_the_market_is_closed(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        opening_short(con, b, desk.limits, 0.0)
        b.market_open = False
        run.postopen(con, b, POSTOPEN, desk.limits)
        assert retries(b) == []


def test_muse_flags_are_read_at_postopen_and_block_adding_to_a_held_symbol(desk, monkeypatch):
    from stockmoney.desk.bridge.policy import BridgeFlags
    b = desk.broker
    seen = []

    def flags(con, ledger, now, mailbox=None):
        seen.append((now, set(ledger.positions())))
        return BridgeFlags(blocked={}, no_add={"AAPL": "Muse critical ALERT: probe"}, notify=[])
    with store.connect(desk.path) as con:
        cid = opening_short(con, b, desk.limits, 0.4)
        monkeypatch.setattr(run, "_bridge_flags", flags)
        run.postopen(con, b, POSTOPEN, desk.limits)
        assert seen == [(POSTOPEN, {"AAPL"})]  # as of the postopen clock, with the partial fill held
        assert retries(b) == []
        row = plan_status(con, cid)
        assert row["status"] == "submitted" and "Muse critical ALERT" in row["note"]


def test_a_broker_read_failure_before_sending_does_not_spend_the_retry(desk, monkeypatch):
    """Grok review 2026-10-21: an unreachable broker used to record the one
    attempt although nothing was sent. Now the plan stays a candidate (after
    one in-run wait and retry) and a manual run before the cutoff sends it."""
    from stockmoney.desk.execution.alpaca import BrokerUnavailable
    b = desk.broker
    real = b.get_order
    down = {"on": True}

    def get_order(c):
        if down["on"] and c.endswith("-rt"):
            raise BrokerUnavailable("timed out")
        return real(c)
    with store.connect(desk.path) as con:
        cid = opening_short(con, b, desk.limits, 0.4)
        monkeypatch.setattr(b, "get_order", get_order)
        out = run.postopen(con, b, POSTOPEN, desk.limits)
        assert retries(b) == [] and "補單暫緩" in out
        assert con.execute("SELECT 1 FROM plan_attempts WHERE key LIKE ?", (f"%{cid}",)).fetchone() is None
        assert plan_status(con, cid)["status"] == "submitted"
        down["on"] = False
        run.postopen(con, b, POSTOPEN.replace(minute=55), desk.limits)
        assert [(o.qty, o.tif) for o in retries(b)] == [(30, TIF.DAY)]


def test_a_failed_send_with_an_unreadable_opening_order_does_not_abort_postopen(desk, monkeypatch):
    """The retry was sent (write-ahead row exists) and failed, then the
    opening order cannot be read either: the attempt is spent, the plan keeps
    its status, and postopen still protects the filled shares."""
    from stockmoney.desk.execution.alpaca import BrokerUnavailable
    b = desk.broker
    real_get, real_submit = b.get_order, b.submit
    state = {"sent": False}

    def submit(intent):
        if intent.client_order_id.endswith("-rt"):
            state["sent"] = True
            raise BrokerUnavailable("submit timed out")
        return real_submit(intent)

    def get_order(c):
        if state["sent"] and not c.endswith("-rt"):
            raise BrokerUnavailable("timed out")
        return real_get(c)
    with store.connect(desk.path) as con:
        cid = opening_short(con, b, desk.limits, 0.4)
        monkeypatch.setattr(b, "submit", submit)
        monkeypatch.setattr(b, "get_order", get_order)
        out = run.postopen(con, b, POSTOPEN, desk.limits)
        assert state["sent"] and "補單失敗" in out
        assert con.execute("SELECT 1 FROM plan_attempts WHERE key LIKE ?", (f"%{cid}",)).fetchone() is not None
        assert plan_status(con, cid)["status"] == "submitted"
        assert [s.qty for s in stops(b, "AAPL")] == [20]


def _rt_read_fails(b, monkeypatch, times):
    from stockmoney.desk.execution.alpaca import BrokerUnavailable
    real = b.get_order
    left = {"n": times}

    def get_order(c):
        if c.endswith("-rt") and left["n"] > 0:
            left["n"] -= 1
            raise BrokerUnavailable("timed out")
        return real(c)
    monkeypatch.setattr(b, "get_order", get_order)


def test_a_transient_read_failure_is_retried_within_the_same_postopen(desk, monkeypatch):
    """Grok re-review 2026-10-21: no scheduled run comes after 09:50 before
    the cutoff, so a pre-send failure waits once and tries again in-run."""
    b = desk.broker
    with store.connect(desk.path) as con:
        opening_short(con, b, desk.limits, 0.4)
        _rt_read_fails(b, monkeypatch, 1)
        out = run.postopen(con, b, POSTOPEN, desk.limits)
        assert [(o.qty, o.tif) for o in retries(b)] == [(30, TIF.DAY)] and "補單暫緩" not in out


@pytest.mark.parametrize("ratio, status", [(0.4, "submitted"), (0.0, "expired")])
def test_an_unretried_remainder_is_voided_after_the_cutoff(desk, monkeypatch, ratio, status):
    """The read keeps failing at 09:50: the notice asks for a manual run, and
    the first run after 10:30 ET (postclose, or the next postopen) closes the
    window instead of leaving the plan open forever. Nothing is sent."""
    b = desk.broker
    with store.connect(desk.path) as con:
        cid = opening_short(con, b, desk.limits, ratio)
        _rt_read_fails(b, monkeypatch, 2)
        out = run.postopen(con, b, POSTOPEN, desk.limits)
        assert "補單暫緩" in out and "手動執行 postopen" in out
        assert run._expire_unretried_opg(con, b, POSTOPEN.replace(minute=59)) == []  # still before 10:30
        lines = run._expire_unretried_opg(con, b, WED.replace(hour=16, minute=45))
        assert len(lines) == 1 and "作廢" in lines[0]
        row = plan_status(con, cid)
        assert row["status"] == status and "超過補單時間窗" in row["note"]
        assert con.execute("SELECT 1 FROM plan_attempts WHERE key LIKE ?", (f"%{cid}",)).fetchone() is not None
        assert run._expire_unretried_opg(con, b, WED.replace(hour=16, minute=50)) == []  # once
        assert retries(b) == []


def test_the_next_days_postopen_closes_a_window_left_open(desk, monkeypatch):
    from datetime import timedelta
    b = desk.broker
    with store.connect(desk.path) as con:
        cid = opening_short(con, b, desk.limits, 0.0)
        _rt_read_fails(b, monkeypatch, 2)
        run.postopen(con, b, POSTOPEN, desk.limits)
        out = run.postopen(con, b, POSTOPEN + timedelta(days=1), desk.limits)
        assert "超過補單時間窗" in out and plan_status(con, cid)["status"] == "expired" and retries(b) == []


def test_an_order_still_unreadable_is_left_for_the_next_run(desk, monkeypatch):
    from stockmoney.desk.execution.alpaca import BrokerUnavailable
    b = desk.broker
    with store.connect(desk.path) as con:
        cid = opening_short(con, b, desk.limits, 0.0)
        monkeypatch.setattr(b, "get_order", lambda c: (_ for _ in ()).throw(BrokerUnavailable("down")))
        assert run._expire_unretried_opg(con, b, WED.replace(hour=16, minute=45)) == []
        assert plan_status(con, cid)["status"] == "submitted"
        assert con.execute("SELECT 1 FROM plan_attempts WHERE key LIKE ?", (f"%{cid}",)).fetchone() is None
