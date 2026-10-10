"""Price cache refresh across a split/dividend (review finding H2). The
download function is replaced by a fake market; nothing touches the network."""
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from stockmoney.desk import data

SYMS = ["SPY", "AAPL", "MSFT"]
DAYS = pd.bdate_range("2024-01-02", periods=80)


class FakeMarket:
    """Adjusted daily closes per symbol, as a download would return them.
    ``last`` is the last available session index; ``scale`` multiplies a
    symbol's whole downloaded history, which is what auto_adjust does when a
    split or dividend happens: the new rows are continuous with the rescaled
    history, but the cache still holds the old values."""

    def __init__(self):
        self.last = 40
        self.scale: dict[str, float] = {}
        self.calls: list[tuple[list[str], date]] = []
        self.fetch_n = 0
        self.fail_full = False

    def base(self, sym: str, i: int) -> float:
        return 100.0 + 10 * SYMS.index(sym) + i * 0.3 + (i % 3) * 0.1

    def close(self, sym: str, i: int) -> float:
        return self.base(sym, i) * self.scale.get(sym, 1.0)

    def download(self, symbols, start, end=None):
        self.calls.append((list(symbols), start))
        if self.fail_full and start < date(2024, 2, 1):
            raise RuntimeError("network down")
        self.fetch_n += 1
        stamp = datetime(2024, 6, 1, tzinfo=timezone.utc) + timedelta(hours=self.fetch_n)
        rows = []
        for sym in symbols:
            for i, d in enumerate(DAYS[: self.last + 1]):
                if d.date() < start:
                    continue
                c = self.close(sym, i)
                rows.append({"symbol": sym, "date": d, "open": c, "high": c * 1.01, "low": c * 0.99,
                             "close": c, "volume": 1e6, "fetched_at": stamp})
        return pd.DataFrame(rows)


@pytest.fixture
def market(monkeypatch):
    m = FakeMarket()
    monkeypatch.setattr(data, "download", m.download)
    monkeypatch.setattr(data, "ALL_SYMBOLS", SYMS)
    return m


def series(frame: pd.DataFrame, sym: str) -> pd.Series:
    return frame[frame.symbol == sym].set_index("date")["close"].sort_index()


def test_split_triggers_full_restatement_not_a_spliced_cliff(market, tmp_path):
    start = date(2024, 1, 1)
    data.refresh(tmp_path, start=start)
    first_stamp = pd.read_parquet(tmp_path / "ohlcv.parquet")["fetched_at"].max()

    market.last = 46                   # six more sessions
    market.scale["AAPL"] = 0.5  # 2:1 split: the whole adjusted history halves
    market.calls.clear()
    merged = data.refresh(tmp_path, start=start)

    aapl = series(merged, "AAPL")
    expected = pd.Series([market.close("AAPL", i) for i in range(47)], index=DAYS[:47])
    pd.testing.assert_series_equal(aapl, expected, check_names=False, check_freq=False)
    assert (aapl.pct_change().abs().dropna() < 0.05).all()  # no cliff anywhere

    # exactly one extra, full-history download, and only for the restated symbol
    assert [(s, d) for s, d in market.calls if d == start] == [(["AAPL"], start)]
    # untouched symbols keep their old per-row fetched_at; recent rows are re-stamped
    msft = merged[merged.symbol == "MSFT"].set_index("date")["fetched_at"]
    assert msft.iloc[0] == first_stamp
    assert msft.iloc[-1] > first_stamp
    assert len(merged) == len(SYMS) * 47


def test_no_restatement_means_no_full_download(market, tmp_path):
    start = date(2024, 1, 1)
    data.refresh(tmp_path, start=start)
    market.last = 44
    market.calls.clear()
    merged = data.refresh(tmp_path, start=start)
    assert all(d != start for _, d in market.calls)
    assert len(merged) == len(SYMS) * 45


def test_dividend_sized_change_is_detected(market, tmp_path):
    start = date(2024, 1, 1)
    data.refresh(tmp_path, start=start)
    market.last = 44
    market.scale["MSFT"] = 0.995  # 0.5% dividend adjustment
    merged = data.refresh(tmp_path, start=start)
    expected = pd.Series([market.close("MSFT", i) for i in range(45)], index=DAYS[:45])
    pd.testing.assert_series_equal(series(merged, "MSFT"), expected, check_names=False, check_freq=False)


def test_failed_full_redownload_keeps_one_consistent_basis(market, tmp_path):
    start = date(2024, 1, 1)
    data.refresh(tmp_path, start=start)
    market.last = 46
    market.scale["AAPL"] = 0.5
    market.fail_full = True
    merged = data.refresh(tmp_path, start=start)
    aapl = series(merged, "AAPL")
    assert aapl.index[-1] == DAYS[40]  # stale but not spliced
    assert (aapl.pct_change().abs().dropna() < 0.05).all()
    assert series(merged, "MSFT").index[-1] == DAYS[46]  # others still advance


def test_restated_symbols_helper_tolerance():
    old = pd.DataFrame({"symbol": ["A", "A", "B"], "date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-02"]),
                        "close": [100.0, 101.0, 50.0]})
    new = old.copy()
    new.loc[1, "close"] = 101.0 * (1 + 5e-7)   # below tolerance
    new.loc[2, "close"] = 50.0 * (1 + 2e-6)    # above tolerance
    assert data.restated_symbols(old, new) == ["B"]
