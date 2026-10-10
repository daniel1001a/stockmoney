"""Broker protocol + an in-memory FakeBroker for tests.

Every adapter (Alpaca paper, Alpaca live, a future Robinhood adapter) must
implement Broker. The FakeBroker is the reference behaviour that unit tests of
the risk layer, reconciliation and orchestrator run against.
"""
from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import datetime, timezone
from typing import Protocol

from stockmoney.desk.execution.types import (
    AccountSnapshot, AssetClass, BrokerOrder, BrokerPosition, OrderIntent,
    OrderStatus, OrderType, Side,
)


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


class InsufficientQty(RuntimeError):
    """Mirrors the broker rejecting a sell whose shares are already reserved
    by another open sell order (Alpaca: HTTP 403 insufficient qty)."""


class FakeBroker:
    """Fills market orders immediately at ``prices[symbol]``; stop orders rest
    until ``tick`` moves the price through them.

    Fidelity notes: ``account``/``get_order``/``open_orders`` return copies, as
    a real API response would. With ``async_cancel=True`` a cancel only moves
    the order to PENDING (Alpaca's pending_cancel); it stays open, can still
    fill, and keeps its shares reserved until ``settle()``. A sell that needs
    shares reserved by another open sell raises ``InsufficientQty``."""

    name = "fake"
    is_paper = True

    def __init__(self, cash: float = 50_000.0, prices: dict[str, float] | None = None,
                 async_cancel: bool = False):
        self.cash = cash
        self.async_cancel = async_cancel
        self._pending_cancel: set[str] = set()
        self.prices = dict(prices or {})
        self.positions: dict[str, BrokerPosition] = {}
        self.orders: dict[str, BrokerOrder] = {}
        self.market_open = True
        self._ids = itertools.count(1)
        self._asset: dict[str, AssetClass] = {}

    def account(self) -> AccountSnapshot:
        for p in self.positions.values():
            p.current_price = self.prices.get(p.symbol, p.current_price)
            p.market_value = p.qty * p.current_price
        equity = self.cash + sum(p.market_value for p in self.positions.values())
        return AccountSnapshot(equity, self.cash, self.cash, [replace(p) for p in self.positions.values()],
                               datetime.now(timezone.utc))

    def _reserved(self, symbol: str) -> float:
        return sum(o.qty - o.filled_qty for o in self.orders.values()
                   if o.symbol == symbol and o.side == Side.SELL and self._is_open(o))

    @staticmethod
    def _is_open(o: BrokerOrder) -> bool:
        return o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING)

    def submit(self, intent: OrderIntent) -> BrokerOrder:
        if intent.client_order_id in self.orders:
            return replace(self.orders[intent.client_order_id])
        price = self.prices[intent.symbol]
        qty = intent.qty if intent.qty is not None else intent.notional / price
        if intent.side == Side.SELL:
            p = self.positions.get(intent.symbol)
            available = (p.qty if p else 0.0) - self._reserved(intent.symbol)
            if qty > available + 1e-9:
                raise InsufficientQty(
                    f"insufficient qty available for order (requested: {qty:g}, available: {max(available, 0):g})")
        o = BrokerOrder(
            str(next(self._ids)), intent.client_order_id, intent.symbol, intent.side,
            OrderStatus.NEW, qty, 0.0, None, datetime.now(timezone.utc), None,
            intent.order_type, intent.tif, intent.stop_price, intent.limit_price,
        )
        self.orders[intent.client_order_id] = o
        self._asset[intent.symbol] = intent.asset_class
        if intent.order_type == OrderType.MARKET:
            self._fill(o, price)
        return replace(o)

    def tick(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price
        for o in self.orders.values():
            if o.symbol == symbol and self._is_open(o) and o.order_type == OrderType.STOP:
                if (o.side == Side.SELL and price <= o.stop_price) or (o.side == Side.BUY and price >= o.stop_price):
                    self._fill(o, price)

    def _fill(self, o: BrokerOrder, price: float) -> None:
        if o.side == Side.BUY:
            if o.qty * price > self.cash + 1e-9:
                o.status = OrderStatus.REJECTED
                return
            self.cash -= o.qty * price
            p = self.positions.get(o.symbol)
            if p:
                p.avg_entry_price = (p.avg_entry_price * p.qty + price * o.qty) / (p.qty + o.qty)
                p.qty += o.qty
            else:
                self.positions[o.symbol] = BrokerPosition(
                    o.symbol, self._asset.get(o.symbol, AssetClass.EQUITY), o.qty, price, o.qty * price, price)
        else:
            p = self.positions.get(o.symbol)
            if p is None or p.qty + 1e-9 < o.qty:
                o.status = OrderStatus.REJECTED
                return
            self.cash += o.qty * price
            p.qty -= o.qty
            if p.qty <= 1e-9:
                del self.positions[o.symbol]
        o.status, o.filled_qty, o.filled_avg_price = OrderStatus.FILLED, o.qty, price
        o.filled_at = datetime.now(timezone.utc)
        self._pending_cancel.discard(o.client_order_id)

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        o = self.orders.get(client_order_id)
        return replace(o) if o else None

    def open_orders(self) -> list[BrokerOrder]:
        return [replace(o) for o in self.orders.values() if self._is_open(o)]

    def cancel(self, client_order_id: str) -> None:
        o = self.orders.get(client_order_id)
        if o is None or o.status not in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
            return
        if self.async_cancel:
            o.status = OrderStatus.PENDING
            self._pending_cancel.add(client_order_id)
        else:
            o.status = OrderStatus.CANCELED

    def settle(self) -> None:
        """Complete every pending asynchronous cancel."""
        for cid in sorted(self._pending_cancel):
            o = self.orders[cid]
            if o.status == OrderStatus.PENDING:
                o.status = OrderStatus.CANCELED
        self._pending_cancel.clear()

    def cancel_all(self) -> None:
        for o in self.orders.values():
            if o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
                self.cancel(o.client_order_id)

    def is_equity_market_open(self) -> bool:
        return self.market_open

    def latest_price(self, symbol: str) -> float | None:
        return self.prices.get(symbol)
