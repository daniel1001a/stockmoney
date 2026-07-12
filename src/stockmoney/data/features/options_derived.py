"""Options-microstructure feature candidates (CLAUDE.md section 2's "選擇權
微結構" row, section 16's still-untested feature-candidate slot): promote raw
options_derived_daily/put_call_ratio_daily rows into feature_store so they're
loadable by feature_matrix._load_features (see the "Options-microstructure
candidates" comment there) for backtest_options_feature_ablation.py's
significance test.

These candidates are picked because they carry information the price-only
FEATURE_COLUMNS (realized_vol/adx/dispersion/macro) structurally cannot:
dealer positioning (gex_estimate) and options-market-implied directional
skew/hedging demand (skew_25delta_chg_1d, put_call_ratio) — as opposed to
rsi_14/volume_zscore_20d, which already failed their ablation test precisely
because they were redundant derivatives of the same price series already
driving realized_vol_20d/adx_14.

No promotion decision is made here or anywhere in this module -- these are
candidates only, following rsi_14/volume_zscore_20d's exact precedent (tested
via backtest_feature_ablation.py, which paired-bootstrap-tested them and
rejected both). CLAUDE.md section 12 requires the same discipline for these.

Unlike the candidates in this module, the VIX-term-structure gap this
docstring used to describe has since been closed: see
data/ingestion/vix_term.py (a free connector -- the constant-maturity VIX
indices, unlike per-underlying option chains, DO have full historical data)
and data/features/vix_term.py. It was tested via
models/backtest_vix_term_ablation.py and did NOT pass the significance gate
(95% CI straddled 0) -- not promoted, kept as a candidate for re-testing.

The candidates in THIS module (gex_estimate/skew_25delta_chg_1d/
put_call_ratio) remain genuinely untestable for now: yfinance option chains
are snapshot-only (fetch_options_snapshot's docstring), so
options_derived_daily/put_call_ratio_daily can only accumulate one day at a
time going forward (scripts/capture_options_snapshot.py) -- there is no way to
backfill history for them short of a paid ORATS/CBOE DataShop subscription
(CLAUDE.md section 16).
"""
from __future__ import annotations

from datetime import date, datetime

import duckdb

from stockmoney.data.features.base import FeatureValue, write_features

GEX_FEATURE_NAME = "gex_estimate"
SKEW_LEVEL_FEATURE_NAME = "skew_25delta"
SKEW_CHG_FEATURE_NAME = "skew_25delta_chg_1d"
PUT_CALL_RATIO_FEATURE_NAME = "put_call_ratio"
FEATURE_VERSION = "v1"


