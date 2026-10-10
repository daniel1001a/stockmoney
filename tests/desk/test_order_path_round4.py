"""Regression tests for the fourth review round of the automated order path:
held exits on a reconcile mismatch, deferred exits, partial fills, halt
sources, the job clock and lock, accepted-status stops, cancel races, the
reboot recovery job and same-session replanning. Offline only: FakeBroker
(including async cancels and partial fills) and a temp SQLite file."""
from __future__ import annotations

import fcntl
import importlib.util
from datetime import datetime, timezone
from math import floor
from pathlib import Path
from types import SimpleNamespace

import pytest

from stockmoney.desk import console, run, store
from stockmoney.desk.execution.broker import FakeBroker
from stockmoney.desk.execution.ledger import Ledger
from stockmoney.desk.execution.risk import Limits, RiskEngine
from stockmoney.desk.execution.types import (
    TIF, AssetClass, BrokerPosition, OrderIntent, OrderStatus, OrderType, Side,
)

from test_alpaca import api_error, make_broker, make_order
from test_run_safety import PLAN_DAY, WED, hold, market_orders, plan, sig, stops

ROOT = Path(__file__).resolve().parents[2]


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


def stop_submits(broker):
    calls = []
    real = broker.submit

    def submit(intent):
        if intent.order_type == OrderType.STOP:
            calls.append(intent)
        return real(intent)
    broker.submit = submit
    return calls


def fill_partly(broker, ratio):
    """Market buys (other than test seeds) fill only ``ratio`` of their qty
    and stay partially_filled, written against the plain broker internals so
    it works with or without built-in partial-fill support."""
    real = broker._fill

    def _fill(o, price, *more):
        if o.side != Side.BUY or o.order_type != OrderType.MARKET or o.client_order_id.startswith("seed-"):
            return real(o, price, *more)
        q = floor(o.qty * ratio)
        broker.cash -= q * price
        p = broker.positions.get(o.symbol)
        if p:
            p.avg_entry_price = (p.avg_entry_price * p.qty + price * q) / (p.qty + q)
            p.qty += q
        else:
            broker.positions[o.symbol] = BrokerPosition(o.symbol, AssetClass.EQUITY, q, price, q * price, price)
        o.status, o.filled_qty, o.filled_avg_price = OrderStatus.PARTIALLY_FILLED, q, price
        o.filled_at = datetime.now(timezone.utc)
    broker._fill = _fill
    return real


def finish_fill(broker, cid, price):
    o = broker.orders[cid]
    rest = o.qty - o.filled_qty
    broker.cash -= rest * price
    p = broker.positions[o.symbol]
    p.avg_entry_price = (p.avg_entry_price * p.qty + price * rest) / (p.qty + rest)
    p.qty += rest
    o.filled_avg_price = (o.filled_avg_price * o.filled_qty + price * rest) / o.qty
    o.filled_qty, o.status = o.qty, OrderStatus.FILLED


# ------------------------------------------------ A. exit held by a reconcile mismatch
def test_exit_on_a_mismatched_symbol_stays_planned_warns_and_runs_once_clean(desk, sent):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", qty=50, stop=90.0)
        protect(con, desk, "AAPL")
        cid = plan(con, "AAPL", side=Side.SELL, qty=50)
        b.positions["AAPL"].qty += 3  # someone bought 3 shares by hand
        run.premarket(con, b, WED, desk.limits)
        assert plan_status(con, cid)["status"] == "planned"
        assert market_orders(b, "AAPL", Side.SELL) == []
        warns = [m for t, m, p in sent if p == run.notify.WARN and "AAPL" in t + m and cid in t + m]
        assert len(warns) == 1
        b.positions["AAPL"].qty -= 3  # the owner sorted it out
        run.premarket(con, b, WED.replace(minute=55), desk.limits)
        assert plan_status(con, cid)["status"] == "submitted"
        assert [o.qty for o in market_orders(b, "AAPL", Side.SELL)] == [50]


