"""Broker protocol + an in-memory FakeBroker for tests.

Every adapter (Alpaca paper, Alpaca live, a future Robinhood adapter) must
implement Broker. The FakeBroker is the reference behaviour that unit tests of
the risk layer, reconciliation and orchestrator run against.

OptionsBroker is a separate protocol for the market data the intraday options
sleeve needs. Option orders go through the ordinary Broker.submit with
AssetClass.OPTION intents (whole contracts, buy to open / sell to close).
"""
from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import date, datetime, timezone
from math import floor
from typing import Protocol

import pandas as pd

from stockmoney.desk.execution.types import (
    TIF, AccountSnapshot, AssetClass, BrokerOrder, BrokerPosition, OptionContract, OrderIntent,
    OrderStatus, OrderType, Side,
)

BAR_COLUMNS = ("open", "high", "low", "close", "volume")


class Broker(Protocol):
    name: str
    is_paper: bool

    def account(self) -> AccountSnapshot: ...
    def submit(self, intent: OrderIntent) -> BrokerOrder:
        """Idempotent on client_order_id: if an order with that id already
        exists, return it instead of submitting again."""
    def get_order(self, client_order_id: str) -> BrokerOrder | None: ...
    def open_orders(self) -> list[BrokerOrder]: ...
    def cancel(self, client_order_id: str) -> None: ...
    def cancel_all(self) -> None: ...
    def is_equity_market_open(self) -> bool: ...
    def latest_price(self, symbol: str) -> float | None:
        """Last trade price, or None when no safe quote is available. Used as
        a sanity check and for sizing just before a market order."""


class OptionsBroker(Protocol):
    """Market data for the options sleeve. Every method returns None / an
    empty result when the data is unavailable instead of inventing values."""

    def option_chain(self, underlying: str, expiry_min: date, expiry_max: date) -> list[OptionContract]:
        """Every listed contract of ``underlying`` expiring in
        [expiry_min, expiry_max] (inclusive), with its latest quote."""
    def option_quote(self, occ: str) -> OptionContract | None:
        """Latest quote of one contract, or None when it cannot be read."""
    def minute_bars(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """1-minute bars of a stock or an OCC contract whose bar start time is
        in [start, end]. Index: tz-aware UTC timestamps (bar start), ascending.
        Columns: open, high, low, close, volume, plus vwap when available."""
    def stock_quote(self, symbol: str) -> tuple[float, float] | None:
        """(bid, ask) of a stock, or None without a sane two-sided quote."""


def empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=list(BAR_COLUMNS), index=pd.DatetimeIndex([], tz="UTC"), dtype=float)


class InsufficientQty(RuntimeError):
    """Mirrors the broker rejecting a sell whose shares are already reserved
    by another open sell order (Alpaca: HTTP 403 insufficient qty)."""


