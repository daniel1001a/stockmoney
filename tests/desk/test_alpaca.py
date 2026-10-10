"""AlpacaBroker tests. TradingClient is always replaced by a scripted fake; no
network, no orders. Response objects are real alpaca-py pydantic models so the
mapping is exercised against the actual SDK shapes."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import TimeInForce
from alpaca.trading.models import Clock, Order, Position, TradeAccount
from alpaca.trading.requests import (
    CancelOrderResponse, LimitOrderRequest, MarketOrderRequest, StopLimitOrderRequest, StopOrderRequest,
)
from requests.exceptions import ConnectionError as ReqConnectionError, Timeout

from stockmoney.desk.execution import alpaca as al
from stockmoney.desk.execution.alpaca import (
    AlpacaBroker, BrokerError, BrokerUnavailable, LiveTradingNotEnabled, MissingCredentials,
)
from stockmoney.desk.execution.types import (
    TIF, AssetClass, OrderIntent, OrderStatus, OrderType, Side,
)

NOW = datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc)


# ---- helpers ----------------------------------------------------------------

def api_error(status: int, message: str = "boom") -> APIError:
    http_error = SimpleNamespace(response=SimpleNamespace(status_code=status), request=None)
    return APIError(json.dumps({"code": status * 1000, "message": message}), http_error)


def make_order(cid: str, *, status: str = "new", symbol: str = "AAPL", asset_class: str = "us_equity",
               side: str = "buy", otype: str = "market", tif: str = "day", qty="10", filled_qty="0",
               filled_avg_price=None, limit_price=None, stop_price=None) -> Order:
    return Order(
        id=uuid.uuid4(), client_order_id=cid, created_at=NOW, updated_at=NOW, submitted_at=NOW,
        symbol=symbol, asset_class=asset_class, qty=qty, filled_qty=filled_qty,
        filled_avg_price=filled_avg_price, order_class="simple", order_type=otype, type=otype,
        side=side, time_in_force=tif, limit_price=limit_price, stop_price=stop_price,
        status=status, extended_hours=False,
    )


class FakeClient:
    """Scripted TradingClient stand-in. ``submit_script`` entries are consumed
    one per submit_order call: an Exception is raised, but with
    ``land_before_raise`` the order is stored first (response lost in transit)."""

    def __init__(self):
        self.orders: dict[str, Order] = {}
        self.submit_calls: list = []
        self.lookup_calls: list[str] = []
        self.submit_script: list = []
        self.lookup_errors: list = []
        self.land_before_raise = False
        self.cancelled: list[str] = []
        self.cancel_error: Exception | None = None
        self.account_obj = None
        self.positions: list = []
        self.clock = Clock(timestamp=NOW, is_open=True, next_open=NOW, next_close=NOW)
        self.cancel_all_result: list = []
        self.open_filter = None

    def get_order_by_client_id(self, client_id):
        self.lookup_calls.append(client_id)
        if self.lookup_errors:
            raise self.lookup_errors.pop(0)
        if client_id not in self.orders:
            raise api_error(404, "order not found")
        return self.orders[client_id]

    def submit_order(self, order_data):
        self.submit_calls.append(order_data)
        step = self.submit_script.pop(0) if self.submit_script else None
        o = make_order(order_data.client_order_id, symbol=order_data.symbol,
                       side=order_data.side.value, tif=order_data.time_in_force.value,
                       qty=str(order_data.qty) if order_data.qty is not None else None)
        if isinstance(step, Exception):
            if self.land_before_raise:
                self.orders[o.client_order_id] = o
            raise step
        self.orders[o.client_order_id] = o
        return o

    def get_orders(self, filter=None):
        self.open_filter = filter
        return [o for o in self.orders.values() if o.status.value in ("new", "partially_filled", "accepted")]

    def cancel_order_by_id(self, order_id):
        if self.cancel_error:
            raise self.cancel_error
        self.cancelled.append(str(order_id))

    def cancel_orders(self):
        return self.cancel_all_result

    def get_account(self):
        return self.account_obj

    def get_all_positions(self):
        return self.positions

    def get_clock(self):
        return self.clock


def make_broker(client: FakeClient | None = None, **kw) -> tuple[AlpacaBroker, FakeClient, list[float]]:
    client = client or FakeClient()
    sleeps: list[float] = []
    kw.setdefault("environ", {})
    b = AlpacaBroker(client, sleep=sleeps.append, **kw)
    return b, client, sleeps


def equity_intent(cid="c1", **kw) -> OrderIntent:
    base = dict(client_order_id=cid, symbol="AAPL", asset_class=AssetClass.EQUITY, side=Side.BUY, qty=10)
    base.update(kw)
    return OrderIntent(**base)


def crypto_intent(cid="k1", **kw) -> OrderIntent:
    base = dict(client_order_id=cid, symbol="BTC/USD", asset_class=AssetClass.CRYPTO, side=Side.BUY,
                notional=500.0, tif=TIF.GTC)
    base.update(kw)
    return OrderIntent(**base)


# ---- live protection / construction -----------------------------------------

def test_paper_is_default_and_flags():
    b, _, _ = make_broker()
    assert b.is_paper is True and b.name == "alpaca-paper"


def test_live_without_confirmation_raises():
    with pytest.raises(LiveTradingNotEnabled):
        AlpacaBroker(FakeClient(), environ={"ALPACA_ENV": "live"})


def test_live_with_wrong_confirmation_raises():
    with pytest.raises(LiveTradingNotEnabled):
        AlpacaBroker(FakeClient(), environ={"ALPACA_ENV": "live", "ALPACA_LIVE_ENABLED": "true"})


def test_live_guard_runs_before_credentials_and_client_construction(monkeypatch):
    built = []
    monkeypatch.setattr(al, "TradingClient", lambda **kw: built.append(kw))
    with pytest.raises(LiveTradingNotEnabled):
        AlpacaBroker(environ={"ALPACA_ENV": "live", "ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"})
    assert built == []


def test_live_with_confirmation_constructs_live_client(monkeypatch):
    built = []
    monkeypatch.setattr(al, "TradingClient", lambda **kw: built.append(kw) or FakeClient())
    b = AlpacaBroker(environ={"ALPACA_ENV": "live", "ALPACA_LIVE_ENABLED": al.LIVE_CONFIRMATION,
                              "ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"})
    assert b.is_paper is False and b.name == "alpaca-live"
    assert built == [dict(api_key="k", secret_key="s", paper=False)]


def test_paper_constructs_paper_client(monkeypatch):
    built = []
    monkeypatch.setattr(al, "TradingClient", lambda **kw: built.append(kw) or FakeClient())
    AlpacaBroker(environ={"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"})
    assert built == [dict(api_key="k", secret_key="s", paper=True)]


def test_env_read_from_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("ALPACA_ENV", "live")
    monkeypatch.delenv("ALPACA_LIVE_ENABLED", raising=False)
    with pytest.raises(LiveTradingNotEnabled):
        AlpacaBroker(FakeClient())


def test_unknown_env_rejected():
    with pytest.raises(ValueError):
        AlpacaBroker(FakeClient(), environ={"ALPACA_ENV": "sandbox"})


def test_missing_credentials_raises():
    with pytest.raises(MissingCredentials):
        AlpacaBroker(environ={})
    with pytest.raises(MissingCredentials):
        AlpacaBroker(environ={"ALPACA_API_KEY": "k"})


# ---- request building / enum mapping ----------------------------------------

def test_market_equity_request_mapping():
    b, c, _ = make_broker()
    o = b.submit(equity_intent(tif=TIF.OPG))
    req = c.submit_calls[0]
    assert isinstance(req, MarketOrderRequest)
    assert (req.symbol, req.qty, req.client_order_id) == ("AAPL", 10, "c1")
    assert req.side.value == "buy" and req.time_in_force == TimeInForce.OPG
    assert o.client_order_id == "c1" and o.side == Side.BUY and o.tif == TIF.OPG


def test_request_class_per_order_type():
    cases = [
        (dict(order_type=OrderType.LIMIT, limit_price=1.5), LimitOrderRequest),
        (dict(order_type=OrderType.STOP, stop_price=1.5, side=Side.SELL, tif=TIF.GTC), StopOrderRequest),
        (dict(order_type=OrderType.STOP_LIMIT, stop_price=1.5, limit_price=1.4, side=Side.SELL, tif=TIF.GTC),
         StopLimitOrderRequest),
    ]
    for kw, cls in cases:
        req = al.build_request(equity_intent(**kw))
        assert type(req) is cls
        assert req.to_request_fields()["type"] == kw["order_type"].value


def test_crypto_notional_request():
    req = al.build_request(crypto_intent())
    assert isinstance(req, MarketOrderRequest)
    fields = req.to_request_fields()
    assert fields["symbol"] == "BTC/USD" and fields["notional"] == 500.0
    assert fields["time_in_force"] == TimeInForce.GTC and "qty" not in fields


@pytest.mark.parametrize("alpaca_status,expected", [
    ("new", OrderStatus.NEW),
    ("partially_filled", OrderStatus.PARTIALLY_FILLED),
    ("filled", OrderStatus.FILLED),
    ("canceled", OrderStatus.CANCELED),
    ("rejected", OrderStatus.REJECTED),
    ("expired", OrderStatus.EXPIRED),
    ("accepted", OrderStatus.PENDING),
    ("pending_new", OrderStatus.PENDING),
    ("pending_cancel", OrderStatus.PENDING_CANCEL),
    ("pending_replace", OrderStatus.PENDING),
    ("done_for_day", OrderStatus.PENDING),
    ("replaced", OrderStatus.PENDING),
    ("held", OrderStatus.PENDING),
    ("stopped", OrderStatus.PENDING),
])
def test_status_mapping(alpaca_status, expected):
    b, c, _ = make_broker()
    c.orders["x"] = make_order("x", status=alpaca_status)
    assert b.get_order("x").status == expected


def test_order_field_mapping_and_crypto_symbol_normalised():
    b, c, _ = make_broker()
    c.orders["x"] = make_order("x", status="partially_filled", symbol="BTCUSD", asset_class="crypto",
                               side="sell", otype="stop_limit", tif="gtc", qty="0.5", filled_qty="0.2",
                               filled_avg_price="60000.5", limit_price="59000", stop_price="59500")
    o = b.get_order("x")
    assert o.symbol == "BTC/USD"
    assert (o.side, o.order_type, o.tif) == (Side.SELL, OrderType.STOP_LIMIT, TIF.GTC)
    assert (o.qty, o.filled_qty, o.filled_avg_price) == (0.5, 0.2, 60000.5)
    assert (o.limit_price, o.stop_price) == (59000.0, 59500.0)
    assert o.broker_order_id == str(c.orders["x"].id)


def test_get_order_returns_none_when_not_found():
    b, c, _ = make_broker()
    assert b.get_order("nope") is None


def test_get_order_non_404_error_propagates():
    b, c, _ = make_broker()
    c.lookup_errors = [api_error(401, "forbidden")]
    with pytest.raises(APIError):
        b.get_order("x")


# ---- validation (nothing is sent) --------------------------------------------

@pytest.mark.parametrize("intent", [
    crypto_intent(tif=TIF.DAY),
    crypto_intent(tif=TIF.OPG),
    crypto_intent(order_type=OrderType.STOP, notional=None, qty=0.1, stop_price=1.0),
    crypto_intent(symbol="BTCUSD"),
    equity_intent(symbol="BTC/USD"),
    equity_intent(tif=TIF.OPG, order_type=OrderType.STOP, stop_price=1.0),
    equity_intent(qty=None),
    equity_intent(qty=1, notional=100.0),
    equity_intent(qty=0),
    equity_intent(qty=-1),
    equity_intent(qty=None, notional=100.0, order_type=OrderType.LIMIT, limit_price=1.0),
    equity_intent(qty=1.5, order_type=OrderType.LIMIT, limit_price=1.0),
    equity_intent(order_type=OrderType.LIMIT),
    equity_intent(order_type=OrderType.STOP),
    equity_intent(order_type=OrderType.STOP_LIMIT, stop_price=1.0),
    equity_intent(limit_price=1.0),
    equity_intent(stop_price=1.0),
    equity_intent(cid=""),
])
def test_invalid_intents_raise_before_any_network_call(intent):
    b, c, _ = make_broker()
    with pytest.raises(ValueError):
        b.submit(intent)
    assert c.lookup_calls == [] and c.submit_calls == []


@pytest.mark.parametrize("tif", [TIF.DAY, TIF.GTC, TIF.OPG, TIF.IOC])
def test_equity_tifs_accepted(tif):
    al.validate_intent(equity_intent(tif=tif))


@pytest.mark.parametrize("tif", [TIF.GTC, TIF.IOC])
def test_crypto_tifs_accepted(tif):
    al.validate_intent(crypto_intent(tif=tif))


def test_fractional_equity_market_order_allowed():
    al.validate_intent(equity_intent(qty=1.5))


# ---- idempotency -------------------------------------------------------------

def test_submit_returns_existing_order_without_submitting():
    b, c, _ = make_broker()
    existing = make_order("c1", status="filled", filled_qty="10", filled_avg_price="190")
    c.orders["c1"] = existing
    o = b.submit(equity_intent())
    assert c.submit_calls == []
    assert o.broker_order_id == str(existing.id) and o.status == OrderStatus.FILLED


def test_submit_twice_sends_once():
    b, c, _ = make_broker()
    first = b.submit(equity_intent())
    second = b.submit(equity_intent())
    assert len(c.submit_calls) == 1
    assert first.broker_order_id == second.broker_order_id


# ---- retry never double-submits ------------------------------------------------

def test_retry_after_lost_response_does_not_resubmit():
    """Order lands at the broker but the response is lost: the retry's lookup
    finds it and nothing more is sent."""
    b, c, sleeps = make_broker()
    c.land_before_raise = True
    c.submit_script = [ReqConnectionError("reset by peer")]
    o = b.submit(equity_intent())
    assert len(c.submit_calls) == 1
    assert c.lookup_calls == ["c1", "c1"]
    assert o.client_order_id == "c1"
    assert len(sleeps) == 1


def test_retry_when_order_did_not_land_submits_again_after_lookup():
    b, c, _ = make_broker()
    c.submit_script = [Timeout("timed out"), api_error(503)]
    o = b.submit(equity_intent())
    assert len(c.submit_calls) == 3
    # a lookup precedes every submit attempt
    assert c.lookup_calls == ["c1", "c1", "c1"]
    assert o.status == OrderStatus.NEW and len(c.orders) == 1


def test_lookup_failure_blocks_resubmission():
    """If we cannot tell whether the order exists, we must not send it."""
    b, c, _ = make_broker(max_attempts=3)
    c.lookup_errors = [Timeout("t"), Timeout("t"), Timeout("t"), Timeout("t")]
    with pytest.raises(BrokerUnavailable):
        b.submit(equity_intent())
    assert c.submit_calls == []


def test_exhausted_retries_raise_after_final_lookup():
    b, c, sleeps = make_broker(max_attempts=3)
    c.submit_script = [Timeout("t")] * 3
    with pytest.raises(BrokerUnavailable):
        b.submit(equity_intent())
    assert len(c.submit_calls) == 3
    assert len(c.lookup_calls) == 4  # 3 pre-submit + 1 final check
    assert sleeps == [1.0, 2.0]  # exponential backoff, none after the last attempt


def test_exhausted_retries_but_order_landed_returns_it():
    b, c, _ = make_broker(max_attempts=2)
    c.land_before_raise = True
    c.submit_script = [Timeout("t"), Timeout("t")]
    # first retry lookup already finds it, so only one submit happens
    o = b.submit(equity_intent())
    assert len(c.submit_calls) == 1 and o.client_order_id == "c1"


def test_non_transient_rejection_is_not_retried():
    b, c, sleeps = make_broker()
    c.submit_script = [api_error(403, "insufficient buying power")]
    with pytest.raises(APIError):
        b.submit(equity_intent())
    assert len(c.submit_calls) == 1 and sleeps == []


def test_duplicate_id_rejection_resolves_to_existing_order():
    """Another process landed the same client_order_id between our lookup and
    submit; the broker's rejection is resolved by a fresh lookup."""
    b, c, _ = make_broker()
    winner = make_order("c1")
    real_submit = c.submit_order

    def racing_submit(req):
        c.orders["c1"] = winner
        raise api_error(422, "client_order_id must be unique")
    c.submit_order = racing_submit
    o = b.submit(equity_intent())
    assert o.broker_order_id == str(winner.id)
    c.submit_order = real_submit


