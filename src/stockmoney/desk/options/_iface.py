"""The ONE seam between the options sleeve (``stockmoney.desk.options``) and
the execution layer (``stockmoney.desk.execution``, line A of
OPTIONS_PLAN.md section 8). Every module under ``desk.options`` imports
broker types, the OptionsBroker surface and the risk gate from HERE ONLY.

What lives here:
  - re-exports of line A's real types (AssetClass.OPTION, OptionContract,
    OrderIntent, RiskDecision, occ_symbol / parse_occ, OptionsBroker);
  - ``RiskGate``: what the engine calls for every live entry and every
    live exit, plus the book keeping around it (reserve after submit,
    release when the order is booked or ends unfilled, book every live
    fill) and the day's forced-flat time;
  - ``LineAGate``: that protocol over line A's OptionsRiskEngine and
    OptionsBook, with limits built from options/config.py;
  - ``ShadowGate``: shadow-only sessions (DESK_OPTIONS_ORDERS=off) and
    ``recover`` in that mode: every new buy refused, but exits, fill booking
    and reservations are line A's real ones, so a leftover of a paper
    session is sold only up to what the broker holds and reaches the book;
  - ``DenyAllGate``: refuses every order (engines without a broker
    account, e.g. tests); never used by a session;
  - ``make_trading_broker``: line A's AlpacaBroker (paper unless the owner
    set the live flags; AlpacaBroker refuses live endpoints otherwise).

Exits go through the gate too (CLAUDE.md section 0: every order passes the
risk layer): line A allows a sell up to the broker's held quantity, so a
sell can never open a short, and refuses a limit far below mid (fat finger);
the engine then falls back to a market sell. Alpaca also enforces
sell_to_close through position_intent.
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Callable, Mapping, Protocol

from stockmoney.desk.execution.broker import OptionsBroker  # noqa: F401
from stockmoney.desk.execution.options_risk import (  # noqa: F401
    OptionsBook, OptionsBookError, OptionsLimits, OptionsRiskEngine, sleeve_state,
)
from stockmoney.desk.execution.symbols import occ_symbol, parse_occ  # noqa: F401
from stockmoney.desk.execution.types import (  # noqa: F401
    TIF, AccountSnapshot, AssetClass, BrokerOrder, BrokerPosition, OptionContract, OrderIntent, OrderStatus, OrderType,
    RiskDecision, Side,
)

log = logging.getLogger("desk.options.gate")


class OptionsTradingBroker(OptionsBroker, Protocol):
    """What the engine needs to trade: the section 8 data surface plus the
    order calls every Broker already has (idempotent submit)."""
    is_paper: bool

    def submit(self, intent: OrderIntent) -> BrokerOrder: ...
    def get_order(self, client_order_id: str) -> BrokerOrder | None: ...
    def cancel(self, client_order_id: str) -> None: ...
    def account(self) -> AccountSnapshot: ...
    def open_orders(self) -> list[BrokerOrder]: ...


class RiskGate(Protocol):
    def check(self, intent: OrderIntent, contract: OptionContract, now: datetime, *,
              marks: Mapping[str, float] | None = None, blocked: Mapping[str, str] | None = None) -> RiskDecision:
        """Decide one live ENTRY. ``contract`` is the quote the order is
        priced on, with its own ``as_of`` (stale quotes are refused)."""
    def check_exit(self, intent: OrderIntent, contract: OptionContract | None, now: datetime) -> RiskDecision:
        """Decide one live SELL to close. ``contract`` may be None (a market
        sell needs no quote). Never resizes; refuses a sell beyond what is held."""
    def set_flat_by(self, t: time) -> None:
        """The day's forced-flat time (earlier on a half day); no buy at/after it."""
    def reserve(self, intent: OrderIntent, price: float) -> None: ...
    def release(self, occ: str) -> None: ...
    def book_fill(self, *, client_order_id: str, occ: str, strategy: str, side: Side, qty: float, price: float,
                  filled_at: datetime | None) -> bool | None:
        """Book a live fill; ``qty`` / ``price`` are the order's CUMULATIVE
        filled qty and average price (idempotent per client_order_id).
        False = the book refused it (the caller keeps the order polled)."""


class DenyAllGate:
    """Refuses every order, books nothing. Not used by any session: a
    shadow-only session uses ShadowGate, whose exits keep line A's checks."""

    def check(self, intent, contract, now, *, marks=None, blocked=None) -> RiskDecision:
        return RiskDecision(False, "shadow-only session: no order path")

    def check_exit(self, intent, contract, now) -> RiskDecision:
        return RiskDecision(False, "deny-all gate: no order path (no broker account to check a sell against)")

    def set_flat_by(self, t) -> None:
        pass

    def reserve(self, intent, price) -> None:
        pass

    def release(self, occ) -> None:
        pass

    def book_fill(self, **kw) -> None:
        pass


