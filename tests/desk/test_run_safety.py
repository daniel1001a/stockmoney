"""Regression tests for order-execution defects found in review. Every test
runs offline against FakeBroker and a temp SQLite file; nothing is sent."""
from __future__ import annotations

import fcntl
import json
import math
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from stockmoney.desk import run, store
from stockmoney.desk.execution.broker import FakeBroker
from stockmoney.desk.execution.ledger import Ledger
from stockmoney.desk.execution.risk import Limits, RiskEngine
from stockmoney.desk.execution.types import (
    TIF, AssetClass, OrderIntent, OrderStatus, OrderType, Side,
)
from stockmoney.desk.strategies import Signal

from test_desk import synthetic_bars

PLAN_DAY = "2026-09-08"  # a Tuesday
WED = datetime(2026, 9, 9, 8, 45, tzinfo=run.ET)
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


def plan(con, sym, strategy="breakout", qty=50, ref=100.0, stop=90.0, side=Side.BUY, day=PLAN_DAY):
    run.ensure_schema(con)
    store.set_setting(con, "last_postclose_through", day)
    return run._plan(con, day, sym, side, qty, strategy, stop if side == Side.BUY else None, ref, "test", "")


def hold(con, broker, sym, strategy="breakout", qty=50, stop=90.0, price=None):
    price = price if price is not None else broker.prices[sym]
    broker.prices[sym] = price
    cid = f"seed-{sym}"
    broker.submit(OrderIntent(cid, sym, AssetClass.EQUITY, Side.BUY, qty=qty, strategy=strategy))
    Ledger(con).apply_fill(client_order_id=cid, symbol=sym, strategy=strategy, side=Side.BUY,
                           qty=qty, price=price, stop=stop)
    run.ensure_schema(con)
    con.execute("INSERT INTO position_meta (symbol, highest_close, initial_stop, entry_date) VALUES (?,?,?,?)",
                (sym, price, stop, "2026-09-01"))


def stops(broker, sym):
    return [o for o in broker.orders.values()
            if o.symbol == sym and o.order_type == OrderType.STOP and o.status in OPEN_STATES]


def market_orders(broker, sym, side):
    return [o for o in broker.orders.values()
            if o.symbol == sym and o.side == side and o.order_type == OrderType.MARKET
            and not o.client_order_id.startswith("seed-")]


def sig(sym, strategy, score, ref=100.0, stop=90.0, weight=None):
    return Signal(strategy, sym, pd.Timestamp(PLAN_DAY), ref, stop, score, "t", "i", target_weight=weight)


# ------------------------------------------------ 1. duplicate symbol
def test_plan_entries_never_plans_one_symbol_twice(desk):
    with store.connect(desk.path) as con:
        run.ensure_schema(con)
        risk = RiskEngine(desk.limits, con)
        ctx = SimpleNamespace(market=SimpleNamespace(exposure_cap=1.0))
        out = run._plan_entries(con, PLAN_DAY, risk.ledger, risk, desk.broker.account(), ctx,
                                [sig("NVDA", "breakout", 2.0), sig("NVDA", "rotation", 50.0, weight=0.1)], set())
        assert [p["symbol"] for p in out] == ["NVDA"]
        n = con.execute("SELECT COUNT(*) FROM planned_orders WHERE symbol='NVDA'").fetchone()[0]
        assert n == 1


