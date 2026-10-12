"""Test doubles for the options sleeve: a mutable clock, synthetic 1-minute
bars, and FakeOptionsBroker implementing the section 8 OptionsBroker surface
plus idempotent order calls. No network, no real broker."""
from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from stockmoney.desk.options._iface import (
    AccountSnapshot, AssetClass, BrokerOrder, BrokerPosition, OptionContract, OrderIntent, OrderStatus, OrderType,
    RiskDecision, Side, occ_symbol, parse_occ,
)
from stockmoney.desk.options.config import OptionsConfig

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 13)  # a Tuesday, NYSE open
PREV = date(2026, 10, 12)
ONE = timedelta(minutes=1)


def et(hh: int, mm: int, ss: int = 0, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), ET)


class Clock:
    def __init__(self, t: datetime):
        self.now = t.astimezone(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def set(self, t: datetime) -> None:
        self.now = t.astimezone(timezone.utc)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def bars(rows: list[tuple]) -> pd.DataFrame:
    """rows: (start datetime, open, high, low, close, volume)."""
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume"])
    df.index = pd.DatetimeIndex(df.pop("t")).tz_convert(ET)
    return df.astype(float)


def day_bars(rth_closes: list[float], *, prev_close: float = 600.0, pm_close: float | None = None,
             pm_volume: float = 50_000, rth_volume=100_000, day: date = DAY, wick: float = 0.05,
             opens: list[float] | None = None, pm_minutes: int = 90) -> pd.DataFrame:
    """Previous day's last 10 minutes at ``prev_close``, ``pm_minutes`` of
    premarket at ``pm_close``, then one regular bar per close from 09:30.
    Each bar opens at the previous close (or ``opens[i]``)."""
    rows = [(datetime.combine(PREV if day == DAY else day - timedelta(days=1), time(15, 50), ET) + i * ONE,
             prev_close, prev_close + 0.05, prev_close - 0.05, prev_close, 10_000) for i in range(10)]
    pm = prev_close if pm_close is None else pm_close
    start_pm = datetime.combine(day, time(9, 30), ET) - pm_minutes * ONE
    rows += [(start_pm + i * ONE, pm, pm + 0.05, pm - 0.05, pm, pm_volume) for i in range(pm_minutes)]
    prev = pm
    for i, c in enumerate(rth_closes):
        o = opens[i] if opens else prev
        v = rth_volume(i) if callable(rth_volume) else rth_volume
        rows.append((datetime.combine(day, time(9, 30), ET) + i * ONE, o, max(o, c) + wick, min(o, c) - wick, c, v))
        prev = c
    return bars(rows)


def orb_breakout_day(level_hi: float = 601.0) -> pd.DataFrame:
    """15 minutes inside 600..601, then a steady rally (breakout 09:45 bar,
    volume rising), flat afterwards: orb15 long at the 09:45 bar close."""
    closes = [600.5 + (0.4 if i % 2 else -0.4) for i in range(15)]
    c = 600.5
    for i in range(75):
        c += 0.08
        closes.append(c)
    closes += [c] * 300
    vol = lambda i: 100_000 if i < 15 else 150_000 + 2_000 * i
    return day_bars(closes, rth_volume=vol, wick=0.05)


def chain(underlying: str = "SPY", expiry: date = DAY, spot: float = 600.0) -> dict[str, dict]:
    """Call and put templates with provider deltas, mids by strike."""
    out = {}
    for k, dc in ((596, 0.80), (598, 0.68), (600, 0.55), (601, 0.45), (602, 0.35), (604, 0.20)):
        out[occ_symbol(underlying, expiry, "C", k)] = {"strike": k, "right": "C", "delta": dc,
                                                       "mid": max(spot - k, 0) + 1.0 + (dc - 0.5)}
        out[occ_symbol(underlying, expiry, "P", k)] = {"strike": k, "right": "P", "delta": -(1 - dc),
                                                       "mid": max(k - spot, 0) + 1.0 + (0.5 - dc)}
    return out


class FakeOptionsBroker:
    name = "fake-options"
    is_paper = True

    def __init__(self, clock: Clock, day_bars_by_symbol: dict[str, pd.DataFrame] | None = None):
        self.clock = clock
        self.bars = dict(day_bars_by_symbol or {})
        self.templates: dict[str, dict] = {}
        self.half_spread = 0.02
        self.mult: dict[str, float] = {}           # occ -> price multiplier applied to the template mid
        self.mult_fn = None                         # (occ, now) -> multiplier, overrides mult
        self.quote_age_s = 1.0
        self.volume = 1_000
        self.oi = 5_000
        self.fill = "immediate"                     # immediate | never
        self.fail_submit_once = False
        self.submit_calls: list[str] = []
        self.orders: dict[str, BrokerOrder] = {}
        self.positions: dict[str, float] = {}
        self.extra_positions: list[BrokerPosition] = []
        self.data_error: Exception | None = None
        self._ids = itertools.count(1)

    # ---- data
    def minute_bars(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        if self.data_error:
            raise self.data_error
        b = self.bars.get(symbol)
        if b is None:
            return bars([])
        lim = min(pd.Timestamp(end), pd.Timestamp(self.clock.now))
        return b[(b.index >= pd.Timestamp(start)) & (b.index + ONE <= lim)].copy()

    def _spot(self, symbol: str) -> float | None:
        b = self.minute_bars(symbol, self.clock.now - timedelta(days=2), self.clock.now)
        return float(b["close"].iloc[-1]) if not b.empty else None

    def stock_quote(self, symbol: str):
        s = self._spot(symbol)
        return (s - 0.01, s + 0.01) if s else None

    def option_quote(self, occ: str) -> OptionContract | None:
        t = self.templates.get(occ)
        if t is None:
            return None
        o = parse_occ(occ)
        und, exp, right, strike = o.root, o.expiry, o.right, o.strike
        m = t["mid"] * (self.mult_fn(occ, self.clock.now) if self.mult_fn else self.mult.get(occ, 1.0))
        bid, ask = round(max(m - self.half_spread, 0.0), 2), round(m + self.half_spread, 2)
        return OptionContract(occ, und, right, float(strike), exp, bid, ask, m, self.volume, self.oi,
                              t.get("delta"), t.get("iv"), self.clock.now - timedelta(seconds=self.quote_age_s))

    def option_chain(self, underlying: str, expiry_min: date, expiry_max: date) -> list[OptionContract]:
        out = []
        for occ in self.templates:
            o = parse_occ(occ)
            und, exp = o.root, o.expiry
            if und == underlying and expiry_min <= exp <= expiry_max:
                out.append(self.option_quote(occ))
        return out

    # ---- orders
    def submit(self, intent: OrderIntent) -> BrokerOrder:
        self.submit_calls.append(intent.client_order_id)
        if intent.client_order_id in self.orders:
            return replace(self.orders[intent.client_order_id])
        o = BrokerOrder(str(next(self._ids)), intent.client_order_id, intent.symbol, intent.side, OrderStatus.NEW,
                        intent.qty, 0.0, None, self.clock.now, None, intent.order_type, intent.tif, None,
                        intent.limit_price, AssetClass.OPTION)
        self.orders[o.client_order_id] = o
        if self.fail_submit_once:
            self.fail_submit_once = False
            raise TimeoutError("network timeout after the broker accepted the order")
        self._try_fill(o)
        return replace(o)

    def _try_fill(self, o: BrokerOrder) -> None:
        if self.fill != "immediate" or o.status != OrderStatus.NEW:
            return
        q = self.option_quote(o.symbol)
        px = q.ask if o.side == Side.BUY else q.bid
        if o.order_type == OrderType.LIMIT and ((o.side == Side.BUY and px > o.limit_price + 1e-9)
                                                or (o.side == Side.SELL and px < o.limit_price - 1e-9)):
            return
        o.status, o.filled_qty, o.filled_avg_price, o.filled_at = OrderStatus.FILLED, o.qty, px, self.clock.now
        self.positions[o.symbol] = self.positions.get(o.symbol, 0) + (o.qty if o.side == Side.BUY else -o.qty)
        if abs(self.positions[o.symbol]) < 1e-9:
            del self.positions[o.symbol]

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        o = self.orders.get(client_order_id)
        if o is None:
            return None
        self._try_fill(o)
        return replace(o)

    def cancel(self, client_order_id: str) -> None:
        o = self.orders.get(client_order_id)
        if o is not None and o.status == OrderStatus.NEW:
            o.status = OrderStatus.CANCELED

    def open_orders(self) -> list[BrokerOrder]:
        return [replace(o) for o in self.orders.values() if o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)]

    def account(self) -> AccountSnapshot:
        pos = []
        for s, q in self.positions.items():
            k = self.option_quote(s)
            m = k.mid if k is not None and k.mid is not None else 0.0
            pos.append(BrokerPosition(s, AssetClass.OPTION, q, 0, q * m * 100, m))
        return AccountSnapshot(0, 0, 0, pos + list(self.extra_positions), self.clock.now)


class FnGate:
    """A RiskGate whose check is ``fn(intent, contract, now) -> RiskDecision``;
    records every call, reservation, release and booked fill."""

    def __init__(self, fn, calls: list | None = None):
        self.fn = fn
        self.calls = calls if calls is not None else []
        self.reserved: list = []
        self.released: list = []
        self.booked: list = []

    def check(self, intent, contract, now, *, marks=None, blocked=None):
        self.calls.append((intent, contract, now))
        return self.fn(intent, contract, now)

    def reserve(self, intent, price):
        self.reserved.append((intent.symbol, intent.qty, price))

    def release(self, occ):
        self.released.append(occ)

    def book_fill(self, **kw):
        self.booked.append(kw)


def allow_gate(calls: list | None = None) -> FnGate:
    return FnGate(lambda intent, contract, now: RiskDecision(True, "ok", intent), calls)


def spy_cfg(tmp_path, **kw) -> OptionsConfig:
    """Universe = SPY only, files under tmp_path."""
    base = dict(index=("SPY",), mega_cap=(), high_vol=(), halt_file=tmp_path / "HALT",
                lock_path=tmp_path / "options.lock", report_dir=tmp_path / "reports")
    base.update(kw)
    return OptionsConfig(**base)
