import numpy as np
import pandas as pd
import pytest

from stockmoney.desk import backtest, engine
from stockmoney.desk.broker import Account, Order
from stockmoney.desk.data import Bars
from stockmoney.desk.indicators import compute
from stockmoney.desk.strategies import REGISTRY
from stockmoney.desk.universe import ALL_SYMBOLS


def synthetic_bars(n: int = 600, seed: int = 1) -> Bars:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-01-01", periods=n)
    drift = rng.normal(0.0004, 0.0003, len(ALL_SYMBOLS))
    rets = rng.normal(drift, 0.015, (n, len(ALL_SYMBOLS)))
    close = pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=ALL_SYMBOLS)
    open_ = close.shift(1).fillna(close) * (1 + rng.normal(0, 0.003, close.shape))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, close.shape)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, close.shape)))
    vol = pd.DataFrame(rng.lognormal(15, 0.4, close.shape), index=idx, columns=ALL_SYMBOLS)
    return Bars(open_, high, low, close, vol)


@pytest.fixture(scope="module")
def bars():
    return synthetic_bars()


def test_indicators_have_no_lookahead(bars):
    full = compute(bars)
    for t in bars.dates[[260, 400, 599]]:
        trunc = compute(bars.upto(t))
        for name in ("sma200", "ema20", "atr20", "prior_high55", "mom_12_1", "rs63_vs_spy", "vol_ratio", "low10"):
            a = getattr(full, name).loc[t]
            b = getattr(trunc, name).loc[t]
            pd.testing.assert_series_equal(a, b, check_names=False, rtol=1e-9)


def test_signals_identical_on_truncated_history(bars):
    t = bars.dates[450]
    strats = {n: c() for n, c in REGISTRY.items() if n != "rotation"}  # rotation peeks at calendar only
    s_full = engine.scan(strats, engine.day_context(compute(bars), t))
    s_trunc = engine.scan(strats, engine.day_context(compute(bars.upto(t)), t))
    assert [(s.symbol, s.strategy, round(s.stop, 6)) for s in s_full] == [
        (s.symbol, s.strategy, round(s.stop, 6)) for s in s_trunc
    ]


def _bar(**v):
    return pd.Series(v, dtype=float)


def test_entry_fills_next_open_and_gap_stop_fills_at_open():
    a = Account("t", 10_000, 10_000)
    a.orders.append(Order("o1", "AAA", "buy", "breakout", "2024-01-01", shares=10, stop=90))
    d1, d2 = pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")
    a.process_open(d1, _bar(AAA=100), _bar(AAA=99))
    assert a.positions["AAA"].entry_price == pytest.approx(100 * 1.0005)
    a.process_open(d2, _bar(AAA=85), _bar(AAA=84))  # gaps through the 90 stop
    assert "AAA" not in a.positions
    assert a.trades[0]["exit_price"] == pytest.approx(85 * 0.9995)


def test_entry_skipped_when_open_below_stop():
    a = Account("t", 10_000, 10_000)
    a.orders.append(Order("o1", "AAA", "buy", "breakout", "2024-01-01", shares=10, stop=90))
    a.process_open(pd.Timestamp("2024-01-02"), _bar(AAA=89), _bar(AAA=88))
    assert not a.positions and a.cash == 10_000


def test_unapproved_orders_expire():
    a = Account("t", 10_000, 10_000)
    a.orders.append(Order("o1", "AAA", "buy", "breakout", "2024-01-01", shares=10, stop=90, status="pending_approval"))
    a.process_open(pd.Timestamp("2024-01-02"), _bar(AAA=100), _bar(AAA=99))
    assert not a.positions and not a.orders


def test_sizing_respects_risk_and_caps():
    a = Account("t", 100_000, 100_000, risk_per_trade=0.01, max_position_pct=0.2)
    # risk 1% of 100k = 1000 / (100-95) = 200 shares = 20k -> exactly at the 20% cap
    assert a.size(100_000, 100, 95, 1.0, 0, None) == 200
    assert a.size(100_000, 100, 99, 1.0, 0, None) == 200  # capped by max_position_pct
    assert a.size(100_000, 100, 95, 0.5, 45_000, None) == 50  # exposure cap leaves 5k
    assert a.size(100_000, 100, 95, 0.0, 0, None) == 0


def test_account_roundtrip_serialisation(bars):
    res = backtest.run(bars, str(bars.dates[300].date()), strategy_sets={"combined": list(REGISTRY)})
    acct = res["accounts"]["combined"]
    again = Account.from_dict(acct.to_dict())
    assert again.to_dict() == acct.to_dict()


def test_stepwise_replay_matches_single_pass(bars):
    """The live runner processes one day at a time from persisted state; that
    must equal the backtest's single pass (no hidden state in memory)."""
    start = str(bars.dates[300].date())
    single = backtest.run(bars, start, strategy_sets={"combined": list(REGISTRY)})["accounts"]["combined"]
    ind = compute(bars)
    strats = {n: c() for n, c in REGISTRY.items()}
    acct = Account("combined", 25_000, 25_000)
    for t in bars.dates[bars.dates >= start]:
        acct = Account.from_dict(acct.to_dict())  # simulate a fresh process each night
        ctx = engine.day_context(ind, t)
        engine.morning(acct, bars, t)
        engine.evening(acct, strats, ctx, engine.scan(strats, ctx))
    assert acct.trades == single.trades
    assert acct.equity_curve[-1] == single.equity_curve[-1]
