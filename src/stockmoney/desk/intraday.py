"""Intraday shadow of the daily stock setups (breakout, pullback). SHADOW ONLY.

What it measures: the daily system decides at the close of day T and enters at
the open of T+1. This module asks, during day T, "if the session closed right
now, would the daily rule fire?", by evaluating the unchanged daily rules on a
partial bar for today built from Alpaca minute bars available at ``now``. The
first time a (symbol, strategy) triggers on a day it is written to
``intraday_shadow``. After the next session's daily bar is in the cache,
``score`` fills the outcome: same-day close, next open, whether the full daily
bar confirmed the signal, and ``ret_vs_next_open`` = next_open / signal_px - 1,
the edge of acting at the intraday trigger instead of the daily system's
next-open entry (positive = the intraday entry was cheaper).

Hard limits (do not relax without the owner):
- never imports or calls the broker / execution order path; no order method
  exists here, and tests/desk/test_intraday.py asserts that importing this
  module loads no ``stockmoney.desk.execution``, ``stockmoney.desk.broker`` or
  ``alpaca.trading`` module;
- REST polling of minute bars only, no websocket streaming;
- no leverage, no sizing, no change to the daily strategies or their tables:
  the only table written is ``intraday_shadow``.

    python -m stockmoney.desk.intraday tick [--feed sip|iex]   # one tick, then score
    python -m stockmoney.desk.intraday score                   # fill outcomes from the daily cache
    python -m stockmoney.desk.intraday report

Intended cadence (no schedule is installed by this module): every 10 minutes
from 09:45 to 15:55 ET on trading days. ``tick`` refuses outside the regular
session (prints one line, exit 0, so a holiday run is harmless) and is silent
when nothing new triggered. The comparison is evaluated only after at least
MIN_SAMPLES (30) scored rows; ``report`` says so below that.

Data: Alpaca historical stock minute bars (alpaca-py StockHistoricalDataClient,
read-only, keys from ALPACA_API_KEY / ALPACA_SECRET_KEY). Default feed is SIP,
which this account may read only 16 minutes behind real time (see
options/marketdata.py), so a SIP tick sees bars that ended by now - 16 min.
SIP volume is consolidated and therefore comparable to the daily cache's
volume, which breakout's volume-ratio rule needs; IEX is real time but carries
only IEX's share of volume, so on IEX breakout's volume rule almost never
passes. Every row records its feed and the number of minute bars used.

Look-ahead: only bars whose minute has fully ended by the data cut-off are
used, whatever the client returns. The daily history is the cache up to the
previous session; a cache that is not current refuses the tick.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from stockmoney.desk import data
from stockmoney.desk.calendar import close_time, is_trading_day, next_trading_day
from stockmoney.desk.data import FIELDS, Bars
from stockmoney.desk.indicators import compute, market_state
from stockmoney.desk.strategies import REGISTRY, Strategy, build_context
from stockmoney.desk.universe import ALL_SYMBOLS

log = logging.getLogger("desk.intraday")
ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
ONE = timedelta(minutes=1)
STRATEGIES = ("breakout", "pullback")
MIN_SAMPLES = 30
FEEDS = ("sip", "iex")
SIP_DELAY = timedelta(minutes=16)  # this account may not read SIP data newer than 15 minutes
HTTP_TIMEOUT_S = 30.0
TICK_TIMEOUT_S = 300  # a tick that hangs must never overlap the next one
DB_PATH = Path("data/desk/desk.sqlite")
# universe spelling -> Alpaca spelling; same entries as execution/symbols.TO_BROKER
# (not imported: this module must not load the execution package)
_TO_ALPACA = {"BRK-B": "BRK.B"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_shadow (
  day TEXT, symbol TEXT, strategy TEXT,
  first_signal_at TEXT, data_through TEXT, signal_px REAL, stop REAL, score REAL, regime TEXT,
  feed TEXT, bar_count INTEGER,
  daily_rule_signal_next_day INTEGER, close_px REAL, next_open_px REAL, next_day TEXT,
  ret_to_close REAL, ret_vs_next_open REAL, scored_at TEXT,
  PRIMARY KEY (day, symbol, strategy));
"""


def ensure_schema(con) -> None:
    con.executescript(SCHEMA)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def regular_hours(now: datetime) -> bool:
    """True inside the regular session of a trading day (09:30 to the close,
    13:00 on an early close)."""
    et = now.astimezone(ET)
    if not is_trading_day(et.date()):
        return False
    return OPEN <= et.time() < time(*close_time(et.date()))


