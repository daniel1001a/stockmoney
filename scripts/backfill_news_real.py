"""Real, per-ticker, multi-year news/event backfill via GDELT 2.0 GKG (BigQuery).

WHY THIS EXISTS
----------------
The live DB's `event_news_gdelt` table (see scripts/backfill_gdelt.py and
stockmoney.data.ingestion.gdelt_events) is a market-wide DAILY AGGREGATE with
symbol=NULL and event_class=NULL -- useful for a broad "how negative is the
news cycle today" feature, useless for testing news as a PER-TICKER trading
signal, which is what this script is for.

SOURCES EVALUATED (see NEWS_BACKFILL_REPORT.md for the full writeup):
  1. GDELT DOC 2.0 API (the free public HTTP endpoint, api.gdeltproject.org).
     Documented to cover 2017-01-01 -> present with explicit startdatetime/
     enddatetime params -- in principle exactly what we need, ticker-name
     searchable, and includes real headlines/URLs. In THIS sandboxed dev
     environment, every request (even a single one, waited >40s after the
     previous) returned HTTP 429 with GDELT's "limit requests to one every 5
     seconds" message -- consistent with the sandbox's egress IP being shared
     across many concurrent users/sessions and already over GDELT's budget
     before we ever send a request. This is an environment limitation, not a
     protocol limitation: a normal single-user machine should be able to use
     the DOC API directly. Documented here so a re-run from an unshared IP can
     revisit it (it would additionally give the real headline text, which the
     BigQuery path below cannot).
  2. Finnhub /company-news: real, ticker-tagged, but free tier is documented
     as only ~1 year of history -- not usable for an 8y backtest and requires
     creating an account (an API key signup), which we didn't do without the
     user's explicit go-ahead.
  3. Alpha Vantage NEWS_SENTIMENT: free tier is 25 requests/day -- with 9
     tickers x many years, not remotely enough call budget to backfill.
  4. yfinance `.news`: only the current ~day's worth of headlines, no
     historical query at all.
  5. GDELT 2.0 GKG via BigQuery's public `gdelt-bq.gdeltv2.gkg_partitioned`
     dataset -- CHOSEN. Not ticker-tagged either, but each 15-minute-batch
     record carries a `V2Organizations` field (parsed named-entity mentions)
     that we substring-match against each company's legal name to derive a
     real per-symbol tag. This reuses the same GDELT_BQ_PROJECT_ID /
     GOOGLE_APPLICATION_CREDENTIALS already configured for
     scripts/backfill_gdelt.py.

COST (measured empirically 2026-07-13, dry_run=True, see NEWS_BACKFILL_REPORT.md):
  A SINGLE query covering ALL 9 tickers combined (one big OR'd LIKE clause)
  over the ENTIRE 2018-01-01..2026-07-13 range costs ~80.7 GB billed --
  comfortably inside BigQuery's 1 TB/month free tier, and (same lesson as
  gdelt_events.py's docstring) issuing 9 separate per-ticker queries instead
  would multiply this SAME byte cost by 9 for zero benefit, because
  `gkg_partitioned` is time-partitioned but not clustered by any entity
  column -- BigQuery's bytes-billed depends on which columns are referenced
  and which date partitions are touched, NOT on how selective the WHERE
  clause's LIKE predicates are. Always query all tickers in one call.

WHAT THIS GIVES YOU THAT event_news_gdelt DOESN'T
--------------------------------------------------
Real per-article rows, each genuinely taggable to one (or more, if the
article discusses several of our companies) of the 9 tickers -- not a
market-wide daily blob.

WHAT THIS DOESN'T GIVE YOU (be honest about this)
---------------------------------------------------
- No real headline or article body text. GKG's schema exposes
  `DocumentIdentifier` (the article URL) and structured metadata
  (organizations/themes/tone), not the headline. `_headline_from_url` below
  does a best-effort slug-to-title reconstruction from the URL path -- this
  is NOT the real headline, just an approximation for human skimming; treat
  the `headline` column as "display label," not "ground truth text."
- V2Organizations substring matching is a heuristic entity-linker, not a
  verified ticker tag: false positives happen for excerpts/quotes, wire
  reprints, "X vs Y" comparison pieces (correctly produce multi-symbol rows),
  and any outlet whose org name happens to substring-match (mitigated by
  matching FULL legal names like "Advanced Micro Devices" / "Nvidia
  Corporation" rather than bare tickers/short names, which would false-
  positive constantly -- e.g. bare "apple" would match "Big Apple", "apple
  pie", etc; this was measured directly, see report).

LOOK-AHEAD SAFETY (CLAUDE.md Sec.2's "actual availability timestamp" rule)
----------------------------------------------------------------------------
GKG's `DATE` field is the timestamp of the 15-minute GDELT ingestion batch
that FIRST recorded the article -- i.e., genuinely close to "when a
hypothetical realtime crawler would have known this exists," unlike the
`events` table's day-only `SQLDATE`. We use it for BOTH `event_time` and
`available_ts` (documented modeling assumption: GKG does not expose an
earlier "true publish time" for free, and using the same ingestion-batch
timestamp for both is the conservative choice -- it can never make an article
look available earlier than GDELT itself could see it).

OUTPUT
------
Partitioned Parquet under data/news_backfill/source=gdelt_gkg/symbol=<TICKER>/
year=<YYYY>/part.parquet -- one file per (symbol, year), append-only, never
touches the live DuckDB.

Usage:
    .venv/bin/python scripts/backfill_news_real.py --start 2018-01-01 --end 2026-07-13
    .venv/bin/python scripts/backfill_news_real.py --start 2018-01-01 --end 2019-12-31 --dry-run
"""
from __future__ import annotations