# ---- account / positions / clock ---------------------------------------------

def account_obj(**kw) -> TradeAccount:
    base = dict(id=uuid.uuid4(), account_number="PA1", status="ACTIVE", equity="50000.25", cash="20000",
                buying_power="40000", trading_blocked=False, account_blocked=False)
    base.update(kw)
    return TradeAccount(**base)


def position(symbol, asset_class, qty, avg, price, mv, side="long") -> Position:
    return Position(asset_id=uuid.uuid4(), symbol=symbol, exchange="NASDAQ" if asset_class == "us_equity" else "CRYPTO",
                    asset_class=asset_class, avg_entry_price=str(avg), qty=str(qty), side=side,
                    market_value=str(mv), cost_basis=str(avg * abs(float(qty))), current_price=str(price))


def test_account_snapshot_mapping():
    b, c, _ = make_broker()
    c.account_obj = account_obj()
    c.positions = [
        position("AAPL", "us_equity", 10, 190.0, 200.0, 2000.0),
        position("BTCUSD", "crypto", 0.01, 60000.0, 65000.0, 650.0),
        position("TSLA", "us_equity", -5, 250.0, 240.0, -1200.0, side="short"),
    ]
    snap = b.account()
    assert (snap.equity, snap.cash, snap.buying_power) == (50000.25, 20000.0, 40000.0)
    assert snap.trading_blocked is False and snap.as_of is not None
    by = {p.symbol: p for p in snap.positions}
    assert set(by) == {"AAPL", "BTC/USD", "TSLA"}
    assert by["AAPL"].asset_class == AssetClass.EQUITY and by["AAPL"].qty == 10
    assert by["BTC/USD"].asset_class == AssetClass.CRYPTO and by["BTC/USD"].current_price == 65000.0
    assert by["TSLA"].qty == -5.0


