from datetime import datetime, timezone

import pytest

from stockmoney.api.live_quotes import QuoteCache, get_quotes


def test_get_quotes_computes_change_from_the_same_fresh_snapshot():
    def fake_fetch(symbols):
        return {"AAPL": {"price": 105.0, "prev_close": 100.0, "as_of": datetime(2026, 7, 11, 14, tzinfo=timezone.utc)}}

    cache = QuoteCache(fetch_fn=fake_fetch)
    result = get_quotes(["AAPL"], cache=cache)

    assert result["AAPL"]["price"] == 105.0
    assert result["AAPL"]["prev_close"] == 100.0
    assert result["AAPL"]["change_pct"] == pytest.approx(0.05)


def test_get_quotes_missing_quote_returns_all_none():
    cache = QuoteCache(fetch_fn=lambda symbols: {})  # yfinance had nothing for it
    result = get_quotes(["AAPL"], cache=cache)

    assert result["AAPL"] == {"price": None, "prev_close": None, "change_pct": None, "as_of": None}


def test_get_quotes_suppresses_implausible_change_as_likely_bad_data():
    # Real incident: a live smoke test showed several symbols with >50% (one
    # ~1300%) "moves" even when price and prev_close came from the same fresh
    # snapshot -- a data-quality artifact (bad tick / stale provider cache),
    # not a real trading day. Must not surface a fabricated-looking number.
    cache = QuoteCache(fetch_fn=lambda symbols: {
        "NFLX": {"price": 73.37, "prev_close": 1051.61, "as_of": datetime(2026, 7, 13, tzinfo=timezone.utc)}
    })
    result = get_quotes(["NFLX"], cache=cache)

    assert result["NFLX"] == {"price": None, "prev_close": None, "change_pct": None, "as_of": None}


def test_get_quotes_keeps_a_plausible_change():
    cache = QuoteCache(fetch_fn=lambda symbols: {
        "AAPL": {"price": 210.0, "prev_close": 200.0, "as_of": datetime(2026, 7, 13, tzinfo=timezone.utc)}  # +5%
    })
    result = get_quotes(["AAPL"], cache=cache)

    assert result["AAPL"]["price"] == 210.0
    assert result["AAPL"]["change_pct"] == pytest.approx(0.05)


def test_quote_cache_respects_ttl_and_serves_stale_within_window():
    calls = []

    def fake_fetch(symbols):
        calls.append(symbols)
        return {"AAPL": {"price": float(len(calls)), "prev_close": 1.0, "as_of": datetime.now(timezone.utc)}}

    cache = QuoteCache(ttl_seconds=10.0, fetch_fn=fake_fetch)
    first = cache.get(["AAPL"], now=1000.0)
    second = cache.get(["AAPL"], now=1005.0)  # within TTL -> served from cache, no new fetch
    third = cache.get(["AAPL"], now=1011.0)  # past TTL -> refetches

    assert first == second
    assert len(calls) == 2
    assert third["AAPL"]["price"] == 2.0


def test_quote_cache_is_independent_per_instance():
    # Regression guard against reintroducing shared module-level mutable state:
    # two independently constructed caches must never see each other's data.
    cache_a = QuoteCache(fetch_fn=lambda symbols: {"AAPL": {"price": 1.0, "prev_close": 1.0, "as_of": None}})
    cache_b = QuoteCache(fetch_fn=lambda symbols: {"AAPL": {"price": 2.0, "prev_close": 1.0, "as_of": None}})

    assert cache_a.get(["AAPL"])["AAPL"]["price"] == 1.0
    assert cache_b.get(["AAPL"])["AAPL"]["price"] == 2.0


def test_get_quotes_empty_symbol_list():
    cache = QuoteCache(fetch_fn=lambda symbols: {})
    assert get_quotes([], cache=cache) == {}