import argparse
import re
from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = REPO_ROOT / "data" / "news_backfill" / "source=gdelt_gkg"

# Full LEGAL / commonly-bylined names, deliberately NOT bare tickers or short
# names (see module docstring: bare "apple"/"google" alone produce massive
# false-positive rates against unrelated organizations, places, idioms).
# Facebook/Meta and Broadcom/Avago both carry pre-rename aliases since our
# window (2018-) spans the Meta rename (Oct 2021) and starts after the Avago
# rename (2016) but some outlets still used "Avago" retrospectively.
TICKER_ORG_PATTERNS: dict[str, list[str]] = {
    "AAPL": ["apple inc"],
    "AMD": ["advanced micro devices"],
    "AMZN": ["amazon.com", "amazon inc"],
    "AVGO": ["broadcom inc", "broadcom limited", "broadcom corporation"],
    "GOOGL": ["alphabet inc", "google llc", "google inc"],
    "META": ["meta platforms", "facebook inc", "facebook, inc"],
    "MSFT": ["microsoft corporation", "microsoft corp"],
    "NVDA": ["nvidia corporation", "nvidia corp"],
    "TSM": ["taiwan semiconductor manufacturing", "tsmc"],
}

GKG_TABLE = "gdelt-bq.gdeltv2.gkg_partitioned"

# Safety valve, same convention as gdelt_events.py's DEFAULT_MAX_BYTES_BILLED:
# refuse to run the real (billed) query if the dry-run estimate exceeds this,
# forcing an explicit --max-bytes-billed override rather than silently
# burning quota on e.g. a pattern-list typo that defeats partition pruning.
# Measured cost for the full 2018-2026 x 9-ticker range was ~81GB; 150GB
# gives headroom for a wider ad-hoc range while still catching anomalies.
DEFAULT_MAX_BYTES_BILLED = 150 * 1024**3  # 150 GB


def _bq_client():
    import os

    load_dotenv(dotenv_path=str(REPO_ROOT / ".env"))
    project = os.environ.get("GDELT_BQ_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError(
            "GDELT_BQ_PROJECT_ID is not set. Add it to .env (see .env.example) -- same "
            "credentials already used by scripts/backfill_gdelt.py."
        )
    from google.cloud import bigquery

    return bigquery.Client(project=project)


def _build_query(start: date, end: date) -> str:
    patterns = [p for plist in TICKER_ORG_PATTERNS.values() for p in plist]
    or_clause = " OR ".join(f"LOWER(V2Organizations) LIKE '%{p}%'" for p in patterns)
    return f"""
SELECT GKGRECORDID, DATE, SourceCommonName, DocumentIdentifier, V2Tone, V2Organizations
FROM `{GKG_TABLE}`
WHERE _PARTITIONTIME BETWEEN TIMESTAMP('{start.isoformat()}') AND TIMESTAMP('{end.isoformat()}')
  AND ({or_clause})
"""


def _headline_from_url(url: str) -> str | None:
    """Best-effort, NOT authoritative: reconstruct a human-skimmable label
    from a news URL's path slug. Many (not all) news CMSes embed the
    headline in the URL, e.g. .../2019/03/nvidia-q4-earnings-beat.html ->
    "nvidia q4 earnings beat". Returns None if no usable slug is found --
    callers must treat this column as a display approximation, never as the
    real headline text (GKG's free schema does not include the real one)."""
    if not url:
        return None
    m = re.search(r"/([a-zA-Z0-9][a-zA-Z0-9\-_]{8,})(?:\.[a-zA-Z]{2,5})?(?:[/?#]|$)", url)
    if not m:
        return None
    slug = m.group(1)
    if slug.isdigit():
        return None
    words = re.split(r"[-_]+", slug)
    words = [w for w in words if w and not w.isdigit()]
    if len(words) < 3:
        return None
    return " ".join(words)


def _parse_tone(v2tone: str | None) -> float | None:
    """V2Tone is a comma-separated string: avgTone,posScore,negScore,polarity,
    activityRefDensity,selfGroupRefDensity,wordCount. We only use the first
    field (average tone, roughly on a -100..+100 scale per GDELT's docs)."""
    if not v2tone:
        return None
    try:
        return float(v2tone.split(",")[0])
    except (ValueError, IndexError):
        return None


