"""Leak-safe daily per-symbol news features from the real GDELT GKG backfill.

Consumes `data/news_backfill/source=gdelt_gkg/symbol=<TICKER>/year=<YYYY>/
part.parquet` (see `scripts/backfill_news_real.py` and
`NEWS_BACKFILL_REPORT.md`) -- real, per-article, per-symbol GDELT GKG rows
with `available_ts` (the earliest verifiable "this system could have known
about it" timestamp; see that script's LOOK-AHEAD SAFETY docstring section).
This module turns that raw article stream into a small set of per-trading-day
features usable by a scoreboard `Strategy` (see `news_strategies.py`).

DECISION-TIME CONVENTION (read before trusting any number downstream)
-----------------------------------------------------------------------
The scoreboard harness (`stockmoney.backtest.scoreboard`) makes its trading
decision for trading day `d` using that day's own close (e.g.
`_sell_put_trades`'s `spot = cl[i]`) -- i.e. "decision time" is close-of-day
`d`. This module follows the SAME day-granularity convention already
established elsewhere in this codebase (`feature_matrix.py`'s "available_at
~= d's close", `_sell_put_trades`'s SMA/vol gate) rather than inventing a new
intraday cutoff: an article is attributed to trading day `d` iff

    prev_trading_day(d) < available_ts.astimezone(UTC) <= end_of_day_utc(d)

i.e. everything strictly after the PREVIOUS trading day's cutoff, up through
end-of-calendar-day `d` in UTC (this also correctly folds weekend/holiday
catch-up news into the next trading day, rather than silently dropping it).
This is a DELIBERATE, DOCUMENTED assumption, not an oversight: end-of-day-UTC
is a few hours later than the actual ~20:00-21:00 UTC US market close, so a
same-day after-hours article (e.g. a post-close earnings release) is treated
as "available for day d's decision" even though it strictly postdates the
printed close. This mirrors how every other daily feature in this repo
already treats "available as of d" at day granularity, not minute
granularity -- flagged here loudly per CLAUDE.md sec. 2's look-ahead
discipline rather than silently assumed.

No feature computed for trading day `d` EVER reads an article whose
`available_ts` falls on a later trading day than `d`. Rolling BASELINE
windows (z-score / acceleration) additionally EXCLUDE day `d` itself from
the "what's normal" reference (mirrors
`scoreboard._sell_put_trades`'s `rvs[max(0, i-LOOKBACK):i]`, which slices up
to but not including index `i`) -- a same-day news spike can never partly
explain away its own baseline.

FEATURES (one row per trading day actually present in `trade_dates`)
-----------------------------------------------------------------------
- `article_count`: raw count of matched articles attributed to this day (0 if
  none -- zero is a real, meaningful value, not missing).
- `tone_mean`: mean `sentiment_score` (~[-1,1], GDELT tone/100) of this day's
  articles; NaN if `article_count == 0`.
- `tone_zscore`: (today's tone_mean - trailing baseline mean-of-daily-tone) /
  trailing baseline std, baseline = up to `baseline_win` PRIOR trading days
  that had >=1 article (NaN if fewer than `min_baseline_n` such days exist,
  or today has no news).
- `volume_zscore`: same z-score construction but on `article_count` itself,
  baseline over ALL prior `baseline_win` trading days (zero-count days count
  as real observations here, unlike the tone baseline).
- `tone_accel`: mean tone over the trailing `short_win` days (today included,
  news-days only) minus mean tone over the `baseline_win`-day baseline
  (excludes the short window) -- a simple "is tone turning" second-derivative
  proxy. NaN if either side lacks data.

This module does NOT touch the live DuckDB and does NOT import
`stockmoney.backtest.scoreboard` (kept a one-way dependency: strategies
import features, not vice versa).
"""
from __future__ import annotations

import glob
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[3]
NEWS_ROOT = REPO_ROOT / "data" / "news_backfill" / "source=gdelt_gkg"

BASELINE_WIN = 20  # trailing trading days used as the "normal" reference
SHORT_WIN = 3       # trailing trading days used for the "current" side of tone_accel
MIN_BASELINE_N = 10  # minimum valid (news-bearing) baseline days before a z-score is trusted


@dataclass(frozen=True)
class NewsFeatureRow:
    trade_date: date
    article_count: int
    tone_mean: float       # NaN if article_count == 0
    tone_zscore: float     # NaN if insufficient baseline
    volume_zscore: float   # NaN if insufficient baseline
    tone_accel: float      # NaN if insufficient data on either side


def load_symbol_news_raw(symbol: str, news_root: Path = NEWS_ROOT) -> pl.DataFrame:
    """Load every partitioned year of real GKG news for `symbol`. Returns an
    empty (zero-row, correctly-typed) frame if this symbol has no backfilled
    news at all (e.g. an ETF like SOXL/SOXS, which isn't in
    `backfill_news_real.TICKER_ORG_PATTERNS` -- legitimately empty, not a
    bug)."""
    pattern = str(news_root / f"symbol={symbol}" / "year=*" / "part.parquet")
    files = sorted(glob.glob(pattern))
    if not files:
        return pl.DataFrame(
            schema={
                "available_ts": pl.Datetime(time_zone="UTC"),
                "sentiment_score": pl.Float64,
            }
        )
    df = pl.concat(
        [pl.read_parquet(f, columns=["available_ts", "sentiment_score"]) for f in files]
    )
    return df.sort("available_ts")


