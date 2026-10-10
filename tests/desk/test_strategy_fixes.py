"""Regression tests for the review findings M1 (month-end calendar), M4
(parameter bounds) and the NaN-stop guard. Offline, synthetic data only."""
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from stockmoney.desk import engine
from stockmoney.desk.calendar import (
    is_last_trading_day_of_month,
    is_trading_day,
    next_trading_day,
    nyse_holidays,
)
from stockmoney.desk.data import Bars
from stockmoney.desk.indicators import compute
from stockmoney.desk.strategies import REGISTRY, Breakout, Context, Pullback, Rotation
from stockmoney.desk.universe import ALL_SYMBOLS

CACHE = Path("data/desk/ohlcv.parquet")


def nyse_index(start: str, end: str) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([d for d in pd.date_range(start, end) if is_trading_day(d)])


def synthetic_nyse_bars(start="2014-06-02", end="2016-12-30", seed=7) -> Bars:
    """Like tests/desk/test_desk.synthetic_bars but on real NYSE sessions and
    with enough drift that all three strategies actually fire."""
    idx = nyse_index(start, end)
    rng = np.random.default_rng(seed)
    k = len(ALL_SYMBOLS)
    rets = rng.normal(rng.normal(0.0007, 0.0004, k), 0.015, (len(idx), k))
    close = pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=ALL_SYMBOLS)
    open_ = close.shift(1).fillna(close) * (1 + rng.normal(0, 0.003, close.shape))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, close.shape)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, close.shape)))
    vol = pd.DataFrame(rng.lognormal(15, 0.4, close.shape), index=idx, columns=ALL_SYMBOLS)
    return Bars(open_, high, low, close, vol)


@pytest.fixture(scope="module")
def bars():
    return synthetic_nyse_bars()


@pytest.fixture(scope="module")
def ind(bars):
    return compute(bars)


# ------------------------------------------------------------------ calendar

@pytest.mark.parametrize("d", ["2018-03-29", "2021-05-28", "2024-03-28", "2027-05-28"])
def test_month_end_before_exchange_holiday_is_month_end(d):
    """BDay-based logic missed these: the next weekday is a holiday in the
    same month, so the 'next business day' looked like a mid-month day."""
    assert is_last_trading_day_of_month(date.fromisoformat(d))


@pytest.mark.parametrize(
    "d",
    [
        "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19",
        "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
        "2023-01-02",   # Jan 1 on a Sunday -> Monday
        "2022-06-20",   # Juneteenth on a Sunday, first year observed
        "2022-12-26",   # Christmas on a Sunday
        "2021-07-05",   # July 4 on a Sunday
        "2015-04-03",   # Good Friday
        "2026-07-03",   # July 4 on a Saturday -> Friday
        "2027-12-24",   # Christmas on a Saturday -> Friday
        "2027-06-18",   # Juneteenth on a Saturday -> Friday
        "2025-01-09",   # national day of mourning
    ],
)
def test_known_nyse_holidays(d):
    assert not is_trading_day(date.fromisoformat(d))


@pytest.mark.parametrize(
    "d",
    [
        "2021-06-18",   # Juneteenth not yet a market holiday in 2021
        "2021-12-31",   # Jan 1 2022 is a Saturday: no Friday closure
        "2027-12-31",   # same rule for 2028
        "2024-03-28", "2024-07-05", "2024-11-29",
    ],
)
def test_known_nyse_open_days(d):
    assert is_trading_day(date.fromisoformat(d))


def test_holiday_counts_and_navigation():
    assert len(nyse_holidays(2024)) == 10
    assert len(nyse_holidays(2019)) == 9  # no Juneteenth before 2022
    assert next_trading_day(date(2024, 3, 28)) == date(2024, 4, 1)
    assert next_trading_day(pd.Timestamp("2024-12-31")) == date(2025, 1, 2)
    assert is_last_trading_day_of_month(pd.Timestamp("2021-12-31"))
    assert not is_last_trading_day_of_month(date(2024, 3, 27))


