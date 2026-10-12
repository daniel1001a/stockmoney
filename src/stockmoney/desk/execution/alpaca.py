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
  * Options (OptionsBroker + AssetClass.OPTION orders). Order rules from
    docs.alpaca.markets "Options Trading": qty must be a whole number, no
    notional, no extended hours, time_in_force day or gtc. This adapter is
    stricter for the intraday sleeve: market or limit only, TIF day only, and
    every option order carries position_intent buy_to_open (buys) or
    sell_to_close (sells), so the broker itself rejects a sell that would open
    a short ("position intent mismatch"). Option positions come back with
    asset_class us_option; Alpaca's market_value already includes the
    contract multiplier (docs example: qty 2, current_price 5.34,
    market_value 1068).
  * Option market data uses OptionHistoricalDataClient with raw responses,
    because the alpaca-py OptionsSnapshot model drops the dailyBar field
    (today's volume). The feed is left to Alpaca unless ALPACA_OPTION_FEED or
    ``option_feed`` sets it ("opra" with a subscription, otherwise
    "indicative"). Open interest comes from the trading API option contracts
    endpoint (published once a day). Stock bars and quotes use the IEX feed
    by default (the free plan's real-time feed; IEX volume is only IEX's
    share of the consolidated tape).

Order status mapping (Alpaca -> OrderStatus). Everything not listed maps to
PENDING (accepted, pending_new, pending_replace, pending_review,
accepted_for_bidding, done_for_day, replaced, stopped, suspended, calculated,
held); a PENDING order is still working (a stop placed after the close stays
``accepted`` until the next open):
    new -> NEW, partially_filled -> PARTIALLY_FILLED, filled -> FILLED,
    canceled -> CANCELED, rejected -> REJECTED, expired -> EXPIRED,
    pending_cancel -> PENDING_CANCEL (no longer protects, may still fill)
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo

import pandas as pd

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AccountStatus,
    AssetClass as AlpacaAssetClass,
    OrderSide,
    OrderStatus as AlpacaOrderStatus,
    PositionIntent,
    PositionSide,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    OrderRequest,
    StopLimitOrderRequest,
    StopOrderRequest,
)
from requests.exceptions import RequestException

from stockmoney.desk.execution.broker import empty_bars
from stockmoney.desk.execution.symbols import from_broker, is_occ_symbol, parse_occ, to_broker

from stockmoney.desk.execution.types import (
    OPTION_MULTIPLIER, TIF, AccountSnapshot, AssetClass, BrokerOrder, BrokerPosition, OptionContract, OrderIntent,
    OrderStatus, OrderType, Side,
)

log = logging.getLogger(__name__)

LIVE_CONFIRMATION = "I_UNDERSTAND_REAL_MONEY"

EQUITY_TIFS = frozenset({TIF.DAY, TIF.GTC, TIF.OPG, TIF.IOC})
CRYPTO_TIFS = frozenset({TIF.GTC, TIF.IOC})
# alpaca.trading.enums.OrderType docstring: crypto supports market, limit, stop_limit.
CRYPTO_ORDER_TYPES = frozenset({OrderType.MARKET, OrderType.LIMIT, OrderType.STOP_LIMIT})
# Intraday options sleeve: stricter than what Alpaca accepts (day/gtc; market/limit/stop/stop_limit).
OPTION_TIFS = frozenset({TIF.DAY})
OPTION_ORDER_TYPES = frozenset({OrderType.MARKET, OrderType.LIMIT})
OPTION_FEEDS = ("opra", "indicative")
ET = ZoneInfo("America/New_York")

