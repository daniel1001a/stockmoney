"""Full-table import of the League's own mutable state -- the matching
reader for export_league_for_sync.py's full-table dumps (issue #7
follow-up: the League's tables were never covered by the original
watermarked raw-data sync, so every cloud-routine session used to start with
an amnesiac League -- no prediction history, no leaderboard, ever).

TRUNCATE + reload per table, not an incremental/anti-join merge -- see
export_league_for_sync.py's docstring for why a full replace is the correct
design here, not a shortcut: the exported Parquet IS the complete, current
state of that table as of the exporting session, so reloading it here makes
this session's local table match that exactly, mutations and all.

A missing Parquet file is not an error: the very first session ever run has
nothing to import yet, and an empty League is a valid, honest starting
state (same "no fabrication" discipline as the rest of this codebase).

Usage:
    uv run python scripts/import_league_from_sync.py
    uv run python scripts/import_league_from_sync.py --db data/stockmoney.duckdb \
        --league-dir data_sync/league_state
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from export_league_for_sync import TABLES

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations


def import_table(conn, table: str, league_dir: Path) -> dict:
    path = league_dir / f"{table}.parquet"
    if not path.exists():
        return {"imported": False, "rows": 0}

    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute(f'DELETE FROM "{table}"')  # noqa: S608 -- table from fixed TABLES list, not user input
        conn.execute(f"INSERT INTO \"{table}\" SELECT * FROM read_parquet('{path}')")  # noqa: S608
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    n = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]  # noqa: S608
    return {"imported": True, "rows": n}


def run_import(db_path: str = DEFAULT_DB_PATH, league_dir: str = "data_sync/league_state") -> dict:
    root = Path(league_dir)
    conn = get_connection(db_path)
    try:
        run_migrations(conn)
        summary = {table: import_table(conn, table, root) for table in TABLES}
    finally:
        conn.close()
    return {
        "league_dir": str(root),
        "league_dir_exists": root.is_dir(),
        "tables": summary,
        "total_rows": sum(t["rows"] for t in summary.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--league-dir", default="data_sync/league_state")
    args = parser.parse_args()
    print(json.dumps(run_import(args.db, args.league_dir), indent=2, default=str))


if __name__ == "__main__":
    main()
