"""Historical GDELT (Global Database of Events, Language, and Tone) backfill
via Google BigQuery's public `gdelt-bq.gdeltv2.events` dataset.

Why BigQuery, not the free DOC 2.0 API or raw bulk CSV downloads: the DOC API
only reliably covers the last ~3 months (nowhere near enough history for a
walk-forward backtest); raw bulk CSVs are ~2.5TB/year unfiltered globally,
impractical for a single-laptop project. BigQuery's public dataset lets SQL
filter (by date range + CAMEO EventRootCode + tone magnitude) before any data
leaves Google's infrastructure, and the 1TB/month free query tier is enough
for a carefully-scoped historical pull. Requires a Google Cloud project with
billing enabled (still free under quota) -- see GDELT_BQ_PROJECT_ID and
GOOGLE_APPLICATION_CREDENTIALS in .env.example.

Column mapping onto `event_news_gdelt` (migration 014 + 028):
    gdelt_event_id  = GLOBALEVENTID
    event_datetime  = SQLDATE, at 00:00:00 UTC (the Events table is daily-
                       granularity only; midnight is the best-available
                       anchor for "which calendar day this happened")
    event_class     = EventRootCode (CAMEO's ~20 top-level categories --
                       coarser than the full EventCode, the right resolution
                       for a daily aggregate feature)
    geo_lat/geo_lon = ActionGeo_Lat/ActionGeo_Long
    tone_score      = AvgTone
    goldstein_scale/num_mentions/num_sources/num_articles = migration 028's
                       additive columns, needed by
                       stockmoney.data.features.gdelt_sentiment's mention-
                       weighted aggregation and coverage-quality reporting
    symbol          = always NULL here. GDELT's Actor1Name/Actor2Name are
                       free-text, not tickers; per-symbol relevance matching
                       needs its own maintained entity table and belongs in
                       feature computation (like compute_xsec_dispersion's
                       per-sector grouping), not in this "raw ingestion stays
                       dumb" connector -- consistent with every other
                       connector in this package.

LOOK-AHEAD SAFETY -- read this before touching `ingested_at` below:
`stockmoney.data.db.append_rows` only auto-stamps `ingested_at` with "now"
when the incoming DataFrame is MISSING that column entirely. Because
`fetch_gdelt_events` below sets `ingested_at` explicitly on every row, that
value survives through `run_ingestion` -> `append_rows` completely unmodified
-- this is the entire mechanism that keeps a historical backfill honest
instead of making 2018 news look like it was knowable today. The value
chosen -- end of day (23:59:59 UTC) on the event's own SQLDATE -- is a
DOCUMENTED MODELING ASSUMPTION, not a measured fact: GDELT's real production
latency is ~15 minutes from event to record, so anchoring to end-of-day is a
deliberately conservative (late, never early) approximation of when a
hypothetical realtime crawler would have had this row. If GDELT's `events`
table turns out to expose a `DATEADDED` field (closer to true crawl time,
distinct from `SQLDATE`) this should be swapped in preference to the
end-of-day approximation -- flagged here for whoever checks this once BigQuery
access is live.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone

import duckdb
import polars as pl
from dotenv import load_dotenv

from stockmoney.data.ingestion.base import run_ingestion

GDELT_TABLE = "gdelt-bq.gdeltv2.events"

# CAMEO EventRootCode v1 starter set (tunable, not final -- see module
# docstring's convention of DEFAULT_* starter constants elsewhere in this
# package, e.g. fred_macro.DEFAULT_SERIES_IDS, rss_news.DEFAULT_FEEDS).
# Chosen for economic/geopolitical market relevance; 01-04 (routine
# statements/comments/consultations) excluded -- high volume, low signal.
#   05 diplomatic cooperation, 06 material cooperation, 07 provide aid,
#   09 investigate, 10 demand, 11 disapprove, 12 reject, 13 threaten,
#   14 protest, 16 reduce relations, 17 coerce (incl. sanctions),
#   18 assault, 19 fight, 20 mass violence
DEFAULT_ROOT_CODES = ["05", "06", "07", "09", "10", "11", "12", "13", "14", "16", "17", "18", "19", "20"]
DEFAULT_MIN_ABS_TONE = 5.0

# Safety valve for the cost-overrun risk: refuse to run a real (billed) query
# whose dry-run bytes-processed estimate exceeds this, forcing an explicit
# override rather than silently burning quota on a filter mistake.
DEFAULT_MAX_BYTES_BILLED = 5 * 1024**3  # 5 GB

# The daily-aggregate query below has a fixed real-world cost regardless of
# date range (~47GB empirically, for the whole 2015-2026 GDELT history in one
# call -- see fetch_gdelt_daily_aggregates' docstring). 5GB would always
# reject it; 60GB gives headroom above the known-good cost while still
# catching a genuine anomaly (e.g. a filter bug scanning far more than
# expected).
DEFAULT_MAX_BYTES_BILLED_AGGREGATE = 60 * 1024**3  # 60 GB

GDELT_QUERY = """
SELECT
  GLOBALEVENTID, SQLDATE, EventRootCode, GoldsteinScale,
  NumMentions, NumSources, NumArticles, AvgTone, ActionGeo_Lat, ActionGeo_Long
