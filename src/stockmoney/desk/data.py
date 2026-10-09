"""Daily OHLCV for the desk: download from yfinance, cache as one parquet.

Prices are split/dividend adjusted (auto_adjust) so trend indicators are not
broken by corporate actions. Every refresh records ``fetched_at`` (the time the
system actually had the data), per the project's look-ahead rule.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from stockmoney.desk.universe import ALL_SYMBOLS

DEFAULT_DIR = Path("data/desk")
FIELDS = ("open", "high", "low", "close", "volume")


@dataclass
class Bars:
    """Wide frames (index=trade date, columns=symbols), one per OHLCV field."""

    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.close.index

    def upto(self, day: pd.Timestamp) -> "Bars":
        """Truncate to rows <= day. Used by leak tests and live runs."""
        return Bars(*(getattr(self, f).loc[:day] for f in FIELDS))


def download(symbols: list[str], start: date, end: date | None = None) -> pd.DataFrame:
    """Long frame: symbol, date, open, high, low, close, volume, fetched_at."""
    import yfinance as yf

    end = end or date.today()
    raw = yf.download(
        symbols,
        start=start,
        end=end + timedelta(days=1),  # yfinance end is exclusive
        auto_adjust=True,
        group_by="ticker",
        progress=False,
        threads=True,
    )
    fetched_at = datetime.now(timezone.utc)
    frames = []
    for sym in symbols:
        if sym not in raw.columns.get_level_values(0):
            continue
        df = raw[sym].dropna(subset=["Close"]).reset_index()
        if df.empty:
            continue
        df.columns = [str(c).lower() for c in df.columns]
        df = df.rename(columns={"index": "date"})
        df["symbol"] = sym
        frames.append(df[["symbol", "date", *FIELDS]])
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None).dt.normalize()
    out["fetched_at"] = fetched_at
    return out


def refresh(data_dir: Path = DEFAULT_DIR, start: date = date(2014, 1, 1), lookback_days: int = 10) -> pd.DataFrame:
    """Incrementally update the cache. Re-downloads the last ``lookback_days``
    to pick up late corrections; full download when the cache is missing."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "ohlcv.parquet"
    if path.exists():
        cached = pd.read_parquet(path)
        missing = sorted(set(ALL_SYMBOLS) - set(cached["symbol"]))
        since = (cached["date"].max() - pd.Timedelta(days=lookback_days)).date()
        parts = [cached[cached["date"] < pd.Timestamp(since)], download(ALL_SYMBOLS, since)]
        if missing:
            parts.append(download(missing, start))
        merged = pd.concat(parts, ignore_index=True)
    else:
        merged = download(ALL_SYMBOLS, start)
    merged = merged.drop_duplicates(["symbol", "date"], keep="last").sort_values(["symbol", "date"])
    merged.to_parquet(path, index=False)
    return merged


def load(data_dir: Path = DEFAULT_DIR) -> pd.DataFrame:
    return pd.read_parquet(data_dir / "ohlcv.parquet")


def to_bars(long: pd.DataFrame) -> Bars:
    wide = {f: long.pivot(index="date", columns="symbol", values=f).sort_index() for f in FIELDS}
    return Bars(**wide)
