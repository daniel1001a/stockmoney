"""Keeps the stock book blind to the options sleeve.

The intraday options sleeve trades in the SAME broker account as the stock
book (docs/desk/OPTIONS_PLAN.md section 8). Every stock job (premarket,
postopen, postclose, recover, crypto) reads positions and open orders from
the broker and would otherwise see option contracts as unknown positions
(reconcile halt), option sells as stray orders, and option premium as gross
exposure. Instead of a condition in every one of those places, each job wraps
its broker in ``stock_only``: positions and open orders that are options are
filtered out, and the view refuses to cancel an option order. The options
sleeve keeps its own book and reconciles it against the broker itself
(execution.options_risk).

What counts as an option: a position or order whose asset class is
AssetClass.OPTION, or whose symbol parses as an OCC option symbol (so an
adapter that does not report the asset class is still covered).
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from stockmoney.desk.execution.symbols import is_occ_symbol
from stockmoney.desk.execution.types import AccountSnapshot, AssetClass, BrokerOrder, BrokerPosition

log = logging.getLogger(__name__)


class OptionOrderProtected(RuntimeError):
    """A stock job tried to cancel an order of the options sleeve."""


def is_option(item: BrokerPosition | BrokerOrder | str) -> bool:
    """True for an option position, an option order or an OCC symbol."""
    if isinstance(item, str):
        return is_occ_symbol(item)
    if getattr(item, "asset_class", None) == AssetClass.OPTION:
        return True
    return is_occ_symbol(getattr(item, "symbol", "") or "")


def without_options(snapshot: AccountSnapshot) -> AccountSnapshot:
    """The snapshot with option positions removed. Equity, cash and buying
    power are the broker's account-wide figures and are left as they are
    (the stock risk layer derives its own equity from the ledger)."""
    kept = [p for p in snapshot.positions if not is_option(p)]
    if len(kept) == len(snapshot.positions):
        return snapshot
    return replace(snapshot, positions=kept)


class StockOnlyBroker:
    """Broker view for the stock jobs. account() and open_orders() hide the
    options sleeve, cancel() refuses an option order and cancel_all() cancels
    only stock and crypto orders. Everything else is passed through to the
    wrapped broker (including attributes such as ``client`` and ``_retry``)."""

    def __init__(self, inner: Any):
        self._inner = inner

    @property
    def inner(self) -> Any:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def account(self) -> AccountSnapshot:
        return without_options(self._inner.account())

    def open_orders(self) -> list[BrokerOrder]:
        return [o for o in self._inner.open_orders() if not is_option(o)]

    def cancel(self, client_order_id: str) -> None:
        o = self._inner.get_order(client_order_id)
        if o is not None and is_option(o):
            raise OptionOrderProtected(
                f"refusing to cancel {client_order_id} ({o.symbol}): it belongs to the options sleeve")
        self._inner.cancel(client_order_id)

    def cancel_all(self) -> None:
        for o in self.open_orders():
            self._inner.cancel(o.client_order_id)


def stock_only(broker: Any) -> Any:
    """Wrap ``broker`` in StockOnlyBroker (idempotent)."""
    return broker if isinstance(broker, StockOnlyBroker) else StockOnlyBroker(broker)