def _prev_session(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def data_cutoff(now: datetime, feed: str) -> datetime:
    """Latest bar end the feed lets us see at ``now``."""
    return now - SIP_DELAY if feed == "sip" else now


# ------------------------------------------------------------------ data client

def _bars_frame(raw: list[dict]) -> pd.DataFrame:
    """Alpaca raw bar dicts (t,o,h,l,c,v) -> DataFrame indexed by bar start (ET)."""
    if not raw:
        return pd.DataFrame(columns=list(FIELDS))
    df = pd.DataFrame(raw)
    df.index = pd.DatetimeIndex(pd.to_datetime(df["t"], utc=True)).tz_convert(ET)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    return df[list(FIELDS)].astype(float).sort_index()


class AlpacaBars:
    """Read-only Alpaca stock minute bars over REST. Market data client only:
    it holds no trading client and has no order method."""

    def __init__(self, feed: str = "sip", key: str | None = None, secret: str | None = None):
        from alpaca.data.historical.stock import StockHistoricalDataClient

        if feed not in FEEDS:
            raise ValueError(f"feed must be one of {FEEDS}, got {feed!r}")
        key = key or os.environ.get("ALPACA_API_KEY", "")
        secret = secret or os.environ.get("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
        self.feed = feed
        self.client = StockHistoricalDataClient(api_key=key, secret_key=secret, raw_data=True)
        _install_timeout(self.client, HTTP_TIMEOUT_S)

    def minute_bars_many(self, symbols: list[str], start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        names = {_TO_ALPACA.get(s, s): s for s in symbols}
        raw = self.client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=list(names), timeframe=TimeFrame.Minute, start=start, end=end,
            feed=DataFeed(self.feed))) or {}
        return {names[k]: _bars_frame(v) for k, v in raw.items() if k in names}

    def minute_bars(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        return self.minute_bars_many([symbol], start, end).get(symbol, _bars_frame([]))


def _install_timeout(client, seconds: float) -> None:
    """Default timeout on every request of an alpaca-py RESTClient (it sets
    none). Same approach as execution/alpaca.install_timeout."""
    session = getattr(client, "_session", None)
    if session is None:
        return
    inner = session.request

    def request(method, url, **kw):
        kw.setdefault("timeout", seconds)
        return inner(method, url, **kw)
    session.request = request


def _fetch(client, symbols: list[str], start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
    """Batch call when the client has one, else one call per symbol (the
    options sleeve's ``minute_bars`` surface). A failing symbol is skipped."""
    many = getattr(client, "minute_bars_many", None)
    if callable(many):
        return many(symbols, start, end)
    out = {}
    for s in symbols:
        try:
            out[s] = client.minute_bars(s, start, end)
        except Exception as e:
            log.warning("minute bars %s: %s", s, e)
    return out


# ------------------------------------------------------------------ partial bar

def partial_bar(minute: pd.DataFrame, day: date, cutoff: datetime) -> tuple[dict | None, int]:
    """Today's OHLCV so far from regular-session minute bars that ENDED by
    ``cutoff`` (bar start + 1 minute <= cutoff). Bars after the cut-off are
    dropped even if the client returned them. Returns (bar or None, bars used)."""
    if minute is None or minute.empty:
        return None, 0
    idx = pd.DatetimeIndex(minute.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    idx = idx.tz_convert(ET)
    start = pd.Timestamp(datetime.combine(day, OPEN, ET))
    close = pd.Timestamp(datetime.combine(day, time(*close_time(day)), ET))
    lim = min(pd.Timestamp(cutoff).tz_convert(ET), close)
    m = minute.set_axis(idx)
    m = m[(idx >= start) & (idx + ONE <= lim)].sort_index()
    if m.empty:
        return None, 0
    bar = {"open": float(m["open"].iloc[0]), "high": float(m["high"].max()), "low": float(m["low"].min()),
           "close": float(m["close"].iloc[-1]), "volume": float(m["volume"].sum()),
           "through": (m.index[-1] + ONE).to_pydatetime()}
    return bar, len(m)


def with_partial(daily: Bars, day: date, partial: dict[str, dict]) -> Bars:
    """Daily history before ``day`` plus one row for ``day`` from ``partial``
    (NaN for symbols without intraday data)."""
    t = pd.Timestamp(day)
    hist = Bars(*(getattr(daily, f).loc[daily.dates < t] for f in FIELDS))
    out = []
    for f in FIELDS:
        frame = getattr(hist, f)
        row = pd.DataFrame([{s: partial[s][f] for s in partial if s in frame.columns}], index=[t],
                           columns=frame.columns, dtype=float)
        out.append(pd.concat([frame, row]))
    return Bars(*out)


# ------------------------------------------------------------------ strategies

def _strategies(con) -> dict[str, Strategy]:
    """Breakout and pullback with the daily system's stored parameters
    (settings.strategy_params); defaults when a stored value is rejected.
    Disabled strategies are still shadowed: this measures the rule, not the book."""
    params: dict = {}
    try:
        r = con.execute("SELECT value FROM settings WHERE key='strategy_params'").fetchone()
        params = json.loads(r[0]) if r and r[0] else {}
    except (sqlite3.OperationalError, ValueError):
        params = {}
    out = {}
    for n in STRATEGIES:
        try:
            out[n] = REGISTRY[n](**params.get(n, {}))
        except (ValueError, TypeError) as e:
            log.warning("strategy %s: stored parameters rejected (%s); using defaults", n, e)
            out[n] = REGISTRY[n]()
    return out


def _signals(strats: dict[str, Strategy], bars: Bars, t: pd.Timestamp):
    ind = compute(bars)
    ctx = build_context(ind, t, market_state(ind, t))
    out = []
    for s in strats.values():
        out.extend(s.candidates(ctx))
    return out, ctx.market.regime


# ------------------------------------------------------------------ tick

@dataclass
class TickResult:
    status: str  # outside_hours | stale_daily | no_data | ok
    recorded: list[tuple[str, str]] = field(default_factory=list)  # (strategy, symbol) new today
    note: str = ""


def tick(con, now: datetime, bars_client, daily_bars: Bars, feed: str | None = None) -> TickResult:
    """One evaluation at ``now``. Writes only intraday_shadow, only the first
    trigger per (day, symbol, strategy)."""
    ensure_schema(con)
    if not regular_hours(now):
        return TickResult("outside_hours", note="非正常交易時段,不執行")
    feed = feed or getattr(bars_client, "feed", None) or "sip"
    day = now.astimezone(ET).date()
    prev = _prev_session(day)
    hist_dates = daily_bars.dates[daily_bars.dates < pd.Timestamp(day)]
    if hist_dates.empty or hist_dates[-1].date() != prev:
        last = hist_dates[-1].date() if not hist_dates.empty else None
        return TickResult("stale_daily", note=f"日線快取最後一日 {last},應為 {prev},不執行")
    cutoff = data_cutoff(now, feed)
    start = datetime.combine(day, OPEN, ET)
    if cutoff <= start + ONE:
        return TickResult("no_data", note="尚無完整的盤中分K")
    symbols = [s for s in ALL_SYMBOLS if s in daily_bars.close.columns]
    raw = _fetch(bars_client, symbols, start, cutoff)
    partial, counts, through = {}, {}, None
    for s in symbols:
        bar, n = partial_bar(raw.get(s), day, cutoff)
        if bar is None:
            continue
        partial[s], counts[s] = bar, n
        through = bar["through"] if through is None else max(through, bar["through"])
    if not partial:
        return TickResult("no_data", note="尚無完整的盤中分K")
    t = pd.Timestamp(day)
    signals, regime = _signals(_strategies(con), with_partial(daily_bars, day, partial), t)
    res = TickResult("ok")
    for s in signals:
        if s.strategy not in STRATEGIES:
            continue
        cur = con.execute(
            "INSERT OR IGNORE INTO intraday_shadow (day, symbol, strategy, first_signal_at, data_through,"
            " signal_px, stop, score, regime, feed, bar_count) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(day), s.symbol, s.strategy, _iso(now), _iso(partial[s.symbol]["through"]), float(s.ref_price),
             float(s.stop), None if pd.isna(s.score) else float(s.score), regime, feed, counts.get(s.symbol, 0)))
        if cur.rowcount:
            res.recorded.append((s.strategy, s.symbol))
    con.commit()
    return res


# ------------------------------------------------------------------ score

def score(con, daily_bars: Bars) -> int:
    """Fill outcomes for rows whose day AND next session are in the daily
    cache (the day's bar is then final). Returns the number of rows scored."""
    ensure_schema(con)
    pending = con.execute("SELECT DISTINCT day FROM intraday_shadow WHERE scored_at IS NULL ORDER BY day").fetchall()
    dates = set(daily_bars.dates)
    ready = []
    for (d,) in pending:
        t, nxt = pd.Timestamp(d), pd.Timestamp(next_trading_day(date.fromisoformat(d)))
        if t in dates and nxt in dates:
            ready.append((d, t, nxt))
    if not ready:
        return 0
    strats = _strategies(con)
    ind = compute(daily_bars)  # causal: row t equals a run on history up to t
    n = 0
    for d, t, nxt in ready:
        ctx = build_context(ind, t, market_state(ind, t))
        daily = {(s.strategy, s.symbol) for st in strats.values() for s in st.candidates(ctx)}
        rows = con.execute("SELECT symbol, strategy, signal_px FROM intraday_shadow WHERE day=? AND scored_at IS NULL",
                           (d,)).fetchall()
        for sym, strat, px in rows:
            if sym not in daily_bars.close.columns:
                continue
            c, o = daily_bars.close.at[t, sym], daily_bars.open.at[nxt, sym]
            if not (_finite(c) and _finite(o) and _finite(px) and px > 0):
                continue
            con.execute(
                "UPDATE intraday_shadow SET daily_rule_signal_next_day=?, close_px=?, next_open_px=?, next_day=?,"
                " ret_to_close=?, ret_vs_next_open=?, scored_at=? WHERE day=? AND symbol=? AND strategy=?",
                (int((strat, sym) in daily), float(c), float(o), str(nxt.date()), float(c) / px - 1,
                 float(o) / px - 1, _iso(datetime.now(timezone.utc)), d, sym, strat))
            n += 1
    con.commit()
    return n


def _finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


# ------------------------------------------------------------------ report

def report(con) -> str:
    ensure_schema(con)
    total = con.execute("SELECT COUNT(*) FROM intraday_shadow").fetchone()[0]
    rows = con.execute(
        "SELECT strategy, COUNT(*), AVG(ret_vs_next_open), AVG(ret_to_close), SUM(daily_rule_signal_next_day)"
        " FROM intraday_shadow WHERE scored_at IS NOT NULL GROUP BY strategy ORDER BY strategy").fetchall()
    n = sum(r[1] for r in rows)
    lines = [f"盤中影子(intraday_shadow):記錄 {total} 筆,已評分 n={n}"]
    for strat, k, adv, rtc, conf in rows:
        lines.append(f"{strat}: n={k},平均 ret_vs_next_open {adv:+.2%},平均 ret_to_close {rtc:+.2%},"
                     f"日線收盤確認 {int(conf or 0)}/{k}")
    if n < MIN_SAMPLES:
        lines.append(f"樣本不足 {MIN_SAMPLES},不評估")
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI

class TickTimeout(BaseException):
    """Outer alarm. BaseException so no retry layer can swallow it."""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path, timeout=30)


def _daily(data_dir: Path) -> Bars:
    return data.to_bars(data.load(data_dir))


def main(argv: list[str], *, clock=lambda: datetime.now(timezone.utc), client_factory=AlpacaBars) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m stockmoney.desk.intraday")
    ap.add_argument("cmd", choices=("tick", "score", "report"))
    ap.add_argument("--feed", choices=FEEDS, default="sip")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--data-dir", type=Path, default=data.DEFAULT_DIR)
    a = ap.parse_args(argv[1:])
    now = clock()
    if a.cmd == "tick" and not regular_hours(now):
        print("盤中影子:非正常交易時段,不執行", file=sys.stderr)
        return 0  # refused before any network or DB access; 0 so a holiday run is harmless

    def _timeout(*_):
        raise TickTimeout(f"intraday {a.cmd} exceeded {TICK_TIMEOUT_S}s")
    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(TICK_TIMEOUT_S)
    try:
        con = _connect(a.db)
        try:
            if a.cmd == "report":
                print(report(con))
                return 0
            daily = _daily(a.data_dir)
            if a.cmd == "tick":
                res = tick(con, now, client_factory(a.feed), daily, feed=a.feed)
                if res.status == "stale_daily":
                    log.warning(res.note)
                for strat, sym in res.recorded:
                    print(f"盤中影子訊號 {strat} {sym}")
            k = score(con, daily)
            if k:
                print(f"盤中影子:評分 {k} 筆")
            return 0
        finally:
            con.close()
    except TickTimeout as e:
        log.error("%s", e)
        return 1
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "locked" not in msg and "busy" not in msg:
            raise
        # another process (the options session) held desk.sqlite past the timeout: nothing
        # was committed, this slot is skipped and the next one runs normally, never a halt
        log.warning("盤中影子:資料庫忙碌,略過本時段:%s", e)
        return 0
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
