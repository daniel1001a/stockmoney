"""Broker protocol + an in-memory FakeBroker for tests.

Every adapter (Alpaca paper, Alpaca live, a future Robinhood adapter) must
implement Broker. The FakeBroker is the reference behaviour that unit tests of
the risk layer, reconciliation and orchestrator run against.
"""
from __future__ import annotations

import itertools
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


class FakeBroker:
    """Fills market orders immediately at ``prices[symbol]``; stop orders rest
    until ``tick`` moves the price through them."""

    name = "fake"
    is_paper = True

    def __init__(self, cash: float = 50_000.0, prices: dict[str, float] | None = None):
        self.cash = cash
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
        return AccountSnapshot(equity, self.cash, self.cash, list(self.positions.values()), datetime.now(timezone.utc))

    def submit(self, intent: OrderIntent) -> BrokerOrder:
        if intent.client_order_id in self.orders:
            return self.orders[intent.client_order_id]
        price = self.prices[intent.symbol]
        qty = intent.qty if intent.qty is not None else intent.notional / price
        o = BrokerOrder(
            str(next(self._ids)), intent.client_order_id, intent.symbol, intent.side,
            OrderStatus.NEW, qty, 0.0, None, datetime.now(timezone.utc), None,
            intent.order_type, intent.tif, intent.stop_price, intent.limit_price,
        )
        self.orders[intent.client_order_id] = o
        self._asset[intent.symbol] = intent.asset_class
        if intent.order_type == OrderType.MARKET:
            self._fill(o, price)
        return o

    def tick(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price
        for o in self.orders.values():
            if o.symbol == symbol and o.status == OrderStatus.NEW and o.order_type == OrderType.STOP:
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

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        return self.orders.get(client_order_id)

    def open_orders(self) -> list[BrokerOrder]:
        return [o for o in self.orders.values() if o.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)]

    def cancel(self, client_order_id: str) -> None:
        o = self.orders.get(client_order_id)
        if o and o.status == OrderStatus.NEW:
            o.status = OrderStatus.CANCELED

    def cancel_all(self) -> None:
        for o in self.open_orders():
            o.status = OrderStatus.CANCELED

    def is_equity_market_open(self) -> bool:
        return self.market_open
