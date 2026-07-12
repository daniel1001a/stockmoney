"""Recompute the core model-layer features (realized_vol_20d, adx_14, rsi_14,
volume_zscore_20d, macro features, cross-sectional dispersion per sector, and
the options-microstructure candidates gex_estimate/skew_25delta(_chg_1d)/
put_call_ratio) from whatever's currently in ohlcv_daily/macro_series_daily/
options_derived_daily/put_call_ratio_daily.

Safe to run every night: `write_features` (see
stockmoney.data.features.base) dedupes on (feature_date, symbol,
feature_name, feature_version) before inserting, so re-running this over the
full history only ever adds genuinely new rows -- it never re-derives or
duplicates a value that's already there. Before this script existed, nothing
called these compute_* functions on a schedule at all, so `feature_store`
silently lagged `ohlcv_daily` by however long it had been since the last
manual run. `scripts/nightly_refresh.py` calls `run_feature_recompute`
directly (on the same connection, after ingestion) so this stays wired into
the automated nightly path too, not just available as a manual script.

Usage:
    uv run python scripts/compute_features.py
"""
from __future__ import annotations

import duckdb

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations
from stockmoney.data.features.adx import compute_adx_14
from stockmoney.data.features.dispersion import SECTOR_MEMBERS, compute_xsec_dispersion
from stockmoney.data.features.gdelt_sentiment import compute_gdelt_sentiment
from stockmoney.data.features.macro import compute_macro_features
from stockmoney.data.features.options_derived import (
    compute_gex_feature,
    compute_put_call_ratio_feature,
    compute_skew_feature,
)
from stockmoney.data.features.realized_vol import compute_realized_vol_20d
from stockmoney.data.features.rsi import compute_rsi_14
from stockmoney.data.features.volume import compute_volume_zscore_20d


def run_feature_recompute(conn: duckdb.DuckDBPyConnection) -> None:
    for label, fn in [
        ("realized_vol_20d", compute_realized_vol_20d),
        ("adx_14", compute_adx_14),
        ("rsi_14", compute_rsi_14),
        ("volume_zscore_20d", compute_volume_zscore_20d),
        ("macro features", compute_macro_features),
        ("gdelt sentiment", compute_gdelt_sentiment),
        ("gex_estimate", compute_gex_feature),
        ("skew_25delta (+chg_1d)", compute_skew_feature),
        ("put_call_ratio", compute_put_call_ratio_feature),
    ]:
        try:
            n = fn(conn)
            print(f"  {label}: +{n} rows")
        except Exception as exc:
            print(f"  {label} FAILED: {exc}")

    for sector in SECTOR_MEMBERS:
        try:
            n = compute_xsec_dispersion(conn, sector)
            print(f"  xsec_dispersion[{sector}]: +{n} rows")
        except Exception as exc:
            print(f"  xsec_dispersion[{sector}] FAILED: {exc}")


def main(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    run_migrations(conn)
    try:
        run_feature_recompute(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
