"""Read-only Alpaca market data for the replay cache fetcher. NO order
methods exist here. The live session does not use this module: it reads and
trades through line A's AlpacaBroker (execution/alpaca.py), which implements
the same section 8 data surface.

Verified 2026-10-12 against this account (read-only calls):
- recent SIP stock data is not permitted ("subscription does not permit
  querying recent SIP data"), so the live session uses the IEX feed; SIP is
  fine for history older than 15 minutes (the replay cache);
- the OPRA agreement is not signed, so option snapshots come from the
  "indicative" feed (Alpaca: trades delayed, quotes modified). Shadow fills
  priced on it are an approximation until OPRA is available;
- option snapshots carry greeks and IV but no open interest; OI comes from
  the contracts endpoint (daily value) and is cached per day.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from stockmoney.desk.options._iface import OptionContract, parse_occ

log = logging.getLogger("desk.options.data")
ET = ZoneInfo("America/New_York")


def _ts(v) -> datetime | None:
    if not v:
        return None
    return pd.Timestamp(v).tz_convert("UTC").to_pydatetime()


def bars_frame(raw: list[dict]) -> pd.DataFrame:
    """Alpaca raw bar dicts (t,o,h,l,c,v,vw) -> the section 8 DataFrame."""
    if not raw:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "vwap"])
    df = pd.DataFrame(raw)
    df.index = pd.DatetimeIndex(pd.to_datetime(df["t"], utc=True)).tz_convert(ET)
    out = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vw": "vwap"})
    cols = [c for c in ("open", "high", "low", "close", "volume", "vwap") if c in out]
    return out[cols].astype(float).sort_index()


def contract_from_snapshot(occ: str, snap: dict, today: date | None = None,
                           oi: int | None = None) -> OptionContract:
    o = parse_occ(occ)
    und, expiry, right, strike = o.root, o.expiry, o.right, o.strike
    q = snap.get("latestQuote") or {}
    tr = snap.get("latestTrade") or {}
    g = snap.get("greeks") or {}
    db = snap.get("dailyBar") or {}
    vol = None
    if db:
        bar_day = pd.Timestamp(db.get("t")).tz_convert(ET).date() if db.get("t") else None
        vol = int(db.get("v") or 0) if (today is None or bar_day == today) else 0
    return OptionContract(
        occ=occ, underlying=und, right=right, strike=strike, expiry=expiry,
        bid=q.get("bp"), ask=q.get("ap"), last=tr.get("p"), volume=vol, open_interest=oi,
        delta=g.get("delta"), iv=snap.get("impliedVolatility"), as_of=_ts(q.get("t")))


class AlpacaOptionsData:
    """Section 8 data surface over alpaca-py (raw responses). Read-only."""

    is_paper = True  # no order path exists; the engine treats it as shadow-only (no ``submit``)

    def __init__(self, key: str | None = None, secret: str | None = None, stock_feed: str = "iex"):
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.historical.stock import StockHistoricalDataClient

        from stockmoney.desk.execution.alpaca import install_timeout, resolve_env
        key = key or os.environ.get("ALPACA_API_KEY", "")
        secret = secret or os.environ.get("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
        self._key, self._secret = key, secret
        self._paper = resolve_env() == "paper"
        self.stock = StockHistoricalDataClient(api_key=key, secret_key=secret, raw_data=True)
        self.option = OptionHistoricalDataClient(api_key=key, secret_key=secret, raw_data=True)
        for c in (self.stock, self.option):
            install_timeout(c)
        self.stock_feed = stock_feed
        self._oi: dict[tuple, dict[str, int]] = {}

    # ------------------------------------------------------------------ section 8
    def minute_bars(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start, end=end,
                               feed=DataFeed(self.stock_feed))
        raw = self.stock.get_stock_bars(req)
        return bars_frame((raw or {}).get(symbol, []))

    def stock_quote(self, symbol: str) -> tuple[float, float] | None:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestQuoteRequest
        raw = self.stock.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol,
                                                                        feed=DataFeed(self.stock_feed)))
        q = (raw or {}).get(symbol) or {}
        bid, ask = q.get("bp"), q.get("ap")
        if not bid or not ask or ask < bid:
            return None
        return float(bid), float(ask)

    def option_chain(self, underlying: str, expiry_min: date, expiry_max: date) -> list[OptionContract]:
        from alpaca.data.requests import OptionChainRequest
        raw = self.option.get_option_chain(OptionChainRequest(
            underlying_symbol=underlying, expiration_date_gte=expiry_min, expiration_date_lte=expiry_max))
        oi = self._open_interest(underlying, expiry_min, expiry_max)
        today = datetime.now(ET).date()
        return [contract_from_snapshot(occ, s, today, oi.get(occ)) for occ, s in (raw or {}).items()]

    def option_quote(self, occ: str) -> OptionContract | None:
        from alpaca.data.requests import OptionSnapshotRequest
        raw = self.option.get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=occ))
        s = (raw or {}).get(occ)
        return contract_from_snapshot(occ, s, datetime.now(ET).date()) if s else None

    def premarket_dollar_volume(self, symbol: str, day: date, now: datetime) -> float | None:
        """Premarket (04:00 to 09:30 ET) dollar volume on SIP, as far as the
        16-minute delay allows at ``now``. Used by gap_go and the scan."""
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        start = datetime.combine(day, time(4, 0), ET)
        end = min(datetime.combine(day, time(9, 30), ET), now - timedelta(minutes=16))
        if end <= start:
            return None
        raw = self.stock.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                                                         start=start, end=end, feed=DataFeed.SIP))
        df = bars_frame((raw or {}).get(symbol, []))
        df = df[df.index < pd.Timestamp(datetime.combine(day, time(9, 30), ET))]
        return float((df["close"] * df["volume"]).sum())

    # ------------------------------------------------------------------ helpers
    def _open_interest(self, underlying: str, lo: date, hi: date) -> dict[str, int]:
        """Daily OI from the contracts endpoint, cached per (underlying, range,
        day). Failure is not fatal: OI is then unknown and not filtered on."""
        key = (underlying, lo, hi, datetime.now(ET).date())
        if key not in self._oi:
            try:
                self._oi[key] = {c["symbol"]: int(c["open_interest"]) for c in
                                 self.contracts(underlying, lo, hi) if c.get("open_interest") not in (None, "")}
            except Exception as e:
                log.warning("open interest for %s unavailable: %s", underlying, e)
                self._oi[key] = {}
        return self._oi[key]

    def contracts(self, underlying: str, lo: date, hi: date, status: str = "active", **kw) -> list[dict]:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import AssetStatus
        from alpaca.trading.requests import GetOptionContractsRequest
        from stockmoney.desk.execution.alpaca import install_timeout
        tc = TradingClient(self._key, self._secret, paper=self._paper, raw_data=True)
        install_timeout(tc)  # one hung open-interest page must not eat the tick's alarm
        out, token = [], None
        while True:
            resp = tc.get_option_contracts(GetOptionContractsRequest(
                underlying_symbols=[underlying], expiration_date_gte=lo, expiration_date_lte=hi,
                status=AssetStatus(status), limit=10000, page_token=token, **kw))
            out += resp.get("option_contracts", [])
            token = resp.get("next_page_token")
            if not token:
                return out

    def option_bars(self, occs: list[str], start: datetime, end: datetime) -> dict[str, list[dict]]:
        from alpaca.data.requests import OptionBarsRequest
        from alpaca.data.timeframe import TimeFrame
        out: dict[str, list[dict]] = {}
        for i in range(0, len(occs), 100):
            raw = self.option.get_option_bars(OptionBarsRequest(symbol_or_symbols=occs[i:i + 100],
                                                                timeframe=TimeFrame.Minute, start=start, end=end))
            for k, v in (raw or {}).items():
                out.setdefault(k, []).extend(v)
        return out


# ------------------------------------------------------------------ replay cache

def fetch_replay_cache(day: date, symbols: list[str], out_dir: Path, buckets: list[tuple[int, int]],
                       data: AlpacaOptionsData | None = None, history_sessions: int = 21) -> Path:
    """Download what a replay of ``day`` needs, read-only API calls only:
    1-minute stock bars (IEX and SIP, premarket included) for ``day`` and the
    ``history_sessions`` sessions before it (the RVOL profile and the
    previous close); the option contracts near the money for each expiry
    bucket with their open interest (contracts endpoint) and their IV in the
    current indicative snapshot (taken NOW, i.e. after the day: the replay's
    synthetic quote model uses it as the IV level); and option 1-minute bars
    when the account may read them (this account may not: HTTP 403, logged
    and left empty). Writes parquet + meta.json under ``out_dir/<day>/``."""
    from stockmoney.desk.options.engine import _prev_trading_day
    from stockmoney.desk.options.select import trading_day_offset
    data = data or AlpacaOptionsData()
    d = out_dir / str(day)
    d.mkdir(parents=True, exist_ok=True)
    first = day
    for _ in range(history_sessions):
        first = _prev_trading_day(first)
    start = datetime.combine(first, time(4, 0), ET)
    # recent SIP data is not permitted without a subscription: stay 16 minutes behind real time
    end = min(datetime.combine(day, time(20, 0), ET), datetime.now(timezone.utc) - timedelta(minutes=16))
    stock_rows = {"sip": [], "iex": []}
    for feed in ("sip", "iex"):
        data.stock_feed = feed
        for s in symbols:
            try:
                df = data.minute_bars(s, start, end)
            except Exception as e:
                log.warning("stock bars %s %s: %s", s, feed, e)
                continue
            if not df.empty:
                stock_rows[feed].append(df.assign(symbol=s))
    for feed, parts in stock_rows.items():
        if parts:
            df = pd.concat(parts)
            df.index.name = "t"
            df.reset_index().to_parquet(d / f"stock_{feed}.parquet", index=False)
    ref = pd.read_parquet(d / ("stock_iex.parquet" if stock_rows["iex"] else "stock_sip.parquet"))
    ref["t"] = pd.to_datetime(ref["t"], utc=True).dt.tz_convert(ET)
    contracts: list[dict] = []
    snap_iv: dict[str, float] = {}
    for s in symbols:
        b = ref[(ref.symbol == s) & (ref.t.dt.date == day)]
        b = b[(b.t.dt.time >= time(9, 30)) & (b.t.dt.time < time(11, 31))]
        if b.empty:
            continue
        lo_px, hi_px, open_px = float(b.low.min()), float(b.high.max()), float(b.open.iloc[0])
        pad = 0.02 if s in ("SPY", "QQQ") else 0.06
        for mn, mx in buckets:
            e_lo, e_hi = trading_day_offset(day, mn), trading_day_offset(day, mx)
            rows = []
            for status in ("active", "inactive"):
                try:
                    rows += data.contracts(s, e_lo, e_hi, status=status, strike_price_gte=str(round(lo_px * (1 - pad), 2)),
                                           strike_price_lte=str(round(hi_px * (1 + pad), 2)))
                except Exception as e:
                    log.warning("contracts %s %s: %s", s, status, e)
            if not rows:
                continue
            nearest = min(r["expiration_date"] for r in rows)
            rows = [r for r in rows if r["expiration_date"] == nearest]
            for right in ("call", "put"):
                rr = sorted((r for r in rows if r["type"] == right), key=lambda r: abs(float(r["strike_price"]) - open_px))
                contracts += rr[:24]
            try:
                exp = date.fromisoformat(nearest)
                for c in data.option_chain(s, exp, exp):
                    if c.iv:
                        snap_iv[c.occ] = float(c.iv)
            except Exception as e:
                log.warning("snapshot IV %s %s: %s", s, nearest, e)
    seen = set()
    crow = []
    for c in contracts:
        if c["symbol"] in seen:
            continue
        seen.add(c["symbol"])
        crow.append({"occ": c["symbol"], "underlying": c["underlying_symbol"], "right": "C" if c["type"] == "call" else "P",
                     "strike": float(c["strike_price"]), "expiry": c["expiration_date"],
                     "open_interest": int(c["open_interest"]) if c.get("open_interest") not in (None, "") else None,
                     "open_interest_date": c.get("open_interest_date"), "iv_snapshot": snap_iv.get(c["symbol"])})
    pd.DataFrame(crow).to_parquet(d / "contracts.parquet", index=False)
    try:
        bars = data.option_bars([c["occ"] for c in crow], datetime.combine(day, time(4, 0), ET), end)
        bars_note = ""
    except Exception as e:  # this account: 403 "OPRA agreement is not signed"
        log.warning("option bars unavailable: %s", e)
        bars, bars_note = {}, f"option bars unavailable: {type(e).__name__}: {str(e)[:200]}"
    orows = [{"occ": k, **b} for k, v in bars.items() for b in v]
    pd.DataFrame(orows, columns=["occ", "t", "o", "h", "l", "c", "v", "vw"] if not orows else None).to_parquet(
        d / "option_bars.parquet", index=False)
    (d / "meta.json").write_text(json.dumps({
        "day": str(day), "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbols": symbols, "history_from": str(first), "contracts": len(crow), "option_bar_rows": len(orows),
        "snapshot_iv": len([c for c in crow if c["iv_snapshot"]]), "option_bars_note": bars_note,
        "stock_feeds": [f for f, p in stock_rows.items() if p],
        "note": "stock bars: Alpaca SIP and IEX historical; option prices: none historical on this account "
                "(indicative feed only): the replay uses a synthetic Black-Scholes quote model; OI: contracts "
                "endpoint as of open_interest_date; iv_snapshot: indicative snapshot IV at fetched_at"}, indent=1))
    return d