# ------------------------------------------------ N1. exit deferred by an unconfirmed stop cancel
def test_exit_deferred_by_unconfirmed_cancel_runs_at_postopen(desk, monkeypatch, sent):
    b = FakeBroker(cash=100_000, prices=dict(desk.broker.prices), async_cancel=True)
    monkeypatch.setattr(run, "CANCEL_WAIT_S", 0.05)
    stuck = {"on": True}
    monkeypatch.setattr(run, "_sleep", lambda s: None if stuck["on"] else b.settle())
    desk.broker = b
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", qty=50, stop=90.0)
        protect(con, desk, "AAPL")
        cid = plan(con, "AAPL", side=Side.SELL, qty=50)
        run.premarket(con, b, WED, desk.limits)
        assert plan_status(con, cid)["status"] == "planned"
        assert any(p == run.notify.WARN and "AAPL" in t + m for t, m, p in sent)
        stuck["on"] = False
        run.postopen(con, b, WED.replace(hour=9, minute=50), desk.limits)
        assert plan_status(con, cid)["status"] == "submitted"
        sells = market_orders(b, "AAPL", Side.SELL)
        assert [(o.qty, o.tif) for o in sells] == [(50, TIF.DAY)]
        assert Ledger(con).position("AAPL") is None
        assert stops(b, "AAPL") == []


