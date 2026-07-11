"""Short-lived read-only DuckDB connections for API request handlers --
same reasoning as scripts/dashboard.py's `_ro_conn`: opened fresh and closed
immediately after each use rather than held as a long-lived resource, so the
API never blocks scripts/nightly_refresh.py or the CLI scripts from writing
while it's running. See https://duckdb.org/docs/stable/connect/concurrency
"""
from __future__ import annotations

from contextlib import contextmanager

import duckdb

from stockmoney.data.db import DEFAULT_DB_PATH


@contextmanager
def ro_connection(db_path: str | None = None):
    # Resolved at call time (module-global lookup), not bound as a default
    # argument value at import time -- so tests can monkeypatch
    # stockmoney.api.db.DEFAULT_DB_PATH to point routes at a throwaway DB.
    conn = duckdb.connect(db_path or DEFAULT_DB_PATH, read_only=True)
    try:
        yield conn
    finally:
        conn.close()