class FakeBroker:
    """Fills market orders immediately at ``prices[symbol]``; stop orders rest
    until ``tick`` moves the price through them.

    Fidelity notes: ``account``/``get_order``/``open_orders`` return copies, as
    a real API response would. With ``async_cancel=True`` a cancel only moves
    the order to PENDING_CANCEL (Alpaca's pending_cancel); it stays open, can
    still fill, and keeps its shares reserved until ``settle()``. A sell that
    needs shares reserved by another open sell raises ``InsufficientQty``.
    ``rest_status=OrderStatus.PENDING`` makes new resting (non-market) orders
    come back ``accepted`` the way Alpaca does after the close; they do not
    trigger until ``open_session()`` turns them into NEW. ``partial_ratio``
    (0..1) makes a market buy fill only that part of its qty and stay
    PARTIALLY_FILLED until ``fill_rest()``.

    Options: ``set_option`` stores a contract quote (``option_contracts``).
    Market buys fill at the ask, market sells at the bid; a limit order
    fills at once when marketable (buy limit >= ask: at the ask; sell limit
    <= bid: at the bid) and otherwise rests until ``set_option`` makes it
    marketable. Cash and market value use the contract multiplier (100). An
    order with no usable quote is REJECTED. ``bars`` (symbol -> DataFrame) and
    ``stock_quotes`` (symbol -> (bid, ask)) feed the OptionsBroker reads."""

    name = "fake"
    is_paper = True

    def __init__(self, cash: float = 50_000.0, prices: dict[str, float] | None = None,
                 async_cancel: bool = False):
        self.cash = cash
        self.async_cancel = async_cancel
        self.rest_status = OrderStatus.NEW
        self.partial_ratio: float | None = None
        self._pending_cancel: set[str] = set()
        self.prices = dict(prices or {})
        self.positions: dict[str, BrokerPosition] = {}
        self.orders: dict[str, BrokerOrder] = {}
        self.market_open = True
        self._ids = itertools.count(1)
        self._asset: dict[str, AssetClass] = {}
        self.option_contracts: dict[str, OptionContract] = {}
        self.bars: dict[str, pd.DataFrame] = {}
        self.stock_quotes: dict[str, tuple[float, float]] = {}

    # ---- options helpers
    def _is_option(self, symbol: str) -> bool:
        return self._asset.get(symbol) == AssetClass.OPTION

    def _mult(self, symbol: str) -> float:
        if self._is_option(symbol):
            k = self.option_contracts.get(symbol)
            return float(k.multiplier) if k else 100.0
        return 1.0

    def _option_mark(self, symbol: str) -> float | None:
        k = self.option_contracts.get(symbol)
        if k is None:
            return None
        return k.mid if k.mid is not None else k.last

    def _option_fill_price(self, symbol: str, side: Side, limit: float | None) -> float | None:
        """Price an option order fills at right now, or None (not marketable / no quote)."""
        k = self.option_contracts.get(symbol)
        if k is None:
            return None
        px = k.ask if side == Side.BUY else k.bid
        if px is None or not px > 0:
            return None
        if limit is not None and ((side == Side.BUY and limit < px) or (side == Side.SELL and limit > px)):
            return None
        return px

    def set_option(self, contract: OptionContract) -> None:
        """Store (or update) a contract quote and fill resting option limit
        orders that it makes marketable."""
        self.option_contracts[contract.occ] = contract
        for o in self.orders.values():
            if (o.symbol == contract.occ and o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)
                    and o.order_type == OrderType.LIMIT):
                px = self._option_fill_price(o.symbol, o.side, o.limit_price)
                if px is not None:
                    self._fill(o, px)

    def account(self) -> AccountSnapshot:
        for p in self.positions.values():
            if p.asset_class == AssetClass.OPTION:
                mark = self._option_mark(p.symbol)
                p.current_price = mark if mark is not None else p.current_price
            else:
                p.current_price = self.prices.get(p.symbol, p.current_price)
            p.market_value = p.qty * p.current_price * self._mult(p.symbol)
        equity = self.cash + sum(p.market_value for p in self.positions.values())
        return AccountSnapshot(equity, self.cash, self.cash, [replace(p) for p in self.positions.values()],
                               datetime.now(timezone.utc))

    def _reserved(self, symbol: str) -> float:
        return sum(o.qty - o.filled_qty for o in self.orders.values()
                   if o.symbol == symbol and o.side == Side.SELL and self._is_open(o))

    @staticmethod
    def _is_open(o: BrokerOrder) -> bool:
        return o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING,
                            OrderStatus.PENDING_CANCEL)

    def submit(self, intent: OrderIntent) -> BrokerOrder:
        if intent.client_order_id in self.orders:
            return replace(self.orders[intent.client_order_id])
        if intent.asset_class == AssetClass.OPTION:
            return self._submit_option(intent)
        price = self.prices[intent.symbol]
        qty = intent.qty if intent.qty is not None else intent.notional / price
        if intent.side == Side.SELL:
            p = self.positions.get(intent.symbol)
            available = (p.qty if p else 0.0) - self._reserved(intent.symbol)
            if qty > available + 1e-9:
                raise InsufficientQty(
                    f"insufficient qty available for order (requested: {qty:g}, available: {max(available, 0):g})")
        status = OrderStatus.NEW if intent.order_type == OrderType.MARKET else self.rest_status
        o = BrokerOrder(
            str(next(self._ids)), intent.client_order_id, intent.symbol, intent.side,
            status, qty, 0.0, None, datetime.now(timezone.utc), None,
            intent.order_type, intent.tif, intent.stop_price, intent.limit_price, intent.asset_class,
        )
        self.orders[intent.client_order_id] = o
        self._asset[intent.symbol] = intent.asset_class
        if intent.order_type == OrderType.MARKET:
            part = None
            if self.partial_ratio is not None and o.side == Side.BUY:
                part = floor(o.qty * self.partial_ratio)
            self._fill(o, price, part)
        return replace(o)

    def _submit_option(self, intent: OrderIntent) -> BrokerOrder:
        if intent.qty is None or intent.qty != int(intent.qty) or intent.qty <= 0:
            raise ValueError("option orders need a whole positive qty")
        if intent.order_type not in (OrderType.MARKET, OrderType.LIMIT):
            raise ValueError("option orders must be market or limit")
        qty = float(intent.qty)
        if intent.side == Side.SELL:
            p = self.positions.get(intent.symbol)
            available = (p.qty if p else 0.0) - self._reserved(intent.symbol)
            if qty > available + 1e-9:
                raise InsufficientQty(
                    f"insufficient qty available for order (requested: {qty:g}, available: {max(available, 0):g})")
        o = BrokerOrder(
            str(next(self._ids)), intent.client_order_id, intent.symbol, intent.side,
            OrderStatus.NEW, qty, 0.0, None, datetime.now(timezone.utc), None,
            intent.order_type, intent.tif, None, intent.limit_price, AssetClass.OPTION,
        )
        self.orders[intent.client_order_id] = o
        self._asset[intent.symbol] = AssetClass.OPTION
        px = self._option_fill_price(intent.symbol, intent.side, intent.limit_price)
        if px is not None:
            part = None
            if self.partial_ratio is not None and o.side == Side.BUY and intent.order_type == OrderType.MARKET:
                part = floor(o.qty * self.partial_ratio)
            self._fill(o, px, part)
        elif intent.order_type == OrderType.MARKET:
            o.status = OrderStatus.REJECTED  # no usable quote to fill against
        return replace(o)

    def tick(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price
        for o in self.orders.values():
            if (o.symbol == symbol and self._is_open(o) and o.status != OrderStatus.PENDING
                    and o.order_type == OrderType.STOP):
                if (o.side == Side.SELL and price <= o.stop_price) or (o.side == Side.BUY and price >= o.stop_price):
                    self._fill(o, price)

    def _fill(self, o: BrokerOrder, price: float, qty: float | None = None) -> None:
        """Fill ``qty`` more of ``o`` (default: everything still open)."""
        rest = o.qty - o.filled_qty
        qty = rest if qty is None else min(qty, rest)
        if qty <= 0:
            return
        mult = self._mult(o.symbol)
        if o.side == Side.BUY:
            if qty * price * mult > self.cash + 1e-9:
                o.status = OrderStatus.REJECTED
                return
            self.cash -= qty * price * mult
            p = self.positions.get(o.symbol)
            if p:
                p.avg_entry_price = (p.avg_entry_price * p.qty + price * qty) / (p.qty + qty)
                p.qty += qty
            else:
                self.positions[o.symbol] = BrokerPosition(
                    o.symbol, self._asset.get(o.symbol, AssetClass.EQUITY), qty, price, qty * price * mult, price)
        else:
            p = self.positions.get(o.symbol)
            if p is None or p.qty + 1e-9 < qty:
                o.status = OrderStatus.REJECTED
                return
            self.cash += qty * price * mult
            p.qty -= qty
            if p.qty <= 1e-9:
                del self.positions[o.symbol]
        done = o.filled_qty
        o.filled_avg_price = (((o.filled_avg_price or 0.0) * done + price * qty) / (done + qty))
        o.filled_qty = done + qty
        o.filled_at = datetime.now(timezone.utc)
        if o.filled_qty >= o.qty - 1e-9:
            o.status = OrderStatus.FILLED
            self._pending_cancel.discard(o.client_order_id)
        elif o.status != OrderStatus.PENDING_CANCEL:
            o.status = OrderStatus.PARTIALLY_FILLED

    def fill_rest(self, client_order_id: str | None = None) -> None:
        """Fill the open remainder of one partially filled order (or of all)."""
        for o in list(self.orders.values()):
            if (client_order_id in (None, o.client_order_id) and o.order_type == OrderType.MARKET
                    and o.status == OrderStatus.PARTIALLY_FILLED):
                if self._is_option(o.symbol):
                    px = self._option_fill_price(o.symbol, o.side, None)
                    if px is not None:
                        self._fill(o, px)
                else:
                    self._fill(o, self.prices[o.symbol])

    def expire_day_orders(self) -> None:
        """The session closed: every working DAY/OPG order expires (filled
        shares stay filled)."""
        for o in self.orders.values():
            if self._is_open(o) and o.tif in (TIF.DAY, TIF.OPG):
                o.status = OrderStatus.EXPIRED
                self._pending_cancel.discard(o.client_order_id)

    def open_session(self) -> None:
        """The market opened: accepted orders become NEW."""
        for o in self.orders.values():
            if o.status == OrderStatus.PENDING:
                o.status = OrderStatus.NEW

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        o = self.orders.get(client_order_id)
        return replace(o) if o else None

    def open_orders(self) -> list[BrokerOrder]:
        return [replace(o) for o in self.orders.values() if self._is_open(o)]

    def cancel(self, client_order_id: str) -> None:
        o = self.orders.get(client_order_id)
        if o is None or o.status not in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING):
            return
        if self.async_cancel:
            o.status = OrderStatus.PENDING_CANCEL
            self._pending_cancel.add(client_order_id)
        else:
            o.status = OrderStatus.CANCELED

    def settle(self) -> None:
        """Complete every pending asynchronous cancel."""
        for cid in sorted(self._pending_cancel):
            o = self.orders[cid]
            if o.status == OrderStatus.PENDING_CANCEL:
                o.status = OrderStatus.CANCELED
        self._pending_cancel.clear()

    def cancel_all(self) -> None:
        for o in self.orders.values():
            if o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING):
                self.cancel(o.client_order_id)

    def is_equity_market_open(self) -> bool:
        return self.market_open

    def latest_price(self, symbol: str) -> float | None:
        return self.prices.get(symbol)

    # ---- OptionsBroker
    def option_chain(self, underlying: str, expiry_min: date, expiry_max: date) -> list[OptionContract]:
        u = underlying.upper()
        return sorted((k for k in self.option_contracts.values()
                       if k.underlying.upper() == u and expiry_min <= k.expiry <= expiry_max),
                      key=lambda k: (k.expiry, k.right, k.strike))

    def option_quote(self, occ: str) -> OptionContract | None:
        return self.option_contracts.get(occ)

    def minute_bars(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        df = self.bars.get(symbol)
        if df is None or df.empty:
            return empty_bars()
        idx = df.index
        return df[(idx >= pd.Timestamp(start)) & (idx <= pd.Timestamp(end))].copy()

    def stock_quote(self, symbol: str) -> tuple[float, float] | None:
        return self.stock_quotes.get(symbol)