FROM `{table}`
WHERE SQLDATE BETWEEN @start AND @end
  AND EventRootCode IN UNNEST(@root_codes)
  AND ABS(AvgTone) >= @min_abs_tone
""".format(table=GDELT_TABLE)
# Column list deliberately trimmed to exactly what fetch_gdelt_events() below
# maps into the DataFrame -- Actor1Name/Actor1CountryCode/Actor2Name/
# Actor2CountryCode/EventCode/QuadClass/SOURCEURL were in an earlier version
# but never actually read downstream. Found (2026-07-11) that
# `gdelt-bq.gdeltv2.events` has NO partitioning or clustering
# (`table.time_partitioning`/`clustering_fields` are both None, confirmed via
# `client.get_table()`), so the SQLDATE date-range filter provides ZERO cost
# reduction -- every query scans the full 901M-row table's worth of the
# SELECTed columns regardless of date range (empirically verified: a 1-day
# query and an 11-year query both cost identical bytes-processed). This
# means (a) chunking by quarter/date range does NOT save money, it multiplies
# a flat per-query cost for no benefit -- fetch as wide a range as needed in
# as few queries as possible instead, and (b) trimming unused columns is the
# only real lever: cut the scan from ~190GB (full column list) to ~68GB
# (this trimmed list) for the exact same date range.

GDELT_AGGREGATE_QUERY = """
SELECT
  SQLDATE,
  SUM(AvgTone * NumMentions) / SUM(NumMentions) AS avg_tone,
  SUM(GoldsteinScale * NumMentions) / SUM(NumMentions) AS avg_goldstein,
  SUM(NumMentions) AS total_mentions,
  SUM(NumSources) AS total_sources,
  SUM(NumArticles) AS total_articles,
  COUNT(*) AS event_count
FROM `{table}`
WHERE SQLDATE BETWEEN @start AND @end
  AND EventRootCode IN UNNEST(@root_codes)
  AND ABS(AvgTone) >= @min_abs_tone
