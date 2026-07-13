"""Batched intraday quote fetch (yfinance free tier: ~15min delayed) for the
live-price overlay on the dashboard.

This is DELIBERATELY separate from every other ingestion module in this
package: it never writes to the database. Module A / the daily batch pipeline
(CLAUDE.md section 17: no intraday streaming in Phase 1) is completely
untouched by this -- it is a pure, ephemeral "what's the tape doing right now"
read that the API layer (api/live_quotes.py) caches in-process and serves
alongside the price/change context. No model, feature, or prediction ever
sees this data.

`price` AND `prev_close` are BOTH read from ONE fresh daily-bar snapshot
(`period="5d", interval="1d"`), never from our own possibly-stale
`ohlcv_daily` table. This was a real bug, found via a live smoke test: mixing
a fresh live pull with `ohlcv_daily`'s last backfilled close (itself anchored
to build_live.py's fixed `--end` date, which goes stale the moment real time
moves past it) produced double-digit-percent "moves" for nearly the entire
watchlist -- an artifact of comparing two different snapshots in time, not
real intraday volatility. Deriving both numbers from the SAME fresh call
removes that whole class of false signal; verified directly (AAPL/JPM/NVDA/
AMZN all show small, plausible same-day deltas once compared this way).

A second, separate intraday call (`period="1d", interval="1m"`) supplies a
tighter `price` when today's session has minute bars available (pre/regular/
after-hours); it always falls back to the daily bar's own latest close when
intraday data isn't there (weekend, holiday, or a symbol yfinance has no
minute data for) -- never raises, so one bad symbol can't take down the whole
refresh.

Batched the same way fetch_ohlcv already is (yfinance_ohlcv.py): a fixed
number of yf.download calls for the whole watchlist (not one per symbol), so
refreshing the whole board costs a couple of HTTP round-trips, not sixty-two.
"""
from __future__ import annotations

import warnings
from datetime import timezone


def fetch_live_quotes(symbols: list[str]) -> dict[str, dict]:
    """{symbol: {"price": float, "prev_close": float, "as_of": aware datetime}}
    for every symbol with at least 2 daily bars available (so a change % is
    computable). Symbols with nothing available (bad ticker, yfinance hiccup)
    are simply absent from the result -- never raises."""
    if not symbols:
        return {}

    warnings.filterwarnings("ignore")
    import yfinance as yf

    daily = yf.download(
        symbols, period="5d", interval="1d", auto_adjust=False, group_by="ticker", progress=False,
    )
    intraday = yf.download(
        symbols, period="1d", interval="1m", auto_adjust=False, group_by="ticker", progress=False,
    )

    out: dict[str, dict] = {}
    for symbol in symbols:
        try:
            daily_close = daily[symbol]["Close"].dropna()
        except (KeyError, IndexError):
            continue
        if len(daily_close) < 2:
            continue  # need at least yesterday + today to compute a change
        prev_close = float(daily_close.iloc[-2])

        price = float(daily_close.iloc[-1])
        as_of = daily_close.index[-1].to_pydatetime()
        try:
            intraday_close = intraday[symbol]["Close"].dropna()
        except (KeyError, IndexError):
            intraday_close = None
        if intraday_close is not None and not intraday_close.empty:
            price = float(intraday_close.iloc[-1])
            as_of = intraday_close.index[-1].to_pydatetime()

        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        out[symbol] = {"price": price, "prev_close": prev_close, "as_of": as_of.astimezone(timezone.utc)}
    return out