@pytest.mark.parametrize("kw", [
    dict(trading_blocked=True), dict(account_blocked=True), dict(status="ACCOUNT_CLOSED"),
])
def test_blocked_account_flags_trading_blocked(kw):
    b, c, _ = make_broker()
    c.account_obj = account_obj(**kw)
    assert b.account().trading_blocked is True


def test_account_missing_equity_raises():
    b, c, _ = make_broker()
    c.account_obj = account_obj(equity=None)
    with pytest.raises(BrokerError):
        b.account()


def test_unsupported_position_class_raises():
    b, c, _ = make_broker()
    c.account_obj = account_obj()
    c.positions = [position("AAPL250117C00200000", "us_option", 1, 1.0, 1.0, 100.0)]
    with pytest.raises(BrokerError):
        b.account()


def test_account_retries_transient_errors():
    b, c, sleeps = make_broker()
    c.account_obj = account_obj()
    real = c.get_account
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise api_error(504)
        return real()
    c.get_account = flaky
    assert b.account().equity == 50000.25
    assert len(calls) == 3 and len(sleeps) == 2


def test_is_equity_market_open():
    b, c, _ = make_broker()
    assert b.is_equity_market_open() is True
    c.clock = Clock(timestamp=NOW, is_open=False, next_open=NOW, next_close=NOW)
    assert b.is_equity_market_open() is False