GROUP BY SQLDATE
ORDER BY SQLDATE
""".format(table=GDELT_TABLE)
# Why aggregate server-side instead of downloading raw events (found
# 2026-07-11, same root cause as the no-partitioning note above): with
# DEFAULT_ROOT_CODES/DEFAULT_MIN_ABS_TONE, the raw per-event query for the
# full 2015-2026 history matches on the order of 10^8 rows -- fine for
# BigQuery's own bill (bytes scanned doesn't depend on rows returned), but
# downloading, materializing, and locally storing that many individual rows
# is not viable on a 24GB-RAM laptop. GROUP BY SQLDATE server-side keeps the
# exact same bytes-scanned cost (grouping doesn't reduce the scan, only the
# result size) but shrinks the result to ~1 row per calendar day -- a few
# thousand rows for over a decade of history, trivial to download and store.

_EMPTY_SCHEMA = {
    "gdelt_event_id": pl.Utf8,
    "event_datetime": pl.Datetime(time_zone="UTC"),
    "symbol": pl.Utf8,
    "event_class": pl.Utf8,
    "geo_lat": pl.Float64,
    "geo_lon": pl.Float64,
    "tone_score": pl.Float64,
    "goldstein_scale": pl.Float64,
    "num_mentions": pl.Int64,
    "num_sources": pl.Int64,
    "num_articles": pl.Int64,
    "source": pl.Utf8,
    "ingested_at": pl.Datetime(time_zone="UTC"),
}


def _bq_client():
    load_dotenv()
    project = os.environ.get("GDELT_BQ_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError(
            "GDELT_BQ_PROJECT_ID is not set. Add it to a local .env file "
            "(see .env.example) or export it in the environment. Also requires "
            "GOOGLE_APPLICATION_CREDENTIALS pointing at a service-account JSON "
            "key with the BigQuery User role (see HANDOFF.md for setup steps)."
        )
    from google.cloud import bigquery  # lazy: most contributors won't need this installed

    return bigquery.Client(project=project)


def _query_params(start: date, end: date, root_codes: list[str], min_abs_tone: float):
    from google.cloud import bigquery

    return [
        bigquery.ScalarQueryParameter("start", "INT64", int(start.strftime("%Y%m%d"))),
        bigquery.ScalarQueryParameter("end", "INT64", int(end.strftime("%Y%m%d"))),
        bigquery.ArrayQueryParameter("root_codes", "STRING", root_codes),
        bigquery.ScalarQueryParameter("min_abs_tone", "FLOAT64", min_abs_tone),
    ]


def fetch_gdelt_events(
    start: date,
    end: date,
    *,
    root_codes: list[str] | None = None,
    min_abs_tone: float = DEFAULT_MIN_ABS_TONE,
    max_bytes_billed: int = DEFAULT_MAX_BYTES_BILLED,
    client=None,
) -> pl.DataFrame:
    """Query GDELT events for [start, end] (inclusive, SQLDATE granularity)
    and reshape into event_news_gdelt's schema. Always dry-runs first and
    refuses to run the real (billed) query if the estimated scan exceeds
    `max_bytes_billed` -- see module docstring's cost-overrun mitigation.
    `client` is injectable for testing (a fake with a `.query()` method
    matching `google.cloud.bigquery.Client`'s shape)."""
    from google.cloud import bigquery

    root_codes = root_codes if root_codes is not None else DEFAULT_ROOT_CODES
    client = client or _bq_client()
    params = _query_params(start, end, root_codes, min_abs_tone)

    dry_run_job = client.query(GDELT_QUERY, job_config=bigquery.QueryJobConfig(query_parameters=params, dry_run=True))
    bytes_processed = dry_run_job.total_bytes_processed
    if bytes_processed > max_bytes_billed:
        raise RuntimeError(
            f"GDELT query for {start}..{end} would scan {bytes_processed / 1e9:.2f} GB, "
            f"exceeding the {max_bytes_billed / 1e9:.2f} GB safety threshold. Narrow the "
            "date range or check root_codes/min_abs_tone filters, or pass a higher "
            "max_bytes_billed if you've confirmed this is expected."
        )
    print(f"  [gdelt dry-run] {start}..{end}: {bytes_processed / 1e9:.3f} GB estimated -- proceeding")

    rows = list(client.query(GDELT_QUERY, job_config=bigquery.QueryJobConfig(query_parameters=params)).result())
    if not rows:
        return pl.DataFrame(schema=_EMPTY_SCHEMA)

    records = []
    for row in rows:
        event_date = datetime.strptime(str(row["SQLDATE"]), "%Y%m%d").date()
        event_datetime = datetime(event_date.year, event_date.month, event_date.day, tzinfo=timezone.utc)
        # See module docstring's LOOK-AHEAD SAFETY note: end-of-day is a
        # deliberately conservative approximation, not a measured fact.
        ingested_at = datetime(event_date.year, event_date.month, event_date.day, 23, 59, 59, tzinfo=timezone.utc)
        records.append(
            {
                "gdelt_event_id": str(row["GLOBALEVENTID"]),
                "event_datetime": event_datetime,
                "symbol": None,
                "event_class": row["EventRootCode"],
                "geo_lat": row["ActionGeo_Lat"],
                "geo_lon": row["ActionGeo_Long"],
                "tone_score": row["AvgTone"],
                "goldstein_scale": row["GoldsteinScale"],
                "num_mentions": row["NumMentions"],
                "num_sources": row["NumSources"],
                "num_articles": row["NumArticles"],
                "source": "gdelt",
                "ingested_at": ingested_at,
            }
        )
    return pl.DataFrame(records, schema=_EMPTY_SCHEMA)


def ingest_gdelt_events(
    conn: duckdb.DuckDBPyConnection,
    start: date,
    end: date,
    *,
    root_codes: list[str] | None = None,
    min_abs_tone: float = DEFAULT_MIN_ABS_TONE,
    max_bytes_billed: int = DEFAULT_MAX_BYTES_BILLED,
    client=None,
) -> int:
    result = run_ingestion(
        conn,
        source="gdelt",
        target_table="event_news_gdelt",
        window_start=start,
        window_end=end,
        fetch_fn=lambda: fetch_gdelt_events(
            start, end, root_codes=root_codes, min_abs_tone=min_abs_tone,
            max_bytes_billed=max_bytes_billed, client=client,
        ),
    )
    return result.rows_written


def fetch_gdelt_daily_aggregates(
    start: date,
    end: date,
    *,
    root_codes: list[str] | None = None,
    min_abs_tone: float = DEFAULT_MIN_ABS_TONE,
    max_bytes_billed: int = DEFAULT_MAX_BYTES_BILLED_AGGREGATE,
    client=None,
) -> pl.DataFrame:
    """Bulk-history variant of `fetch_gdelt_events`: aggregates server-side
    (GROUP BY SQLDATE) so the whole [start, end] range -- even a decade --
    downloads as roughly one row per calendar day, not one row per matching
    event. Shaped as synthetic one-per-day "events" fitting
    event_news_gdelt's schema (gdelt_event_id="daily-agg-YYYYMMDD",
    event_class=None since no single EventRootCode applies to a whole day's
    aggregate) so `gdelt_sentiment.compute_gdelt_sentiment`'s existing
    per-day mention-weighted read works unchanged -- each synthetic row IS
    already the day's weighted average with the day's total NumMentions as
    its weight, so re-aggregating a single row per day is a no-op that
    preserves the value exactly.

    Use this for historical backfill. `fetch_gdelt_events` (raw, per-event)
    remains appropriate for the ongoing/live path, where a single day's
    volume is small enough to store individually."""
    from google.cloud import bigquery

    root_codes = root_codes if root_codes is not None else DEFAULT_ROOT_CODES
    client = client or _bq_client()
    params = _query_params(start, end, root_codes, min_abs_tone)

    dry_run_job = client.query(
        GDELT_AGGREGATE_QUERY, job_config=bigquery.QueryJobConfig(query_parameters=params, dry_run=True)
    )
    bytes_processed = dry_run_job.total_bytes_processed
    if bytes_processed > max_bytes_billed:
        raise RuntimeError(
            f"GDELT aggregate query for {start}..{end} would scan {bytes_processed / 1e9:.2f} GB, "
            f"exceeding the {max_bytes_billed / 1e9:.2f} GB safety threshold. Narrow the "
            "date range or check root_codes/min_abs_tone filters, or pass a higher "
            "max_bytes_billed if you've confirmed this is expected."
        )
    print(f"  [gdelt dry-run, aggregate] {start}..{end}: {bytes_processed / 1e9:.3f} GB estimated -- proceeding")

    rows = list(
        client.query(GDELT_AGGREGATE_QUERY, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
    )
    if not rows:
        return pl.DataFrame(schema=_EMPTY_SCHEMA)

    records = []
    for row in rows:
        event_date = datetime.strptime(str(row["SQLDATE"]), "%Y%m%d").date()
        event_datetime = datetime(event_date.year, event_date.month, event_date.day, tzinfo=timezone.utc)
        # See module docstring's LOOK-AHEAD SAFETY note: end-of-day is a
        # deliberately conservative approximation, not a measured fact.
        ingested_at = datetime(event_date.year, event_date.month, event_date.day, 23, 59, 59, tzinfo=timezone.utc)
        records.append(
            {
                "gdelt_event_id": f"daily-agg-{row['SQLDATE']}",
                "event_datetime": event_datetime,
                "symbol": None,
                "event_class": None,
                "geo_lat": None,
                "geo_lon": None,
                "tone_score": row["avg_tone"],
                "goldstein_scale": row["avg_goldstein"],
                "num_mentions": row["total_mentions"],
                "num_sources": row["total_sources"],
                "num_articles": row["total_articles"],
                "source": "gdelt",
                "ingested_at": ingested_at,
            }
        )
    return pl.DataFrame(records, schema=_EMPTY_SCHEMA)


def ingest_gdelt_daily_aggregates(
    conn: duckdb.DuckDBPyConnection,
    start: date,
    end: date,
    *,
    root_codes: list[str] | None = None,
    min_abs_tone: float = DEFAULT_MIN_ABS_TONE,
    max_bytes_billed: int = DEFAULT_MAX_BYTES_BILLED_AGGREGATE,
    client=None,
) -> int:
    result = run_ingestion(
        conn,
        source="gdelt",
        target_table="event_news_gdelt",
        window_start=start,
        window_end=end,
        fetch_fn=lambda: fetch_gdelt_daily_aggregates(
            start, end, root_codes=root_codes, min_abs_tone=min_abs_tone,
            max_bytes_billed=max_bytes_billed, client=client,
        ),
    )
    return result.rows_written
