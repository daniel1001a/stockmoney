"""Regression tests for execution-layer defects found in review (risk,
reconcile, FakeBroker fidelity, Alpaca adapter, engine ranking). Offline only."""
from __future__ import annotations

import pandas as pd
import pytest
import requests

from stockmoney.desk import engine
from stockmoney.desk.broker import Account
from stockmoney.desk.execution import alpaca as al
from stockmoney.desk.execution.broker import FakeBroker
from stockmoney.desk.execution.ledger import norm
from stockmoney.desk.execution.reconcile import reconcile
from stockmoney.desk.execution.risk import HWM_KEY, Limits, RiskEngine
from stockmoney.desk.execution.types import (
    TIF, AccountSnapshot, AssetClass, BrokerPosition, OrderIntent, OrderStatus, OrderType, Side,
)
from stockmoney.desk.indicators import compute
from stockmoney.desk.strategies import Signal

from desk_helpers import buy
from test_desk import synthetic_bars


# ------------------------------------------------ 13. crypto dust
def test_crypto_dust_on_one_side_is_not_a_mismatch(ledger):
    dust = BrokerPosition("BTC/USD", AssetClass.CRYPTO, 5e-7, 50_000, 0.025, 50_000)
    assert reconcile(ledger, AccountSnapshot(1, 1, 1, [dust])) == []
    ledger.apply_fill(client_order_id="d", symbol="ETH/USD", strategy="crypto", side=Side.BUY, qty=5e-7, price=2_000)
    assert reconcile(ledger, AccountSnapshot(1, 1, 1, [])) == []


def test_equity_one_sided_is_still_exact(ledger):
    tiny = BrokerPosition("AAPL", AssetClass.EQUITY, 5e-7, 100, 0.00005, 100)
    assert len(reconcile(ledger, AccountSnapshot(1, 1, 1, [tiny]))) == 1


# ------------------------------------------------ 14. high-water mark follows the budget
def test_hwm_resets_when_capital_budget_changes(con, halt_file, broker):
    e50 = RiskEngine(Limits(halt_file=halt_file), con)
    e50.high_water_mark(60_000)
    assert e50.high_water_mark() == pytest.approx(60_000)
    e30 = RiskEngine(Limits(halt_file=halt_file, capital_budget=30_000), con)
    assert e30.high_water_mark() == pytest.approx(30_000)
    assert e30.mode(broker.account())[0] == "normal"


def test_hwm_legacy_value_kept_when_budget_unchanged(con, halt_file, ledger):
    ledger.set_state(HWM_KEY, repr(55_000.0))  # written by the previous version, no budget recorded
    assert RiskEngine(Limits(halt_file=halt_file), con).high_water_mark() == pytest.approx(55_000)


# ------------------------------------------------ 9. pending exposure within a run
def test_reserved_notional_counts_toward_caps(make_engine, broker):
    e = make_engine(max_gross_exposure_pct=0.3)  # 15k
    d1 = e.check(buy("AAPL", qty=100), broker.account(), 100.0, 0)
    assert d1.allow and d1.intent.qty == 100
    e.reserve(d1.intent, 100.0)
    d2 = e.check(buy("MSFT", qty=100, cid="c2"), broker.account(), 100.0, 1)
    assert d2.allow and d2.intent.qty == 50 and "gross" in d2.reason


def test_buy_notional_uses_live_price_when_given(make_engine, broker):
    e = make_engine(max_order_notional_pct=0.1)  # 5k
    d = e.check(buy(qty=50), broker.account(), 100.0, 0, market_price=105.0)
    assert d.allow and d.intent.qty == 47  # 5,000 / 105


# ------------------------------------------------ FakeBroker fidelity
def test_fake_account_returns_copies(broker, hold):
    hold("AAPL", "breakout", 10)
    snap = broker.account()
    snap.positions[0].qty = 999
    assert broker.positions["AAPL"].qty == 10


def test_fake_async_cancel_keeps_shares_reserved_until_settle():
    b = FakeBroker(cash=50_000, prices={"AAPL": 100.0}, async_cancel=True)
    b.submit(OrderIntent("seed", "AAPL", AssetClass.EQUITY, Side.BUY, qty=10))
    stop = OrderIntent("s0", "AAPL", AssetClass.EQUITY, Side.SELL, qty=10, order_type=OrderType.STOP,
                       tif=TIF.GTC, stop_price=90.0)
    b.submit(stop)
    b.cancel("s0")
    assert b.get_order("s0").status == OrderStatus.PENDING_CANCEL
    assert [o.client_order_id for o in b.open_orders()] == ["s0"]
    with pytest.raises(Exception, match="insufficient qty"):
        b.submit(OrderIntent("s1", "AAPL", AssetClass.EQUITY, Side.SELL, qty=10, order_type=OrderType.STOP,
                             tif=TIF.GTC, stop_price=95.0))
    b.settle()
    assert b.get_order("s0").status == OrderStatus.CANCELED
    assert b.submit(OrderIntent("s1", "AAPL", AssetClass.EQUITY, Side.SELL, qty=10, order_type=OrderType.STOP,
                                tif=TIF.GTC, stop_price=95.0)).status == OrderStatus.NEW