def _matched_symbols(v2orgs: str | None) -> list[str]:
    if not v2orgs:
        return []
    low = v2orgs.lower()
    return [sym for sym, patterns in TICKER_ORG_PATTERNS.items() if any(p in low for p in patterns)]


def fetch_gkg_news(start: date, end: date, *, max_bytes_billed: int = DEFAULT_MAX_BYTES_BILLED, client=None) -> pl.DataFrame:
    """Query GKG for [start, end], reshape into one row per (article, matched
    symbol). Always dry-runs first (see DEFAULT_MAX_BYTES_BILLED)."""
    from google.cloud import bigquery

    client = client or _bq_client()
    query = _build_query(start, end)

    dry = client.query(query, job_config=bigquery.QueryJobConfig(dry_run=True))
    bytes_processed = dry.total_bytes_processed
    print(f"  [gkg dry-run] {start}..{end}: {bytes_processed / 1e9:.3f} GB estimated")
    if bytes_processed > max_bytes_billed:
        raise RuntimeError(
            f"Query for {start}..{end} would scan {bytes_processed / 1e9:.2f} GB, exceeding "
            f"the {max_bytes_billed / 1e9:.2f} GB safety threshold. Narrow the range or pass "
            "a higher --max-bytes-billed if you've confirmed this is expected."
        )

    print(f"  [gkg] running real query for {start}..{end} ...")
    rows = list(client.query(query).result())
    print(f"  [gkg] {len(rows)} raw GKG records matched (pre symbol-tagging, may include duplicates across symbols)")

    records: list[dict] = []
    for row in rows:
        symbols = _matched_symbols(row["V2Organizations"])
        if not symbols:
            continue
        date_int = row["DATE"]
        dt = datetime.strptime(str(date_int), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        tone = _parse_tone(row["V2Tone"])
        url = row["DocumentIdentifier"]
        headline = _headline_from_url(url)
        for sym in symbols:
            records.append(
                {
                    "symbol": sym,
                    "event_time": dt,
                    "available_ts": dt,  # see module docstring's LOOK-AHEAD SAFETY note
                    "headline": headline,
                    "url": url,
                    "source": row["SourceCommonName"],
                    "sentiment_score": (tone / 100.0) if tone is not None else None,
                    "raw_tone": tone,
                    "gkg_record_id": row["GKGRECORDID"],
                }
            )

    if not records:
        return pl.DataFrame(
            schema={
                "symbol": pl.Utf8, "event_time": pl.Datetime(time_zone="UTC"),
                "available_ts": pl.Datetime(time_zone="UTC"), "headline": pl.Utf8,
                "url": pl.Utf8, "source": pl.Utf8, "sentiment_score": pl.Float64,
                "raw_tone": pl.Float64, "gkg_record_id": pl.Utf8,
            }
        )
    return pl.DataFrame(records)


def write_partitioned(df: pl.DataFrame) -> dict[str, int]:
    """Write one Parquet file per (symbol, year) under OUTPUT_ROOT. Append-
    only within a (symbol, year) partition -- re-running the same range
    overwrites just that partition's file (idempotent per range, never
    touches the live DuckDB)."""
    if df.height == 0:
        return {}
    df = df.with_columns(pl.col("event_time").dt.year().alias("_year"))
    counts: dict[str, int] = {}
    for (symbol, year), part in df.group_by(["symbol", "_year"]):
        out_dir = OUTPUT_ROOT / f"symbol={symbol}" / f"year={year}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "part.parquet"
        part.drop("_year").sort("event_time").write_parquet(out_path)
        counts[f"{symbol}/{year}"] = part.height
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=lambda s: date.fromisoformat(s), default=date(2018, 1, 1))
    parser.add_argument("--end", type=lambda s: date.fromisoformat(s), default=date.today())
    parser.add_argument("--max-bytes-billed", type=int, default=DEFAULT_MAX_BYTES_BILLED)
    parser.add_argument("--dry-run", action="store_true", help="Only print the BigQuery cost estimate, fetch nothing.")
    args = parser.parse_args()

    if args.dry_run:
        client = _bq_client()
        from google.cloud import bigquery

        job = client.query(_build_query(args.start, args.end), job_config=bigquery.QueryJobConfig(dry_run=True))
        print(f"Estimated bytes billed for {args.start}..{args.end}: {job.total_bytes_processed / 1e9:.3f} GB")
        return

    df = fetch_gkg_news(args.start, args.end, max_bytes_billed=args.max_bytes_billed)
    print(f"\nTotal symbol-tagged rows: {df.height}")
    if df.height:
        print(df.group_by("symbol").len().sort("symbol"))
        print(f"Date span: {df['event_time'].min()} .. {df['event_time'].max()}")

    counts = write_partitioned(df)
    print(f"\nWrote {len(counts)} (symbol, year) partitions under {OUTPUT_ROOT}")
    for key in sorted(counts):
        print(f"  {key}: {counts[key]} rows")


if __name__ == "__main__":
    main()
