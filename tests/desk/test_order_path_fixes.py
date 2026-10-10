"""Regression tests for the automated order path: failed exits, stops for
shares already gone, exit expiry, unprotected positions after an unconfirmed
cancel, write-ahead submission, postclose reruns and calendar fallbacks.
Offline only: FakeBroker (including async cancels) and a temp SQLite file."""
from __future__ import annotations

import math
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from stockmoney.desk import run, store
from stockmoney.desk.execution.alpaca import AlpacaBroker
from stockmoney.desk.execution.broker import FakeBroker
from stockmoney.desk.execution.ledger import Ledger, norm
from stockmoney.desk.execution.reconcile import reconcile
from stockmoney.desk.execution.risk import Limits, RiskEngine
from stockmoney.desk.execution.types import (
    TIF, AssetClass, OrderIntent, OrderStatus, OrderType, Side,
)

from test_desk import synthetic_bars
from test_run_safety import PLAN_DAY, WED, _at, _bars_and_broker, hold, market_orders, plan, sig, stops

OPEN_STATES = (OrderStatus.NEW, OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING_CANCEL)


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    for k in ("DESK_MAIL_ADDRESS", "DESK_MAIL_APP_PASSWORD", "MUSE_ADDRESS", "MUSE_SHARED_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    out = []
    monkeypatch.setattr(run.notify, "send", lambda title, msg, priority=3, tags=None: out.append((title, msg, priority)))
    return out


@pytest.fixture
def desk(tmp_path):
    return SimpleNamespace(
        path=tmp_path / "desk.sqlite",
        broker=FakeBroker(cash=100_000, prices={"AAPL": 100.0, "MSFT": 100.0, "NVDA": 100.0, "AMD": 100.0}),
        limits=Limits(halt_file=tmp_path / "HALT"),
    )


def plan_status(con, cid):
    return con.execute("SELECT status, note FROM planned_orders WHERE client_order_id=?", (cid,)).fetchone()


def protect(con, desk, sym):
    risk = RiskEngine(desk.limits, con)
    run.ensure_stops(con, desk.broker, risk.ledger, risk, desk.broker.account(), PLAN_DAY)
    assert len(stops(desk.broker, sym)) == 1
    return risk


def record_submits(broker):
    calls = []
    real = broker.submit

    def submit(intent):
        calls.append(intent)
        return real(intent)
    broker.submit = submit
    return calls


# ------------------------------------------------ 1. failed exit keeps a stop and is retried
@pytest.mark.parametrize("fail", ["submit", "account"])
def test_failed_exit_restores_stop_and_retries_same_morning(desk, fail):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", qty=50, stop=90.0)
        protect(con, desk, "AAPL")
        cid = plan(con, "AAPL", side=Side.SELL, qty=50)
        real_submit, real_account = b.submit, b.account
        n_account = [0]

        def submit(intent):
            if intent.order_type == OrderType.MARKET and intent.side == Side.SELL:
                raise RuntimeError("broker down")
            return real_submit(intent)

        def account():
            n_account[0] += 1
            if n_account[0] == 2:  # the live read that sizes the exit, right after the stop cancel
                raise RuntimeError("broker down")
            return real_account()
        if fail == "submit":
            b.submit = submit
        else:
            b.account = account
        run.premarket(con, b, WED, desk.limits)
        assert plan_status(con, cid)["status"] == "planned"
        live = stops(b, "AAPL")
        assert len(live) == 1 and live[0].stop_price == 90.0 and live[0].qty == 50
        assert market_orders(b, "AAPL", Side.SELL) == []
        b.submit, b.account = real_submit, real_account
        run.premarket(con, b, WED.replace(minute=50), desk.limits)
        assert plan_status(con, cid)["status"] == "submitted"
        assert len(market_orders(b, "AAPL", Side.SELL)) == 1
        assert stops(b, "AAPL") == []


def test_exit_gives_up_after_three_failures_with_critical_notice(desk, sent):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", qty=50, stop=90.0)
        protect(con, desk, "AAPL")
        cid = plan(con, "AAPL", side=Side.SELL, qty=50)
        real = b.submit

        def submit(intent):
            if intent.order_type == OrderType.MARKET:
                raise RuntimeError("broker down")
            return real(intent)
        b.submit = submit
        for minute in (45, 47, 49):
            run.premarket(con, b, WED.replace(minute=minute), desk.limits)
            assert len(stops(b, "AAPL")) == 1
        assert plan_status(con, cid)["status"] == "error"
        assert any("AAPL" in t + m and p == run.notify.CRITICAL for t, m, p in sent)


# ------------------------------------------------ 2. stops for shares already gone
def test_stop_that_fired_after_sync_is_booked_not_replaced(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", qty=50, stop=90.0)
        risk = protect(con, desk, "AAPL")
        risk.ledger.set_stop("AAPL", 92.0)  # trailed up: the stop would be replaced
        snap = b.account()
        real_open = b.open_orders
        fired = []

        def open_orders():
            if not fired:
                fired.append(1)
                b.tick("AAPL", 89.0)  # the old stop fills between sync_fills and this read
            return real_open()
        b.open_orders = open_orders
        calls = record_submits(b)
        run.ensure_stops(con, b, risk.ledger, risk, snap, PLAN_DAY)
        assert [c for c in calls if c.order_type == OrderType.STOP] == []
        assert Ledger(con).position("AAPL") is None
        assert reconcile(Ledger(con), b.account()) == []


def test_stop_is_sized_from_a_fresh_broker_read(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", qty=50, stop=90.0)
        risk = RiskEngine(desk.limits, con)
        snap = b.account()  # job-start snapshot: 50 shares
        b.positions["AAPL"].qty = 20  # 30 shares left the account after the snapshot
        run.ensure_stops(con, b, risk.ledger, risk, snap, PLAN_DAY)
        assert [o.qty for o in stops(b, "AAPL")] == [20]


@pytest.mark.parametrize("job", ["postopen", "postclose"])
def test_orphan_sell_stop_is_swept(desk, monkeypatch, job):
    b = FakeBroker(cash=100_000, prices=dict(desk.broker.prices), async_cancel=True)
    monkeypatch.setattr(run, "_sleep", lambda s: b.settle())
    if job == "postclose":
        bars, t, _ = _bars_and_broker()
        b.prices.update({s: float(v) for s, v in bars.close.loc[t].items()})
    b.submit(OrderIntent("seed", "AAPL", AssetClass.EQUITY, Side.BUY, qty=50))
    b.submit(OrderIntent("orphan", "AAPL", AssetClass.EQUITY, Side.SELL, qty=50, order_type=OrderType.STOP,
                         tif=TIF.GTC, stop_price=50.0))
    del b.positions["AAPL"]  # the shares are gone, the stop is still working
    with store.connect(desk.path) as con:
        if job == "postopen":
            run.postopen(con, b, datetime(2026, 9, 9, 9, 50, tzinfo=run.ET), desk.limits)
        else:
            run.postclose(con, b, _at(t, 17, 0), bars=bars, limits=desk.limits)
    assert b.get_order("orphan").status == OrderStatus.CANCELED


def test_sweep_leaves_a_stop_for_shares_the_broker_still_holds(desk):
    b = desk.broker
    b.submit(OrderIntent("manual", "AAPL", AssetClass.EQUITY, Side.BUY, qty=10))
    b.submit(OrderIntent("manual-stop", "AAPL", AssetClass.EQUITY, Side.SELL, qty=10, order_type=OrderType.STOP,
                         tif=TIF.GTC, stop_price=50.0))
    with store.connect(desk.path) as con:
        run.postopen(con, b, datetime(2026, 9, 9, 9, 50, tzinfo=run.ET), desk.limits)
    assert b.get_order("manual-stop").status == OrderStatus.NEW


# ------------------------------------------------ 3. exits do not expire
def test_exit_from_an_older_plan_still_executes(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", qty=50, stop=90.0)
        protect(con, desk, "AAPL")
        cid = plan(con, "AAPL", side=Side.SELL, qty=50, day="2026-09-03")  # premarket on 09-04 and 09-08 missed
        store.set_setting(con, run.PLAN_THROUGH_KEY, PLAN_DAY)
        run.premarket(con, b, WED, desk.limits)
        assert plan_status(con, cid)["status"] == "submitted"
        assert [o.qty for o in market_orders(b, "AAPL", Side.SELL)] == [50]


def test_exit_for_a_position_already_gone_is_cancelled_with_a_note(desk):
    with store.connect(desk.path) as con:
        cid = plan(con, "NVDA", side=Side.SELL, qty=50, day="2026-09-03")
        store.set_setting(con, run.PLAN_THROUGH_KEY, PLAN_DAY)
        run.premarket(con, desk.broker, WED, desk.limits)
        row = plan_status(con, cid)
        assert row["status"] == "cancelled" and row["note"]


def test_postclose_does_not_plan_a_second_exit_while_one_is_pending(desk, monkeypatch):
    bars, t, b = _bars_and_broker()
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", stop=round(b.prices["AAPL"] * 0.9, 2))
        plan(con, "AAPL", side=Side.SELL, qty=50, day="2026-09-03")
        monkeypatch.setattr(run.REGISTRY["breakout"], "exit_reason", lambda self, ctx, sym, pos: "test exit")
        run.postclose(con, b, _at(t, 17, 0), bars=bars, limits=desk.limits)
        n = con.execute("SELECT COUNT(*) FROM planned_orders WHERE symbol='AAPL' AND side='sell' "
                        "AND status='planned'").fetchone()[0]
        assert n == 1


# ------------------------------------------------ 4. every premarket run protects held symbols
def test_premarket_places_missing_stops_before_buying(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", qty=50, stop=90.0)  # the postclose stop replacement never finished
        plan(con, "MSFT", "pullback")
        calls = record_submits(b)
        run.premarket(con, b, WED, desk.limits)  # 08:45: not a late run
        live = stops(b, "AAPL")
        assert len(live) == 1 and live[0].stop_price == 90.0
        kinds = [(c.symbol, c.order_type) for c in calls]
        assert kinds.index(("AAPL", OrderType.STOP)) < kinds.index(("MSFT", OrderType.MARKET))


def test_premarket_does_not_restop_a_symbol_it_just_sold(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", qty=50, stop=90.0)
        protect(con, desk, "AAPL")
        plan(con, "AAPL", side=Side.SELL, qty=50)
        run.premarket(con, b, WED, desk.limits)
        assert len(market_orders(b, "AAPL", Side.SELL)) == 1
        assert stops(b, "AAPL") == []


# ------------------------------------------------ 5. write-ahead submission
class Crash(BaseException):
    """The process dies (power loss, kill -9) right after the broker accepted."""


def test_crash_after_submit_leaves_a_record_and_rerun_books_it(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        cid = plan(con, "AAPL", qty=50)
        real = b.submit

        def submit(intent):
            o = real(intent)
            if intent.order_type == OrderType.MARKET:
                raise Crash()
            return o
        b.submit = submit
        with pytest.raises(Crash):
            run.premarket(con, b, WED, desk.limits)
    with store.connect(desk.path) as con:
        row = con.execute("SELECT status FROM submitted WHERE client_order_id=?", (cid,)).fetchone()
        assert row is not None and row["status"] == "pending_submit"
        b.submit = real
        run.premarket(con, b, WED.replace(minute=50), desk.limits)
        assert Ledger(con).position("AAPL").qty == 50
        assert plan_status(con, cid)["status"] == "submitted"
        assert len(market_orders(b, "AAPL", Side.BUY)) == 1
        assert reconcile(Ledger(con), b.account()) == []


def test_pending_submit_never_sent_is_marked_not_sent_next_day(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        cid = plan(con, "AAPL", qty=50)

        def submit(intent):
            raise RuntimeError("connection reset")
        b.submit = submit
        run.premarket(con, b, WED, desk.limits)
        row = con.execute("SELECT status, recorded FROM submitted WHERE client_order_id=?", (cid,)).fetchone()
        assert row is not None and row["status"] == "pending_submit" and row["recorded"] == 0
        sent_day = run._today(datetime.fromisoformat(
            con.execute("SELECT submitted_at FROM submitted WHERE client_order_id=?", (cid,)).fetchone()[0]))
        run.sync_fills(con, b, Ledger(con), sent_day)  # same day: the broker may still show it
        assert con.execute("SELECT status FROM submitted WHERE client_order_id=?", (cid,)).fetchone()[0] == "pending_submit"
        run.sync_fills(con, b, Ledger(con), (pd.Timestamp(sent_day) + pd.Timedelta(days=1)).date().isoformat())
        row = con.execute("SELECT status, recorded FROM submitted WHERE client_order_id=?", (cid,)).fetchone()
        assert row["status"] == "not_sent" and row["recorded"] == 1


# ------------------------------------------------ 6. postclose rerun does not over-plan
def test_rerun_of_plan_entries_respects_caps(desk, monkeypatch):
    monkeypatch.setattr(run, "MAX_POSITIONS", 2)
    with store.connect(desk.path) as con:
        run.ensure_schema(con)
        risk = RiskEngine(desk.limits, con)
        ctx = SimpleNamespace(market=SimpleNamespace(exposure_cap=1.0))
        snap = desk.broker.account()
        run._plan_entries(con, PLAN_DAY, risk.ledger, risk, snap, ctx, [sig("NVDA", "breakout", 2.0)], set())
        run._plan_entries(con, PLAN_DAY, risk.ledger, risk, snap, ctx,
                          [sig("AAPL", "breakout", 3.0), sig("MSFT", "breakout", 1.0)], set())
        rows = con.execute("SELECT symbol FROM planned_orders WHERE plan_date=? AND side='buy' AND status='planned'",
                           (PLAN_DAY,)).fetchall()
        assert sorted(r[0] for r in rows) == ["AAPL", "NVDA"]  # NVDA stays, one slot was left


def test_rerun_of_plan_entries_counts_existing_dollars(desk):
    with store.connect(desk.path) as con:
        run.ensure_schema(con)
        risk = RiskEngine(desk.limits, con)
        ctx = SimpleNamespace(market=SimpleNamespace(exposure_cap=0.3))  # 15k of exposure
        snap = desk.broker.account()
        run._plan_entries(con, PLAN_DAY, risk.ledger, risk, snap, ctx, [sig("NVDA", "rotation", 1.0, weight=1.0)], set())
        run._plan_entries(con, PLAN_DAY, risk.ledger, risk, snap, ctx, [sig("AAPL", "rotation", 1.0, weight=1.0)], set())
        total = con.execute("SELECT SUM(qty * ref_price) FROM planned_orders WHERE plan_date=? AND side='buy'",
                            (PLAN_DAY,)).fetchone()[0]
        assert total <= 15_000 + 1e-6


# ------------------------------------------------ 7. calendar, clock, missing bars
@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 9, 8, 8, 45, tzinfo=run.ET), "2026-09-04"),   # Monday 09-07 was Labor Day
    (datetime(2026, 9, 7, 17, 0, tzinfo=run.ET), "2026-09-04"),   # Labor Day evening is not a session
    (datetime(2026, 9, 8, 17, 0, tzinfo=run.ET), "2026-09-08"),
])
def test_weekday_fallback_skips_holidays(now, expected):
    assert run._last_closed_session(FakeBroker(), now) == expected


def test_opens_today_retries_a_transient_clock_failure():
    now = datetime(2026, 9, 9, 8, 45, tzinfo=run.ET)
    calls = []

    def get_clock():
        calls.append(1)
        if len(calls) == 1:
            raise requests.ConnectionError("reset")
        return SimpleNamespace(is_open=False, next_open=now.replace(hour=9, minute=30))
    b = AlpacaBroker(SimpleNamespace(get_clock=get_clock), environ={}, sleep=lambda s: None)
    assert run._opens_today(b, now) is True
    assert len(calls) == 2


def test_held_symbol_without_a_bar_is_reported_and_still_protected(desk, sent):
    bars, t, b = _bars_and_broker()
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", stop=round(b.prices["AAPL"] * 0.9, 2))
        bars.close.loc[t, "AAPL"] = math.nan
        out = run.postclose(con, b, _at(t, 17, 0), bars=bars, limits=desk.limits)
        run.postclose(con, b, _at(t, 17, 30), bars=bars, limits=desk.limits)
    assert "AAPL" in out and "缺少" in out
    assert sum(1 for title, m, p in sent if "缺少" in m and "AAPL" in m and p == run.notify.WARN) == 1
    assert len(stops(b, "AAPL")) == 1


# ------------------------------------------------ end to end
def test_multi_day_walk_keeps_every_position_protected(tmp_path, monkeypatch):
    bars = synthetic_bars(n=560, seed=7)
    broker = FakeBroker(cash=100_000, async_cancel=True)
    limits = Limits(halt_file=tmp_path / "HALT")
    stuck = {"on": False}
    monkeypatch.setattr(run, "CANCEL_WAIT_S", 0.05)
    # the broker settles asynchronous cancels while we wait, unless it is "stuck"
    monkeypatch.setattr(run, "_sleep", lambda s: None if stuck["on"] else broker.settle())
    real_submit = broker.submit
    seen = {"fail": None, "missed": None, "stuck": 0, "reruns": 0}

    def no_orphans(con, where):
        held = {norm(s) for s in Ledger(con).positions()}
        orphans = [o.client_order_id for o in broker.open_orders()
                   if o.side == Side.SELL and o.order_type == OrderType.STOP and norm(o.symbol) not in held]
        assert orphans == [], (where, orphans)

    def no_lost_exits(con, where):
        bad = con.execute("SELECT client_order_id, status, note FROM planned_orders WHERE side='sell' "
                          "AND status IN ('expired','error','blocked')").fetchall()
        assert [tuple(r) for r in bad] == [], where
        for r in con.execute("SELECT symbol, note FROM planned_orders WHERE side='sell' AND status='cancelled'"):
            assert r["note"], where

    def clean(con, where):
        assert reconcile(Ledger(con), broker.account()) == [], where

    def protected(con, where):
        for sym, pos in Ledger(con).positions().items():
            live = [o for o in broker.open_orders() if o.symbol == sym and o.order_type == OrderType.STOP]
            assert len(live) == 1, (where, sym, live)
            assert live[0].stop_price == pytest.approx(pos.stop, abs=0.01), (where, sym)
            assert live[0].qty == pytest.approx(pos.qty), (where, sym)

    def planned_sells(con):
        return con.execute("SELECT * FROM planned_orders WHERE side='sell' AND status='planned'").fetchall()

    days = bars.dates[-110:]
    with store.connect(tmp_path / "desk.sqlite") as con:
        for i, t in enumerate(days):
            if i > 0:
                broker.prices.update({s: float(v) for s, v in bars.open.loc[t].items()})
                sells = planned_sells(con)
                if sells and seen["fail"] is None:
                    target = sells[0]["symbol"]

                    def flaky(intent, target=target):
                        if intent.symbol == target and intent.order_type == OrderType.MARKET:
                            raise RuntimeError("503 service unavailable")
                        return real_submit(intent)
                    broker.submit = flaky
                    run.premarket(con, broker, _at(t, 8, 45), limits)
                    broker.submit = real_submit
                    row = con.execute("SELECT status FROM planned_orders WHERE client_order_id=?",
                                      (sells[0]["client_order_id"],)).fetchone()
                    assert row["status"] == "planned"
                    assert len([o for o in broker.open_orders()
                                if o.symbol == target and o.order_type == OrderType.STOP]) == 1
                    no_orphans(con, ("failed premarket", t))
                    no_lost_exits(con, ("failed premarket", t))
                    run.premarket(con, broker, _at(t, 8, 55), limits)
                    row = con.execute("SELECT status FROM planned_orders WHERE client_order_id=?",
                                      (sells[0]["client_order_id"],)).fetchone()
                    assert row["status"] in ("submitted", "cancelled")
                    seen["fail"] = t
                elif sells and seen["missed"] is None and seen["fail"] is not None:
                    seen["missed"] = t  # the Mac slept through premarket and postopen
                else:
                    run.premarket(con, broker, _at(t, 8, 45), limits)
                no_orphans(con, ("premarket", t))
                no_lost_exits(con, ("premarket", t))
                if seen["missed"] != t:
                    run.postopen(con, broker, _at(t, 9, 50), limits)
                    clean(con, ("postopen", t))
                    protected(con, ("postopen", t))
                    no_orphans(con, ("postopen", t))
                    no_lost_exits(con, ("postopen", t))
                for s, v in bars.low.loc[t].items():  # intraday lows trigger resting stops
                    broker.tick(s, float(v))
            broker.prices.update({s: float(v) for s, v in bars.close.loc[t].items()})
            stuck["on"] = i % 3 == 0  # on these nights the broker does not confirm cancels in time
            out = run.postclose(con, broker, _at(t, 17, 0), bars=bars.upto(t), limits=limits)
            stuck["on"] = False
            seen["stuck"] += "取消未確認" in out
            if i % 5 == 0:
                q = "SELECT COUNT(*) FROM planned_orders WHERE side='buy' AND status='planned'"
                before, n_orders = con.execute(q).fetchone()[0], len(broker.orders)
                run.postclose(con, broker, _at(t, 17, 30), bars=bars.upto(t), limits=limits)
                assert con.execute(q).fetchone()[0] == before, ("postclose rerun", t)
                seen["reruns"] += 1
            clean(con, ("postclose", t))
            no_orphans(con, ("postclose", t))
            no_lost_exits(con, ("postclose", t))
        assert seen["fail"] is not None and seen["missed"] is not None and seen["stuck"] > 0, seen
        assert seen["reruns"] >= 10
        fills = dict(con.execute("SELECT side, COUNT(*) FROM fills GROUP BY side").fetchall())
        assert fills.get("buy", 0) >= 5 and fills.get("sell", 0) >= 3, fills
        assert store.get_setting(con, "halt_new_entries", "0") == "0"