@pytest.mark.parametrize("status", ["planned", run.ADVISORY])
def test_slot_skips_unique_per_symbol_session_and_report_week(desk, monkeypatch, status):
    monkeypatch.setattr(run, "MAX_POSITIONS", 1)
    with store.connect(desk.path) as con:
        run.ensure_schema(con)
        risk = RiskEngine(desk.limits, con)
        ctx = SimpleNamespace(market=SimpleNamespace(exposure_cap=1.0))
        signals = [sig("AAPL", "breakout", 3), sig("NVDA", "breakout", 2),
                   sig("NVDA", "pullback", 1), sig("MSFT", "breakout", 0, stop=math.nan)]
        for _ in range(2):
            out = run._plan_entries(con, PLAN_DAY, risk.ledger, risk, desk.broker.account(), ctx,
                                    signals, set(), status=status)
            assert [p["symbol"] for p in out] == ["AAPL"]
        assert con.execute("SELECT symbol FROM slot_skips").fetchall()[0][0] == "NVDA"
        assert "skipped for slots: 1 this week" in run._slot_skip_report(con, PLAN_DAY, status)
        con.executemany("INSERT INTO slot_skips VALUES (?,?,?,?)", [
            ("2026-09-07", "AMD", run._PROFILE.name, status),
            ("2026-09-04", "AMD", run._PROFILE.name, status),  # prior week
            ("2026-09-09", "AMD", run._PROFILE.name, status),  # future
            (PLAN_DAY, "AMD", "paper_wide", status),  # other profile
            (PLAN_DAY, "AMD", run._PROFILE.name, "other"),  # other mode
        ])
        assert "skipped for slots: 2 this week" in run._slot_skip_report(con, PLAN_DAY, status)
        assert "skipped for slots: 0 this week" in run._slot_skip_report(con, "2026-09-14", status)
        assert not desk.broker.orders


def test_premarket_refuses_second_buy_for_same_symbol(desk):
    with store.connect(desk.path) as con:
        plan(con, "NVDA", "breakout")
        plan(con, "NVDA", "rotation")
        run.premarket(con, desk.broker, WED, desk.limits)
        assert len(market_orders(desk.broker, "NVDA", Side.BUY)) == 1


def test_sync_fills_isolates_a_bad_row(desk, sent):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "NVDA", "breakout")
        run._submit(con, b, OrderIntent("x-rot", "NVDA", AssetClass.EQUITY, Side.BUY, qty=10, strategy="rotation"),
                    "entry", 80.0)
        run._submit(con, b, OrderIntent("x-aapl", "AAPL", AssetClass.EQUITY, Side.BUY, qty=10, strategy="breakout"),
                    "entry", 90.0)
        run.sync_fills(con, b, Ledger(con))
        assert Ledger(con).position("AAPL") is not None
        assert store.get_setting(con, "halt_new_entries") == "1"
        assert any(p == run.notify.CRITICAL for _, _, p in sent)


