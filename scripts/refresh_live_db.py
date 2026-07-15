"""Refresh the app-served LIVE DuckDB from the freshly-ingested working DB.

Single-machine deployment note
------------------------------
On the OpenClaw machine, the cron jobs (news ingest, nightly refresh, options
snapshot) all write into the *working* DB (``data/stockmoney.duckdb``), while
the FastAPI backend serves the *live* DB (``data/stockmoney_live.duckdb``,
pointed at by ``STOCKMONEY_DB`` in ``.claude/launch.json``). This split exists
because DuckDB allows only one read-write process at a time: the API opens the
live file read-only with short-lived connections, so the writers never contend
with it (see ``src/stockmoney/api/db.py``).

The original cross-machine design shipped the working DB to a *separate* dev
machine as watermarked Parquet (``export_for_sync.py`` -> git ->
``import_from_sync.py``). When ingest + serve happen on the *same* machine that
round-trip is pointless and, worse, left the live DB half-built (only the
exported subset of tables), so fresh news never reached the app.

This script does the single-machine equivalent: it validates the working DB,
then atomically swaps a byte-for-byte copy into place as the live DB. Atomic
``os.replace`` means an API request either sees the whole old file or the whole
new one, never a torn copy; connections already open keep reading the old inode
until they close (their next request opens the new one). The whole live DB is
therefore always internally consistent and complete.

Usage
-----
    .venv/bin/python scripts/refresh_live_db.py
    .venv/bin/python scripts/refresh_live_db.py --source data/stockmoney.duckdb \
        --live data/stockmoney_live.duckdb
    .venv/bin/python scripts/refresh_live_db.py --skip-validation   # not advised
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile

import duckdb

from stockmoney.data.validation import validate_all

DEFAULT_SOURCE = "data/stockmoney.duckdb"
DEFAULT_LIVE = "data/stockmoney_live.duckdb"


def refresh_live(
    source: str = DEFAULT_SOURCE,
    live: str = DEFAULT_LIVE,
    *,
    skip_validation: bool = False,
) -> dict:
    if not os.path.exists(source):
        raise FileNotFoundError(f"source DB not found: {source}")

    violations: list = []
    if not skip_validation:
        conn = duckdb.connect(source, read_only=True)
        try:
            violations = validate_all(conn)
        finally:
            conn.close()
        if violations:
            # Refuse to publish a DB that fails the standing sanity checks --
            # better a stale-but-sane live DB than a fresh-but-broken one.
            return {
                "status": "refused",
                "reason": "source DB failed validation; live DB left untouched",
                "violations": [str(v) for v in violations],
            }

    live_dir = os.path.dirname(os.path.abspath(live)) or "."
    os.makedirs(live_dir, exist_ok=True)

    # Copy to a temp file on the SAME filesystem, then atomically rename over
    # the live path. os.replace is atomic within a filesystem, so the API never
    # observes a partially-written live DB.
    fd, tmp_path = tempfile.mkstemp(prefix=".live_refresh_", dir=live_dir)
    os.close(fd)
    try:
        shutil.copy2(source, tmp_path)
        os.replace(tmp_path, live)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    size = os.path.getsize(live)

    # Confirm the freshly-published live DB opens and reports its news high-water
    # mark -- cheap end-to-end proof that the swap produced a usable file.
    conn = duckdb.connect(live, read_only=True)
    try:
        news_total, news_max = conn.execute(
            "SELECT count(*), max(created_at) FROM news_items"
        ).fetchone()
        news_24h = conn.execute(
            "SELECT count(*) FROM news_items WHERE created_at >= now() - INTERVAL 24 HOUR"
        ).fetchone()[0]
        watchlist = conn.execute("SELECT count(*) FROM watchlist_members").fetchone()[0]
    finally:
        conn.close()

    return {
        "status": "ok",
        "source": source,
        "live": live,
        "live_bytes": size,
        "news_items_total": news_total,
        "news_items_max_created": str(news_max),
        "news_items_last_24h": news_24h,
        "watchlist_members": watchlist,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--live", default=DEFAULT_LIVE)
    parser.add_argument("--skip-validation", action="store_true")
    args = parser.parse_args()

    result = refresh_live(
        args.source, args.live, skip_validation=args.skip_validation
    )
    print(json.dumps(result, indent=2))
    if result.get("status") != "ok":
        sys.exit(1)


if __name__ == "__main__":
    main()
