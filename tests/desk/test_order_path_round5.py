"""Regression tests for the fifth review round of the automated order path:
a broker read failure during fill sync is not a sticky halt, an opening exit
that ends unfilled is re-planned behind a restored stop, partial-fill keys do
not collide, the reconcile-halt lift cannot overwrite a manual pause, and the
price cache is replaced atomically. Offline only: FakeBroker and temp files."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from stockmoney.desk import console, data, run, store
from stockmoney.desk.execution.alpaca import BrokerUnavailable
from stockmoney.desk.execution.broker import FakeBroker
from stockmoney.desk.execution.ledger import Ledger
from stockmoney.desk.execution.risk import Limits
from stockmoney.desk.execution.types import TIF, AssetClass, OrderIntent, OrderStatus, OrderType, Side

from test_data_refresh import market  # noqa: F401  (fixture)
from test_order_path_round4 import plan_status, protect
from test_run_safety import PLAN_DAY, WED, hold, market_orders, plan, stops


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
        broker=FakeBroker(cash=100_000, prices={"AAPL": 100.0, "MSFT": 100.0, "NVDA": 100.0}),
        limits=Limits(halt_file=tmp_path / "HALT"),
    )


AT = datetime(2026, 9, 9, 11, 0, tzinfo=run.ET)


def entry(con, broker, cid, sym, strategy="breakout", qty=10):
    intent = OrderIntent(cid, sym, AssetClass.EQUITY, Side.BUY, qty=qty, strategy=strategy)
    run._submit(con, broker, intent, "entry", 90.0)


# ------------------------------------------------ 1. a broker read failure is not a sticky halt
def test_sync_read_failure_does_not_halt_and_a_healthy_rerun_clears(desk, monkeypatch):
    b = desk.broker
    down = {"on": True}
    real = b.get_order

    def get_order(cid):
        if down["on"] and cid == "b-MSFT":
            raise BrokerUnavailable("alpaca get_order_by_client_id failed after 4 attempts: timed out")
        return real(cid)
    monkeypatch.setattr(b, "get_order", get_order)
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL")
        entry(con, b, "b-MSFT", "MSFT")
        run.recover(con, b, AT, desk.limits)
        assert store.get_setting(con, "halt_source", "") != "sync"
        assert con.execute("SELECT recorded FROM submitted WHERE client_order_id='b-MSFT'").fetchone()[0] == 0
        down["on"] = False
        run.recover(con, b, AT.replace(minute=5), desk.limits)
        assert Ledger(con).position("MSFT").qty == 10
        assert store.get_setting(con, "halt_new_entries", "0") == "0"


def test_sync_booking_error_still_sets_a_sticky_sync_halt(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL")
        entry(con, b, "b-AAPL-rot", "AAPL", strategy="rotation")  # AAPL is held by breakout
        run.recover(con, b, AT, desk.limits)
        assert store.get_setting(con, "halt_new_entries") == "1"
        assert store.get_setting(con, "halt_source") == "sync"


# ------------------------------------------------ 2. an opening exit that ends unfilled
def hold_sells(broker):
    """Market sells stay working (not filled) while ``flag['on']``."""
    flag = {"on": True}
    real = broker._fill

    def _fill(o, price, *more):
        if flag["on"] and o.side == Side.SELL and o.order_type == OrderType.MARKET:
            return
        return real(o, price, *more)
    broker._fill = _fill
    return flag


def test_unfilled_opening_exit_is_replanned_behind_a_restored_stop(desk):
    b = desk.broker
    flag = hold_sells(b)
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", qty=50, stop=90.0)
        protect(con, desk, "AAPL")
        cid = plan(con, "AAPL", side=Side.SELL, qty=50)
        run.premarket(con, b, WED, desk.limits)
        assert plan_status(con, cid)["status"] == "submitted"
        assert stops(b, "AAPL") == []
        b.expire_day_orders()  # the opening sell did not execute
        run.postopen(con, b, WED.replace(hour=9, minute=50), desk.limits)
        assert [(o.qty, o.stop_price) for o in stops(b, "AAPL")] == [(50, 90.0)]
        assert plan_status(con, cid)["status"] == "planned"
        assert [o.status for o in market_orders(b, "AAPL", Side.SELL)] == [OrderStatus.EXPIRED]
        flag["on"] = False
        run.postopen(con, b, WED.replace(hour=10, minute=20), desk.limits)
        sells = market_orders(b, "AAPL", Side.SELL)
        new = [o for o in sells if o.status != OrderStatus.EXPIRED]
        assert [(o.qty, o.tif, o.status) for o in new] == [(50, TIF.DAY, OrderStatus.FILLED)]
        assert plan_status(con, cid)["status"] == "submitted"
        assert Ledger(con).position("AAPL") is None
        assert stops(b, "AAPL") == []


def test_unfilled_exit_retries_stop_after_max_attempts(desk, sent, monkeypatch):
    monkeypatch.setattr(run, "CANCEL_WAIT_S", 0.05)  # the working sells never settle here
    b = desk.broker
    hold_sells(b)
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", qty=50, stop=90.0)
        protect(con, desk, "AAPL")
        cid = plan(con, "AAPL", side=Side.SELL, qty=50)
        run.premarket(con, b, WED, desk.limits)
        for i in range(run.MAX_EXIT_ATTEMPTS):
            b.expire_day_orders()
            run.postopen(con, b, WED.replace(hour=10, minute=i * 10), desk.limits)  # re-plans, restores the stop
            assert len(stops(b, "AAPL")) == 1
            if i + 1 < run.MAX_EXIT_ATTEMPTS:
                assert plan_status(con, cid)["status"] == "planned"
                run.postopen(con, b, WED.replace(hour=10, minute=i * 10 + 5), desk.limits)  # sends the retry
                assert plan_status(con, cid)["status"] == "submitted"
        assert plan_status(con, cid)["status"] == "error"
        assert len(market_orders(b, "AAPL", Side.SELL)) == run.MAX_EXIT_ATTEMPTS
        assert any(p == run.notify.CRITICAL and "AAPL" in t for t, m, p in sent)


# ------------------------------------------------ 3. partial-fill keys
def test_partial_fill_keys_do_not_collide_at_six_digits(desk):
    with store.connect(desk.path) as con:
        led = Ledger(con)
        kw = dict(client_order_id="o1", symbol="BTC/USD", strategy="breakout", side=Side.BUY, cumulative=True)
        assert led.apply_fill(qty=0.1, price=100.0, **kw) is True
        assert led.apply_fill(qty=0.12345674, price=100.0, **kw) is True
        assert led.apply_fill(qty=0.12345678, price=100.0, **kw) is True
        assert led.position("BTC/USD").qty == pytest.approx(0.12345678, abs=1e-12)
        assert led.apply_fill(qty=0.12345678, price=100.0, **kw) is False


def test_legacy_partial_fill_key_is_still_recognised(desk):
    with store.connect(desk.path) as con:
        led = Ledger(con)
        kw = dict(client_order_id="o1", symbol="AAPL", strategy="breakout", side=Side.BUY, cumulative=True)
        assert led.apply_fill(qty=30, price=100.0, **kw) is True
        con.execute("INSERT INTO fills (client_order_id, symbol, strategy, side, qty, price) "
                    "VALUES ('o1#50', 'AAPL', 'breakout', 'buy', 20, 100.0)")  # written by the old format
        con.execute("UPDATE sleeve_positions SET qty=50 WHERE symbol='AAPL'")
        assert led.apply_fill(qty=50, price=100.0, **kw) is False
        assert led._seen_cumulative("o1", 50.0) and not led._seen_cumulative("o1", 50.5)
        assert led.position("AAPL").qty == 50


# ------------------------------------------------ 4. halt lift vs a manual pause
class Hooked:
    """Connection proxy that runs ``hook`` once, right before the first write
    to the settings table: a deterministic stand-in for another process
    writing between this one's read and its write."""

    def __init__(self, con, hook):
        self._con, self._hook = con, hook

    def execute(self, sql, *args):
        if self._hook and sql.lstrip().upper().startswith(("UPDATE SETTINGS", "INSERT INTO SETTINGS")):
            hook, self._hook = self._hook, None
            hook()
        return self._con.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._con, name)