_STATUS_MAP = {
    AlpacaOrderStatus.NEW: OrderStatus.NEW,
    AlpacaOrderStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
    AlpacaOrderStatus.FILLED: OrderStatus.FILLED,
    AlpacaOrderStatus.CANCELED: OrderStatus.CANCELED,
    AlpacaOrderStatus.REJECTED: OrderStatus.REJECTED,
    AlpacaOrderStatus.EXPIRED: OrderStatus.EXPIRED,
    AlpacaOrderStatus.PENDING_CANCEL: OrderStatus.PENDING_CANCEL,
}
_TERMINAL = frozenset({OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED})
_TRANSIENT_HTTP = frozenset({429, 500, 502, 503, 504})
_CRYPTO_QUOTES = ("USDT", "USDC", "USD")
HTTP_TIMEOUT_S = 30.0  # per HTTP request; the job-level alarm is the outer bound

T = TypeVar("T")
_Info = tuple  # (open interest or None, contract size, underlying symbol or None)


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


def _validate_option(intent: OrderIntent) -> None:
    """Option intents: one contract symbol in broker spelling, whole
    contracts, market or limit, TIF day. Buy means buy to open and sell means
    sell to close (enforced at the broker through position_intent)."""
    try:
        parse_occ(intent.symbol)
    except ValueError:
        raise ValueError(f"option symbol must be an OCC contract symbol, got {intent.symbol!r}") from None
    if " " in intent.symbol:
        raise ValueError(f"option symbol must use the unpadded broker spelling, got {intent.symbol!r}")
    if intent.side not in (Side.BUY, Side.SELL):
        raise ValueError(f"unsupported side {intent.side!r}")
    if intent.tif not in OPTION_TIFS:
        raise ValueError(f"TIF {intent.tif.value!r} not valid for options; allowed: day")
    if intent.order_type not in OPTION_ORDER_TYPES:
        raise ValueError(f"order type {intent.order_type.value!r} not supported for options; allowed: market, limit")
    if intent.notional is not None:
        raise ValueError("notional orders are not supported for options")
    q = intent.qty
    if q is None or not isinstance(q, (int, float)) or not math.isfinite(q) or q != int(q) or q < 1:
        raise ValueError(f"option qty must be a whole number of contracts >= 1, got {q!r}")
    if intent.stop_price is not None:
        raise ValueError("stop_price not allowed on option orders")
    if intent.order_type == OrderType.LIMIT:
        lp = intent.limit_price
        if lp is None:
            raise ValueError("limit order requires limit_price")
        if not (isinstance(lp, (int, float)) and math.isfinite(lp) and lp > 0):
            raise ValueError(f"limit_price must be positive and finite, got {lp!r}")
        if abs(lp * 100 - round(lp * 100)) > 1e-6:
            raise ValueError(f"option limit_price must be in whole cents, got {lp!r}")
    elif intent.limit_price is not None:
        raise ValueError("limit_price not allowed on market order")


def validate_intent(intent: OrderIntent) -> None:
    """Raise ValueError for any combination the broker would reject."""
    if not intent.client_order_id:
        raise ValueError("client_order_id is required (idempotency key)")
    if not intent.symbol:
        raise ValueError("symbol is required")
    if intent.asset_class == AssetClass.OPTION:
        _validate_option(intent)
        return
    if is_occ_symbol(intent.symbol):
        raise ValueError(f"{intent.symbol!r} is an option contract; asset_class must be {AssetClass.OPTION.value}")
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
    if intent.asset_class == AssetClass.OPTION:
        common["symbol"] = intent.symbol
        common["qty"] = int(intent.qty)
        common["position_intent"] = (PositionIntent.BUY_TO_OPEN if intent.side == Side.BUY
                                     else PositionIntent.SELL_TO_CLOSE)
        if intent.order_type == OrderType.MARKET:
            return MarketOrderRequest(**common)
        return LimitOrderRequest(limit_price=intent.limit_price, **common)
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


