from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import duckdb
import polars as pl

from stockmoney.data.db import append_rows


@dataclass
class FeatureValue:
    feature_date: date
    symbol: str
    value: float | None = None
    value_text: str | None = None
    available_at: datetime | None = None
    """When this value was actually knowable, e.g. the ingested_at of the raw
    row(s) it was computed from. Falls back to computed_at (now) if omitted,
    which is only correct for features with no historical raw-data lag."""


def write_features(
    conn: duckdb.DuckDBPyConnection,
    *,
    feature_name: str,
    feature_version: str,
    source_table: str,
    values: list[FeatureValue],
) -> int:
    """Write computed feature values into ``feature_store`` (append-only).

    Every feature computation job (regime labels, realized vol,
    cross-sectional dispersion, ...) should end by calling this function, so
    ``feature_store`` stays the single unified write path used by the model
    layer and ``available_at`` is never forgotten.

    ``feature_store``'s primary key is ``(feature_date, symbol, feature_name,
    feature_version)`` and rows are immutable once written (a fixed feature
    version must never change value under a model that already trained on
    it). A batch job is typically re-run over its whole lookback window on
    every cron tick, so values whose key already exists are silently skipped
    here rather than raising a constraint error — this makes repeated runs
    idempotent and only ever adds newly-computable dates.
    """
    if not values:
        return 0

    existing = {
        (row[0], row[1])
        for row in conn.execute(
            "SELECT feature_date, symbol FROM feature_store "
            "WHERE feature_name = ? AND feature_version = ?",
            [feature_name, feature_version],
        ).fetchall()
    }
    values = [v for v in values if (v.feature_date, v.symbol) not in existing]
    if not values:
        return 0

    computed_at = datetime.now(timezone.utc)
    df = pl.DataFrame(
        {
            "feature_date": [v.feature_date for v in values],
            "symbol": [v.symbol for v in values],
            "feature_name": [feature_name] * len(values),
            "feature_value": [v.value for v in values],
            "feature_value_text": [v.value_text for v in values],
            "feature_version": [feature_version] * len(values),
            "available_at": [v.available_at or computed_at for v in values],
            "computed_at": [computed_at] * len(values),
            "source_table": [source_table] * len(values),
        }
    )
    return append_rows(conn, "feature_store", df)
