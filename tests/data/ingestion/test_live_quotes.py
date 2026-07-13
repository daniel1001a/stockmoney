from datetime import timezone

import pandas as pd

from stockmoney.data.ingestion.live_quotes import fetch_live_quotes


def _fake_daily(symbols, period, interval, auto_adjust, group_by, progress):
    idx = pd.to_datetime(["2026-07-09", "2026-07-10"], utc=True)
    frames = {}
    for i, symbol in enumerate(symbols):
        frames[symbol] = pd.DataFrame({"Close": [100.0 + i, 102.0 + i]}, index=idx)
    return pd.concat(frames, axis=1)


def _fake_intraday(symbols, period, interval, auto_adjust, group_by, progress):
    idx = pd.to_datetime(["2026-07-10 13:30", "2026-07-10 13:31"], utc=True)
    frames = {}
    for i, symbol in enumerate(symbols):
        frames[symbol] = pd.DataFrame({"Close": [102.5 + i, 103.0 + i]}, index=idx)
    return pd.concat(frames, axis=1)


def _mock_downloads(monkeypatch, daily=_fake_daily, intraday=_fake_intraday):
    calls = {"n": 0}

    def dispatch(symbols, period, interval, auto_adjust, group_by, progress):
        calls["n"] += 1
        fn = daily if interval == "1d" else intraday
        return fn(symbols, period, interval, auto_adjust, group_by, progress)

    monkeypatch.setattr("yfinance.download", dispatch)
    return calls


def test_fetch_live_quotes_prefers_intraday_price_but_daily_prev_close(monkeypatch):
    _mock_downloads(monkeypatch)

    quotes = fetch_live_quotes(["AAPL", "MSFT"])

    assert set(quotes) == {"AAPL", "MSFT"}
    # price comes from the intraday bar (last one), prev_close from the daily
    # bar two rows back -- both same-snapshot-consistent, never mixed with a
    # separately-fetched/stale source.
    assert quotes["AAPL"]["price"] == 103.0
    assert quotes["AAPL"]["prev_close"] == 100.0
    assert quotes["AAPL"]["as_of"].tzinfo is not None


def test_fetch_live_quotes_falls_back_to_daily_when_no_intraday_bars(monkeypatch):
    def empty_intraday(symbols, period, interval, auto_adjust, group_by, progress):
        return pd.concat({s: pd.DataFrame({"Close": []}) for s in symbols}, axis=1)

    _mock_downloads(monkeypatch, intraday=empty_intraday)

    quotes = fetch_live_quotes(["AAPL"])

    # No intraday bars (e.g. market fully closed) -> price falls back to the
    # daily bar's own latest close, not silently dropped.
    assert quotes["AAPL"]["price"] == 102.0
    assert quotes["AAPL"]["prev_close"] == 100.0


def test_fetch_live_quotes_empty_symbol_list_is_a_noop(monkeypatch):
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("yfinance.download should not be called for an empty symbol list")

    monkeypatch.setattr("yfinance.download", _boom)
    assert fetch_live_quotes([]) == {}
    assert called["n"] == 0


def test_fetch_live_quotes_skips_symbol_with_fewer_than_two_daily_bars(monkeypatch):
    def one_bar_daily(symbols, period, interval, auto_adjust, group_by, progress):
        idx = pd.to_datetime(["2026-07-10"], utc=True)
        frames = {
            "AAPL": pd.DataFrame({"Close": [100.0]}, index=idx),
            "ZZZZ": pd.DataFrame({"Close": [None]}, index=idx),  # e.g. delisted/no data today
        }
        return pd.concat(frames, axis=1)

    _mock_downloads(monkeypatch, daily=one_bar_daily)

    quotes = fetch_live_quotes(["AAPL", "ZZZZ"])

    # AAPL has only 1 daily bar in this fixture -> can't compute prev_close ->
    # skipped, same as ZZZZ which has none at all.
    assert quotes == {}