def limits_from_config(cfg) -> OptionsLimits:
    """OptionsLimits from options/config.py, so the gate and the engine use
    the same budget, liquidity filters, quote age and clock."""
    budget = cfg.capital_budget * cfg.sleeve_pct
    return OptionsLimits(
        capital_budget=cfg.capital_budget, sleeve_pct=cfg.sleeve_pct,
        max_trade_pct=cfg.max_premium_per_trade / budget if budget > 0 else 0.0,
        max_positions=cfg.max_open_positions, max_spread_pct=cfg.max_spread_pct, min_mid=cfg.min_mid,
        min_open_interest=cfg.min_open_interest, min_volume=cfg.min_volume, max_quote_age_s=cfg.max_quote_age_s,
        entry_start=time(*cfg.entry_window_start), entry_end=time(*cfg.entry_window_end),
        flat_by=time(*cfg.force_flat), halt_file=cfg.halt_file)


class LineAGate:
    """RiskGate over line A's OptionsRiskEngine. Every check reads a fresh
    broker snapshot and the broker's working orders (so a restart, which
    loses the in-memory reservations, still counts working buys)."""

    def __init__(self, engine: OptionsRiskEngine, broker, on_error: Callable[[str, str], None] | None = None):
        self.engine = engine
        self.broker = broker
        self.on_error = on_error or (lambda kind, msg: log.warning("%s: %s", kind, msg))

    def check(self, intent, contract, now, *, marks=None, blocked=None) -> RiskDecision:
        snap = self.broker.account()
        orders = self.broker.open_orders() if hasattr(self.broker, "open_orders") else None
        return self.engine.check(intent, contract, snap, now, open_orders=orders, marks=marks,
                                 blocked=blocked if blocked is not None else {})

    def check_exit(self, intent, contract, now) -> RiskDecision:
        """Line A's exit rules (OptionsRiskEngine routes a SELL to
        _check_exit): held quantity from a fresh broker snapshot, working
        sells from the broker's open orders, the limit's price band."""
        if intent.side != Side.SELL:
            return RiskDecision(False, "check_exit takes sells only")
        snap = self.broker.account()
        orders = self.broker.open_orders() if hasattr(self.broker, "open_orders") else None
        return self.engine.check(intent, contract, snap, now, open_orders=orders, blocked={})

    def set_flat_by(self, t) -> None:
        self.engine.limits.flat_by = t

    def reserve(self, intent, price) -> None:
        self.engine.reserve(intent, price)

    def release(self, occ) -> None:
        self.engine.release(occ)

    def book_fill(self, *, client_order_id, occ, strategy, side, qty, price, filled_at) -> bool:
        """A book error is reported, never raised, and returned as False: the
        book then disagrees with the broker and the risk engine blocks entries
        (exits go on). True when booked or already booked."""
        try:
            self.engine.book.apply_fill(client_order_id=client_order_id, occ=occ, strategy=strategy, side=side,
                                        qty=qty, price=price, filled_at=filled_at, cumulative=True)
        except OptionsBookError as e:
            self.on_error("options_book", f"{client_order_id} {occ}: {e}")
            return False
        return True


class ShadowGate(LineAGate):
    """Shadow-only: no new buy, ever. Sells to close (a leftover of a paper
    session), fill booking, reservations and flat_by are LineAGate's, i.e.
    line A's held-quantity / working-sell checks and its OptionsBook."""

    def check(self, intent, contract, now, *, marks=None, blocked=None) -> RiskDecision:
        return RiskDecision(False, "shadow-only session: no new entries")


def make_risk_gate(con, broker, cfg, on_error=None) -> LineAGate:
    return LineAGate(OptionsRiskEngine(limits_from_config(cfg), con), broker, on_error)


def make_shadow_gate(con, broker, cfg, on_error=None) -> ShadowGate:
    return ShadowGate(OptionsRiskEngine(limits_from_config(cfg), con), broker, on_error)


def make_trading_broker():
    """Line A's AlpacaBroker: paper by default; it refuses the live endpoint
    unless the owner set the live flags (execution.alpaca.resolve_env)."""
    from stockmoney.desk.execution.alpaca import AlpacaBroker
    return AlpacaBroker()
