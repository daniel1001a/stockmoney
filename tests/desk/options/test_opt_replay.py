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