# ---- open orders / cancel -------------------------------------------------------

def test_open_orders_queries_open_status():
    b, c, _ = make_broker()
    c.orders["a"] = make_order("a", status="new")
    c.orders["b"] = make_order("b", status="filled")
    c.orders["c"] = make_order("c", status="accepted")
    out = b.open_orders()
    assert {o.client_order_id for o in out} == {"a", "c"}
    assert c.open_filter.status.value == "open"
    assert {o.client_order_id: o.status for o in out}["c"] == OrderStatus.PENDING


def test_cancel_uses_broker_order_id():
    b, c, _ = make_broker()
    c.orders["a"] = make_order("a", status="new")
    b.cancel("a")
    assert c.cancelled == [str(c.orders["a"].id)]


def test_cancel_unknown_or_terminal_is_noop():
    b, c, _ = make_broker()
    c.orders["f"] = make_order("f", status="filled")
    b.cancel("missing")
    b.cancel("f")
    assert c.cancelled == []


def test_cancel_race_with_fill_is_swallowed_but_real_failure_raises():
    b, c, _ = make_broker()
    c.orders["a"] = make_order("a", status="new")
    c.cancel_error = api_error(422, "order not cancelable")
    with pytest.raises(APIError):  # order still working: surface it
        b.cancel("a")

    calls = {"n": 0}
    real_lookup = c.get_order_by_client_id

    def lookup(cid):
        calls["n"] += 1
        if calls["n"] > 1:  # after the failed cancel the order shows as filled
            c.orders["a"] = make_order("a", status="filled")
        return real_lookup(cid)
    c.get_order_by_client_id = lookup
    b.cancel("a")


def test_cancel_all():
    b, c, _ = make_broker()
    oid = uuid.uuid4()
    c.cancel_all_result = [CancelOrderResponse(id=oid, status=200), CancelOrderResponse(id=uuid.uuid4(), status=404)]
    b.cancel_all()
    c.cancel_all_result = [CancelOrderResponse(id=oid, status=500)]
    with pytest.raises(BrokerError):
        b.cancel_all()