def other(path):
    con = sqlite3.connect(path, timeout=0.05)
    con.row_factory = sqlite3.Row
    return con


def reconcile_halt(con):
    run.ensure_schema(con)
    store.set_setting(con, "halt_new_entries", "1")
    store.set_setting(con, "halt_source", "reconcile")
    con.commit()


def test_manual_halt_set_between_lift_read_and_write_survives(desk):
    def manual():
        c = other(desk.path)
        console.set_halt(c, True, "")
        c.commit()
        c.close()
    with store.connect(desk.path) as con:
        reconcile_halt(con)
        run._lift_reconcile_halt(Hooked(con, manual), PLAN_DAY)
        assert store.get_setting(con, "halt_new_entries") == "1"
        assert store.get_setting(con, "halt_source") == "manual"


def test_lift_cannot_clear_a_halt_while_set_halt_is_writing(desk):
    def lift():
        c = other(desk.path)
        try:
            run._lift_reconcile_halt(c, PLAN_DAY)
        except sqlite3.OperationalError:
            pass  # locked out until the manual pause is committed
        c.close()
    with store.connect(desk.path) as con:
        reconcile_halt(con)
        console.set_halt(Hooked(con, lift), True, "")
        assert store.get_setting(con, "halt_new_entries") == "1"
        assert store.get_setting(con, "halt_source") == "manual"


# ------------------------------------------------ 5. atomic price cache write
def test_failed_cache_write_leaves_the_old_file_intact(market, tmp_path, monkeypatch):  # noqa: F811
    data.refresh(tmp_path, start=date(2024, 1, 1))
    path = tmp_path / "ohlcv.parquet"
    before = pd.read_parquet(path)
    market.last = 46

    def broken(self, target, *a, **kw):
        with open(target, "wb") as fh:
            fh.write(b"PAR1 half a file")
        raise OSError("disk full")
    monkeypatch.setattr(pd.DataFrame, "to_parquet", broken)
    with pytest.raises(OSError):
        data.refresh(tmp_path, start=date(2024, 1, 1))
    pd.testing.assert_frame_equal(pd.read_parquet(path), before)
    assert [p.name for p in tmp_path.iterdir() if p.name != "ohlcv.parquet"] == []
