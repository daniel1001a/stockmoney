"""Leakage canaries across the three places module A could peek at the future:

1. Feature computation (adx_14 / xsec_dispersion / realized_vol_20d): does
   appending FUTURE raw OHLCV rows change an already-computed HISTORICAL
   feature value? It must not — each function walks the series forward and
   should only ever use data up to and including the target date.
2. Regime detection: covered in test_regime.py (filtered vs smoothed HMM
   states) — the analogous canary for the clustering/HMM layer.
3. Walk-forward split: covered in test_walk_forward.py (purge/embargo
   structural checks + corruption-based causal proof that pure-OOS rows never
   influence training).

An earlier version of this file tried a single end-to-end canary using a
feature defined as `label + noise`. That construction is same-row target
leakage (the feature IS the label), which no train/test time-split can catch
by design — purge/embargo governs *which rows* enter training, not *how a
row's own feature was computed*. That is a data-pipeline correctness concern,
which is what test (1) below actually exercises.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import duckdb
import polars as pl

from stockmoney.data.db import append_rows, run_migrations
from stockmoney.data.features.adx import compute_adx_14
from stockmoney.data.features.dispersion import SECTOR_MEMBERS, compute_xsec_dispersion
from stockmoney.data.features.realized_vol import compute_realized_vol_20d

SYMBOL = "SOXL"
CUTOFF_DAYS = 60   # history available at "decision time"
FUTURE_DAYS = 30   # additional rows appended in the "if we could see the future" run


def _ohlcv_df(symbol: str, closes: list[float], start: date) -> pl.DataFrame:
    n = len(closes)
    dates = [start + timedelta(days=i) for i in range(n)]
    ingested = [datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc) for d in dates]
    return pl.DataFrame(
        {
            "symbol": [symbol] * n,
            "trade_date": dates,
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "source": ["test"] * n,
            "ingested_at": ingested,
        }
    )


def _random_walk(n: int, seed: int) -> list[float]:
    import random

    rng = random.Random(seed)
    price = 100.0
    out = []
    for _ in range(n):
        price *= 1.0 + rng.uniform(-0.02, 0.02)
        out.append(price)
    return out


def _historical_and_future_conns():
    start = date(2026, 1, 1)
    closes = _random_walk(CUTOFF_DAYS + FUTURE_DAYS, seed=42)

    conn_hist = duckdb.connect(":memory:")
    run_migrations(conn_hist)
    append_rows(conn_hist, "ohlcv_daily", _ohlcv_df(SYMBOL, closes[:CUTOFF_DAYS], start))
    for member in SECTOR_MEMBERS["semiconductor"]:
        append_rows(conn_hist, "ohlcv_daily", _ohlcv_df(member, closes[:CUTOFF_DAYS], start))

    conn_full = duckdb.connect(":memory:")
    run_migrations(conn_full)
    append_rows(conn_full, "ohlcv_daily", _ohlcv_df(SYMBOL, closes, start))
    for member in SECTOR_MEMBERS["semiconductor"]:
        append_rows(conn_full, "ohlcv_daily", _ohlcv_df(member, closes, start))

    return conn_hist, conn_full


def _values_up_to_cutoff(conn, feature_name: str, symbol: str, cutoff: date) -> list:
    return conn.execute(
        "SELECT feature_date, feature_value FROM feature_store "
        "WHERE feature_name = ? AND symbol = ? AND feature_date <= ? "
        "ORDER BY feature_date",
        [feature_name, symbol, cutoff],
    ).fetchall()


def test_adx_14_unaffected_by_future_ohlcv_rows():
    conn_hist, conn_full = _historical_and_future_conns()
    cutoff = date(2026, 1, 1) + timedelta(days=CUTOFF_DAYS - 1)

    compute_adx_14(conn_hist, symbols=[SYMBOL])
    compute_adx_14(conn_full, symbols=[SYMBOL])

    hist_vals = _values_up_to_cutoff(conn_hist, "adx_14", SYMBOL, cutoff)
    full_vals = _values_up_to_cutoff(conn_full, "adx_14", SYMBOL, cutoff)
    assert len(hist_vals) > 0
    assert hist_vals == full_vals


def test_realized_vol_20d_unaffected_by_future_ohlcv_rows():
    conn_hist, conn_full = _historical_and_future_conns()
    cutoff = date(2026, 1, 1) + timedelta(days=CUTOFF_DAYS - 1)

    compute_realized_vol_20d(conn_hist)
    compute_realized_vol_20d(conn_full)

    hist_vals = _values_up_to_cutoff(conn_hist, "realized_vol_20d", SYMBOL, cutoff)
    full_vals = _values_up_to_cutoff(conn_full, "realized_vol_20d", SYMBOL, cutoff)
    assert len(hist_vals) > 0
    assert hist_vals == full_vals


def test_xsec_dispersion_unaffected_by_future_ohlcv_rows():
    from stockmoney.data.db import sector_symbol

    conn_hist, conn_full = _historical_and_future_conns()
    cutoff = date(2026, 1, 1) + timedelta(days=CUTOFF_DAYS - 1)
    group_symbol = sector_symbol("semiconductor")

    compute_xsec_dispersion(conn_hist, "semiconductor")
    compute_xsec_dispersion(conn_full, "semiconductor")

    hist_vals = _values_up_to_cutoff(conn_hist, "xsec_dispersion", group_symbol, cutoff)
    full_vals = _values_up_to_cutoff(conn_full, "xsec_dispersion", group_symbol, cutoff)
    assert len(hist_vals) > 0
    assert hist_vals == full_vals