def test_ensure_stops_with_async_cancel_broker(tmp_path, monkeypatch):
    from stockmoney.desk import run, store
    from stockmoney.desk.execution.ledger import Ledger
    monkeypatch.setattr(run, "CANCEL_WAIT_S", 0)
    b = FakeBroker(cash=100_000, prices={"AAPL": 100.0}, async_cancel=True)
    with store.connect(tmp_path / "d.sqlite") as con:
        run.ensure_schema(con)
        b.submit(OrderIntent("seed", "AAPL", AssetClass.EQUITY, Side.BUY, qty=50, strategy="breakout"))
        Ledger(con).apply_fill(client_order_id="seed", symbol="AAPL", strategy="breakout", side=Side.BUY,
                               qty=50, price=100.0, stop=90.0)
        con.execute("INSERT INTO position_meta (symbol, highest_close, initial_stop, entry_date) "
                    "VALUES ('AAPL', 100, 90, '2026-09-01')")
        risk = RiskEngine(Limits(halt_file=tmp_path / "HALT"), con)
        run.ensure_stops(con, b, risk.ledger, risk, b.account(), "2026-09-08")
        risk.ledger.set_stop("AAPL", 95.0)
        ev = run.ensure_stops(con, b, risk.ledger, risk, b.account(), "2026-09-08")
        assert any("取消未確認" in e for e in ev)
        assert [o.stop_price for o in b.open_orders()] == [90.0]  # old one, pending cancel
        b.settle()
        run.ensure_stops(con, b, risk.ledger, risk, b.account(), "2026-09-08")
        assert [o.stop_price for o in b.open_orders()] == [95.0]


def test_fake_latest_price(broker):
    assert broker.latest_price("AAPL") == 100.0
    assert broker.latest_price("ZZZZ") is None


# ------------------------------------------------ 5. Alpaca HTTP timeout
def test_alpaca_session_gets_a_default_timeout(monkeypatch):
    seen = []
    monkeypatch.setattr(requests.Session, "request", lambda self, method, url, **kw: seen.append(kw))
    b = al.AlpacaBroker(environ={"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"})
    b.client._session.request("GET", "https://example.invalid/x")
    b.client._session.request("GET", "https://example.invalid/x", timeout=3)
    assert seen[0]["timeout"] == al.HTTP_TIMEOUT_S and seen[1]["timeout"] == 3


# ------------------------------------------------ LOW b. BRK-B vs BRK.B
def test_symbol_translation_for_alpaca():
    req = al.build_request(OrderIntent("c", "BRK-B", AssetClass.EQUITY, Side.BUY, qty=1))
    assert req.symbol == "BRK.B"
    assert norm("BRK.B") == norm("BRK-B")


def test_alpaca_positions_and_orders_come_back_in_universe_spelling():
    from test_alpaca import FakeClient, account_obj, make_order, position
    c = FakeClient()
    c.account_obj = account_obj()
    c.positions = [position("BRK.B", "us_equity", 3, 400.0, 410.0, 1230.0)]
    c.orders["x"] = make_order("x", symbol="BRK.B")
    b = al.AlpacaBroker(c, environ={}, sleep=lambda s: None)
    assert [p.symbol for p in b.account().positions] == ["BRK-B"]
    assert b.get_order("x").symbol == "BRK-B"


# ------------------------------------------------ B1 / ranking in the engine
@pytest.fixture(scope="module")
def ctx():
    bars = synthetic_bars(n=560, seed=7)
    return engine.day_context(compute(bars), bars.dates[-1])


def _sig(ctx, sym, strategy, score, weight=None):
    px = float(ctx.ind.close.loc[ctx.t, sym])
    return Signal(strategy, sym, ctx.t, px, px * 0.9, score, "t", "i", target_weight=weight)


def test_evening_never_queues_one_symbol_twice(ctx):
    from stockmoney.desk.strategies import REGISTRY
    acct = Account("t", 50_000, 50_000)
    strats = {n: cls() for n, cls in REGISTRY.items()}
    ctx.market.exposure_cap = 1.0
    orders = engine.evening(acct, strats, ctx, [_sig(ctx, "NVDA", "rotation", 50.0, 0.1),
                                                _sig(ctx, "NVDA", "breakout", 2.0)])
    buys = [o for o in orders if o.side == "buy"]
    assert [(o.symbol, o.strategy) for o in buys] == [("NVDA", "breakout")]


def test_rank_signals_uses_within_strategy_percentile(ctx):
    sigs = [_sig(ctx, "AAPL", "rotation", 90.0, 0.1), _sig(ctx, "MSFT", "rotation", 80.0, 0.1),
            _sig(ctx, "NVDA", "breakout", 3.0), _sig(ctx, "AMD", "breakout", 1.0),
            _sig(ctx, "AAPL", "breakout", 2.0)]
    ranked = engine.rank_signals(sigs)
    assert [(s.symbol, s.strategy) for s in ranked] == [
        ("NVDA", "breakout"), ("AAPL", "rotation"), ("MSFT", "rotation"), ("AMD", "breakout")]