def test_every_month_has_exactly_one_last_trading_day():
    idx = nyse_index("2015-01-01", "2026-12-31")
    flags = pd.Series([is_last_trading_day_of_month(d) for d in idx], index=idx)
    assert flags.groupby([idx.year, idx.month]).sum().eq(1).all()
    # and it is the last session actually present in the month
    assert list(flags[flags].index) == list(idx.to_series().groupby([idx.year, idx.month]).max())


# ------------------------------------------------------------------ rotation parity

def _ctx(index: pd.DatetimeIndex, t: pd.Timestamp) -> Context:
    close = pd.DataFrame({"SPY": 1.0}, index=index)
    return Context(t, SimpleNamespace(close=close), None, None)  # type: ignore[arg-type]


def _parity(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """Live decision (history ends at t) must equal the backtest's (full
    data, next row visible) for every month-end; returns the month-ends."""
    month_ends = index.to_series().groupby([index.year, index.month]).max()
    all_ends = set(month_ends)
    month_ends = month_ends[(month_ends.index.get_level_values(0) >= 2015) & (month_ends.index.get_level_values(0) <= 2026)]
    # the final row of a data set has no next row, so it is not a checkable month-end
    month_ends = month_ends[[index.get_loc(t) + 1 < len(index) for t in month_ends]]
    for t in month_ends:
        i = index.get_loc(t)
        backtest = index[i + 1].month != t.month
        live = Rotation._rebalance_day(_ctx(index[: i + 1], t))
        full = Rotation._rebalance_day(_ctx(index, t))
        assert live == backtest == full, f"live/backtest disagree on {t.date()}"
        assert live, f"{t.date()} is the month's last session but not a rebalance day"
    # and no mid-month day triggers
    for t in index[::7]:
        if t not in all_ends:
            assert not Rotation._rebalance_day(_ctx(index[: index.get_loc(t) + 1], t))
    return list(month_ends)


def test_rotation_rebalance_live_equals_backtest_synthetic():
    ends = _parity(nyse_index("2014-12-01", "2026-12-31"))
    assert pd.Timestamp("2018-03-29") in ends and pd.Timestamp("2024-03-28") in ends


@pytest.mark.skipif(not CACHE.exists(), reason="no desk price cache on this machine")
def test_rotation_rebalance_live_equals_backtest_on_price_cache():
    close = pd.read_parquet(CACHE, columns=["symbol", "date", "close"])
    index = pd.DatetimeIndex(sorted(close["date"].unique()))
    _parity(index)


# ------------------------------------------------------------------ truncated history

def _first_signal_days(strat, ind, dates, k=3):
    out = []
    for t in dates[260:]:
        if strat.candidates(engine.day_context(ind, t)):
            out.append(t)
            if len(out) == k:
                break
    return out


@pytest.mark.parametrize("name", ["breakout", "rotation", "pullback"])
def test_signals_identical_on_truncated_history_on_signalling_days(bars, ind, name):
    """The existing leak test picks one day that may carry no signal at all.
    Here the days are searched for, so the comparison is never vacuous."""
    strat = REGISTRY[name]()
    days = _first_signal_days(strat, ind, bars.dates)
    assert len(days) == 3, f"synthetic data produced no {name} signals"
    for t in days:
        full = strat.candidates(engine.day_context(ind, t))
        trunc_bars = bars.upto(t)
        trunc = strat.candidates(engine.day_context(compute(trunc_bars), t))
        assert full and [(s.symbol, round(s.stop, 6), round(s.score, 9)) for s in full] == [
            (s.symbol, round(s.stop, 6), round(s.score, 9)) for s in trunc
        ]


# ------------------------------------------------------------------ NaN stop guard

@pytest.mark.parametrize("name", ["breakout", "rotation", "pullback"])
def test_candidates_skip_symbols_with_unusable_stop(bars, ind, name):
    strat = REGISTRY[name]()
    t = _first_signal_days(strat, ind, bars.dates, k=1)[0]
    clean = strat.candidates(engine.day_context(ind, t))
    victim = clean[0].symbol
    bad = SimpleNamespace(**{f: getattr(ind, f).copy() for f in ind.__dataclass_fields__})
    bad.atr20.at[t, victim] = np.nan
    got = strat.candidates(engine.day_context(bad, t))
    assert victim not in {s.symbol for s in got}
    assert all(np.isfinite(s.stop) and s.stop > 0 and np.isfinite(s.ref_price) for s in got)
    # negative stop (ATR larger than the price) is skipped too
    bad.atr20.at[t, victim] = 1e9
    assert victim not in {s.symbol for s in strat.candidates(engine.day_context(bad, t))}


# ------------------------------------------------------------------ parameter bounds

def test_every_parameter_has_a_declared_bound():
    for cls in REGISTRY.values():
        assert set(cls.bounds) == set(cls.params), cls.name
        cls()  # defaults satisfy their own bounds


def test_unknown_parameter_key_rejected():
    with pytest.raises(ValueError, match="unknown parameter"):
        Breakout(stop_atrr=2.0)
    with pytest.raises(ValueError, match="unknown parameter"):
        Rotation(max_hold_days=30)  # real param of other strategies, not of this one


@pytest.mark.parametrize(
    "cls,kw",
    [
        (Pullback, {"rs_quantile": 0.0}), (Pullback, {"rs_quantile": 1.0}), (Pullback, {"rs_quantile": -0.1}),
        (Breakout, {"stop_atr": 0.5}), (Breakout, {"stop_atr": 0.0}), (Breakout, {"stop_atr": 10.01}),
        (Breakout, {"trail_atr": -3}), (Pullback, {"trail_atr": 50}), (Pullback, {"max_stop_atr": 0}),
        (Breakout, {"min_vol_ratio": 0.49}), (Breakout, {"min_vol_ratio": 11}),
        (Breakout, {"max_hold_days": 4}), (Breakout, {"max_hold_days": 401}),
        (Rotation, {"top_n": 0}), (Rotation, {"top_n": 21, "keep_rank": 30}),
        (Rotation, {"keep_rank": 4}),            # < default top_n 5
        (Rotation, {"top_n": 12}),               # > default keep_rank 10
        (Breakout, {"stop_atr": float("nan")}), (Breakout, {"stop_atr": float("inf")}),
        (Breakout, {"stop_atr": "2.5"}),
    ],
)
def test_out_of_range_parameters_rejected(cls, kw):
    with pytest.raises(ValueError):
        cls(**kw)


@pytest.mark.parametrize("cls,kw", [(Breakout, {"max_hold_days": 60.5}), (Rotation, {"top_n": 4.7})])
def test_non_integral_int_parameter_is_an_error_not_a_truncation(cls, kw):
    with pytest.raises(ValueError, match="whole number"):
        cls(**kw)


def test_valid_parameters_accepted_and_integer_floats_become_ints():
    b = Breakout(stop_atr=0.51, trail_atr=10, min_vol_ratio=0.5, max_hold_days=60.0)
    assert b.params["max_hold_days"] == 60 and isinstance(b.params["max_hold_days"], int)
    r = Rotation(top_n=7.0, keep_rank=7)  # keep_rank == top_n is allowed
    assert r.params["top_n"] == 7 and isinstance(r.params["top_n"], int)
    assert Pullback(rs_quantile=0.999).params["rs_quantile"] == 0.999
    assert Rotation.validate_params({"top_n": 3.0})["top_n"] == 3


def test_breakout_description_states_it_trades_in_neutral_regime():
    assert "中性" in Breakout.description and "空頭" in Breakout.description


def test_docstrings_point_at_tests_that_exist():
    import re

    from stockmoney.desk import backtest, indicators

    for mod in (indicators, backtest):
        paths = re.findall(r"tests/desk/\w+\.py", mod.__doc__)
        assert paths, mod.__name__
        assert all(Path(p).exists() for p in paths), (mod.__name__, paths)
