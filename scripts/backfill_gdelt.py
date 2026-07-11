"""One-time historical backfill of GDELT daily sentiment aggregates via
BigQuery (see stockmoney.data.ingestion.gdelt_events for the connector +
look-ahead safety discussion).

Single query for the whole date range, not chunked by quarter: found
(2026-07-11) that `gdelt-bq.gdeltv2.events` has no partitioning or
clustering, so BigQuery's billed bytes-scanned is the SAME regardless of how
narrow the date range is -- chunking by quarter would have multiplied a flat
per-query cost by the number of chunks for zero benefit. One call for the
full range is both cheaper and simpler. Uses the server-side daily
aggregation query (`fetch_gdelt_daily_aggregates`), not the raw per-event
one, since the raw query's result set for a decade of history is ~10^8 rows
-- not viable to download/store on a 24GB-RAM laptop; the aggregate query
returns ~1 row per calendar day instead.

Guards against accidentally re-querying (and re-paying for) a range that's
already backfilled: checks `event_news_gdelt` for existing rows in [start,
end] before calling BigQuery at all.

Requires GDELT_BQ_PROJECT_ID + GOOGLE_APPLICATION_CREDENTIALS in .env (see
.env.example and HANDOFF.md for GCP setup steps) -- fails fast with an
actionable error if missing, via gdelt_events._bq_client().

Usage:
    uv run python scripts/backfill_gdelt.py --start 2015-01-01 --end 2026-07-10
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations
from stockmoney.data.ingestion.gdelt_events import ingest_gdelt_daily_aggregates


def _already_covered(conn, start: date, end: date) -> bool:
    n = conn.execute(
        "SELECT count(*) FROM event_news_gdelt WHERE event_datetime BETWEEN ? AND ?",
        [datetime(start.year, start.month, start.day, tzinfo=timezone.utc),
         datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)],
    ).fetchone()[0]
    return n > 0


def main(start: date, end: date, db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    run_migrations(conn)

    if _already_covered(conn, start, end):
        print(f"event_news_gdelt already has rows in {start}..{end} -- skipping to avoid a "
              "duplicate (and re-billed) query. Delete the existing range first if you really "
              "want to re-run it.")
        conn.close()
        return

    print(f"GDELT daily-aggregate backfill: {start}..{end}")
    n = ingest_gdelt_daily_aggregates(conn, start, end)
    print(f"  {n} daily rows written")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=lambda s: date.fromisoformat(s), required=True)
    parser.add_argument("--end", type=lambda s: date.fromisoformat(s), required=True)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    main(args.start, args.end, db_path=args.db_path)
