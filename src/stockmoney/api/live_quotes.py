"""API-layer glue for the live-price overlay: rate-limits how often the
upstream yfinance call can actually fire, and turns a cached quote into the
{price, prev_close, change_pct, as_of} shape the frontend consumes.

Why a cache: /api/quotes may be polled every ~60s by every open tab
(refreshCadence.py's power-hour cadence) -- without a floor, N tabs would each
trigger their own yfinance round-trip. A short TTL means the whole app shares
one upstream fetch per window, independent of how many clients are polling.
`QuoteCache` is a small object (not module-level mutable state) specifically
so tests can construct a fresh one instead of fighting shared global state --
a real deployment holds one instance for the process lifetime (single-box
scope, same assumption every other part of this repo makes).

price/prev_close both come from data.ingestion.live_quotes' OWN fresh
snapshot (see that module's docstring for why -- mixing a fresh live price
with our possibly-stale ohlcv_daily close produced fabricated-looking
double-digit "moves" for most of the watchlist in a live smoke test). This
module does not touch the database at all.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from stockmoney.data.ingestion.live_quotes import fetch_live_quotes

CACHE_TTL_SECONDS = 20.0

# Sanity guard: even with both numbers from the same fresh snapshot, a single-
# day move past this threshold for a name on this watchlist is far more likely
# a data-quality artifact (bad tick, split-adjustment glitch, stale cache on
# yfinance's end) than a genuine move -- rather than display a number nobody
# can vouch for, suppress it entirely. Found via a live smoke test where
# several symbols showed >50% (and in one case, ~1300%) implausible deltas.
MAX_PLAUSIBLE_ABS_CHANGE = 0.5


@dataclass
class QuoteCache:
    ttl_seconds: float = CACHE_TTL_SECONDS
    fetch_fn: Callable[[list[str]], dict[str, dict]] = fetch_live_quotes
    _quotes: dict[str, dict] = field(default_factory=dict)
    _fetched_at: float | None = None

    def get(self, symbols: list[str], *, now: float | None = None) -> dict[str, dict]:
        now = time.monotonic() if now is None else now
        if self._fetched_at is None or (now - self._fetched_at) >= self.ttl_seconds:
            self._quotes = self.fetch_fn(symbols)
            self._fetched_at = now
        return self._quotes


# One process-lifetime cache for the real API route (single-box scope, see
# module docstring); route handlers pass this in so tests can substitute
# their own QuoteCache() instead of sharing this instance.
default_cache = QuoteCache()


def get_quotes(symbols: list[str], *, cache: QuoteCache | None = None) -> dict[str, dict]:
    cache = cache or default_cache
    quotes = cache.get(symbols)

    out: dict[str, dict] = {}
    for symbol in symbols:
        quote = quotes.get(symbol)
        price = quote["price"] if quote else None
        prev_close = quote["prev_close"] if quote else None
        as_of = quote["as_of"] if quote else None
        change_pct = (
            (price - prev_close) / prev_close
            if price is not None and prev_close is not None and prev_close
            else None
        )
        if change_pct is not None and abs(change_pct) > MAX_PLAUSIBLE_ABS_CHANGE:
            price, prev_close, as_of, change_pct = None, None, None, None

        out[symbol] = {
            "price": price, "prev_close": prev_close, "change_pct": change_pct, "as_of": as_of,
        }
    return out