# ------------------------------------------------ 2. premarket atomicity
def test_premarket_submit_failure_keeps_earlier_orders_booked(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        plan(con, "AAPL", "breakout")
        plan(con, "MSFT", "pullback")
    real = b.submit

    def flaky(intent):
        if intent.symbol == "MSFT":
            raise RuntimeError("broker down")
        return real(intent)
    b.submit = flaky
    try:
        with store.connect(desk.path) as con:
            run.premarket(con, b, WED, desk.limits)
    except RuntimeError:
        pass
    with store.connect(desk.path) as con:
        assert con.execute("SELECT 1 FROM submitted WHERE symbol='AAPL'").fetchone()
        row = con.execute("SELECT status, note FROM planned_orders WHERE symbol='MSFT'").fetchone()
        assert row["status"] == "error" and "broker down" in row["note"]


# ------------------------------------------------ 3. stop filled during exit
def test_exit_does_not_sell_when_stop_filled_during_cancel(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", qty=50, stop=90.0)
        risk = RiskEngine(desk.limits, con)
        run.ensure_stops(con, b, risk.ledger, risk, b.account(), PLAN_DAY)
        assert len(stops(b, "AAPL")) == 1
        plan(con, "AAPL", side=Side.SELL, qty=50)
        real_cancel = b.cancel

        def cancel(cid):
            b.tick("AAPL", 89.0)  # the stop fills a moment before our cancel lands
            real_cancel(cid)
        b.cancel = cancel
        run.premarket(con, b, WED, desk.limits)
        assert market_orders(b, "AAPL", Side.SELL) == []
        assert Ledger(con).position("AAPL") is None


# ------------------------------------------------ 4. mismatch keeps exits and stops
def test_mismatch_on_one_symbol_still_places_stops_for_clean_ones(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL")
        hold(con, b, "MSFT")
        b.positions["MSFT"].qty += 3
        out = run.postopen(con, b, datetime(2026, 9, 9, 9, 50, tzinfo=run.ET), desk.limits)
        assert "對帳不一致" in out
        assert store.get_setting(con, "halt_new_entries") == "1"
        assert len(stops(b, "AAPL")) == 1
        assert stops(b, "MSFT") == []


def test_mismatch_on_one_symbol_still_submits_exits_for_clean_ones(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL")
        hold(con, b, "MSFT")
        b.positions["MSFT"].qty += 3
        plan(con, "AAPL", side=Side.SELL, qty=50)
        plan(con, "NVDA", "breakout")
        run.premarket(con, b, WED, desk.limits)
        assert len(market_orders(b, "AAPL", Side.SELL)) == 1
        assert market_orders(b, "NVDA", Side.BUY) == []


# ------------------------------------------------ 5. job timeout
class _HangingClient:
    """get_account blocks; like urllib3/requests it converts any Exception
    raised inside the blocking read into requests.ReadTimeout."""

    def __init__(self):
        self.calls = 0

    def get_account(self):
        self.calls += 1
        try:
            time.sleep(3)
        except Exception as e:
            raise requests.ReadTimeout("read timed out") from e
        raise requests.ReadTimeout("read timed out")


def test_job_timeout_is_not_swallowed_by_broker_retry(desk, monkeypatch, tmp_path, sent):
    from stockmoney.desk.execution.alpaca import AlpacaBroker
    client = _HangingClient()
    monkeypatch.setattr(run, "make_broker",
                        lambda: AlpacaBroker(client, environ={}, max_attempts=2, sleep=lambda s: None))
    monkeypatch.setattr(run, "JOB_TIMEOUT_S", 1)
    monkeypatch.setattr(run, "LOCK_PATH", tmp_path / "run.lock", raising=False)
    real_connect = store.connect
    monkeypatch.setattr(store, "connect", lambda path=None: real_connect(desk.path))
    t0 = time.monotonic()
    assert run.main(["run", "postopen"]) == 1
    assert client.calls == 1
    assert time.monotonic() - t0 < 2.5
    assert any(p == run.notify.CRITICAL for _, _, p in sent)


# ------------------------------------------------ 6. stop replacement waits for the cancel
def test_stop_replacement_waits_for_async_cancel(desk, monkeypatch):
    b = desk.broker
    monkeypatch.setattr(run, "CANCEL_WAIT_S", 0, raising=False)
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", stop=90.0)
        risk = RiskEngine(desk.limits, con)
        run.ensure_stops(con, b, risk.ledger, risk, b.account(), PLAN_DAY)
        risk.ledger.set_stop("AAPL", 95.0)

        def pending_cancel(cid):  # Alpaca: pending_cancel, the shares stay reserved
            b.orders[cid].status = OrderStatus.PENDING
        b.cancel = pending_cancel
        run.ensure_stops(con, b, risk.ledger, risk, b.account(), PLAN_DAY)
        assert len(stops(b, "AAPL")) == 1  # never two working stops for one position
        for o in stops(b, "AAPL"):
            o.status = OrderStatus.CANCELED  # the cancel settles
        run.ensure_stops(con, b, risk.ledger, risk, b.account(), PLAN_DAY)
        live = stops(b, "AAPL")
        assert len(live) == 1 and live[0].stop_price == 95.0


# ------------------------------------------------ 7. one job at a time
def test_main_refuses_to_run_while_another_job_holds_the_lock(desk, monkeypatch, tmp_path):
    lock = tmp_path / "run.lock"
    monkeypatch.setattr(run, "LOCK_PATH", lock, raising=False)
    monkeypatch.setattr(run, "LOCK_WAIT_S", 0.3, raising=False)
    real_connect = store.connect
    monkeypatch.setattr(store, "connect", lambda path=None: real_connect(desk.path))
    made = []
    monkeypatch.setattr(run, "make_broker", lambda: made.append(1) or desk.broker)
    with open(lock, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        rc = run.main(["run", "postopen"])
    assert rc != 0 and made == []


# ------------------------------------------------ 8. stale plans
def test_plans_from_before_the_last_session_are_expired(desk):
    with store.connect(desk.path) as con:
        plan(con, "AAPL")
        run.premarket(con, desk.broker, datetime(2026, 9, 10, 8, 45, tzinfo=run.ET), desk.limits)  # Thursday
        assert market_orders(desk.broker, "AAPL", Side.BUY) == []
        assert con.execute("SELECT status FROM planned_orders").fetchone()[0] == "expired"


def test_fresh_plans_are_submitted(desk):
    with store.connect(desk.path) as con:
        plan(con, "AAPL")
        run.premarket(con, desk.broker, WED, desk.limits)
        assert len(market_orders(desk.broker, "AAPL", Side.BUY)) == 1


# ------------------------------------------------ 9. cumulative exposure
def test_exposure_caps_accumulate_within_one_run(desk, tmp_path):
    limits = Limits(halt_file=tmp_path / "HALT", max_gross_exposure_pct=0.3)  # 15k
    with store.connect(desk.path) as con:
        plan(con, "AAPL", "breakout", qty=100)
        plan(con, "MSFT", "pullback", qty=100)
        run.premarket(con, desk.broker, WED, limits)
        qty = {s: sum(o.qty for o in market_orders(desk.broker, s, Side.BUY)) for s in ("AAPL", "MSFT")}
        assert qty == {"AAPL": 100, "MSFT": 50}


# ------------------------------------------------ 10. live price check
def test_market_buy_blocked_when_live_price_far_from_plan(desk):
    desk.broker.prices["AAPL"] = 120.0
    with store.connect(desk.path) as con:
        plan(con, "AAPL", ref=100.0)
        run.premarket(con, desk.broker, WED, desk.limits)
        assert market_orders(desk.broker, "AAPL", Side.BUY) == []
        assert con.execute("SELECT status FROM planned_orders").fetchone()[0] == "blocked"


def test_market_buy_sized_against_live_price(desk):
    desk.broker.prices["AAPL"] = 105.0
    with store.connect(desk.path) as con:
        plan(con, "AAPL", ref=100.0, qty=50)
        run.premarket(con, desk.broker, WED, desk.limits)
        assert [o.qty for o in market_orders(desk.broker, "AAPL", Side.BUY)] == [47]


# ------------------------------------------------ 11. late entries get a stop
def test_late_premarket_places_stops_for_its_fills(desk):
    with store.connect(desk.path) as con:
        plan(con, "AAPL", stop=90.0)
        run.premarket(con, desk.broker, datetime(2026, 9, 9, 9, 40, tzinfo=run.ET), desk.limits)
        live = stops(desk.broker, "AAPL")
        assert len(live) == 1 and live[0].stop_price == 90.0


# ------------------------------------------------ 12. untracked stop
def test_untracked_stop_is_adopted_not_duplicated(desk):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL", stop=90.0)
        b.submit(OrderIntent("orphan", "AAPL", AssetClass.EQUITY, Side.SELL, qty=50, order_type=OrderType.STOP,
                             tif=TIF.GTC, stop_price=90.0, strategy="breakout"))
        risk = RiskEngine(desk.limits, con)
        run.ensure_stops(con, b, risk.ledger, risk, b.account(), PLAN_DAY)
        assert [o.client_order_id for o in stops(b, "AAPL")] == ["orphan"]
        meta = con.execute("SELECT stop_order_id FROM position_meta WHERE symbol='AAPL'").fetchone()
        assert meta[0] == "orphan"
        assert con.execute("SELECT 1 FROM submitted WHERE client_order_id='orphan'").fetchone()


# ------------------------------------------------ 15. repeated halts notify every day
def test_halt_is_notified_again_on_a_later_day(desk, sent):
    b = desk.broker
    with store.connect(desk.path) as con:
        hold(con, b, "AAPL")
        b.positions["AAPL"].qty += 3
        run.postopen(con, b, datetime(2026, 9, 9, 9, 50, tzinfo=run.ET), desk.limits)
        run.postopen(con, b, datetime(2026, 9, 10, 9, 50, tzinfo=run.ET), desk.limits)
    assert sum(1 for t, _, p in sent if p == run.notify.CRITICAL) == 2


# ------------------------------------------------ 16. client order ids
def test_client_order_ids_do_not_collide_across_strategies():
    a = run._cid(PLAN_DAY, "b", "NVDA", "rotation")
    b = run._cid(PLAN_DAY, "b", "NVDA", "rotation_v2")
    assert a != b and len(a) <= 128 and len(b) <= 128
    assert a == run._cid(PLAN_DAY, "b", "NVDA", "rotation")  # deterministic


# ------------------------------------------------ H1. fresh clock after the poll
def test_veto_received_during_premarket_is_honoured(desk, monkeypatch):
    from stockmoney.desk.bridge.mailbox import FakeMailbox
    from stockmoney.desk.bridge.outbox import build_subject
    token = "t" * 32
    for k, v in (("DESK_MAIL_ADDRESS", "desk@example.com"), ("DESK_MAIL_APP_PASSWORD", "x"),
                 ("MUSE_ADDRESS", "muse@example.com"), ("MUSE_SHARED_TOKEN", token)):
        monkeypatch.setenv(k, v)
    real_now = datetime.now(timezone.utc)
    box = FakeMailbox()
    body = ('{"msg_id":"alert0002","sent_at":"%s","type":"ALERT","ticker":"AAPL","severity":"critical",'
            '"headline":"probe","source_urls":["https://example.com/x"],"observed_at":"%s"}'
            % (real_now.isoformat(), (real_now - timedelta(hours=1)).isoformat()))
    box.inject("muse@example.com", build_subject("ALERT", "alert0002", token), body)
    with store.connect(desk.path) as con:
        plan(con, "AAPL")
        run.premarket(con, desk.broker, WED, desk.limits, mailbox=box)  # job clock is earlier than the poll
        assert market_orders(desk.broker, "AAPL", Side.BUY) == []
        assert con.execute("SELECT status FROM planned_orders").fetchone()[0] == "blocked"


# ------------------------------------------------ H3. planning failure keeps stop bookkeeping
def _bars_and_broker():
    bars = synthetic_bars(n=560, seed=7)
    t = bars.dates[-1]
    b = FakeBroker(cash=100_000, prices={s: float(v) for s, v in bars.close.loc[t].items()})
    return bars, t, b


def _at(day: pd.Timestamp, h: int, m: int) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=run.ET)


def test_planning_failure_does_not_lose_stop_bookkeeping(desk, monkeypatch):
    bars, t, b = _bars_and_broker()
    with store.connect(desk.path) as con:
        px = b.prices["AAPL"]
        hold(con, b, "AAPL", stop=round(px * 0.9, 2))

    def boom(*a, **k):
        raise RuntimeError("scan failed")
    monkeypatch.setattr(run.engine, "scan", boom)
    try:
        with store.connect(desk.path) as con:
            run.postclose(con, b, _at(t, 17, 0), bars=bars, limits=desk.limits)
    except RuntimeError:
        pass
    assert len(stops(b, "AAPL")) == 1
    with store.connect(desk.path) as con:
        assert con.execute("SELECT 1 FROM submitted WHERE symbol='AAPL' AND kind='stop'").fetchone()


def test_nan_stop_signal_is_never_planned(desk):
    with store.connect(desk.path) as con:
        run.ensure_schema(con)
        risk = RiskEngine(desk.limits, con)
        ctx = SimpleNamespace(market=SimpleNamespace(exposure_cap=1.0))
        signals = [sig("NVDA", "rotation", 1.0, stop=math.nan, weight=0.1),
                   sig("AAPL", "breakout", 1.0, stop=math.nan)]
        out = run._plan_entries(con, PLAN_DAY, risk.ledger, risk, desk.broker.account(), ctx, signals, set())
        assert out == []


# ------------------------------------------------ M2. live sizing caps
def test_plan_entries_caps_position_at_20_percent(desk):
    with store.connect(desk.path) as con:
        run.ensure_schema(con)
        risk = RiskEngine(desk.limits, con)
        ctx = SimpleNamespace(market=SimpleNamespace(exposure_cap=1.0))
        out = run._plan_entries(con, PLAN_DAY, risk.ledger, risk, desk.broker.account(), ctx,
                                [sig("NVDA", "rotation", 1.0, weight=0.5)], set())
        assert [p["qty"] for p in out] == [100]  # 20% of 50k at 100


# ------------------------------------------------ bad stored strategy parameters
@pytest.mark.parametrize("value,needs_validation", [("abc", False), (100_000, True)])
def test_bad_stored_strategy_param_falls_back_to_defaults(desk, sent, value, needs_validation):
    from stockmoney.desk.strategies import Strategy
    if needs_validation and not hasattr(Strategy, "validate_params"):
        pytest.skip("out-of-range values are only rejected once Strategy.validate_params exists")
    bars, t, b = _bars_and_broker()
    with store.connect(desk.path) as con:
        store.change_param(con, "breakout", "max_hold_days", value, "test: stale setting")
        out = run.postclose(con, b, _at(t, 17, 0), bars=bars, limits=desk.limits)
        assert "規劃新進場失敗" not in out and "跳過" not in out
        assert con.execute("SELECT 1 FROM equity_history WHERE date=?", (str(t.date()),)).fetchone()
    assert any("breakout" in title and p == run.notify.WARN for title, _, p in sent)


# ------------------------------------------------ LOW a. postclose after midnight
def test_postclose_after_midnight_still_manages_the_last_session(desk):
    bars, t, b = _bars_and_broker()
    with store.connect(desk.path) as con:
        nxt = t + pd.Timedelta(days=1)
        out = run.postclose(con, b, _at(nxt, 0, 30), bars=bars, limits=desk.limits)
        assert "跳過" not in out
        assert con.execute("SELECT 1 FROM equity_history WHERE date=?", (str(t.date()),)).fetchone()


def test_bad_paper_settings_retain_plan_until_repaired(desk, sent):
    with store.connect(desk.path) as con:
        plan(con, "AAPL")
        store.set_setting(con, "paper_strategy_params", "[]")
        run.premarket(con, desk.broker, WED, desk.limits)
        assert not market_orders(desk.broker, "AAPL", Side.BUY)
        assert con.execute("SELECT status FROM planned_orders").fetchone()[0] == "planned"
        # every buy held back by paper settings still pages the owner, once per day
        run.premarket(con, desk.broker, WED, desk.limits)
        pages = [(t, m) for t, m, p in sent if p == run.notify.WARN and "paper" in t.lower()]
        assert len(pages) == 1 and "AAPL" in pages[0][1]
        store.set_setting(con, "paper_strategy_params", "{}")
        run.premarket(con, desk.broker, WED + timedelta(minutes=1), desk.limits)
        assert len(market_orders(desk.broker, "AAPL", Side.BUY)) == 1


@pytest.mark.parametrize("owner", ['{"breakout": null}', '{"breakout": []}', '{"breakout": 3}'])
def test_non_object_owner_entry_with_paper_overlay_keeps_postclose_running(desk, sent, owner):
    bars, t, b = _bars_and_broker()
    with store.connect(desk.path) as con:
        store.set_setting(con, "strategy_params", owner)
        store.set_setting(con, "paper_strategy_params", '{"breakout": {"max_hold_days": 10}}')
        out = run.postclose(con, b, _at(t, 17, 0), bars=bars, limits=desk.limits)
        assert con.execute("SELECT 1 FROM equity_history WHERE date=?", (str(t.date()),)).fetchone()
        assert "paper 策略參數覆寫無效" in out and "股票策略只給建議" not in out
    assert any("paper" in title.lower() and p == run.notify.WARN for title, _, p in sent)


def test_invalid_overlay_postclose_rerun_keeps_planned_rows(desk, sent, monkeypatch):
    bars, t, b = _bars_and_broker()
    called = []
    real = run._plan_entries
    monkeypatch.setattr(run, "_plan_entries", lambda *a, **kw: called.append(kw) or real(*a, **kw))
    with store.connect(desk.path) as con:
        plan(con, "AAPL", day=str(t.date()))
        store.set_setting(con, "paper_strategy_params", "[]")
        run.postclose(con, b, _at(t, 17, 0), bars=bars, limits=desk.limits)
        run.postclose(con, b, _at(t, 18, 0), bars=bars, limits=desk.limits)  # same-evening rerun
        assert called == []  # neither planned nor advisory rows are written
        assert con.execute("SELECT status FROM planned_orders WHERE symbol='AAPL'").fetchone()[0] == "planned"
