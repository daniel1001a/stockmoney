"""Import Agent 3's (OpenClaw) Parquet exports into the local live DB.

This is the dev-machine side of the cross-machine handshake described in
NEXT_AGENT_PLAN.md. The `.duckdb` files are gitignored on both machines by
design, so the ONLY way the irreplaceable data Agent 3 captures reaches this
machine is through committed Parquet files under `data_sync/exports/<table>/`:

  - per-symbol option snapshots (iv_surface_daily / put_call_ratio_daily /
    options_derived_daily) -- lost forever for any day the capture cron misses,
    so every exported row is precious and must not be dropped or duplicated,
  - the LLM catalyst outputs (catalyst_signals) and the unified news feed
    (news_items) produced by Agent 3's Haiku/Sonnet chain.

Design guarantees (why this is safe to schedule / re-run blindly):
  * Idempotent two ways. (1) A local watermark table `_sync_import_log` records
    the SHA-256 of every Parquet file already imported, so an unchanged file is
    skipped without even being read. (2) Even when a file *is* read (new, or
    re-exported with rows appended -> new checksum), rows are inserted with a
    PK anti-join (`WHERE NOT EXISTS ...`), so rows already present are never
    duplicated. Correctness never depends on the watermark alone.
  * Look-ahead preserving. `ingested_at` / `available_at` are taken verbatim
    from the export -- NEVER regenerated here -- because they are the
    actually-available timestamps the whole backtest honesty story (CLAUDE.md
    section 2) depends on, and for the options tables they are part of the PK.
  * Loud, not lossy. Only the known set of exportable tables is accepted; a
    stray column or a missing NOT NULL column raises rather than silently
    corrupting or dropping data.

Usage (Agent 3 schedules the cron; this script is pure + idempotent):
    .venv/bin/python scripts/import_from_sync.py
    .venv/bin/python scripts/import_from_sync.py --db data/stockmoney_live.duckdb
    .venv/bin/python scripts/import_from_sync.py --exports-dir data_sync/exports
    .venv/bin/python scripts/import_from_sync.py --table catalyst_signals
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import polars as pl

from stockmoney.data.db import get_connection, run_migrations

# Default live DB (kept separate from the demo DB, matching build_live.py).
DEFAULT_LIVE_DB = "data/stockmoney_live.duckdb"
DEFAULT_EXPORTS_DIR = "data_sync/exports"

# The only tables Agent 3 exports, each mapped to its primary key. The PK is
# what makes the row-level insert idempotent (anti-join), so it must exactly
# match the migration definitions. Adding a new synced table = add it here.
TABLE_PRIMARY_KEYS: dict[str, list[str]] = {
    "iv_surface_daily": ["symbol", "trade_date", "expiry_date", "delta_bucket", "ingested_at"],
    "put_call_ratio_daily": ["symbol", "trade_date", "ingested_at"],
    "options_derived_daily": ["symbol", "trade_date", "metric_name", "method_version", "ingested_at"],
    "catalyst_signals": ["signal_id"],
    "news_items": ["item_id"],
}


def _ensure_import_log(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the local watermark table if absent.

    Not a schema migration: it is dev-machine-local bookkeeping that never
    leaves this box (the demo/live DBs are gitignored), so it does not belong
    in the shared, checksum-guarded migration history.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _sync_import_log (
            table_name    VARCHAR NOT NULL,
            file_name     VARCHAR NOT NULL,
            file_sha256   VARCHAR NOT NULL,
            rows_in_file  BIGINT NOT NULL,
            rows_inserted BIGINT NOT NULL,
            imported_at   TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (table_name, file_sha256)
        )
        """
    )


