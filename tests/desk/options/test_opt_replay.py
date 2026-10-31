"""Replay cache fetcher and synthetic quote model (no network: a fake data client)."""
import json
from datetime import date, datetime, time, timedelta

import pandas as pd

from stockmoney.desk.options import replay
from stockmoney.desk.options.config import OptionsConfig
from stockmoney.desk.options.marketdata import fetch_replay_cache

from opt_fakes import ET, day_bars, history_bars

DAY = date(2026, 10, 9)  # in the past: the fetcher never asks for data newer than 16 minutes


class FakeData:
    """The AlpacaOptionsData methods fetch_replay_cache uses. Option bars
    answer like this account does in real time (refused)."""
    stock_feed = "iex"

    def __init__(self):
        df = pd.concat([history_bars(day=DAY), day_bars([600.0 + 0.01 * i for i in range(390)], day=DAY)])
        self.df = df[~df.index.duplicated(keep="last")].sort_index()

    def minute_bars(self, symbol, start, end):
        return self.df[(self.df.index >= pd.Timestamp(start)) & (self.df.index <= pd.Timestamp(end))]

    def contracts(self, und, lo, hi, status="active", **kw):
        if status != "active":
            return []
        return [{"symbol": f"SPY{lo:%y%m%d}{r[0].upper()}00{k}000", "underlying_symbol": "SPY", "type": r,
                 "strike_price": str(k), "expiration_date": str(lo), "open_interest": "500",
                 "open_interest_date": "2026-10-12"} for k in range(596, 606) for r in ("call", "put")]

    def option_chain(self, und, lo, hi):
        return []

    def option_bars(self, occs, start, end):
        raise RuntimeError('{"message":"OPRA agreement is not signed"}')


def test_fetch_cache_without_option_bars_and_synthetic_quotes(tmp_path):
    d = fetch_replay_cache(DAY, ["SPY"], tmp_path, [(0, 0), (1, 3)], data=FakeData(), history_sessions=21)
    meta = json.loads((d / "meta.json").read_text())
    assert meta["option_bar_rows"] == 0 and "OPRA" in meta["option_bars_note"]
    st = pd.read_parquet(d / "stock_iex.parquet")
    assert pd.to_datetime(st["t"], utc=True).dt.tz_convert(ET).dt.date.nunique() == 22  # 21 sessions + the day
    m = replay.ReplayMarket(d, "iex", OptionsConfig())
    now = datetime.combine(DAY, time(10, 0), ET)
    occ = next(o for o in m.contracts if o.startswith("SPY261009C"))
    q, src = m.quote(occ, now)
    assert q is not None and src == "bs_default_iv" and q.volume == 0 and q.ask > q.bid > 0
    assert q.as_of == now - timedelta(seconds=1)


class SpreadFakeData(FakeData):
    """Weekly calls 7 to 21 days out on every strike asked for."""

    def __init__(self):
        super().__init__()
        self.asked = []

    def contracts(self, und, lo, hi, status="active", **kw):
        if (hi - lo).days < 7:
            return super().contracts(und, lo, hi, status, **kw)
        self.asked.append((lo, hi, float(kw["strike_price_gte"]), float(kw["strike_price_lte"])))
        if status != "active":
            return []
        out = []
        for exp in (DAY + timedelta(days=7), DAY + timedelta(days=14), DAY + timedelta(days=21)):
            for k in range(int(float(kw["strike_price_gte"])) + 1, int(float(kw["strike_price_lte"])) + 1, 5):
                for r in ("call", "put"):
                    out.append({"symbol": f"SPY{exp:%y%m%d}{r[0].upper()}00{k}000", "underlying_symbol": "SPY",
                                "type": r, "strike_price": str(k), "expiration_date": str(exp),
                                "open_interest": "500", "open_interest_date": "2026-10-12"})
        return out


def test_fetch_cache_adds_the_spread_chain(tmp_path):
    data = SpreadFakeData()
    d = fetch_replay_cache(DAY, ["SPY"], tmp_path, [(0, 0), (1, 3)], data=data, history_sessions=21,
                           spread_window=(7, 21, 14))
    lo, hi, k_lo, k_hi = data.asked[0]
    assert (lo, hi) == (DAY + timedelta(days=7), DAY + timedelta(days=21))
    assert k_hi >= 600 * 1.10  # wide enough for a 0.30-delta short two weeks out
    c = pd.read_parquet(d / "contracts.parquet")
    spread = c[c.expiry == str(DAY + timedelta(days=14))]
    assert len(spread) > 0 and set(spread.right) == {"C"}
    assert not (c.expiry == str(DAY + timedelta(days=7))).any()  # only the target expiry


def test_fetch_cache_without_spread_window_is_unchanged(tmp_path):
    data = SpreadFakeData()
    fetch_replay_cache(DAY, ["SPY"], tmp_path, [(0, 0), (1, 3)], data=data, history_sessions=21)
    assert data.asked == []


class NoSpreadChainData(SpreadFakeData):
    """A later session: bars for ``day``, and no 7-21 day calls on offer."""

    def __init__(self, day):
        FakeData.__init__(self)
        self.asked = []
        df = pd.concat([history_bars(day=day), day_bars([610.0 + 0.01 * i for i in range(390)], day=day)])
        self.df = df[~df.index.duplicated(keep="last")].sort_index()

    def contracts(self, und, lo, hi, status="active", **kw):
        if (hi - lo).days < 7:
            return FakeData.contracts(self, und, lo, hi, status, **kw)
        return []


def test_fetch_cache_carries_an_earlier_days_spread_chain_forward(tmp_path):
    # A spread opened on DAY must still be quotable at its own strikes and
    # expiry on the next session, whatever that session's own chain holds.
    first = fetch_replay_cache(DAY, ["SPY"], tmp_path, [(0, 0), (1, 3)], data=SpreadFakeData(),
                               history_sessions=21, spread_window=(7, 21, 14))
    held = pd.read_parquet(first / "contracts.parquet")
    held = set(held[held.expiry == str(DAY + timedelta(days=14))].occ)
    nxt = date(2026, 10, 12)
    d = fetch_replay_cache(nxt, ["SPY"], tmp_path, [(0, 0), (1, 3)], data=NoSpreadChainData(nxt),
                           history_sessions=21, spread_window=(7, 21, 14))
    c = pd.read_parquet(d / "contracts.parquet")
    assert held and held <= set(c.occ)
    m = replay.ReplayMarket(d, "iex", OptionsConfig())
    q, _ = m.quote(sorted(held)[0], datetime.combine(nxt, time(10, 0), ET))
    assert q is not None and q.ask > q.bid >= 0
    # an expired call is not carried
    later = fetch_replay_cache(DAY + timedelta(days=17), ["SPY"], tmp_path, [(0, 0), (1, 3)],
                               data=NoSpreadChainData(DAY + timedelta(days=17)), history_sessions=21,
                               spread_window=(7, 21, 14))
    assert not set(pd.read_parquet(later / "contracts.parquet").occ) & held