def test_postopen_does_not_send_exits_when_the_market_is_closed(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", qty=50, stop=90.0)
        cid = plan(con, "AAPL", side=Side.SELL, qty=50)
        b.market_open = False
        run.postopen(con, b, WED.replace(hour=9, minute=50), desk.limits)
        assert plan_status(con, cid)["status"] == "planned"
        assert market_orders(b, "AAPL", Side.SELL) == []
        assert len(stops(b, "AAPL")) == 1


# ------------------------------------------------ N2a. partial fills are booked as they arrive
def test_ledger_books_only_the_delta_of_a_cumulative_fill(desk):
    with store.connect(desk.path) as con:
        led = Ledger(con)
        kw = dict(client_order_id="o1", symbol="AAPL", strategy="breakout", side=Side.BUY, cumulative=True)
        assert led.apply_fill(qty=30, price=100.0, **kw) is True
        assert led.apply_fill(qty=30, price=100.0, **kw) is False  # same cumulative qty: no-op
        assert led.apply_fill(qty=50, price=101.0, **kw) is True   # 20 more, cumulative avg 101
        pos = led.position("AAPL")
        assert pos.qty == 50 and pos.avg_price == pytest.approx(101.0)
        assert con.execute("SELECT SUM(qty) FROM fills").fetchone()[0] == 50
        assert led.apply_fill(qty=50, price=101.0, **kw) is False


def test_partial_fill_of_a_late_buy_is_booked_and_protected(desk):
    b = desk.broker
    fill_partly(b, 0.6)
    with store.connect(desk.path) as con:
        cid = plan(con, "AAPL", qty=50, stop=90.0)
        run.premarket(con, b, WED.replace(hour=9, minute=40), desk.limits)  # late: plain market order
        assert b.get_order(cid).status == OrderStatus.PARTIALLY_FILLED
        assert Ledger(con).position("AAPL").qty == 30
        assert [o.qty for o in stops(b, "AAPL")] == [30]
        assert store.get_setting(con, "halt_new_entries", "0") == "0"
        finish_fill(b, cid, 100.0)
        run.postopen(con, b, WED.replace(hour=9, minute=50), desk.limits)
        assert Ledger(con).position("AAPL").qty == 50
        assert [o.qty for o in stops(b, "AAPL")] == [50]
        assert con.execute("SELECT SUM(qty) FROM fills WHERE symbol='AAPL'").fetchone()[0] == 50
        assert con.execute("SELECT recorded FROM submitted WHERE client_order_id=?", (cid,)).fetchone()[0] == 1
        assert store.get_setting(con, "halt_new_entries", "0") == "0"


# ------------------------------------------------ N2b. who set the halt
def test_reconcile_halt_clears_when_clean_and_a_manual_halt_sticks(desk, sent):
    b = desk.broker
    at = datetime(2026, 9, 9, 9, 50, tzinfo=run.ET)
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL")
        b.positions["AAPL"].qty += 3
        run.postopen(con, b, at, desk.limits)
        assert store.get_setting(con, "halt_new_entries") == "1"
        assert store.get_setting(con, "halt_source") == "reconcile"
        b.positions["AAPL"].qty -= 3
        n = len(sent)
        run.postopen(con, b, at.replace(minute=55), desk.limits)
        assert store.get_setting(con, "halt_new_entries") == "0"
        assert any("解除" in t + m for t, m, p in sent[n:])
        console.set_halt(con, True, "")
        assert store.get_setting(con, "halt_source") == "manual"
        run.postopen(con, b, at.replace(minute=58), desk.limits)
        assert store.get_setting(con, "halt_new_entries") == "1"


def test_manual_halt_survives_a_mismatch_that_later_clears(desk):
    b = desk.broker
    at = datetime(2026, 9, 9, 9, 50, tzinfo=run.ET)
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL")
        console.set_halt(con, True, "")
        b.positions["AAPL"].qty += 3
        run.postopen(con, b, at, desk.limits)
        b.positions["AAPL"].qty -= 3
        run.postopen(con, b, at.replace(minute=55), desk.limits)
        assert store.get_setting(con, "halt_new_entries") == "1"
        assert store.get_setting(con, "halt_source") == "manual"


def test_legacy_halt_without_a_source_is_sticky(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL")
        store.set_setting(con, "halt_new_entries", "1")
        run.postopen(con, b, datetime(2026, 9, 9, 9, 50, tzinfo=run.ET), desk.limits)
        assert store.get_setting(con, "halt_new_entries") == "1"


# ------------------------------------------------ N3. the job clock starts after the lock
def test_main_takes_the_clock_after_the_lock(desk, monkeypatch, tmp_path):
    state = {"locked": False}
    before = datetime(2026, 9, 9, 13, 24, tzinfo=timezone.utc)  # 09:24 ET
    after = datetime(2026, 9, 9, 13, 29, tzinfo=timezone.utc)   # 09:29 ET

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return after if state["locked"] else before
    monkeypatch.setattr(run, "datetime", Clock)
    real_lock = run._acquire_lock

    def lock(path, wait_s):
        fh = real_lock(path, wait_s)
        state["locked"] = True
        return fh
    monkeypatch.setattr(run, "_acquire_lock", lock)
    monkeypatch.setattr(run, "LOCK_PATH", tmp_path / "run.lock", raising=False)
    real_connect = store.connect
    monkeypatch.setattr(store, "connect", lambda path=None: real_connect(desk.path))
    monkeypatch.setattr(run, "make_broker", lambda: desk.broker)
    seen = []
    monkeypatch.setattr(run, "premarket", lambda con, broker, now: seen.append(now) or "ok")
    assert run.main(["run", "premarket"]) == 0
    assert seen == [after]


def test_opg_cutoff_is_rechecked_before_each_submit(desk, monkeypatch):
    b = desk.broker
    clock = [0.0]
    monkeypatch.setattr(run, "_monotonic", lambda: clock[0], raising=False)
    real = b.submit

    def submit(intent):
        o = real(intent)
        if intent.order_type == OrderType.MARKET:
            clock[0] += 120.0  # each market order takes two minutes to go through
        return o
    b.submit = submit
    with store.connect(desk.path) as con:
        plan(con, "AAPL", stop=90.0)
        plan(con, "MSFT", "pullback", stop=90.0)
        run.premarket(con, b, datetime(2026, 9, 9, 9, 24, tzinfo=run.ET), desk.limits)
    tifs = {s: [o.tif for o in market_orders(b, s, Side.BUY)] for s in ("AAPL", "MSFT")}
    assert tifs == {"AAPL": [TIF.OPG], "MSFT": [TIF.DAY]}
    assert len(stops(b, "MSFT")) == 1  # the late order got its stop right away


# ------------------------------------------------ N4. lock wait and slow work outside the lock
def test_lock_wait_covers_a_full_job():
    assert run.LOCK_WAIT_S >= run.JOB_TIMEOUT_S


def test_postclose_refreshes_data_before_taking_the_lock(desk, monkeypatch, tmp_path):
    lock_path = tmp_path / "run.lock"
    monkeypatch.setattr(run, "LOCK_PATH", lock_path, raising=False)
    real_connect = store.connect
    monkeypatch.setattr(store, "connect", lambda path=None: real_connect(desk.path))
    monkeypatch.setattr(run, "make_broker", lambda: desk.broker)
    held = []

    def refresh():
        with open(lock_path, "a+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                held.append(False)
            except BlockingIOError:
                held.append(True)
        return "frame"
    monkeypatch.setattr(run.data, "refresh", refresh)
    monkeypatch.setattr(run.data, "to_bars", lambda df: ("bars", df))
    got = []
    monkeypatch.setattr(run, "postclose", lambda con, broker, now, bars=None: got.append(bars) or "ok")
    assert run.main(["run", "postclose"]) == 0
    assert held == [False]
    assert got == [("bars", "frame")]


# ------------------------------------------------ N5. accepted stops are live
def test_accepted_stop_is_kept_not_replaced(desk, monkeypatch):
    b = desk.broker
    monkeypatch.setattr(run, "CANCEL_WAIT_S", 0.05)

    def cancel(cid):  # an accepted order can be cancelled at once
        if b.orders[cid].status not in run._TERMINAL:
            b.orders[cid].status = OrderStatus.CANCELED
    b.cancel = cancel
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", qty=50, stop=90.0)
        risk = protect(con, desk, "AAPL")
        (live,) = stops(b, "AAPL")
        b.orders[live.client_order_id].status = OrderStatus.PENDING  # Alpaca: accepted, after the close
        calls = stop_submits(b)
        run.ensure_stops(con, b, risk.ledger, risk, b.account(), PLAN_DAY)
        assert calls == []
        assert [o.client_order_id for o in stops(b, "AAPL")] == [live.client_order_id]
        assert b.orders[live.client_order_id].status == OrderStatus.PENDING


def test_pending_cancel_stop_is_not_kept(desk, monkeypatch):
    b = desk.broker
    monkeypatch.setattr(run, "CANCEL_WAIT_S", 0)
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", qty=50, stop=90.0)
        risk = protect(con, desk, "AAPL")
        (live,) = stops(b, "AAPL")
        b.orders[live.client_order_id].status = OrderStatus.PENDING_CANCEL
        out = run.ensure_stops(con, b, risk.ledger, risk, b.account(), PLAN_DAY)
        assert any("取消未確認" in e for e in out)
        b.orders[live.client_order_id].status = OrderStatus.CANCELED
        run.ensure_stops(con, b, risk.ledger, risk, b.account(), PLAN_DAY)
        assert [o.status for o in stops(b, "AAPL")] == [OrderStatus.NEW]


@pytest.mark.parametrize("alpaca_status,expected", [
    ("pending_cancel", "PENDING_CANCEL"),
    ("accepted", "PENDING"),
    ("pending_new", "PENDING"),
])
def test_alpaca_maps_pending_cancel_apart_from_accepted(alpaca_status, expected):
    b, c, _ = make_broker()
    c.orders["x"] = make_order("x", status=alpaca_status)
    assert b.get_order("x").status == getattr(OrderStatus, expected)


# ------------------------------------------------ B. cancel races and per-symbol isolation
@pytest.mark.parametrize("status", ["pending_cancel", "accepted", "pending_new"])
def test_alpaca_cancel_of_an_order_already_in_flight_does_not_raise(status):
    b, c, _ = make_broker()
    c.orders["a"] = make_order("a", status=status, side="sell", otype="stop", tif="gtc", stop_price="90")
    c.cancel_error = api_error(422, "order is not cancelable")
    b.cancel("a")  # no exception: the caller's wait loop polls for the outcome


def test_one_failing_orphan_cancel_does_not_stop_the_sweep(desk):
    b = desk.broker
    for sym in ("AAPL", "MSFT"):
        b.submit(OrderIntent(f"seed-{sym}", sym, AssetClass.EQUITY, Side.BUY, qty=10))
        b.submit(OrderIntent(f"orphan-{sym}", sym, AssetClass.EQUITY, Side.SELL, qty=10,
                             order_type=OrderType.STOP, tif=TIF.GTC, stop_price=50.0))
        del b.positions[sym]
    real = b.cancel

    def cancel(cid):
        if cid == "orphan-AAPL":
            raise RuntimeError("422 order is not cancelable")
        return real(cid)
    b.cancel = cancel
    with store.connect(desk.path) as con:
        run.ensure_schema(con)
        out = run.sweep_orphan_stops(con, b, Ledger(con), PLAN_DAY)
    assert b.get_order("orphan-MSFT").status == OrderStatus.CANCELED
    assert any("orphan-AAPL" in e for e in out)


# ------------------------------------------------ C. clearing a stop id never creates a row
def test_clearing_a_stop_id_does_not_create_position_meta(desk):
    with store.connect(desk.path) as con:
        run.ensure_schema(con)
        run._set_stop_id(con, "ZZZ", None)
        assert con.execute("SELECT COUNT(*) FROM position_meta").fetchone()[0] == 0


# ------------------------------------------------ N6. recovery after a reboot
def test_recover_restores_stops_and_sends_nothing_else(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", qty=50, stop=90.0)  # crashed before its stop was placed
        b.submit(OrderIntent("seed-NVDA", "NVDA", AssetClass.EQUITY, Side.BUY, qty=10))
        b.submit(OrderIntent("orphan", "NVDA", AssetClass.EQUITY, Side.SELL, qty=10, order_type=OrderType.STOP,
                             tif=TIF.GTC, stop_price=50.0))
        del b.positions["NVDA"]
        buy = plan(con, "MSFT", qty=10)
        sell = plan(con, "AAPL", side=Side.SELL, qty=50)
        run.recover(con, b, datetime(2026, 9, 9, 11, 0, tzinfo=run.ET), desk.limits)
        assert [(o.qty, o.stop_price) for o in stops(b, "AAPL")] == [(50, 90.0)]
        assert b.get_order("orphan").status == OrderStatus.CANCELED
        assert market_orders(b, "MSFT", Side.BUY) == [] and market_orders(b, "AAPL", Side.SELL) == []
        assert plan_status(con, buy)["status"] == "planned" and plan_status(con, sell)["status"] == "planned"
    assert "recover" in run.JOBS


def _schedule():
    spec = importlib.util.spec_from_file_location("desk_schedule", ROOT / "scripts" / "desk_schedule.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_recover_is_scheduled_at_load():
    sch = _schedule()
    assert "recover" in sch.JOBS and "recover" in sch.DEFAULT_JOBS
    p = sch.plist("recover", sch.JOBS["recover"])
    assert p["RunAtLoad"] is True
    assert "StartCalendarInterval" not in p and "StartInterval" not in p
    assert p["ProgramArguments"][-1] == "recover"
    assert "RunAtLoad" not in sch.plist("premarket", sch.JOBS["premarket"])


# ------------------------------------------------ N7. submitted but unbooked buys use up caps
def test_rerun_counts_submitted_unbooked_buys_against_caps(desk, monkeypatch):
    monkeypatch.setattr(run, "MAX_POSITIONS", 1)
    with store.connect(desk.path) as con:
        run.ensure_schema(con)
        risk = RiskEngine(desk.limits, con)
        ctx = SimpleNamespace(market=SimpleNamespace(exposure_cap=1.0))
        snap = desk.broker.account()
        run._plan_entries(con, PLAN_DAY, risk.ledger, risk, snap, ctx, [sig("NVDA", "breakout", 2.0)], set())
        (row,) = con.execute("SELECT * FROM planned_orders").fetchall()
        con.execute("UPDATE planned_orders SET status='submitted' WHERE client_order_id=?", (row["client_order_id"],))
        con.execute("INSERT INTO submitted (client_order_id, kind, symbol, asset_class, strategy, side, qty, stop, "
                    "submitted_at, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (row["client_order_id"], "entry", "NVDA", "us_equity", "breakout", "buy", row["qty"], 90.0,
                     store.now(), "new"))
        out = run._plan_entries(con, PLAN_DAY, risk.ledger, risk, snap, ctx, [sig("AAPL", "breakout", 3.0)], set())
        assert out == []
        n = con.execute("SELECT COUNT(*) FROM planned_orders WHERE symbol='AAPL'").fetchone()[0]
        assert n == 0


# ------------------------------------------------ end to end
def test_multi_day_walk_with_wake_shuffles_partials_accepted_stops_and_a_mismatch(tmp_path, monkeypatch, sent):
    """Like test_multi_day_walk_keeps_every_position_protected, plus: mornings
    the Mac slept through, where premarket, postopen and recover run in a
    shuffled order after the open and late buys fill only partly; stops
    placed outside the session come back ``accepted``; one exit is deferred
    by an unconfirmed stop cancel; one held exit sits through a reconcile
    mismatch that the owner later clears. After every job: one live stop per
    held, reconciled symbol at the ledger price and size, no sell stop for a
    symbol the ledger does not hold, no exit plan lost, and no halt left by
    reconcile once it is clean."""
    import random
    from stockmoney.desk.execution.reconcile import mismatches
    from test_desk import synthetic_bars
    from test_run_safety import _at

    rng = random.Random(11)
    bars = synthetic_bars(n=560, seed=7)
    broker = FakeBroker(cash=100_000, async_cancel=True)
    limits = Limits(halt_file=tmp_path / "HALT")
    stuck = {"on": False}
    monkeypatch.setattr(run, "CANCEL_WAIT_S", 0.05)
    monkeypatch.setattr(run, "_sleep", lambda s: None if stuck["on"] else broker.settle())
    seen = {"wake": 0, "recover": 0, "defer": None, "mismatch": None, "cleared": None, "kept": 0, "jobs": 0}
    live_states = (OrderStatus.NEW, OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)
    open_states = live_states + (OrderStatus.PENDING_CANCEL,)

    def sell_stops(sym=None):
        return [o for o in broker.orders.values()
                if o.side == Side.SELL and o.order_type == OrderType.STOP and o.status in open_states
                and (sym is None or o.symbol == sym)]

    def check(con, where):
        seen["jobs"] += 1
        led = Ledger(con)
        held = led.positions()
        bad = mismatches(led, broker.account())
        exiting = {r[0] for r in con.execute("SELECT symbol FROM submitted WHERE kind='exit' AND recorded=0")}
        for sym, pos in held.items():
            if sym in bad or sym in exiting:
                continue
            mine = sell_stops(sym)
            if stuck["on"]:
                assert len(mine) <= 1, (where, sym, mine)
                continue
            assert len(mine) == 1, (where, sym, mine)
            assert mine[0].status in live_states, (where, sym, mine[0])
            assert mine[0].stop_price == pytest.approx(pos.stop, abs=0.01), (where, sym)
            assert mine[0].qty == pytest.approx(pos.qty), (where, sym)
        for o in sell_stops():
            if o.symbol not in held and o.symbol not in bad:
                assert stuck["on"] and o.status == OrderStatus.PENDING_CANCEL, (where, "orphan", o)
        lost = con.execute("SELECT client_order_id, status, note FROM planned_orders WHERE side='sell' "
                           "AND status NOT IN ('planned','submitted','cancelled')").fetchall()
        assert [tuple(r) for r in lost] == [], where
        for r in con.execute("SELECT note FROM planned_orders WHERE side='sell' AND status='cancelled'"):
            assert r["note"], where
        if not bad:
            assert not (store.get_setting(con, "halt_new_entries", "0") == "1"
                        and store.get_setting(con, "halt_source", "") == "reconcile"), where

    def planned_sells(con):
        return [r for r in con.execute("SELECT * FROM planned_orders WHERE side='sell' AND status='planned'")
                if r["symbol"] in broker.positions]

    def plan_of(con, cid):
        return con.execute("SELECT status FROM planned_orders WHERE client_order_id=?", (cid,)).fetchone()[0]

    days = bars.dates[-120:]
    extra = None  # (symbol, shares) bought by hand outside the desk
    with store.connect(tmp_path / "desk.sqlite") as con:
        for i, t in enumerate(days):
            if i > 0:
                broker.prices.update({s: float(v) for s, v in bars.open.loc[t].items()})
                if extra is not None and seen["cleared"] is None and t > seen["mismatch"][0]:
                    sym, n = extra  # the owner sells the stray shares again
                    p = broker.positions.get(sym)
                    if p is not None:
                        p.qty -= n
                        broker.cash += n * broker.prices[sym]
                        if p.qty <= 1e-9:
                            del broker.positions[sym]
                    seen["cleared"] = t
                sells = planned_sells(con)
                wake = i % 4 == 2 and seen["cleared"] != t
                if wake:
                    # asleep through 08:45 and 09:50; launchd runs the missed jobs on wake, in any order
                    seen["wake"] += 1
                    broker.open_session()
                    broker.rest_status = OrderStatus.NEW
                    broker.partial_ratio = 0.5
                    jobs = ["premarket", "postopen"] + (["recover"] if i % 8 == 2 else [])
                    rng.shuffle(jobs)
                    for k, job in enumerate(jobs):
                        at = _at(t, 10, 5 + k)
                        if job == "recover":
                            run.recover(con, broker, at, limits)
                            seen["recover"] += 1
                        else:
                            getattr(run, job)(con, broker, at, limits)
                        check(con, (job, "wake", t))
                    broker.partial_ratio = None
                    if i % 8 == 2:
                        broker.fill_rest()  # the rest of the late buys fills during the day
                else:
                    broker.rest_status = OrderStatus.PENDING  # before the open, new stops are accepted
                    if sells and seen["defer"] is None:
                        target = sells[0]
                        stuck["on"] = True  # the stop cancel is not confirmed in time
                        run.premarket(con, broker, _at(t, 8, 45), limits)
                        assert plan_of(con, target["client_order_id"]) == "planned"
                        check(con, ("premarket stuck", t))
                        stuck["on"] = False
                        broker.open_session()
                        broker.rest_status = OrderStatus.NEW
                        run.postopen(con, broker, _at(t, 9, 50), limits)
                        assert plan_of(con, target["client_order_id"]) in ("submitted", "cancelled")
                        check(con, ("postopen after deferral", t))
                        seen["defer"] = t
                    else:
                        if sells and seen["defer"] is not None and seen["mismatch"] is None and i >= 20:
                            sym = sells[0]["symbol"]
                            extra = (sym, 3)
                            broker.positions[sym].qty += 3
                            broker.cash -= 3 * broker.prices[sym]
                            seen["mismatch"] = (t, sells[0]["client_order_id"])
                        stops_before = {s: (p.stop, p.qty) for s, p in Ledger(con).positions().items()}
                        before = {o.symbol: o.client_order_id for o in sell_stops()  # live and already right
                                  if o.status in live_states and o.symbol in stops_before
                                  and o.stop_price == pytest.approx(stops_before[o.symbol][0], abs=0.005)
                                  and o.qty == pytest.approx(stops_before[o.symbol][1])}
                        selling = {r["symbol"] for r in sells}
                        n_warn = len(sent)
                        run.premarket(con, broker, _at(t, 8, 45), limits)
                        check(con, ("premarket", t))
                        for s, cid in before.items():
                            pos = Ledger(con).position(s)
                            if (pos is None or s in selling or stops_before.get(s) != (pos.stop, pos.qty)
                                    or (extra and s == extra[0])):
                                continue
                            assert [o.client_order_id for o in sell_stops(s)] == [cid], ("accepted stop", s, t)
                            seen["kept"] += 1
                        if seen["mismatch"] and seen["mismatch"][0] == t:
                            assert plan_of(con, seen["mismatch"][1]) == "planned"
                            assert store.get_setting(con, "halt_source") == "reconcile"
                            assert any(p == run.notify.WARN and extra[0] in tt + m for tt, m, p in sent[n_warn:])
                        if seen["cleared"] == t:
                            assert plan_of(con, seen["mismatch"][1]) in ("submitted", "cancelled")
                            assert store.get_setting(con, "halt_new_entries") == "0"
                        broker.open_session()
                        broker.rest_status = OrderStatus.NEW
                        run.postopen(con, broker, _at(t, 9, 50), limits)
                        check(con, ("postopen", t))
                for s, v in bars.low.loc[t].items():  # intraday lows trigger resting stops
                    broker.tick(s, float(v))
            broker.expire_day_orders()
            broker.prices.update({s: float(v) for s, v in bars.close.loc[t].items()})
            broker.rest_status = OrderStatus.PENDING  # after the close, new stops are accepted
            stuck["on"] = i % 3 == 0
            run.postclose(con, broker, _at(t, 17, 0), bars=bars.upto(t), limits=limits)
            check(con, ("postclose", t))
            stuck["on"] = False
            if i % 7 == 3:  # a reboot in the evening
                run.recover(con, broker, _at(t, 20, 0), limits)
                seen["recover"] += 1
                check(con, ("recover", t))
        assert seen["defer"] is not None and seen["mismatch"] is not None and seen["cleared"] is not None, seen
        assert seen["wake"] >= 20 and seen["recover"] >= 10 and seen["kept"] >= 10, seen
        partial = con.execute("SELECT COUNT(*) FROM fills WHERE client_order_id LIKE '%#%'").fetchone()[0]
        assert partial >= 1, partial
        fills = dict(con.execute("SELECT side, COUNT(*) FROM fills GROUP BY side").fetchall())
        assert fills.get("buy", 0) >= 5 and fills.get("sell", 0) >= 3, fills
        assert store.get_setting(con, "halt_new_entries", "0") == "0"
