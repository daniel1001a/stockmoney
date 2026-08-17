"""Full-table export of the League's own mutable state, for cross-session
persistence across the stateless Claude Code cloud routines that replaced
OpenClaw (issue #7 follow-up).

Unlike export_for_sync.py's watermark-based incremental export -- built for
append-only raw ingestion tables (OHLCV, news, options snapshots) -- the
League's own tables get UPDATED after insertion: a call gets confirmed,
withdrawn, or graded; a portfolio's cash changes on every fill. An
insert-only anti-join sync (export_for_sync.py/import_from_sync.py's design)
would ship the original 'pending' row once and then never see any of those
updates again. Retrofitting change-tracking columns and upsert logic onto
already-shipped tables is real engineering; given these tables are small (a
handful of traders' calls and trades, not raw market ticks), the simplest
CORRECT fix is a full-table dump/replace every run instead.

Every run OVERWRITES data_sync/league_state/<table>.parquet with that
table's CURRENT complete contents -- not append-only, not watermarked. The
importer (import_league_from_sync.py) does the matching TRUNCATE+reload, so
after both sides run, the importing session's local table is byte-for-byte
what this table looked like at export time.

Known limitation: this is last-writer-wins, not a merge. If two sessions ever
write League state concurrently, whichever exports last silently overwrites
the other's changes. Acceptable for the actual schedule (issue #7's decision-
point cloud routines run at well-separated times, never concurrently) -- do
not repurpose this for genuinely concurrent writers without redesigning it.

Usage:
    uv run python scripts/export_league_for_sync.py
    uv run python scripts/export_league_for_sync.py --db data/stockmoney.duckdb
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations

EXPORT_DIR = Path("data_sync/league_state")

# Every table that holds League-written, post-insert-mutable state. `traders`
# is deliberately excluded -- it's seeded identically by migrations on every
# machine (030_traders.sql/039_more_traders.sql), so it needs no sync.
TABLES = [
    "trader_predictions",
    "trader_prediction_grades",
    "trader_portfolios",
    "trader_trades",
    "trader_review_log",
    "trader_divergence_log",
    "trader_method_versions",
    "trader_method_proposals",
]


def export_table(conn, table: str) -> dict:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPORT_DIR / f"{table}.parquet"
    n = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]  # noqa: S608 -- table from fixed TABLES list, not user input
    conn.execute(f'COPY (SELECT * FROM "{table}") TO \'{path}\' (FORMAT PARQUET)')  # noqa: S608
    return {"rows": n, "file": str(path)}


def run_export(db_path: str = DEFAULT_DB_PATH) -> dict:
    conn = get_connection(db_path)
    try:
        run_migrations(conn)
        summary = {table: export_table(conn, table) for table in TABLES}
    finally:
        conn.close()
    return {"tables": summary, "total_rows": sum(r["rows"] for r in summary.values())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    print(json.dumps(run_export(args.db), indent=2, default=str))


if __name__ == "__main__":
    main()
