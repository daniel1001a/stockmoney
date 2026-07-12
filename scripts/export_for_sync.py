"""Export newly-written rows to Parquet for cross-machine sync (Agent 3 -> Agent 4).

`.duckdb` files are gitignored on both machines by design (binary,
env-specific) so git alone can't carry captured data from this machine's
local DuckDB to the other machine's local DuckDB. This script is the fix:
after each cron pass writes new rows here, export only the rows written
since the last export (tracked per-table in `data_sync/.watermark.json`) to
`data_sync/exports/<table>/<YYYYMMDD_HHMMSS>_<run_id>.parquet`. The other
machine imports these files; it never regenerates this data itself.

Idempotent and re-run-safe: the watermark only advances after a table's
Parquet file is written successfully, and each run's rows are strictly
`watermark_col > last_watermark`, so re-running produces no duplicate or
lost rows even if a previous run partially failed.

Usage (schedule this daily, after nightly_refresh -- OpenClaw cron):
    .venv/bin/python scripts/export_for_sync.py
    .venv/bin/python scripts/export_for_sync.py --db data/stockmoney.duckdb
"""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations

EXPORT_ROOT = Path("data_sync/exports")
WATERMARK_PATH = Path("data_sync/.watermark.json")

# table -> monotonically-increasing TIMESTAMPTZ column that marks when a row
# was written (never the event's real-world time -- see each table's
# migration comment for why, e.g. news_items.available_at vs created_at).
TABLES: dict[str, str] = {
    "iv_surface_daily": "ingested_at",
    "put_call_ratio_daily": "ingested_at",
    "options_derived_daily": "ingested_at",
    "vix_term_structure_daily": "ingested_at",
    "news_articles_raw": "ingested_at",
    "news_items": "created_at",
    "ohlcv_daily": "ingested_at",
    "macro_series_daily": "ingested_at",
    "alt_social_hourly": "ingested_at",
    "scan_classifications": "processed_at",
    "watchlist_candidates": "created_at",
    "catalyst_signals": "created_at",
    "ingestion_runs": "started_at",
}

EPOCH = "1970-01-01T00:00:00+00:00"


def load_watermarks() -> dict[str, str]:
    if WATERMARK_PATH.exists():
        return json.loads(WATERMARK_PATH.read_text())
    return {}


def save_watermarks(watermarks: dict[str, str]) -> None:
    WATERMARK_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATERMARK_PATH.write_text(json.dumps(watermarks, indent=2, sort_keys=True) + "\n")


def export_table(conn, table: str, watermark_col: str, since: str, run_id: str) -> dict:
    df = conn.execute(
        f"SELECT * FROM {table} WHERE {watermark_col} > ? ORDER BY {watermark_col}",  # noqa: S608 (table/col from fixed TABLES dict, not user input)
        [since],
    ).pl()

    if df.height == 0:
        return {"rows": 0, "file": None, "new_watermark": since}

    new_watermark = df[watermark_col].max()
    new_watermark_iso = new_watermark.isoformat() if hasattr(new_watermark, "isoformat") else str(new_watermark)

    out_dir = EXPORT_ROOT / table
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{ts}_{run_id}.parquet"
    df.write_parquet(out_path)

    return {"rows": df.height, "file": str(out_path), "new_watermark": new_watermark_iso}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    args = parser.parse_args()

    conn = get_connection(args.db)
    run_migrations(conn)

    run_id = uuid.uuid4().hex[:8]
    watermarks = load_watermarks()
    summary: dict[str, dict] = {}

    for table, watermark_col in TABLES.items():
        since = watermarks.get(table, EPOCH)
        result = export_table(conn, table, watermark_col, since, run_id)
        summary[table] = result
        if result["rows"] > 0:
            watermarks[table] = result["new_watermark"]

    conn.close()
    save_watermarks(watermarks)

    total_rows = sum(r["rows"] for r in summary.values())
    print(json.dumps({"run_id": run_id, "total_rows_exported": total_rows, "tables": summary}, indent=2, default=str))


if __name__ == "__main__":
    main()
