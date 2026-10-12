"""INTERFACE SHIM (line B side of OPTIONS_PLAN.md section 8).

Line A owns the real definitions in ``stockmoney.desk.execution``:
``AssetClass.OPTION``, ``OptionContract`` (types.py), ``occ_symbol`` /
``parse_occ`` (symbols.py), the ``OptionsBroker`` Protocol (broker.py) and
``OptionsRiskEngine`` (options_risk.py). Until those land, this module holds
copies that match section 8 field for field, and every module under
``stockmoney.desk.options`` imports them from HERE ONLY.

Integration = edit this one file:
  1. replace ``AssetClass``, ``OptionContract``, ``occ_symbol``, ``parse_occ``
     and ``OptionsBroker`` with imports from ``stockmoney.desk.execution``;
  2. make ``make_risk_gate`` wrap ``OptionsRiskEngine.check``;
  3. make ``make_trading_broker`` return line A's Alpaca broker.
Nothing else in the package needs to change. Until then ``make_risk_gate``
and ``make_trading_broker`` refuse, so a session runs shadow-only and can
never send an order through an unreviewed path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Callable, Protocol

import pandas as pd

# Re-exported unchanged from the stock side: these already exist and are
# broker-agnostic (OrderIntent.asset_class is not type-checked at runtime).
from stockmoney.desk.execution.types import (  # noqa: F401
    TIF, AccountSnapshot, BrokerOrder, BrokerPosition, OrderIntent, OrderStatus, OrderType, RiskDecision, Side,
)


class AssetClass(str, Enum):
    """Mirror of execution.types.AssetClass plus OPTION (section 8)."""
    EQUITY = "us_equity"
    CRYPTO = "crypto"
    OPTION = "us_option"


@dataclass(frozen=True)
class OptionContract:
    """Section 8: occ, underlying, right "C"/"P", strike, expiry: date, bid,
    ask, last, volume, open_interest, delta, iv, as_of. Quote fields may be
    None when the provider has no value; ``as_of`` is the quote time."""
    occ: str
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
    as_of: datetime | None = None


_OCC = re.compile(r"^([A-Z][A-Z0-9.]{0,5})(\d{6})([CP])(\d{8})$")


def occ_symbol(underlying: str, expiry: date, right: str, strike: float) -> str:
    """OCC option symbol without padding, as Alpaca spells it:
    SPY261013C00781000 = SPY 2026-10-13 call, strike 781.000."""
    if right not in ("C", "P"):
        raise ValueError(f"right must be C or P, got {right!r}")
    milli = round(strike * 1000)
    if milli <= 0 or milli >= 10**8:
        raise ValueError(f"strike out of range: {strike}")
    return f"{underlying}{expiry:%y%m%d}{right}{milli:08d}"


def parse_occ(occ: str) -> tuple[str, date, str, float]:
    """Inverse of occ_symbol: (underlying, expiry, right, strike)."""
    m = _OCC.match(occ)
    if not m:
        raise ValueError(f"not an OCC symbol: {occ!r}")
    root, ymd, right, milli = m.groups()
    expiry = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
    return root, expiry, right, int(milli) / 1000


class OptionsBroker(Protocol):
    """Section 8 market-data surface (read-only)."""

    def option_chain(self, underlying: str, expiry_min: date, expiry_max: date) -> list[OptionContract]: ...
    def option_quote(self, occ: str) -> OptionContract | None: ...
    def minute_bars(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Index: tz-aware bar START times; columns open/high/low/close/volume
        (+ vwap when available)."""
    def stock_quote(self, symbol: str) -> tuple[float, float] | None: ...


class OptionsTradingBroker(OptionsBroker, Protocol):
    """What the engine needs to trade: the section 8 data surface plus the
    order calls every existing Broker already has (idempotent submit)."""
    is_paper: bool

    def submit(self, intent: OrderIntent) -> BrokerOrder: ...
    def get_order(self, client_order_id: str) -> BrokerOrder | None: ...
    def cancel(self, client_order_id: str) -> None: ...
    def account(self) -> AccountSnapshot: ...


# One injectable callable: (intent, contract, premium_usd, now) -> RiskDecision.
# The engine calls it for every live ENTRY and only sends the order when
# ``decision.allow``. Exits never go through it (exits are always allowed).
RiskGate = Callable[[OrderIntent, OptionContract, float, datetime], RiskDecision]


def deny_all_gate(intent: OrderIntent, contract: OptionContract, premium_usd: float, now: datetime) -> RiskDecision:
    return RiskDecision(False, "no options risk engine wired (line A not integrated): shadow only")


def make_risk_gate(con) -> RiskGate:
    """Integration point 2. Today: no line A risk engine exists in this tree,
    so every live entry is denied (shadow continues). At integration:

        from stockmoney.desk.execution.options_risk import OptionsLimits, OptionsRiskEngine
        eng = OptionsRiskEngine(OptionsLimits(), con)
        return lambda intent, c, prem, now: eng.check(prem, c, now, intent=intent)
    """
    return deny_all_gate


def make_trading_broker() -> OptionsTradingBroker:
    """Integration point 3. Today raises: the Alpaca options adapter is line
    A's. ``run.session`` falls back to the read-only data adapter
    (marketdata.AlpacaOptionsData) and shadow-only mode."""
    raise NotImplementedError("line A options broker not integrated")