def _table_columns(conn: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'main' AND table_name = ?
        ORDER BY ordinal_position
        """,
        [table],
    ).fetchall()
    return [row[0] for row in rows]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _already_imported(conn: duckdb.DuckDBPyConnection, table: str, sha: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM _sync_import_log WHERE table_name = ? AND file_sha256 = ?",
        [table, sha],
    ).fetchone()
    return row is not None


def _insert_new_rows(
    conn: duckdb.DuckDBPyConnection, table: str, df: pl.DataFrame, pk: list[str]
) -> int:
    """Insert only rows whose PK is not already in ``table``. Returns count inserted.

    The dedup runs inside DuckDB (register df + `WHERE NOT EXISTS`) rather than
    as a polars anti-join, so PK comparison -- especially TIMESTAMPTZ
    `ingested_at` -- uses the engine's own type semantics and can't drift on a
    parquet<->duckdb round-trip. `ingested_at`/`available_at` are inserted as
    provided; they are deliberately never regenerated (look-ahead guard).
    """
    table_cols = _table_columns(conn, table)
    if not table_cols:
        raise ValueError(f"Unknown table: {table!r} (not migrated in this DB)")

    unknown = [c for c in df.columns if c not in table_cols]
    if unknown:
        raise ValueError(
            f"Parquet for {table!r} has columns {unknown} not on the table. "
            f"Valid columns: {table_cols}"
        )
    missing_pk = [c for c in pk if c not in df.columns]
    if missing_pk:
        raise ValueError(
            f"Parquet for {table!r} is missing primary-key columns {missing_pk}; "
            "cannot dedup safely."
        )

    cols = df.columns
    col_list = ", ".join(f'"{c}"' for c in cols)
    pk_match = " AND ".join(f't."{c}" = d."{c}"' for c in pk)

    before = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    conn.register("_sync_import_df", df)
    try:
        conn.execute(
            f'INSERT INTO "{table}" ({col_list}) '
            f'SELECT {col_list} FROM _sync_import_df d '
            f'WHERE NOT EXISTS (SELECT 1 FROM "{table}" t WHERE {pk_match})'
        )
    finally:
        conn.unregister("_sync_import_df")
    after = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    return after - before


def import_table(
    conn: duckdb.DuckDBPyConnection, table: str, exports_dir: Path
) -> dict:
    """Import every Parquet file for one table, idempotently."""
    pk = TABLE_PRIMARY_KEYS[table]
    table_dir = exports_dir / table
    result = {
        "files_seen": 0,
        "files_imported": 0,
        "files_skipped": 0,
        "rows_inserted": 0,
    }
    if not table_dir.is_dir():
        return result

    for path in sorted(table_dir.glob("*.parquet")):
        result["files_seen"] += 1
        sha = _sha256(path)
        if _already_imported(conn, table, sha):
            result["files_skipped"] += 1
            continue

        df = pl.read_parquet(path)
        conn.execute("BEGIN TRANSACTION")
        try:
            inserted = _insert_new_rows(conn, table, df, pk)
            conn.execute(
                "INSERT INTO _sync_import_log "
                "(table_name, file_name, file_sha256, rows_in_file, rows_inserted, imported_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [table, path.name, sha, df.height, inserted, datetime.now(timezone.utc)],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        result["files_imported"] += 1
        result["rows_inserted"] += inserted

    return result


def run_import(db_path: str, exports_dir: str, only_table: str | None = None) -> dict:
    if only_table is not None and only_table not in TABLE_PRIMARY_KEYS:
        raise ValueError(
            f"Unknown table {only_table!r}. Importable tables: "
            f"{sorted(TABLE_PRIMARY_KEYS)}"
        )
    tables = [only_table] if only_table else list(TABLE_PRIMARY_KEYS)
    root = Path(exports_dir)

    conn = get_connection(db_path)
    try:
        run_migrations(conn)
        _ensure_import_log(conn)
        summary = {
            "db": db_path,
            "exports_dir": str(root),
            "exports_dir_exists": root.is_dir(),
            "tables": {},
        }
        for table in tables:
            summary["tables"][table] = import_table(conn, table, root)
        summary["total_rows_inserted"] = sum(
            t["rows_inserted"] for t in summary["tables"].values()
        )
    finally:
        conn.close()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_LIVE_DB)
    parser.add_argument("--exports-dir", default=DEFAULT_EXPORTS_DIR)
    parser.add_argument(
        "--table",
        default=None,
        help="Import only this table (default: all). One of: "
        + ", ".join(sorted(TABLE_PRIMARY_KEYS)),
    )
    args = parser.parse_args()
    summary = run_import(args.db, args.exports_dir, args.table)
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