def _latest_derived_metric(
    conn: duckdb.DuckDBPyConnection, metric_name: str, symbols: list[str] | None
) -> dict[str, list[tuple[date, float, datetime]]]:
    """Latest-ingested_at value per (symbol, trade_date) for one
    options_derived_daily metric_name, grouped by symbol and sorted by date."""
    where = ""
    params: list = [metric_name]
    if symbols:
        placeholders = ", ".join(["?"] * len(symbols))
        where = f"AND symbol IN ({placeholders})"
        params += list(symbols)

    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT symbol, trade_date, metric_value, ingested_at,
                   row_number() OVER (
                       PARTITION BY symbol, trade_date ORDER BY ingested_at DESC
                   ) AS rn
            FROM options_derived_daily
            WHERE metric_name = ? AND metric_value IS NOT NULL {where}
        )
        SELECT symbol, trade_date, metric_value, ingested_at
        FROM latest WHERE rn = 1
        ORDER BY symbol, trade_date
        """,
        params,
    ).fetchall()

    by_symbol: dict[str, list[tuple[date, float, datetime]]] = {}
    for symbol, trade_date, value, ingested_at in rows:
        by_symbol.setdefault(symbol, []).append((trade_date, float(value), ingested_at))
    return by_symbol


def compute_gex_feature(conn: duckdb.DuckDBPyConnection, symbols: list[str] | None = None) -> int:
    """Promote options_derived_daily's gex_estimate (dealer gamma exposure
    proxy, already computed at ingestion by options_chain.py) into
    feature_store, one row per (symbol, trade_date), no further transform."""
    by_symbol = _latest_derived_metric(conn, "gex_estimate", symbols)
    values = [
        FeatureValue(feature_date=d, symbol=symbol, value=v, available_at=ia)
        for symbol, series in by_symbol.items()
        for d, v, ia in series
    ]
    return write_features(
        conn, feature_name=GEX_FEATURE_NAME, feature_version=FEATURE_VERSION,
        source_table="options_derived_daily", values=values,
    )


def compute_skew_feature(conn: duckdb.DuckDBPyConnection, symbols: list[str] | None = None) -> int:
    """Promote options_derived_daily's skew_25delta level, and its 1-day
    change, into feature_store. CLAUDE.md section 2 names the *change rate*
    ("25-delta skew 變化率") as the candidate signal -- a shifting skew
    reflects fresh hedging/positioning demand, whereas the level alone mostly
    just reflects the symbol's structural skew shape. The level is written too
    (matching macro.py's raw-level + momentum precedent) so both are available
    if the ablation test ever wants to compare them."""
    by_symbol = _latest_derived_metric(conn, "skew_25delta", symbols)
    level_values = [
        FeatureValue(feature_date=d, symbol=symbol, value=v, available_at=ia)
        for symbol, series in by_symbol.items()
        for d, v, ia in series
    ]
    total = write_features(
        conn, feature_name=SKEW_LEVEL_FEATURE_NAME, feature_version=FEATURE_VERSION,
        source_table="options_derived_daily", values=level_values,
    )

    chg_values = []
    for symbol, series in by_symbol.items():
        for i in range(1, len(series)):
            d, v, ia = series[i]
            prev_v = series[i - 1][1]
            chg_values.append(FeatureValue(feature_date=d, symbol=symbol, value=v - prev_v, available_at=ia))
    total += write_features(
        conn, feature_name=SKEW_CHG_FEATURE_NAME, feature_version=FEATURE_VERSION,
        source_table="options_derived_daily", values=chg_values,
    )
    return total


def compute_put_call_ratio_feature(conn: duckdb.DuckDBPyConnection, symbols: list[str] | None = None) -> int:
    """Volume-based put/call ratio (put_volume / call_volume) from
    put_call_ratio_daily -- volume reflects same-day positioning/flow, unlike
    open interest which accumulates and decays slowly, so it is the more
    responsive of the two ratios this table could support. Rows with
    call_volume missing or 0 are skipped (undefined ratio), not zeroed."""
    where = ""
    params: list = []
    if symbols:
        placeholders = ", ".join(["?"] * len(symbols))
        where = f"WHERE symbol IN ({placeholders})"
        params = list(symbols)

    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT symbol, trade_date, put_volume, call_volume, ingested_at,
                   row_number() OVER (
                       PARTITION BY symbol, trade_date ORDER BY ingested_at DESC
                   ) AS rn
            FROM put_call_ratio_daily
            {where}
        )
        SELECT symbol, trade_date, put_volume, call_volume, ingested_at
        FROM latest WHERE rn = 1
        ORDER BY symbol, trade_date
        """,
        params,
    ).fetchall()

    values = []
    for symbol, trade_date, put_volume, call_volume, ingested_at in rows:
        if not call_volume or put_volume is None:
            continue
        values.append(
            FeatureValue(
                feature_date=trade_date, symbol=symbol,
                value=put_volume / call_volume, available_at=ingested_at,
            )
        )
    return write_features(
        conn, feature_name=PUT_CALL_RATIO_FEATURE_NAME, feature_version=FEATURE_VERSION,
        source_table="put_call_ratio_daily", values=values,
    )
