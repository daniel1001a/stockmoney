"""Execution contract shared by every broker adapter, the risk layer and the
orchestrator. Broker-agnostic on purpose: Alpaca today, another broker later
means one new adapter, nothing else changes.

Source of truth: the BROKER holds positions and cash. Our own ledger only adds
attribution (which strategy sleeve owns which position) and is reconciled
against the broker every run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class AssetClass(str, Enum):
    EQUITY = "us_equity"
    CRYPTO = "crypto"


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
    PENDING = "pending"  # any not-yet-accepted state


@dataclass(frozen=True)
class OrderIntent:
    """What the system wants to do. client_order_id makes submission
    idempotent: re-running a job must never double-submit."""

    client_order_id: str
    symbol: str  # equities "AAPL"; crypto "BTC/USD"
    asset_class: AssetClass
    side: Side
    qty: float | None = None  # shares/coins; exactly one of qty/notional
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


@dataclass
class RiskDecision:
    allow: bool
    reason: str
    intent: OrderIntent | None = None  # possibly resized; None when blocked
