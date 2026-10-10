"""Daily OHLCV for the desk: download from yfinance, cache as one parquet.

Prices are split/dividend adjusted (auto_adjust) so trend indicators are not
broken by corporate actions. Every refresh records ``fetched_at`` (the time the
system actually had the data), per the project's look-ahead rule.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from stockmoney.desk.universe import ALL_SYMBOLS

log = logging.getLogger(__name__)

DEFAULT_DIR = Path("data/desk")
FIELDS = ("open", "high", "low", "close", "volume")
RESTATE_TOL = 1e-6  # relative close difference that marks a changed adjustment basis


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


def restated_symbols(cached: pd.DataFrame, fresh: pd.DataFrame, tol: float = RESTATE_TOL) -> list[str]:
    """Symbols whose freshly downloaded closes differ from the cached closes on
    the overlapping dates. ``auto_adjust`` rewrites the whole history after a
    split or dividend, so any such difference means the cached rows sit on a
    different adjustment basis than the new ones and must be replaced."""
    both = cached[["symbol", "date", "close"]].merge(
        fresh[["symbol", "date", "close"]], on=["symbol", "date"], suffixes=("_old", "_new"))
    if both.empty:
        return []
    rel = (both["close_new"] / both["close_old"] - 1).abs()
    bad = both.loc[(rel > tol) | rel.isna(), "symbol"]
    return sorted(set(bad))


def _restate(symbols: list[str], start: date) -> pd.DataFrame:
    """Full-history download for symbols whose adjustment basis changed. Empty
    frame (and a warning) when it fails: the caller then keeps the old rows."""
    if not symbols:
        return pd.DataFrame(columns=["symbol", "date", *FIELDS, "fetched_at"])
    log.info("price basis changed for %s; re-downloading full history", symbols)
    try:
        return download(symbols, start)
    except Exception as e:  # never splice two bases together
        log.warning("full re-download of %s failed (%s); keeping cached history", symbols, e)
        return pd.DataFrame(columns=["symbol", "date", *FIELDS, "fetched_at"])


def refresh(data_dir: Path = DEFAULT_DIR, start: date = date(2014, 1, 1), lookback_days: int = 10) -> pd.DataFrame:
    """Incrementally update the cache. Re-downloads the last ``lookback_days``
    to pick up late corrections; full download when the cache is missing.

    Adjusted prices change retroactively (splits, dividends), so the overlap
    between cache and download is compared per symbol; a symbol whose overlap
    disagrees gets its full history re-downloaded and replaced, never spliced.
    ``fetched_at`` stays per row: untouched cached rows keep their own stamp."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "ohlcv.parquet"
    if path.exists():
        cached = pd.read_parquet(path)
        missing = sorted(set(ALL_SYMBOLS) - set(cached["symbol"]))
        since = pd.Timestamp((cached["date"].max() - pd.Timedelta(days=lookback_days)).date())
        fresh = download(ALL_SYMBOLS, since.date())
        stale = restated_symbols(cached[cached["date"] >= since], fresh)
        full = _restate(stale, start)
        redone = set(full["symbol"])
        # restated symbols take their whole history from the full download; one
        # that could not be restated keeps its old, internally consistent rows
        # and gets none of the new-basis ones
        keep = cached[(cached["date"] < since) | cached["symbol"].isin(set(stale) - redone)]
        keep = keep[~keep["symbol"].isin(redone)]
        parts = [keep, fresh[~fresh["symbol"].isin(stale)], full]
        if missing:
            parts.append(download(missing, start))
        merged = pd.concat([p for p in parts if not p.empty], ignore_index=True)
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