def _end_of_day_utc(d: date) -> datetime:
    """Cutoff timestamp for trading day `d`: start of the NEXT calendar day in
    UTC (see module docstring's DECISION-TIME CONVENTION). An article is
    attributed to `d` iff its available_ts is < this value and >= the prior
    trading day's cutoff."""
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1)


def compute_news_features(
    trade_dates: list[date],
    raw: pl.DataFrame,
    *,
    baseline_win: int = BASELINE_WIN,
    short_win: int = SHORT_WIN,
    min_baseline_n: int = MIN_BASELINE_N,
) -> list[NewsFeatureRow]:
    """Bucket `raw` (available_ts, sentiment_score) into the (prev_trading_day,
    trading_day] windows implied by `trade_dates` (must be sorted ascending,
    one row per trading day -- exactly what `ScoreboardContext.closes()`
    returns), then derive the rolling features. Pure function, no DB/IO.

    Leak-safety invariant (see tests/backtest/test_news_features.py): the
    feature row for `trade_dates[i]` is a deterministic function of
    `raw` rows with `available_ts < _end_of_day_utc(trade_dates[i])` ONLY --
    mutating any `raw` row with `available_ts >= _end_of_day_utc(trade_dates[i])`
    must never change row `i`'s output.
    """
    n = len(trade_dates)
    if n == 0:
        return []

    if raw.height == 0:
        ts = np.array([], dtype="datetime64[ns]")
        tone = np.array([], dtype=float)
    else:
        ts = raw["available_ts"].to_numpy()
        tone = raw["sentiment_score"].to_numpy().astype(float)

    cutoffs = np.array(
        [np.datetime64(_end_of_day_utc(d).replace(tzinfo=None), "ns") for d in trade_dates]
    )
    # boundary[i] = index into ts of the first article attributed to
    # trade_dates[i] or later; boundary[-1] (implicit, = 0) is "before the
    # dawn of time" for i==0.
    boundaries = np.searchsorted(ts, cutoffs, side="left")

    article_count = np.zeros(n, dtype=int)
    tone_mean = np.full(n, np.nan)
    prev_b = 0
    for i in range(n):
        b = boundaries[i]
        seg = tone[prev_b:b]
        article_count[i] = len(seg)
        if len(seg) > 0:
            tone_mean[i] = float(seg.mean())
        prev_b = b

    has_news = article_count > 0

    tone_zscore = np.full(n, np.nan)
    volume_zscore = np.full(n, np.nan)
    tone_accel = np.full(n, np.nan)

    for i in range(n):
        lo = max(0, i - baseline_win)
        # --- tone baseline: only news-bearing PRIOR days, days lo..i-1 -----
        base_tone_mask = has_news[lo:i]
        base_tone_vals = tone_mean[lo:i][base_tone_mask]
        if has_news[i] and len(base_tone_vals) >= min_baseline_n:
            mu = float(base_tone_vals.mean())
            sd = float(base_tone_vals.std(ddof=1)) if len(base_tone_vals) > 1 else 0.0
            if sd > 0:
                tone_zscore[i] = (tone_mean[i] - mu) / sd

        # --- volume baseline: ALL prior days (zero counts are real) -------
        base_vol_vals = article_count[lo:i].astype(float)
        if len(base_vol_vals) >= min_baseline_n:
            mu = float(base_vol_vals.mean())
            sd = float(base_vol_vals.std(ddof=1)) if len(base_vol_vals) > 1 else 0.0
            if sd > 0:
                volume_zscore[i] = (article_count[i] - mu) / sd

        # --- tone acceleration: short window (news-days only, incl today) -
        s_lo = max(0, i - short_win + 1)
        short_mask = has_news[s_lo : i + 1]
        short_vals = tone_mean[s_lo : i + 1][short_mask]
        if len(short_vals) > 0 and len(base_tone_vals) >= min_baseline_n:
            tone_accel[i] = float(short_vals.mean()) - float(base_tone_vals.mean())

    return [
        NewsFeatureRow(
            trade_date=trade_dates[i],
            article_count=int(article_count[i]),
            tone_mean=float(tone_mean[i]),
            tone_zscore=float(tone_zscore[i]),
            volume_zscore=float(volume_zscore[i]),
            tone_accel=float(tone_accel[i]),
        )
        for i in range(n)
    ]


def news_features_for_symbol(
    symbol: str,
    trade_dates: list[date],
    *,
    news_root: Path = NEWS_ROOT,
    baseline_win: int = BASELINE_WIN,
    short_win: int = SHORT_WIN,
    min_baseline_n: int = MIN_BASELINE_N,
) -> list[NewsFeatureRow]:
    """Convenience wrapper: load + compute in one call. Symbols with no
    backfilled news (e.g. SOXL) get an all-NaN/zero-count feature series
    (never an exception) so callers can gate on `article_count == 0 ->
    unknown, treat as neutral` uniformly."""
    raw = load_symbol_news_raw(symbol, news_root=news_root)
    return compute_news_features(
        trade_dates, raw, baseline_win=baseline_win, short_win=short_win, min_baseline_n=min_baseline_n
    )
