"""Execution contract shared by every broker adapter, the risk layer and the
orchestrator. Broker-agnostic on purpose: Alpaca today, another broker later
means one new adapter, nothing else changes.

Source of truth: the BROKER holds positions and cash. Our own ledger only adds
attribution (which strategy sleeve owns which position) and is reconciled
against the broker every run.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

OPTION_MULTIPLIER = 100  # shares per standard US equity option contract


class AssetClass(str, Enum):
    EQUITY = "us_equity"
    CRYPTO = "crypto"
    OPTION = "us_option"  # single-leg US equity options; symbol is the OCC contract symbol


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TIF(str, Enum):
    DAY = "day"
    GTC = "gtc"
    OPG = "opg"  # market/limit on open, equities only
    IOC = "ioc"


class OrderStatus(str, Enum):
    NEW = "new"  # accepted, working
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    PENDING = "pending"  # accepted / pending_new / other non-final states; still a live order
    PENDING_CANCEL = "pending_cancel"  # a cancel was requested: not live, but it can still fill


@dataclass(frozen=True)
class OrderIntent:
    """What the system wants to do. client_order_id makes submission
    idempotent: re-running a job must never double-submit."""

    client_order_id: str
    symbol: str  # equities "AAPL"; crypto "BTC/USD"; options the OCC symbol "SPY261013C00580000"
    asset_class: AssetClass
    side: Side
    qty: float | None = None  # shares/coins/whole contracts; exactly one of qty/notional
    notional: float | None = None  # USD
    order_type: OrderType = OrderType.MARKET
    tif: TIF = TIF.DAY
    limit_price: float | None = None
    stop_price: float | None = None
    strategy: str = ""
    reason: str = ""


@dataclass
class BrokerOrder:
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: Side
    status: OrderStatus
    qty: float | None
    filled_qty: float
    filled_avg_price: float | None
    submitted_at: datetime | None
    filled_at: datetime | None
    order_type: OrderType
    tif: TIF
    stop_price: float | None = None
    limit_price: float | None = None
    asset_class: AssetClass | None = None  # None when the adapter does not report it


@dataclass
class BrokerPosition:
    symbol: str
    asset_class: AssetClass
    qty: float
    avg_entry_price: float
    market_value: float
    current_price: float


@dataclass
class AccountSnapshot:
    equity: float
    cash: float
    buying_power: float
    positions: list[BrokerPosition] = field(default_factory=list)
    as_of: datetime | None = None
    trading_blocked: bool = False


@dataclass(frozen=True)
class OptionContract:
    """One option contract with its latest quote. Prices are per share (the
    premium of one contract is price * multiplier). Every market field is
    None when the data source did not provide it; nothing is guessed.
    ``volume`` is today's traded contracts, ``open_interest`` is the latest
    published figure (normally as of the previous session)."""

    occ: str  # broker spelling of the OCC symbol, e.g. "SPY261013C00580000"
    underlying: str
    right: str  # "C" or "P"
    strike: float
    expiry: date
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    delta: float | None = None
    iv: float | None = None
    as_of: datetime | None = None  # time of the quote
    multiplier: int = OPTION_MULTIPLIER

    @property
    def mid(self) -> float | None:
        """(bid + ask) / 2 for a sane two-sided quote, else None."""
        b, a = self.bid, self.ask
        if b is None or a is None or not (math.isfinite(b) and math.isfinite(a)) or b < 0 or a <= 0 or b > a:
            return None
        return (b + a) / 2.0

    @property
    def spread_pct(self) -> float | None:
        """(ask - bid) / mid, or None without a sane two-sided quote."""
        m = self.mid
        if m is None or m <= 0:
            return None
        return (self.ask - self.bid) / m


@dataclass
class RiskDecision:
    allow: bool
    reason: str
    intent: OrderIntent | None = None  # possibly resized; None when blocked
