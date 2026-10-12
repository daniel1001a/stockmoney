"""The options sleeve shares the broker account with the stock book. These
tests prove the stock jobs never see, count, cancel, close or halt on option
positions and option orders, and that stock behaviour is unchanged by them."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from stockmoney.desk import run, store
from stockmoney.desk.execution.broker import FakeBroker
from stockmoney.desk.execution.isolation import (
    OptionOrderProtected, StockOnlyBroker, is_option, stock_only, without_options,
)
from stockmoney.desk.execution.ledger import Ledger
from stockmoney.desk.execution.reconcile import mismatches, reconcile
from stockmoney.desk.execution.risk import Limits, RiskEngine
from stockmoney.desk.execution.types import (
    TIF, AccountSnapshot, AssetClass, BrokerOrder, BrokerPosition, OptionContract, OrderIntent, OrderStatus,
    OrderType, Side,
)

from test_desk import synthetic_bars

OCC_HELD = "NVDA261218C00200000"
OCC_HELD2 = "SPY261218P00500000"
OCC_WORKING = "AAPL261218C00250000"


@pytest.fixture(autouse=True)
def no_push(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    for k in ("DESK_MAIL_ADDRESS", "DESK_MAIL_APP_PASSWORD", "MUSE_ADDRESS", "MUSE_SHARED_TOKEN"):
        monkeypatch.delenv(k, raising=False)


def _at(day: pd.Timestamp, h: int, m: int) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=run.ET)


def contract(occ: str, underlying: str, right: str, strike: float, bid=2.0, ask=2.1) -> OptionContract:
    return OptionContract(occ, underlying, right, strike, date(2026, 12, 18), bid=bid, ask=ask, last=2.05,
                          volume=1000, open_interest=5000, delta=0.5, iv=0.4,
                          as_of=datetime.now(timezone.utc))


def seed_options(broker: FakeBroker) -> list[str]:
    """Two held option positions, one working option buy, one working option
    sell-to-close limit, and two option STOP sells placed by hand in the
    account (one without an asset class: detected by its OCC shape)."""
    broker.set_option(contract(OCC_HELD, "NVDA", "C", 200))
    broker.set_option(contract(OCC_HELD2, "SPY", "P", 500, bid=3.0, ask=3.2))
    broker.set_option(contract(OCC_WORKING, "AAPL", "C", 250, bid=1.0, ask=1.1))
    for cid, occ, qty in (("opt-b1", OCC_HELD, 3), ("opt-b2", OCC_HELD2, 2)):
        o = broker.submit(OrderIntent(cid, occ, AssetClass.OPTION, Side.BUY, qty=qty, strategy="opt"))
        assert o.status == OrderStatus.FILLED
    w1 = broker.submit(OrderIntent("opt-w1", OCC_WORKING, AssetClass.OPTION, Side.BUY, qty=1,
                                   order_type=OrderType.LIMIT, limit_price=0.5, strategy="opt"))
    w2 = broker.submit(OrderIntent("opt-w2", OCC_HELD, AssetClass.OPTION, Side.SELL, qty=1,
                                   order_type=OrderType.LIMIT, limit_price=9.0, strategy="opt"))
    assert w1.status == OrderStatus.NEW and w2.status == OrderStatus.NEW
    now = datetime.now(timezone.utc)
    for cid, ac in (("opt-stop-a", AssetClass.OPTION), ("opt-stop-b", None)):
        broker.orders[cid] = BrokerOrder(
            f"x-{cid}", cid, OCC_HELD2, Side.SELL, OrderStatus.NEW, 1.0, 0.0, None, now, None,
            OrderType.STOP, TIF.GTC, 1.0, None, ac)
    return ["opt-w1", "opt-w2", "opt-stop-a", "opt-stop-b"]


def option_state(broker: FakeBroker):
    pos = {s: (p.qty, p.avg_entry_price) for s, p in broker.positions.items() if p.asset_class == AssetClass.OPTION}
    orders = {c: (o.status, o.filled_qty) for c, o in broker.orders.items() if is_option(o)}
    return pos, orders


def _cycle(tmp_path, with_options: bool, n_days: int = 60):
    bars = synthetic_bars(n=560, seed=7)
    broker = FakeBroker(cash=100_000)
    limits = Limits(halt_file=tmp_path / "HALT")
    working = seed_options(broker) if with_options else []
    before = option_state(broker)
    days = bars.dates[-n_days:]
    with store.connect(tmp_path / "desk.sqlite") as con:
        for i, t in enumerate(days):
            if i > 0:
                broker.prices.update({s: float(v) for s, v in bars.open.loc[t].items()})
                run.premarket(con, broker, _at(t, 8, 45), limits)
                run.postopen(con, broker, _at(t, 9, 50), limits)
                for s, v in bars.low.loc[t].items():
                    broker.tick(s, float(v))
            broker.prices.update({s: float(v) for s, v in bars.close.loc[t].items()})
            run.postclose(con, broker, _at(t, 17, 0), bars=bars.upto(t), limits=limits)
            assert reconcile(Ledger(con), broker.account()) == [], t
            assert store.get_setting(con, "halt_new_entries", "0") == "0", t
        t = days[-1]
        run.recover(con, broker, _at(t, 18, 0), limits)
        run.crypto(con, broker, _at(t, 20, 0))
        assert store.get_setting(con, "halt_new_entries", "0") == "0"
        fills = [tuple(r) for r in con.execute(
            "SELECT client_order_id, symbol, side, qty, price, realized_pnl FROM fills ORDER BY id").fetchall()]
        submitted = [tuple(r) for r in con.execute(
            "SELECT client_order_id, kind, symbol, side, qty, stop, status FROM submitted "
            "ORDER BY client_order_id").fetchall()]
        planned = [tuple(r) for r in con.execute(
            "SELECT client_order_id, symbol, side, qty, status FROM planned_orders ORDER BY client_order_id").fetchall()]
        ledger = {s: (p.qty, p.avg_price, p.stop) for s, p in Ledger(con).positions().items()}
        notes = [r[0] for r in con.execute("SELECT key FROM notified").fetchall()]
    stock_orders = {c: (o.symbol, o.side, o.status, o.qty, o.filled_qty, o.order_type, o.stop_price)
                    for c, o in broker.orders.items() if not is_option(o)}
    return dict(fills=fills, submitted=submitted, planned=planned, ledger=ledger, stock_orders=stock_orders,
                before=before, after=option_state(broker), working=working, notes=notes)


def test_stock_cycle_ignores_options_and_is_unchanged(tmp_path):
    base = _cycle(tmp_path / "base", with_options=False)
    mixed = _cycle(tmp_path / "mixed", with_options=True)
    assert base["fills"], "the stock book must actually trade in this window"
    # the option book is exactly as it was seeded: nothing sold, closed or cancelled
    assert mixed["after"] == mixed["before"]
    for cid in mixed["working"]:
        assert mixed["after"][1][cid][0] == OrderStatus.NEW, cid
    # and the stock book behaved exactly as without any options in the account
    for k in ("fills", "submitted", "planned", "ledger", "stock_orders"):
        assert mixed[k] == base[k], k
    assert not any("orphanstops" in n or "halt" in n for n in mixed["notes"]), mixed["notes"]


def test_stock_only_view_filters_positions_and_orders():
    b = FakeBroker(cash=100_000, prices={"AAPL": 100.0})
    b.submit(OrderIntent("s1", "AAPL", AssetClass.EQUITY, Side.BUY, qty=10))
    seed_options(b)
    v = stock_only(b)
    assert stock_only(v) is v
    assert [p.symbol for p in v.account().positions] == ["AAPL"]
    assert len(b.account().positions) == 3
    assert v.account().equity == b.account().equity  # account-wide figures untouched
    assert v.open_orders() == []
    assert len(b.open_orders()) == 4
    assert v.name == b.name and v.is_paper  # passthrough
    assert v.inner is b


def test_stock_only_view_refuses_to_cancel_option_orders():
    b = FakeBroker(cash=100_000, prices={"AAPL": 100.0})
    b.submit(OrderIntent("s1", "AAPL", AssetClass.EQUITY, Side.BUY, qty=10))
    b.submit(OrderIntent("stop1", "AAPL", AssetClass.EQUITY, Side.SELL, qty=10, order_type=OrderType.STOP,
                         tif=TIF.GTC, stop_price=90.0))
    seed_options(b)
    v = StockOnlyBroker(b)
    with pytest.raises(OptionOrderProtected):
        v.cancel("opt-w1")
    with pytest.raises(OptionOrderProtected):
        v.cancel("opt-stop-b")  # no asset class, recognised by its OCC symbol
    v.cancel_all()
    assert b.orders["stop1"].status == OrderStatus.CANCELED
    assert all(b.orders[c].status == OrderStatus.NEW for c in ("opt-w1", "opt-w2", "opt-stop-a", "opt-stop-b"))


def test_reconcile_ignores_option_positions(con):
    snap = AccountSnapshot(100_000, 50_000, 50_000, [
        BrokerPosition(OCC_HELD, AssetClass.OPTION, 3, 2.1, 630.0, 2.1),
        BrokerPosition(OCC_HELD2, AssetClass.EQUITY, 2, 3.2, 640.0, 3.2),  # mis-tagged: caught by OCC shape
    ])
    assert mismatches(Ledger(con), snap) == {}
    assert without_options(snap).positions == []


def test_stock_risk_blocks_option_intents_and_ignores_option_exposure(con, tmp_path):
    risk = RiskEngine(Limits(halt_file=tmp_path / "HALT"), con)
    opt = BrokerPosition(OCC_HELD, AssetClass.OPTION, 1000, 20.0, 2_000_000.0, 20.0)  # absurd premium
    snap = AccountSnapshot(100_000, 50_000, 50_000, [opt])
    d = risk.check(OrderIntent("o1", OCC_HELD, AssetClass.OPTION, Side.BUY, qty=1), snap, ref_price=2.0,
                   orders_this_run=0)
    assert not d.allow and "options sleeve" in d.reason
    d = risk.check(OrderIntent("o2", OCC_HELD, AssetClass.OPTION, Side.SELL, qty=1), snap, ref_price=2.0,
                   orders_this_run=0)
    assert not d.allow
    # a stock buy is sized as if the option did not exist (no gross-exposure cut)
    stock = OrderIntent("b1", "AAPL", AssetClass.EQUITY, Side.BUY, qty=10)
    assert risk.check(stock, snap, ref_price=100.0, orders_this_run=0).reason == "ok"
    assert risk.system_equity(snap) == pytest.approx(50_000)


def test_sweep_and_ensure_stops_called_directly_leave_options_alone(con, tmp_path):
    b = FakeBroker(cash=100_000, prices={"AAPL": 100.0})
    seed_options(b)
    before = option_state(b)
    risk = RiskEngine(Limits(halt_file=tmp_path / "HALT"), con)
    run.ensure_schema(con)
    events = run.sweep_orphan_stops(con, b, risk.ledger, "2026-10-13")
    events += run.ensure_stops(con, b, risk.ledger, risk, b.account(), "2026-10-13")
    assert events == []
    assert option_state(b) == before


def test_option_position_does_not_block_stock_buy_of_same_underlying(tmp_path):
    """An open NVDA option order must not count as a conflicting NVDA order."""
    b = FakeBroker(cash=100_000, prices={"NVDA": 100.0})
    seed_options(b)
    with store.connect(tmp_path / "desk.sqlite") as con:
        run.ensure_schema(con)
        assert run._order_conflict(con, stock_only(b), "NVDA", "cid") is None


def test_premarket_with_only_options_held_changes_nothing(tmp_path):
    b = FakeBroker(cash=100_000)
    seed_options(b)
    before = option_state(b)
    day = pd.Timestamp("2026-10-13")
    with store.connect(tmp_path / "desk.sqlite") as con:
        out = run.premarket(con, b, _at(day, 8, 45), Limits(halt_file=tmp_path / "HALT"))
        assert "對帳" not in out
        run.postopen(con, b, _at(day, 9, 50), Limits(halt_file=tmp_path / "HALT"))
        run.recover(con, b, _at(day, 10, 0), Limits(halt_file=tmp_path / "HALT"))
        assert store.get_setting(con, "halt_new_entries", "0") == "0"
    assert option_state(b) == before
