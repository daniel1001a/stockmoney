import statistics
from datetime import date, datetime, timedelta, timezone

import duckdb
import polars as pl
import pytest

from stockmoney.data.db import append_rows, run_migrations, sector_symbol
from stockmoney.data.features.dispersion import (
    SECTOR_MEMBERS,
    compute_xsec_dispersion,
)


def _seed(conn, closes_by_symbol: dict[str, list[float]], start: date):
    for symbol, closes in closes_by_symbol.items():
        n = len(closes)
        dates = [start + timedelta(days=i) for i in range(n)]
        df = pl.DataFrame(
            {
                "symbol": [symbol] * n,
                "trade_date": dates,
                "close": closes,
                "source": ["test"] * n,
                "ingested_at": [
                    datetime(d.year, d.month, d.day, 21, tzinfo=timezone.utc) for d in dates
                ],
            }
        )
        append_rows(conn, "ohlcv_daily", df)


def test_dispersion_matches_manual_std():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)

    start = date(2026, 1, 1)
    # Day-2 returns: NVDA +10%, AVGO +0%, AMD -10%, TSM +5%.
    _seed(conn, {
        "NVDA": [100.0, 110.0],
        "AVGO": [100.0, 100.0],
        "AMD": [100.0, 90.0],
        "TSM": [100.0, 105.0],
    }, start)

    written = compute_xsec_dispersion(conn, "semiconductor")
    assert written == 1  # only one date has returns for all names

    rets = [0.10, 0.0, -0.10, 0.05]
    mean = statistics.fmean(rets)
    expected = statistics.stdev([r - mean for r in rets])

    row = conn.execute(
        "SELECT symbol, feature_value FROM feature_store WHERE feature_name = 'xsec_dispersion'"
    ).fetchone()
    assert row[0] == sector_symbol("semiconductor")
    assert row[1] == pytest.approx(expected)


def test_dispersion_available_at_is_latest_contributor():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    start = date(2026, 1, 1)
    _seed(conn, {s: [100.0, 101.0] for s in SECTOR_MEMBERS["semiconductor"]}, start)

    compute_xsec_dispersion(conn, "semiconductor")
    available_at = conn.execute(
        "SELECT available_at FROM feature_store WHERE feature_name = 'xsec_dispersion'"
    ).fetchone()[0]
    # All contributors ingested on day 2 at 21:00 UTC.
    assert available_at == datetime(2026, 1, 2, 21, tzinfo=timezone.utc)
