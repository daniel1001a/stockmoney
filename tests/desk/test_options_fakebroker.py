"""FakeBroker as the reference OptionsBroker: chain/quote/bars reads and the
option fill model (buy at the ask, sell at the bid, multiplier 100)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from stockmoney.desk.execution.broker import FakeBroker, InsufficientQty, OptionsBroker
from stockmoney.desk.execution.types import AssetClass, OptionContract, OrderIntent, OrderStatus, OrderType, Side

OCC = "SPY261013C00580000"


def k(occ=OCC, underlying="SPY", right="C", strike=580.0, expiry=date(2026, 10, 13), bid=1.00, ask=1.10, **kw):
    return OptionContract(occ, underlying, right, strike, expiry, bid=bid, ask=ask, **kw)


def buy(cid="b1", qty=2, **kw):
    return OrderIntent(cid, OCC, AssetClass.OPTION, Side.BUY, qty=qty, **kw)


def sell(cid="s1", qty=2, **kw):
    return OrderIntent(cid, OCC, AssetClass.OPTION, Side.SELL, qty=qty, **kw)


def test_fake_broker_satisfies_protocol():
    b: OptionsBroker = FakeBroker()
    assert callable(b.option_chain) and callable(b.minute_bars)


def test_market_buy_fills_at_ask_with_multiplier_and_sell_at_bid():
    b = FakeBroker(cash=10_000)
    b.set_option(k())
    o = b.submit(buy())
    assert o.status == OrderStatus.FILLED and o.filled_avg_price == pytest.approx(1.10)
    assert o.asset_class == AssetClass.OPTION
    assert b.cash == pytest.approx(10_000 - 2 * 1.10 * 100)
    snap = b.account()
    (p,) = snap.positions
    assert p.asset_class == AssetClass.OPTION and p.qty == 2
    assert p.current_price == pytest.approx(1.05)  # mark = mid
    assert p.market_value == pytest.approx(2 * 1.05 * 100)
    assert snap.equity == pytest.approx(b.cash + 210.0)
    b.set_option(k(bid=1.50, ask=1.60))
    o = b.submit(sell())
    assert o.status == OrderStatus.FILLED and o.filled_avg_price == pytest.approx(1.50)
    assert b.cash == pytest.approx(10_000 - 220 + 300)
    assert b.account().positions == []


def test_limit_buy_rests_until_marketable():
    b = FakeBroker(cash=10_000)
    b.set_option(k())
    o = b.submit(buy(order_type=OrderType.LIMIT, limit_price=1.00))
    assert o.status == OrderStatus.NEW
    b.set_option(k(bid=0.90, ask=0.98))
    o = b.get_order("b1")
    assert o.status == OrderStatus.FILLED and o.filled_avg_price == pytest.approx(0.98)


def test_marketable_limit_fills_at_ask_not_limit():
    b = FakeBroker(cash=10_000)
    b.set_option(k())
    o = b.submit(buy(order_type=OrderType.LIMIT, limit_price=1.30))
    assert o.status == OrderStatus.FILLED and o.filled_avg_price == pytest.approx(1.10)


def test_sell_without_position_is_refused():
    b = FakeBroker(cash=10_000)
    b.set_option(k())
    with pytest.raises(InsufficientQty):
        b.submit(sell())


def test_market_order_without_quote_is_rejected():
    b = FakeBroker(cash=10_000)
    b.set_option(k(bid=None, ask=None))
    assert b.submit(buy()).status == OrderStatus.REJECTED


def test_buy_beyond_cash_is_rejected():
    b = FakeBroker(cash=100)
    b.set_option(k())
    assert b.submit(buy(qty=1)).status == OrderStatus.REJECTED


@pytest.mark.parametrize("intent", [
    OrderIntent("x", OCC, AssetClass.OPTION, Side.BUY, qty=1.5),
    OrderIntent("x", OCC, AssetClass.OPTION, Side.BUY, notional=100.0),
    OrderIntent("x", OCC, AssetClass.OPTION, Side.BUY, qty=1, order_type=OrderType.STOP, stop_price=1.0),
])
def test_invalid_option_intents_raise(intent):
    b = FakeBroker()
    b.set_option(k())
    with pytest.raises(ValueError):
        b.submit(intent)


def test_option_chain_filters_by_underlying_and_expiry():
    b = FakeBroker()
    b.set_option(k())
    b.set_option(k(occ="SPY261016P00570000", right="P", strike=570.0, expiry=date(2026, 10, 16)))
    b.set_option(k(occ="SPY261120C00580000", expiry=date(2026, 11, 20)))
    b.set_option(k(occ="QQQ261013C00500000", underlying="QQQ", strike=500.0))
    got = b.option_chain("spy", date(2026, 10, 13), date(2026, 10, 16))
    assert [c.occ for c in got] == ["SPY261013C00580000", "SPY261016P00570000"]
    assert b.option_quote("SPY261120C00580000").expiry == date(2026, 11, 20)
    assert b.option_quote("NOPE") is None


def test_minute_bars_slice_and_empty():
    b = FakeBroker()
    idx = pd.date_range("2026-10-13 13:30", periods=10, freq="1min", tz="UTC")
    b.bars["SPY"] = pd.DataFrame({"open": range(10), "high": range(10), "low": range(10), "close": range(10),
                                  "volume": range(10)}, index=idx, dtype=float)
    df = b.minute_bars("SPY", datetime(2026, 10, 13, 13, 32, tzinfo=timezone.utc),
                       datetime(2026, 10, 13, 13, 34, tzinfo=timezone.utc))
    assert list(df["close"]) == [2.0, 3.0, 4.0]
    empty = b.minute_bars("QQQ", idx[0].to_pydatetime(), idx[-1].to_pydatetime())
    assert empty.empty and list(empty.columns) == ["open", "high", "low", "close", "volume"]
    assert str(empty.index.tz) == "UTC"


def test_stock_quote():
    b = FakeBroker()
    b.stock_quotes["SPY"] = (580.0, 580.02)
    assert b.stock_quote("SPY") == (580.0, 580.02)
    assert b.stock_quote("QQQ") is None


def test_stock_fills_unchanged_by_options_support():
    b = FakeBroker(cash=10_000, prices={"AAPL": 100.0})
    o = b.submit(OrderIntent("e1", "AAPL", AssetClass.EQUITY, Side.BUY, qty=10))
    assert o.filled_avg_price == 100.0 and b.cash == pytest.approx(9_000)
    assert b.account().positions[0].market_value == pytest.approx(1_000)
