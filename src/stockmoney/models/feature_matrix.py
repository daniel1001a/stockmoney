"""Assemble the regime/direction modelling matrix from feature_store.

Produces one row per target trading day: the three regime-observation features
known as of that day's close, plus a 5-day-forward 3-class direction label
(down / range / up). The label span (`label_end_date`) is carried on every row
so the walk-forward harness can purge training samples whose label window
overlaps the test window.

Look-ahead discipline here:
- Feature values for date d are computed only from data with feature_date <= d,
  and each carries an `available_at` (≈ d's close). `available_at` is exposed so
  the harness can enforce that a decision at date d only uses features with
  available_at <= d. (Today's features are same-day-available; when lagged
  features like macro revisions are added, this column keeps the alignment
  honest.)
- The label is the *target*, deliberately forward-looking, and is never fed back
  in as a feature.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

import duckdb
import numpy as np
import polars as pl

from stockmoney.data.db import MARKET_SYMBOL, sector_symbol

FEATURE_COLUMNS = [
    "realized_vol_20d", "adx_14", "xsec_dispersion",
    "yield_curve_10y2y", "dxy_chg_1d_ffill", "oil_chg_1d_ffill",
]
# CLAUDE.md section 4 (regime detection): "觀測特徵:已實現波動率、趨勢強度(如
# ADX)、跨股離散度指標" -- regime clustering (regime.py) may observe ONLY these
# 3 columns. The other 3 FEATURE_COLUMNS (yield_curve_10y2y, dxy_chg_1d_ffill,
# oil_chg_1d_ffill) are macro features for the per-regime direction model
# (direction.py) only -- see Dataset.X vs Dataset.regime_X below.
REGIME_COLUMNS = ["realized_vol_20d", "adx_14", "xsec_dispersion"]
assert all(c in FEATURE_COLUMNS for c in REGIME_COLUMNS)
# rsi_14 / volume_zscore_20d are computed and stored in feature_store (see
# scripts/compute_features.py) but deliberately NOT included above: a paired
# bootstrap comparison (backtest_feature_ablation.py) found they significantly
# WORSEN OOS Brier score (95% CI [+0.0027, +0.0231], fully positive), so per
# CLAUDE.md section 12 they stay in the feature store for later re-testing
# rather than being promoted into the production feature set.
#
# dxy_chg_1d_ffill/oil_chg_1d_ffill (not dxy_chg_1d/oil_chg_1d): DTWEXBGS in
# particular can go 5+ trading days between FRED prints (a real publication-
# lag characteristic, not an ingestion bug -- see stockmoney.data.features.
# macro's module docstring), which stalled "today" for every symbol under
# the raw feature. The _ffill variants forward-fill the raw level onto every
# trading day before differencing, so momentum reads 0 on no-print days
# instead of blocking. Identical values on days without a print gap (the
# vast majority of history); this is a pipeline-robustness fix to an
# already-approved feature, not a new signal requiring re-testing.
TRADING_DAYS = 252

# Label classes.
DOWN, RANGE, UP = 0, 1, 2


@dataclass
class Dataset:
    X: np.ndarray             # (n, 6) feature matrix, column order = FEATURE_COLUMNS
    regime_X: np.ndarray       # (n, 3) regime-observation subset, column order = REGIME_COLUMNS
    y: np.ndarray              # (n,) int labels in {DOWN, RANGE, UP}
    trade_dates: list[date]
    available_at: list[datetime]
    label_end_dates: list[date]
    fwd_return: np.ndarray


def build_feature_matrix(
    conn: duckdb.DuckDBPyConnection,
    *,
    target_symbol: str = "SOXL",
    sector: str = "semiconductor",
    horizon: int = 5,
    band_k: float = 0.5,
) -> pl.DataFrame:
    feats = _load_features(conn, target_symbol, sector)
    closes = _load_closes(conn, target_symbol)  # sorted [(date, close)]
    close_idx = {d: i for i, (d, _) in enumerate(closes)}

    rows = []
    for d, i in close_idx.items():
        if d not in feats:
            continue
        f = feats[d]
        if any(f.get(c) is None for c in FEATURE_COLUMNS):
            continue
        if i + horizon >= len(closes):  # no full forward window yet
            continue

        c0 = closes[i][1]
        cN = closes[i + horizon][1]
        if not c0 or c0 <= 0:
            continue
        fwd_return = cN / c0 - 1.0

        daily_vol = f["realized_vol_20d"] / math.sqrt(TRADING_DAYS)
        band = band_k * daily_vol * math.sqrt(horizon)
        if fwd_return > band:
            label = UP
        elif fwd_return < -band:
            label = DOWN
        else:
            label = RANGE

        row = {
            "trade_date": d,
            "available_at": max(f["_available_at"][c] for c in FEATURE_COLUMNS),
            "fwd_return": fwd_return,
            "label": label,
            "label_end_date": closes[i + horizon][0],
        }
        row.update({c: f[c] for c in FEATURE_COLUMNS})
        rows.append(row)

    if not rows:
        schema = {
            "trade_date": pl.Date, "available_at": pl.Datetime,
            "fwd_return": pl.Float64, "label": pl.Int64, "label_end_date": pl.Date,
        } | {c: pl.Float64 for c in FEATURE_COLUMNS}
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows).sort("trade_date")


def latest_unresolved_feature_rows(
    conn: duckdb.DuckDBPyConnection,
    *,
    target_symbol: str = "SOXL",
    sector: str = "semiconductor",
    horizon: int = 5,
    n: int = 1,
) -> pl.DataFrame:
    """The rows `build_feature_matrix` discards: complete features but no
    resolved forward-return label yet (production inference's live input).

    The cutoff here is the exact logical negation of build_feature_matrix's
    `if i + horizon >= len(closes): continue` -- these two functions must stay
    exhaustive and disjoint over trade_date (see
    tests/models/test_feature_matrix.py's disjointness test). This is why
    _load_features/_load_closes are reused verbatim rather than
    reimplemented: any divergence between training-time and inference-time
    feature computation is itself a look-ahead-bias risk, independent of the
    cutoff logic.

    Columns: trade_date, available_at, + FEATURE_COLUMNS. No fwd_return/label/
    label_end_date -- those aren't knowable yet by construction.
    """
    feats = _load_features(conn, target_symbol, sector)
    closes = _load_closes(conn, target_symbol)
    close_idx = {d: i for i, (d, _) in enumerate(closes)}

    rows = []
    for d, i in close_idx.items():
        if d not in feats:
            continue
        f = feats[d]
        if any(f.get(c) is None for c in FEATURE_COLUMNS):
            continue
        if i + horizon < len(closes):  # has a resolved label already
            continue

        row = {
            "trade_date": d,
            "available_at": max(f["_available_at"][c] for c in FEATURE_COLUMNS),
        }
        row.update({c: f[c] for c in FEATURE_COLUMNS})
        rows.append(row)

    schema = {"trade_date": pl.Date, "available_at": pl.Datetime} | {c: pl.Float64 for c in FEATURE_COLUMNS}
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows).sort("trade_date").tail(n)


def to_dataset(matrix: pl.DataFrame) -> Dataset:
    return Dataset(
        X=matrix.select(FEATURE_COLUMNS).to_numpy(),
        regime_X=matrix.select(REGIME_COLUMNS).to_numpy(),
        y=matrix["label"].to_numpy(),
        trade_dates=matrix["trade_date"].to_list(),
        available_at=matrix["available_at"].to_list(),
        label_end_dates=matrix["label_end_date"].to_list(),
        fwd_return=matrix["fwd_return"].to_numpy(),
    )


def _load_features(
    conn: duckdb.DuckDBPyConnection, target_symbol: str, sector: str
) -> dict[date, dict]:
    symbol_by_feature = {
        "realized_vol_20d": target_symbol,
        "adx_14": target_symbol,
        "xsec_dispersion": sector_symbol(sector),
        "yield_curve_10y2y": MARKET_SYMBOL,
        "dxy_chg_1d_ffill": MARKET_SYMBOL,
        "oil_chg_1d_ffill": MARKET_SYMBOL,
        "rsi_14": target_symbol,
        "volume_zscore_20d": target_symbol,
        # Candidates, not promoted (not in FEATURE_COLUMNS) -- loaded here so
        # backtest_feature_ablation.py can test them, same as rsi_14/
        # volume_zscore_20d above.
        "gdelt_avgtone_1d": MARKET_SYMBOL,
        "gdelt_goldstein_1d": MARKET_SYMBOL,
    }
    out: dict[date, dict] = {}
    for feature_name, symbol in symbol_by_feature.items():
        for feature_date, value, available_at in conn.execute(
            """
            SELECT feature_date, feature_value, available_at
            FROM feature_store
            WHERE feature_name = ? AND symbol = ?
            """,
            [feature_name, symbol],
        ).fetchall():
            slot = out.setdefault(feature_date, {"_available_at": {}})
            slot[feature_name] = value
            slot["_available_at"][feature_name] = available_at
    return out


def _load_closes(
    conn: duckdb.DuckDBPyConnection, target_symbol: str
) -> list[tuple[date, float]]:
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT trade_date, close,
                   row_number() OVER (
                       PARTITION BY trade_date ORDER BY ingested_at DESC
                   ) AS rn
            FROM ohlcv_daily
            WHERE symbol = ? AND close IS NOT NULL
        )
        SELECT trade_date, close FROM latest WHERE rn = 1 ORDER BY trade_date
        """,
        [target_symbol],
    ).fetchall()
    return [(d, c) for d, c in rows]
