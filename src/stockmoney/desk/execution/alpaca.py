"""Alpaca adapter implementing the Broker protocol (paper by default).

Environment:
    ALPACA_API_KEY, ALPACA_SECRET_KEY   credentials (paper keys for paper)
    ALPACA_ENV                          "paper" (default) or "live"
    ALPACA_LIVE_ENABLED                 must equal LIVE_CONFIRMATION when ALPACA_ENV=live;
                                        only the human operator sets this

Design notes:
  * Idempotent submit: client_order_id is looked up before every submission
    attempt, including each retry after a network error, so a retry can never
    double-submit.
  * The alpaca-py client already retries HTTP 429/504 internally (3 attempts,
    3s apart, POST included). Our own retry layer sits on top for connection
    errors and 5xx, with exponential backoff.
  * Invalid asset-class / TIF / order-type combinations raise ValueError before
    anything is sent, instead of being rejected by the broker.
  * alpaca-py sets no HTTP timeout (alpaca/common/rest.py calls
    ``self._session.request`` without one), so a stalled socket would block
    forever. Every client this adapter builds or is given gets a default
    per-request timeout (HTTP_TIMEOUT_S) unless the call passes its own.
  * Symbols are translated with execution.symbols on the way in and out, so
    the rest of the system only sees the universe spelling ("BRK-B").
  * latest_price uses the market-data clients (IEX feed for equities, which a
    free data plan may query; US feed for crypto).

Order status mapping (Alpaca -> OrderStatus). Everything not listed maps to
PENDING (accepted, pending_new, pending_cancel, pending_replace, pending_review,
accepted_for_bidding, done_for_day, replaced, stopped, suspended, calculated,
held):
    new -> NEW, partially_filled -> PARTIALLY_FILLED, filled -> FILLED,
    canceled -> CANCELED, rejected -> REJECTED, expired -> EXPIRED
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AccountStatus,
    AssetClass as AlpacaAssetClass,
    OrderSide,
    OrderStatus as AlpacaOrderStatus,
    PositionSide,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    OrderRequest,
    StopLimitOrderRequest,
    StopOrderRequest,
)
from requests.exceptions import RequestException

from stockmoney.desk.execution.symbols import from_broker, to_broker

from stockmoney.desk.execution.types import (
    TIF, AccountSnapshot, AssetClass, BrokerOrder, BrokerPosition, OrderIntent,
    OrderStatus, OrderType, Side,
)

log = logging.getLogger(__name__)

LIVE_CONFIRMATION = "I_UNDERSTAND_REAL_MONEY"

EQUITY_TIFS = frozenset({TIF.DAY, TIF.GTC, TIF.OPG, TIF.IOC})
CRYPTO_TIFS = frozenset({TIF.GTC, TIF.IOC})
# alpaca.trading.enums.OrderType docstring: crypto supports market, limit, stop_limit.
CRYPTO_ORDER_TYPES = frozenset({OrderType.MARKET, OrderType.LIMIT, OrderType.STOP_LIMIT})

_STATUS_MAP = {
    AlpacaOrderStatus.NEW: OrderStatus.NEW,
    AlpacaOrderStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
    AlpacaOrderStatus.FILLED: OrderStatus.FILLED,
    AlpacaOrderStatus.CANCELED: OrderStatus.CANCELED,
    AlpacaOrderStatus.REJECTED: OrderStatus.REJECTED,
    AlpacaOrderStatus.EXPIRED: OrderStatus.EXPIRED,
}
_TERMINAL = frozenset({OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED})
_TRANSIENT_HTTP = frozenset({429, 500, 502, 503, 504})
_CRYPTO_QUOTES = ("USDT", "USDC", "USD")
HTTP_TIMEOUT_S = 30.0  # per HTTP request; the job-level alarm is the outer bound

T = TypeVar("T")


class LiveTradingNotEnabled(RuntimeError):
    """ALPACA_ENV=live without the explicit operator confirmation."""


class MissingCredentials(RuntimeError):
    """ALPACA_API_KEY / ALPACA_SECRET_KEY not set."""


class BrokerError(RuntimeError):
    """Broker returned something we cannot or must not act on."""


class BrokerUnavailable(BrokerError):
    """Transient failures persisted through every retry."""


def resolve_env(environ: dict[str, str] | None = None) -> str:
    """Validate ALPACA_ENV and the live-trading guard; return "paper" or "live"."""
    env = environ if environ is not None else os.environ
    mode = (env.get("ALPACA_ENV") or "paper").strip().lower()
    if mode not in ("paper", "live"):
        raise ValueError(f"ALPACA_ENV must be 'paper' or 'live', got {mode!r}")
    if mode == "live" and env.get("ALPACA_LIVE_ENABLED") != LIVE_CONFIRMATION:
        raise LiveTradingNotEnabled(
            "ALPACA_ENV=live requires ALPACA_LIVE_ENABLED to be set by the account owner; refusing to construct")
    return mode


def validate_intent(intent: OrderIntent) -> None:
    """Raise ValueError for any combination the broker would reject."""
    if not intent.client_order_id:
        raise ValueError("client_order_id is required (idempotency key)")
    if not intent.symbol:
        raise ValueError("symbol is required")
    crypto = intent.asset_class == AssetClass.CRYPTO
    if crypto and "/" not in intent.symbol:
        raise ValueError(f"crypto symbol must look like 'BTC/USD', got {intent.symbol!r}")
    if not crypto and "/" in intent.symbol:
        raise ValueError(f"equity symbol must not contain '/', got {intent.symbol!r}")

    allowed_tifs = CRYPTO_TIFS if crypto else EQUITY_TIFS
    if intent.tif not in allowed_tifs:
        raise ValueError(
            f"TIF {intent.tif.value!r} not valid for {intent.asset_class.value}; "
            f"allowed: {sorted(t.value for t in allowed_tifs)}")
    if crypto and intent.order_type not in CRYPTO_ORDER_TYPES:
        raise ValueError(f"order type {intent.order_type.value!r} not supported for crypto")
    if intent.tif == TIF.OPG and intent.order_type not in (OrderType.MARKET, OrderType.LIMIT):
        raise ValueError("TIF opg only applies to market/limit orders")

    if (intent.qty is None) == (intent.notional is None):
        raise ValueError("exactly one of qty / notional must be set")
    size = intent.qty if intent.qty is not None else intent.notional
    if not size > 0:
        raise ValueError(f"order size must be positive, got {size}")
    if intent.notional is not None and intent.order_type != OrderType.MARKET:
        raise ValueError("notional orders must be market orders")
    if (not crypto and intent.qty is not None and intent.qty != int(intent.qty)
            and intent.order_type != OrderType.MARKET):
        raise ValueError("fractional equity qty is only valid for market orders")

    needs_limit = intent.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT)
    needs_stop = intent.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
    if needs_limit and intent.limit_price is None:
        raise ValueError(f"{intent.order_type.value} order requires limit_price")
    if needs_stop and intent.stop_price is None:
        raise ValueError(f"{intent.order_type.value} order requires stop_price")
    if not needs_limit and intent.limit_price is not None:
        raise ValueError(f"limit_price not allowed on {intent.order_type.value} order")
    if not needs_stop and intent.stop_price is not None:
        raise ValueError(f"stop_price not allowed on {intent.order_type.value} order")
    for name in ("limit_price", "stop_price"):
        v = getattr(intent, name)
        if v is not None and not v > 0:
            raise ValueError(f"{name} must be positive, got {v}")


def install_timeout(client: Any, seconds: float = HTTP_TIMEOUT_S) -> None:
    """Give every request made through an alpaca-py RESTClient a default
    timeout. alpaca/common/rest.py keeps a requests.Session in ``_session``
    and calls ``self._session.request(method, url, **opts)``; opts never
    carries a timeout. Clients without a session (test fakes) are left alone."""
    session = getattr(client, "_session", None)
    if session is None or getattr(session, "_desk_timeout", None) is not None:
        return
    inner = session.request

    def request(method, url, **kw):
        kw.setdefault("timeout", seconds)
        return inner(method, url, **kw)
    session.request = request
    session._desk_timeout = seconds


def build_request(intent: OrderIntent) -> OrderRequest:
    """Validate and translate an intent into the matching alpaca-py request."""
    validate_intent(intent)
    common: dict[str, Any] = dict(
        symbol=to_broker(intent.symbol),
        side=OrderSide(intent.side.value),
        time_in_force=TimeInForce(intent.tif.value),
        client_order_id=intent.client_order_id,
    )
    if intent.qty is not None:
        common["qty"] = intent.qty
    else:
        common["notional"] = intent.notional
    if intent.order_type == OrderType.MARKET:
        return MarketOrderRequest(**common)
    if intent.order_type == OrderType.LIMIT:
        return LimitOrderRequest(limit_price=intent.limit_price, **common)
    if intent.order_type == OrderType.STOP:
        return StopOrderRequest(stop_price=intent.stop_price, **common)
    return StopLimitOrderRequest(limit_price=intent.limit_price, stop_price=intent.stop_price, **common)


def _f(x: Any) -> float | None:
    return None if x is None else float(x)


def _enum_val(x: Any) -> Any:
    return getattr(x, "value", x)


def _norm_symbol(symbol: str | None, crypto: bool) -> str:
    """Positions may come back as 'BTCUSD'; our contract uses 'BTC/USD'. Source of
    alpaca-py types symbol as plain str, so this normalisation is defensive."""
    symbol = symbol or ""
    if crypto and "/" not in symbol:
        for q in _CRYPTO_QUOTES:
            if symbol.endswith(q) and len(symbol) > len(q):
                return f"{symbol[:-len(q)]}/{q}"
    return from_broker(symbol)


def _api_status(exc: BaseException) -> int | None:
    try:
        return exc.status_code  # type: ignore[attr-defined]
    except Exception:
        return None


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, APIError):
        return _api_status(exc) in _TRANSIENT_HTTP
    return isinstance(exc, RequestException)


class AlpacaBroker:
    is_paper: bool

    def __init__(
        self,
        client: TradingClient | None = None,
        *,
        environ: dict[str, str] | None = None,
        max_attempts: int = 4,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        data_clients: tuple[Any, Any] | None = None,
    ):
        env = environ if environ is not None else os.environ
        mode = resolve_env(env)  # live guard runs first, before anything else
        self.is_paper = mode == "paper"
        self.name = "alpaca-paper" if self.is_paper else "alpaca-live"
        if client is None:
            key, secret = env.get("ALPACA_API_KEY"), env.get("ALPACA_SECRET_KEY")
            if not key or not secret:
                raise MissingCredentials("ALPACA_API_KEY and ALPACA_SECRET_KEY must both be set")
            client = TradingClient(api_key=key, secret_key=secret, paper=self.is_paper)
        install_timeout(client)  # no-op for test fakes without a requests session
        self._client = client
        self._keys = (env.get("ALPACA_API_KEY"), env.get("ALPACA_SECRET_KEY"))
        self._data = data_clients  # (stock, crypto) market-data clients, built lazily
        self._max_attempts = max(1, max_attempts)
        self._backoff = backoff_seconds
        self._sleep = sleep

    @property
    def client(self) -> TradingClient:
        """Underlying client, exposed for read-only tooling (alpaca_check)."""
        return self._client

    # ---- retry plumbing -------------------------------------------------

    def _wait(self, attempt: int) -> None:
        self._sleep(min(self._backoff * (2 ** attempt), 30.0))

    def _retry(self, fn: Callable[[], T], what: str) -> T:
        """Retry a read-only / naturally idempotent call on transient errors."""
        last: BaseException | None = None
        for attempt in range(self._max_attempts):
            try:
                return fn()
            except (APIError, RequestException) as e:
                if not _is_transient(e):
                    raise
                last = e
                log.warning("alpaca %s transient failure (%d/%d): %s", what, attempt + 1, self._max_attempts, e)
                if attempt + 1 < self._max_attempts:
                    self._wait(attempt)
        raise BrokerUnavailable(f"alpaca {what} failed after {self._max_attempts} attempts: {last}") from last

    # ---- mapping --------------------------------------------------------

    def _map_order(self, o: Any) -> BrokerOrder:
        if o.side is None:
            raise BrokerError(f"order {o.id} has no side (multi-leg orders are unsupported)")
        crypto = o.asset_class == AlpacaAssetClass.CRYPTO
        raw_type = o.order_type or o.type
        try:
            otype = OrderType(_enum_val(raw_type))
        except ValueError:
            log.warning("unmapped order type %r on order %s; reporting as stop", raw_type, o.id)
            otype = OrderType.STOP
        try:
            tif = TIF(_enum_val(o.time_in_force))
        except ValueError:
            log.warning("unmapped TIF %r on order %s; reporting as day", o.time_in_force, o.id)
            tif = TIF.DAY
        return BrokerOrder(
            broker_order_id=str(o.id),
            client_order_id=o.client_order_id,
            symbol=_norm_symbol(o.symbol, crypto),
            side=Side(_enum_val(o.side)),
            status=_STATUS_MAP.get(o.status, OrderStatus.PENDING),
            qty=_f(o.qty),
            filled_qty=_f(o.filled_qty) or 0.0,
            filled_avg_price=_f(o.filled_avg_price),
            submitted_at=o.submitted_at,
            filled_at=o.filled_at,
            order_type=otype,
            tif=tif,
            stop_price=_f(o.stop_price),
            limit_price=_f(o.limit_price),
        )

    @staticmethod
    def _map_position(p: Any) -> BrokerPosition:
        cls = _enum_val(p.asset_class)
        if cls == AlpacaAssetClass.US_EQUITY.value:
            asset = AssetClass.EQUITY
        elif cls == AlpacaAssetClass.CRYPTO.value:
            asset = AssetClass.CRYPTO
        else:
            raise BrokerError(f"unsupported position asset class {cls!r} for {p.symbol}")
        qty = abs(float(p.qty))
        if p.side == PositionSide.SHORT:
            qty = -qty
        avg = float(p.avg_entry_price)
        price = _f(p.current_price)
        mv = _f(p.market_value)
        if price is None:
            price = mv / qty if (mv is not None and qty) else avg
        if mv is None:
            mv = qty * price
        return BrokerPosition(_norm_symbol(p.symbol, asset == AssetClass.CRYPTO), asset, qty, avg, mv, price)

    # ---- Broker protocol ------------------------------------------------

    def account(self) -> AccountSnapshot:
        acct = self._retry(self._client.get_account, "get_account")
        raw_positions = self._retry(self._client.get_all_positions, "get_all_positions")
        if acct.equity is None or acct.cash is None:
            raise BrokerError("account response is missing equity/cash")
        return AccountSnapshot(
            equity=float(acct.equity),
            cash=float(acct.cash),
            buying_power=float(acct.buying_power or 0.0),
            positions=[self._map_position(p) for p in raw_positions],
            as_of=datetime.now(timezone.utc),
            trading_blocked=(bool(acct.trading_blocked) or bool(acct.account_blocked)
                             or acct.status != AccountStatus.ACTIVE),
        )

    def _lookup_once(self, client_order_id: str) -> BrokerOrder | None:
        try:
            return self._map_order(self._client.get_order_by_client_id(client_order_id))
        except APIError as e:
            if _api_status(e) == 404:
                return None
            raise

    def get_order(self, client_order_id: str) -> BrokerOrder | None:
        return self._retry(lambda: self._lookup_once(client_order_id), "get_order_by_client_id")

    def submit(self, intent: OrderIntent) -> BrokerOrder:
        """Idempotent on client_order_id. Every attempt (first or retry) starts
        with a lookup; an order is only sent when the lookup positively says
        it does not exist."""
        req = build_request(intent)  # ValueError before any network call
        cid = intent.client_order_id
        last: BaseException | None = None
        for attempt in range(self._max_attempts):
            try:
                existing = self._lookup_once(cid)
                if existing is not None:
                    return existing
                return self._map_order(self._client.submit_order(req))
            except (APIError, RequestException) as e:
                if not _is_transient(e):
                    # e.g. 422 duplicate id raced with another process, or a real rejection
                    try:
                        existing = self._lookup_once(cid)
                    except (APIError, RequestException):
                        existing = None
                    if existing is not None:
                        return existing
                    raise
                last = e
                log.warning("alpaca submit %s transient failure (%d/%d): %s",
                            cid, attempt + 1, self._max_attempts, e)
                if attempt + 1 < self._max_attempts:
                    self._wait(attempt)
        # The last attempt may have been processed even though the response was lost.
        try:
            existing = self._lookup_once(cid)
        except (APIError, RequestException):
            existing = None
        if existing is not None:
            return existing
        raise BrokerUnavailable(f"alpaca submit {cid} failed after {self._max_attempts} attempts: {last}") from last

    def open_orders(self) -> list[BrokerOrder]:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
        orders = self._retry(lambda: self._client.get_orders(req), "get_orders")
        return [self._map_order(o) for o in orders]

    def cancel(self, client_order_id: str) -> None:
        order = self.get_order(client_order_id)
        if order is None or order.status in _TERMINAL:
            return
        try:
            self._retry(lambda: self._client.cancel_order_by_id(order.broker_order_id), "cancel_order_by_id")
        except APIError as e:
            # Raced with a fill/expiry: fine if the order is now terminal, otherwise surface it.
            if _api_status(e) in (404, 422):
                again = self.get_order(client_order_id)
                if again is None or again.status in _TERMINAL:
                    return
            raise

    def cancel_all(self) -> None:
        results = self._retry(self._client.cancel_orders, "cancel_orders")
        failed = [r for r in results if not (200 <= r.status < 300 or r.status in (404, 422))]
        if failed:
            raise BrokerError("cancel_all failed for: " + ", ".join(f"{r.id}={r.status}" for r in failed))

    def is_equity_market_open(self) -> bool:
        return bool(self._retry(self._client.get_clock, "get_clock").is_open)

    def _data_clients(self) -> tuple[Any, Any] | None:
        if self._data is None:
            key, secret = self._keys
            if not key or not secret:
                return None
            from alpaca.data.historical.crypto import CryptoHistoricalDataClient
            from alpaca.data.historical.stock import StockHistoricalDataClient
            self._data = (StockHistoricalDataClient(api_key=key, secret_key=secret),
                          CryptoHistoricalDataClient(api_key=key, secret_key=secret))
            for c in self._data:
                install_timeout(c)
        return self._data

    def latest_price(self, symbol: str) -> float | None:
        """Latest trade price, or None when market data is unavailable."""
        clients = self._data_clients()
        if clients is None:
            return None
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import CryptoLatestTradeRequest, StockLatestTradeRequest
        stock, crypto = clients
        sym = to_broker(symbol)
        if "/" in symbol:
            fn = lambda: crypto.get_crypto_latest_trade(CryptoLatestTradeRequest(symbol_or_symbols=sym))
        else:
            fn = lambda: stock.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=sym, feed=DataFeed.IEX))
        trade = self._retry(fn, "latest_trade").get(sym)
        price = _f(getattr(trade, "price", None))
        return price if price is not None and price > 0 else None