def _num(x: Any) -> float | None:
    """A finite float, or None for anything missing, non-numeric or NaN/inf."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


_FRACTION = re.compile(r"(\.\d{6})\d+")


def _ts(x: Any) -> datetime | None:
    """Parse an RFC-3339 market-data timestamp (nanosecond precision, 'Z')."""
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    if not isinstance(x, str) or not x:
        return None
    try:
        d = datetime.fromisoformat(_FRACTION.sub(r"\1", x.replace("Z", "+00:00")))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _asset_of(raw: Any) -> AssetClass | None:
    v = _enum_val(raw)
    return {AlpacaAssetClass.US_EQUITY.value: AssetClass.EQUITY, AlpacaAssetClass.CRYPTO.value: AssetClass.CRYPTO,
            AlpacaAssetClass.US_OPTION.value: AssetClass.OPTION}.get(v)


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
        option_data_client: Any | None = None,
        option_feed: str | None = None,
        stock_feed: str = "iex",
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
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
        self._opt_data = option_data_client  # raw-response option data client, built lazily
        feed = (option_feed if option_feed is not None else env.get("ALPACA_OPTION_FEED") or "").strip().lower()
        if feed and feed not in OPTION_FEEDS:
            raise ValueError(f"option feed must be one of {OPTION_FEEDS}, got {feed!r}")
        self.option_feed = feed or None  # None: Alpaca picks (opra with a subscription, else indicative)
        self.stock_feed = stock_feed
        self._now = now
        self._oi_cache: dict[tuple[str, date], _Info] = {}
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
            asset_class=_asset_of(o.asset_class),
        )

    @staticmethod
    def _map_position(p: Any) -> BrokerPosition:
        asset = _asset_of(p.asset_class)
        if asset is None:
            raise BrokerError(f"unsupported position asset class {_enum_val(p.asset_class)!r} for {p.symbol}")
        qty = abs(float(p.qty))
        if p.side == PositionSide.SHORT:
            qty = -qty
        avg = float(p.avg_entry_price)
        price = _f(p.current_price)
        mv = _f(p.market_value)
        # option prices are per share; market_value is per position and already includes the multiplier
        mult = OPTION_MULTIPLIER if asset == AssetClass.OPTION else 1
        if price is None:
            price = mv / (qty * mult) if (mv is not None and qty) else avg
        if mv is None:
            mv = qty * price * mult
        if asset == AssetClass.OPTION:
            return BrokerPosition(p.symbol, asset, qty, avg, mv, price)
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
        """Request a cancel. Returns without raising when the order is already
        terminal or already being cancelled, and when a 404/422 answer shows
        the order is in flight (accepted, pending_new, pending_cancel): the
        caller polls get_order for the outcome. A 404/422 on an order that is
        still plainly working (new, partially_filled) is raised."""
        order = self.get_order(client_order_id)
        if order is None or order.status in _TERMINAL or order.status == OrderStatus.PENDING_CANCEL:
            return
        try:
            self._retry(lambda: self._client.cancel_order_by_id(order.broker_order_id), "cancel_order_by_id")
        except APIError as e:
            # Raced with a fill/expiry, or the order is between states: re-read it.
            if _api_status(e) in (404, 422):
                again = self.get_order(client_order_id)
                if again is None or again.status in _TERMINAL or again.status in (
                        OrderStatus.PENDING_CANCEL, OrderStatus.PENDING):
                    log.warning("cancel %s answered %s, order is %s; the caller polls for the outcome",
                                client_order_id, _api_status(e), again.status.value if again else "gone")
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

    # ---- OptionsBroker ---------------------------------------------------

    def _option_client(self) -> Any | None:
        if self._opt_data is None:
            key, secret = self._keys
            if not key or not secret:
                return None
            from alpaca.data.historical.option import OptionHistoricalDataClient
            self._opt_data = OptionHistoricalDataClient(api_key=key, secret_key=secret, raw_data=True)
            install_timeout(self._opt_data)
        return self._opt_data

    def _feed(self) -> Any:
        if self.option_feed is None:
            return None
        from alpaca.data.enums import OptionsFeed
        return OptionsFeed(self.option_feed)

    def _today_et(self) -> date:
        return self._now().astimezone(ET).date()

    def _contract_info(self, underlying: str, expiry_min: date, expiry_max: date) -> dict[str, _Info]:
        """{occ: (open interest or None, contract size)} from the trading API
        contracts endpoint. A failure is logged and yields {}: open interest
        is then unknown, which the options risk layer treats as failing its
        liquidity filter (entries blocked, exits unaffected)."""
        out: dict[str, _Info] = {}
        token: str | None = None
        try:
            for _ in range(50):  # page cap: 50 x 10000 contracts is far beyond any chain
                req = GetOptionContractsRequest(underlying_symbols=[underlying], expiration_date_gte=expiry_min,
                                                expiration_date_lte=expiry_max, limit=10_000, page_token=token)
                resp = self._retry(lambda: self._client.get_option_contracts(req), "get_option_contracts")
                for c in resp.option_contracts or []:
                    out[c.symbol] = _info(c)
                token = resp.next_page_token
                if not token:
                    break
        except Exception as e:  # BaseException (JobTimeout) still propagates
            log.warning("option contracts for %s unavailable (open interest unknown): %s", underlying, e)
            return {}
        return out

    def _single_info(self, occ: str) -> _Info | None:
        key = (occ, self._today_et())
        if key not in self._oi_cache:
            try:
                c = self._retry(lambda: self._client.get_option_contract(occ), "get_option_contract")
            except Exception as e:
                log.warning("option contract %s unavailable (open interest unknown): %s", occ, e)
                return None
            self._oi_cache[key] = _info(c)
        return self._oi_cache[key]

    def _to_contract(self, occ: str, snap: dict, info: _Info | None) -> OptionContract | None:
        try:
            parts = parse_occ(occ)
        except ValueError:
            log.warning("skipping unparseable option symbol %r", occ)
            return None
        snap = snap or {}
        q = snap.get("latestQuote") or {}
        t = snap.get("latestTrade") or {}
        g = snap.get("greeks") or {}
        bar = snap.get("dailyBar") or None
        bid = _num(q.get("bp"))
        ask = _num(q.get("ap"))
        last = _num(t.get("p"))
        iv = _num(snap.get("impliedVolatility"))
        volume: int | None = None
        if bar:
            v = _num(bar.get("v"))
            bar_day = _ts(bar.get("t"))
            # the daily bar of the last session the contract traded in: older than today means 0 today
            if v is not None and bar_day is not None:
                volume = int(v) if bar_day.astimezone(ET).date() == self._today_et() else 0
        oi, size, und = info if info is not None else (None, OPTION_MULTIPLIER, None)
        return OptionContract(
            occ=occ, underlying=from_broker(und) if und else parts.root, right=parts.right, strike=parts.strike, expiry=parts.expiry,
            bid=bid if bid is not None and bid >= 0 else None,
            ask=ask if ask is not None and ask > 0 else None,
            last=last if last is not None and last > 0 else None,
            volume=volume, open_interest=oi,
            delta=_num(g.get("delta")),
            iv=iv if iv is not None and iv > 0 else None,
            as_of=_ts(q.get("t")), multiplier=size)

    def option_chain(self, underlying: str, expiry_min: date, expiry_max: date) -> list[OptionContract]:
        client = self._option_client()
        if client is None or expiry_max < expiry_min:
            return []
        from alpaca.data.requests import OptionChainRequest
        u = to_broker(underlying.upper())
        req = OptionChainRequest(underlying_symbol=u, feed=self._feed(),
                                 expiration_date_gte=expiry_min, expiration_date_lte=expiry_max)
        raw = self._retry(lambda: client.get_option_chain(req), "option_chain") or {}
        info = self._contract_info(u, expiry_min, expiry_max)
        out = [c for occ, snap in raw.items() if (c := self._to_contract(occ, snap, info.get(occ))) is not None]
        out = [c for c in out if expiry_min <= c.expiry <= expiry_max]
        return sorted(out, key=lambda c: (c.expiry, c.right, c.strike))

    def option_quote(self, occ: str) -> OptionContract | None:
        parse_occ(occ)  # ValueError for a malformed symbol: a caller bug, not missing data
        client = self._option_client()
        if client is None:
            return None
        from alpaca.data.requests import OptionSnapshotRequest
        req = OptionSnapshotRequest(symbol_or_symbols=occ, feed=self._feed())
        raw = self._retry(lambda: client.get_option_snapshot(req), "option_snapshot") or {}
        snap = raw.get(occ)
        if not snap:
            return None
        return self._to_contract(occ, snap, self._single_info(occ))

    def _forbidden_is_empty(self, fn: Callable[[], Any], what: str) -> Any:
        """HTTP 403 from the data API means this account may not read that
        data (2026-10-12 on the paper keys: option bars answer "OPRA agreement
        is not signed"; recent SIP stock data needs a subscription). That is
        "unavailable", not a transient failure: logged, and None returned."""
        try:
            return self._retry(fn, what)
        except APIError as e:
            if _api_status(e) != 403:
                raise
            log.warning("alpaca %s not permitted for this account: %s", what, e)
            return None

    def minute_bars(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """See OptionsBroker.minute_bars. Data the account may not read
        (HTTP 403) yields an empty frame and a warning."""
        from alpaca.data.timeframe import TimeFrame
        rows: list[dict] = []
        if is_occ_symbol(symbol):
            client = self._option_client()
            if client is None:
                return empty_bars()
            from alpaca.data.requests import OptionBarsRequest
            req = OptionBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start, end=end)
            raw = self._forbidden_is_empty(lambda: client.get_option_bars(req), "option_bars") or {}
            for b in raw.get(symbol) or []:
                rows.append(dict(t=_ts(b.get("t")), open=_num(b.get("o")), high=_num(b.get("h")),
                                 low=_num(b.get("l")), close=_num(b.get("c")), volume=_num(b.get("v")),
                                 vwap=_num(b.get("vw"))))
        else:
            clients = self._data_clients()
            if clients is None:
                return empty_bars()
            from alpaca.data.enums import DataFeed
            from alpaca.data.requests import StockBarsRequest
            sym = to_broker(symbol)
            req = StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Minute, start=start, end=end,
                                   feed=DataFeed(self.stock_feed))
            barset = self._forbidden_is_empty(lambda: clients[0].get_stock_bars(req), "stock_bars")
            for b in (getattr(barset, "data", None) or {}).get(sym, []):
                rows.append(dict(t=_ts(b.timestamp), open=_num(b.open), high=_num(b.high), low=_num(b.low),
                                 close=_num(b.close), volume=_num(b.volume), vwap=_num(b.vwap)))
        rows = [r for r in rows if r["t"] is not None]
        if not rows:
            return empty_bars()
        df = pd.DataFrame(rows).set_index("t").sort_index()
        df.index = pd.DatetimeIndex(df.index).tz_convert("UTC")
        df.index.name = None
        df = df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
        if df["vwap"].isna().all():
            df = df.drop(columns="vwap")
        return df.astype(float)

    def stock_quote(self, symbol: str) -> tuple[float, float] | None:
        clients = self._data_clients()
        if clients is None:
            return None
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestQuoteRequest
        sym = to_broker(symbol)
        req = StockLatestQuoteRequest(symbol_or_symbols=sym, feed=DataFeed(self.stock_feed))
        q = (self._retry(lambda: clients[0].get_stock_latest_quote(req), "stock_latest_quote") or {}).get(sym)
        bid, ask = _num(getattr(q, "bid_price", None)), _num(getattr(q, "ask_price", None))
        if bid is None or ask is None or bid <= 0 or ask <= 0 or bid > ask:
            return None
        return bid, ask


def _info(c: Any) -> _Info:
    """(open interest or None, contract size, underlying symbol or None) of an
    alpaca-py OptionContract. For an adjusted contract the underlying differs
    from the OCC root and the size may differ from 100."""
    oi = _num(getattr(c, "open_interest", None))
    size = _num(getattr(c, "size", None))
    und = getattr(c, "underlying_symbol", None)
    return (int(oi) if oi is not None and oi >= 0 else None,
            int(size) if size is not None and size > 0 else OPTION_MULTIPLIER,
            und if isinstance(und, str) and und else None)
