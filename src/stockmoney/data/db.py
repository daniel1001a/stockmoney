from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import polars as pl

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
DEFAULT_DB_PATH = "data/stockmoney.duckdb"

# Sentinel for market-wide rows in key columns that cannot be NULL
# (e.g. feature_store.symbol for a market regime label).
MARKET_SYMBOL = "__MARKET__"

# Prefix for sector-group rows in feature_store.symbol, e.g. a cross-sectional
# dispersion computed over the semiconductor single names lives under
# f"{SECTOR_SYMBOL_PREFIX}semiconductor".
SECTOR_SYMBOL_PREFIX = "__SECTOR:"


def sector_symbol(sector: str) -> str:
    return f"{SECTOR_SYMBOL_PREFIX}{sector}"


def get_connection(db_path: str) -> duckdb.DuckDBPyConnection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(db_path)


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def run_migrations(conn: duckdb.DuckDBPyConnection) -> None:
    """Apply pending SQL migrations in filename order.

    Forward-only and idempotent. Each applied file's SHA-256 is recorded; on
    every run the stored checksum is compared against the current file, so an
    accidental edit to an already-applied migration is detected and rejected
    rather than silently ignored.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _schema_migrations (
            filename VARCHAR PRIMARY KEY,
            checksum VARCHAR,
            applied_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    # Legacy DBs created before the checksum column existed: add it in place.
    conn.execute("ALTER TABLE _schema_migrations ADD COLUMN IF NOT EXISTS checksum VARCHAR")

    applied = {
        row[0]: row[1]
        for row in conn.execute("SELECT filename, checksum FROM _schema_migrations").fetchall()
    }

    for path in sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name):
        sql = path.read_text()
        current = _checksum(sql)

        if path.name in applied:
            stored = applied[path.name]
            if stored is None:
                # Backfill checksum for rows applied before this feature existed.
                conn.execute(
                    "UPDATE _schema_migrations SET checksum = ? WHERE filename = ?",
                    [current, path.name],
                )
            elif stored != current:
                raise RuntimeError(
                    f"Migration {path.name} was modified after being applied "
                    f"(stored {stored[:12]}… != current {current[:12]}…). "
                    "Applied migrations are immutable; add a new forward migration instead."
                )
            continue

        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(sql)
            conn.execute(
                "INSERT INTO _schema_migrations (filename, checksum, applied_at) VALUES (?, ?, ?)",
                [path.name, current, datetime.now(timezone.utc)],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


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


def append_rows(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    df: pl.DataFrame,
    *,
    ingested_at: datetime | None = None,
) -> int:
    """Append a polars DataFrame to a raw/feature table (append-only, INSERT only).

    This is the single supported write path for crawlers/agents: callers only
    prepare data columns and never build SQL themselves, which is exactly the
    stability guarantee the project wants.

    - Validates every DataFrame column exists on the target table (rejects typos
      / stray columns rather than silently dropping them).
    - If the table has an ``ingested_at`` column and the DataFrame omits it,
      it is auto-filled with ``ingested_at`` (default: now, UTC) so the
      "actually-available timestamp" rule is never forgotten.
    - Never updates or deletes existing rows.

    Returns the number of rows inserted.
    """
    table_cols = _table_columns(conn, table)
    if not table_cols:
        raise ValueError(f"Unknown table: {table!r}")

    if df.height == 0:
        return 0

    unknown = [c for c in df.columns if c not in table_cols]
    if unknown:
        raise ValueError(
            f"Columns {unknown} do not exist on table {table!r}. "
            f"Valid columns: {table_cols}"
        )

    if "ingested_at" in table_cols and "ingested_at" not in df.columns:
        stamp = ingested_at or datetime.now(timezone.utc)
        df = df.with_columns(pl.lit(stamp).alias("ingested_at"))

    cols = df.columns
    col_list = ", ".join(f'"{c}"' for c in cols)
    conn.register("_append_rows_df", df)
    try:
        conn.execute(
            f'INSERT INTO "{table}" ({col_list}) SELECT {col_list} FROM _append_rows_df'
        )
    finally:
        conn.unregister("_append_rows_df")
    return df.height


def migrate_cli(db_path: str = DEFAULT_DB_PATH) -> None:
    """Entry point: apply migrations to ``db_path`` and print a summary."""
    conn = get_connection(db_path)
    try:
        run_migrations(conn)
        n = conn.execute("SELECT count(*) FROM _schema_migrations").fetchone()[0]
        print(f"Applied migrations: {n} (db: {db_path})")
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    migrate_cli(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH)
