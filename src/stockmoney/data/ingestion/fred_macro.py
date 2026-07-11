from __future__ import annotations

import os
from datetime import date, datetime, timezone

import duckdb
import polars as pl
import requests
from dotenv import load_dotenv

from stockmoney.data.ingestion.base import run_ingestion

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"

# v1 starter set (CLAUDE.md section 2 "總體經濟" row): rate curve, USD index,
# a commodity benchmark. Long/tidy format (series_id as a row value, not a
# column) means adding another FRED series later is a data-only change, no
# migration needed.
DEFAULT_SERIES_IDS = ["DGS10", "DGS2", "DTWEXBGS", "DCOILWTICO"]

_EMPTY_SCHEMA = {
    "series_id": pl.Utf8,
    "observation_date": pl.Date,
    "value": pl.Float64,
    "vintage_date": pl.Date,
    "source": pl.Utf8,
}


def _fred_api_key() -> str:
    load_dotenv()
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise RuntimeError(
            "FRED_API_KEY is not set. Add it to a local .env file "
            "(see .env.example) or export it in the environment."
        )
    return key


def fetch_macro_series(
    series_ids: list[str], start: date, end: date, *, vintage_date: date | None = None
) -> pl.DataFrame:
    """Fetch daily observations for ``series_ids`` from FRED.

    FRED observations can be revised after first release (e.g. GDP, and even
    daily series get occasional corrections), so every value is stamped with
    ``vintage_date`` — the date *we* pulled it, i.e. the vintage currently in
    effect — rather than overwriting a prior pull. Re-running this on a later
    date naturally records any revision as a new row instead of silently
    replacing history, matching the append-only design of macro_series_daily.
    """
    key = _fred_api_key()
    vintage_date = vintage_date or datetime.now(timezone.utc).date()

    frames = []
    for series_id in series_ids:
        resp = requests.get(
            FRED_OBSERVATIONS_URL,
            params={
                "series_id": series_id,
                "api_key": key,
                "file_type": "json",
                "observation_start": start.isoformat(),
                "observation_end": end.isoformat(),
            },
            timeout=30,
        )
        resp.raise_for_status()
        observations = resp.json()["observations"]

        obs_dates, values = [], []
        for obs in observations:
            if obs["value"] == ".":  # FRED's marker for no data that day
                continue
            obs_dates.append(date.fromisoformat(obs["date"]))
            values.append(float(obs["value"]))

        if not obs_dates:
            continue
        frames.append(
            pl.DataFrame(
                {
                    "series_id": [series_id] * len(obs_dates),
                    "observation_date": obs_dates,
                    "value": values,
                    "vintage_date": [vintage_date] * len(obs_dates),
                    "source": ["fred"] * len(obs_dates),
                },
                schema=_EMPTY_SCHEMA,
            )
        )

    if not frames:
        return pl.DataFrame(schema=_EMPTY_SCHEMA)
    return pl.concat(frames)


def _fetch_and_drop_existing_vintages(
    conn: duckdb.DuckDBPyConnection, series_ids: list[str], start: date, end: date
) -> pl.DataFrame:
    """`macro_series_daily`'s PK is (series_id, observation_date, vintage_date)
    — one row per *day's* vintage, not per ingestion run like the other raw
    tables. Re-running the ingest more than once on the same calendar day (a
    manual run plus that day's cron, or a cron retry) would otherwise refetch
    identical rows and hit a hard PK collision instead of being a safe no-op,
    so already-present keys are filtered out before the write.
    """
    df = fetch_macro_series(series_ids, start, end)
    if df.height == 0:
        return df

    existing = set(
        conn.execute(
            "SELECT series_id, observation_date, vintage_date FROM macro_series_daily "
            "WHERE series_id = ANY(?)",
            [series_ids],
        ).fetchall()
    )
    keep = [
        (row["series_id"], row["observation_date"], row["vintage_date"]) not in existing
        for row in df.iter_rows(named=True)
    ]
    return df.filter(pl.Series(keep))


def ingest_macro_series(
    conn: duckdb.DuckDBPyConnection,
    start: date,
    end: date,
    series_ids: list[str] | None = None,
) -> int:
    """Ingest FRED macro series into ``macro_series_daily``. Safe to re-run
    multiple times on the same day (rows already recorded for today's
    vintage are skipped, not re-inserted)."""
    series_ids = series_ids or DEFAULT_SERIES_IDS
    result = run_ingestion(
        conn,
        source="fred",
        target_table="macro_series_daily",
        window_start=start,
        window_end=end,
        fetch_fn=lambda: _fetch_and_drop_existing_vintages(conn, series_ids, start, end),
    )
    return result.rows_written
