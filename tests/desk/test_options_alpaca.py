"""AlpacaBroker options support: order validation and request mapping,
position/order mapping, and the OptionsBroker reads. No network: the trading
client and the data clients are scripted fakes; response objects are real
alpaca-py models (or the raw dicts the raw-data option client returns)."""
from __future__ import annotations

import math
import uuid
from datetime import date, datetime, timezone

import pandas as pd
import pytest
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.models import BarSet, Quote
from alpaca.trading.enums import PositionIntent
from alpaca.trading.models import OptionContract as AlpacaContract, OptionContractsResponse
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from stockmoney.desk.execution import alpaca as al
from stockmoney.desk.execution.alpaca import AlpacaBroker, build_request, validate_intent
from stockmoney.desk.execution.types import TIF, AssetClass, OrderIntent, OrderType, Side

from test_alpaca import FakeClient, account_obj, api_error, make_order, position

OCC = "SPY261013C00580000"
NOW = datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)  # 10:00 ET


def opt(cid="o1", side=Side.BUY, qty=2, **kw) -> OrderIntent:
    return OrderIntent(cid, kw.pop("symbol", OCC), AssetClass.OPTION, side, qty=qty, **kw)


# ---- validation and request mapping -----------------------------------------

def test_market_buy_maps_to_buy_to_open():
    r = build_request(opt())
    assert isinstance(r, MarketOrderRequest)
    assert r.symbol == OCC and r.qty == 2 and r.time_in_force.value == "day"
    assert r.position_intent == PositionIntent.BUY_TO_OPEN and r.side.value == "buy"
    assert r.to_request_fields()["position_intent"] == PositionIntent.BUY_TO_OPEN


def test_limit_sell_maps_to_sell_to_close():
    r = build_request(opt(side=Side.SELL, order_type=OrderType.LIMIT, limit_price=1.25))
    assert isinstance(r, LimitOrderRequest)
    assert r.limit_price == 1.25 and r.position_intent == PositionIntent.SELL_TO_CLOSE and r.side.value == "sell"


@pytest.mark.parametrize("intent", [
    opt(tif=TIF.GTC), opt(tif=TIF.OPG), opt(tif=TIF.IOC),
    opt(order_type=OrderType.STOP, stop_price=1.0),
    opt(order_type=OrderType.STOP_LIMIT, stop_price=1.0, limit_price=1.0),
    OrderIntent("o", OCC, AssetClass.OPTION, Side.BUY, notional=100.0),
    opt(qty=1.5), opt(qty=0), opt(qty=-1), opt(qty=float("nan")), opt(qty=float("inf")), opt(qty=None),
    opt(order_type=OrderType.LIMIT), opt(order_type=OrderType.LIMIT, limit_price=1.234),
    opt(order_type=OrderType.LIMIT, limit_price=0.0), opt(order_type=OrderType.LIMIT, limit_price=float("nan")),
    opt(limit_price=1.0), opt(stop_price=1.0),
    opt(symbol="SPY   261013C00580000"), opt(symbol="SPY"), opt(symbol="BTC/USD"), opt(symbol="SPY261013X00580000"),
    OrderIntent("o", OCC, AssetClass.EQUITY, Side.BUY, qty=1),  # an option symbol sent as equity
])
def test_invalid_option_intents_raise_before_network(intent):
    b, c, _ = _broker()
    with pytest.raises(ValueError):
        b.submit(intent)
    assert c.submit_calls == [] and c.lookup_calls == []


def test_valid_option_intents():
    validate_intent(opt())
    validate_intent(opt(qty=3.0, order_type=OrderType.LIMIT, limit_price=0.05))
    validate_intent(opt(side=Side.SELL, order_type=OrderType.LIMIT, limit_price=12.3))


# ---- broker with fakes --------------------------------------------------------

class OptClient(FakeClient):
    """FakeClient plus the option contracts endpoints."""

    def __init__(self):
        super().__init__()
        self.contracts: list[AlpacaContract] = []
        self.page_size = 10_000
        self.contract_requests: list = []
        self.single_calls: list[str] = []
        self.contracts_error: Exception | None = None

    def submit_order(self, order_data):
        self.submit_calls.append(order_data)
        o = make_order(order_data.client_order_id, symbol=order_data.symbol, asset_class="us_option",
                       side=order_data.side.value, tif=order_data.time_in_force.value, qty=str(order_data.qty))
        self.orders[o.client_order_id] = o
        return o

    def get_option_contracts(self, req):
        self.contract_requests.append(req)
        if self.contracts_error:
            raise self.contracts_error
        start = int(req.page_token or 0)
        page = self.contracts[start:start + self.page_size]
        nxt = start + self.page_size
        return OptionContractsResponse(option_contracts=page,
                                       next_page_token=str(nxt) if nxt < len(self.contracts) else None)

    def get_option_contract(self, occ):
        self.single_calls.append(occ)
        if self.contracts_error:
            raise self.contracts_error
        return next(c for c in self.contracts if c.symbol == occ)


class OptData:
    """Raw-data OptionHistoricalDataClient stand-in."""

    def __init__(self):
        self.chain: dict = {}
        self.snapshots: dict = {}
        self.bars: dict = {}
        self.requests: list = []

    def get_option_chain(self, req):
        self.requests.append(req)
        return self.chain

    def get_option_snapshot(self, req):
        self.requests.append(req)
        return {k: v for k, v in self.snapshots.items() if k == req.symbol_or_symbols}

    def get_option_bars(self, req):
        self.requests.append(req)
        return self.bars


class StockData:
    def __init__(self):
        self.barset = BarSet({})
        self.quotes: dict = {}
        self.requests: list = []

    def get_stock_bars(self, req):
        self.requests.append(req)
        return self.barset

    def get_stock_latest_quote(self, req):
        self.requests.append(req)
        return self.quotes


def _broker(**kw):
    c = OptClient()
    kw.setdefault("environ", {})
    b = AlpacaBroker(c, sleep=lambda s: None, now=lambda: NOW, **kw)
    return b, c, None


def _full(**kw):
    data, stock = OptData(), StockData()
    b, c, _ = _broker(option_data_client=data, data_clients=(stock, object()), **kw)
    return b, c, data, stock


def contract(occ, oi="1500", size="100", underlying="SPY", strike=580.0, typ="call", exp=date(2026, 10, 13)):
    root = occ[:-15]
    return AlpacaContract(id=str(uuid.uuid4()), symbol=occ, name=occ, status="active", tradable=True,
                          expiration_date=exp, root_symbol=root, underlying_symbol=underlying,
                          underlying_asset_id=uuid.uuid4(), type=typ, style="american", strike_price=strike,
                          size=size, open_interest=oi)


def snap(bid=1.0, ask=1.1, last=1.05, iv=0.25, delta=0.5, vol=1234, bar_t="2026-10-13T13:31:00Z",
         quote_t="2026-10-13T13:59:58.123456789Z"):
    out = {"latestQuote": {"bp": bid, "ap": ask, "bs": 10, "as": 12, "t": quote_t},
           "latestTrade": {"p": last, "s": 1, "t": quote_t},
           "impliedVolatility": iv,
           "greeks": {"delta": delta, "gamma": 0.1, "rho": 0.01, "theta": -0.2, "vega": 0.05}}
    if vol is not None:
        out["dailyBar"] = {"o": 1, "h": 1, "l": 1, "c": 1, "v": vol, "t": bar_t, "n": 5, "vw": 1}
    return out


def test_submit_option_order_round_trip():
    b, c, _ = _broker()
    o = b.submit(opt(order_type=OrderType.LIMIT, limit_price=1.1))
    assert o.asset_class == AssetClass.OPTION and o.symbol == OCC and o.qty == 2
    assert c.submit_calls[0].position_intent == PositionIntent.BUY_TO_OPEN
    again = b.submit(opt(order_type=OrderType.LIMIT, limit_price=1.1))
    assert again.broker_order_id == o.broker_order_id and len(c.submit_calls) == 1  # idempotent


def test_option_position_mapping_uses_alpaca_market_value():
    b, c, _ = _broker()
    c.account_obj = account_obj()
    # the docs example: 2 contracts, avg 6.05, current 5.34, market_value 1068
    c.positions = [position("PTON240126C00000500", "us_option", 2, 6.05, 5.34, 1068.0),
                   position("AAPL", "us_equity", 10, 190.0, 200.0, 2000.0)]
    by = {p.symbol: p for p in b.account().positions}
    p = by["PTON240126C00000500"]
    assert p.asset_class == AssetClass.OPTION and p.qty == 2
    assert p.avg_entry_price == 6.05 and p.current_price == 5.34 and p.market_value == 1068.0
    assert by["AAPL"].asset_class == AssetClass.EQUITY


def test_option_position_missing_price_derives_per_share_price():
    b, c, _ = _broker()
    p = position(OCC, "us_option", 2, 1.0, 1.0, 300.0)
    p.current_price = None
    mapped = b._map_position(p)
    assert mapped.current_price == pytest.approx(1.5)
    p2 = position(OCC, "us_option", 2, 1.0, 1.5, 300.0)
    p2.market_value = None
    assert b._map_position(p2).market_value == pytest.approx(300.0)


def test_option_position_without_any_mark_stays_unmarked():
    """No current_price and no market_value: current_price stays None (never
    the entry price, which would hide the loss from the options daily stop)."""
    b, _, _ = _broker()
    p = position(OCC, "us_option", 2, 1.0, 1.0, 300.0)
    p.current_price, p.market_value = None, None
    mapped = b._map_position(p)
    assert mapped.current_price is None and mapped.market_value is None and mapped.avg_entry_price == 1.0
    s = position("AAPL", "us_equity", 10, 190.0, 200.0, 2000.0)
    s.current_price, s.market_value = None, None
    assert b._map_position(s).current_price == 190.0  # stocks unchanged


def test_option_order_mapping():
    b, _, _ = _broker()
    o = b._map_order(make_order("x", symbol=OCC, asset_class="us_option", otype="limit", limit_price="1.1"))
    assert o.asset_class == AssetClass.OPTION and o.symbol == OCC
    assert b._map_order(make_order("y")).asset_class == AssetClass.EQUITY


def test_option_chain_parses_quotes_greeks_and_open_interest():
    b, c, data, _ = _full()
    put = "SPY261014P00570000"
    stale = "SPY261013C00590000"
    data.chain = {OCC: snap(), put: snap(bid=0.0, ask=0.05, iv=float("nan"), delta=-0.1),
                  stale: snap(bar_t="2026-10-09T13:30:00Z"), "GARBAGE": snap()}
    c.contracts = [contract(OCC, oi="1500"), contract(put, oi=None, typ="put", strike=570.0, exp=date(2026, 10, 14))]
    got = b.option_chain("SPY", date(2026, 10, 13), date(2026, 10, 14))
    assert [k.occ for k in got] == [OCC, stale, put]
    k = got[0]
    assert (k.underlying, k.right, k.strike, k.expiry) == ("SPY", "C", 580.0, date(2026, 10, 13))
    assert (k.bid, k.ask, k.last, k.iv, k.delta) == (1.0, 1.1, 1.05, 0.25, 0.5)
    assert k.volume == 1234 and k.open_interest == 1500 and k.multiplier == 100
    assert k.as_of == datetime(2026, 10, 13, 13, 59, 58, 123456, tzinfo=timezone.utc)
    p = got[2]
    assert p.bid == 0.0 and p.iv is None and p.open_interest is None
    assert got[1].volume == 0  # last traded on an earlier day: no volume today
    assert got[1].open_interest is None  # not in the contracts list
    req = data.requests[0]
    assert req.underlying_symbol == "SPY" and req.feed == OptionsFeed("indicative")  # explicit default
    assert c.contract_requests[0].underlying_symbols == ["SPY"]


def test_option_chain_paginates_contracts_and_tolerates_failure():
    b, c, data, _ = _full()
    occs = [f"SPY261013C00{500 + i:03d}000" for i in range(5)]
    data.chain = {o: snap() for o in occs}
    c.contracts = [contract(o, oi=str(i)) for i, o in enumerate(occs)]
    c.page_size = 2
    got = b.option_chain("SPY", date(2026, 10, 13), date(2026, 10, 13))
    assert [k.open_interest for k in got] == [0, 1, 2, 3, 4]
    assert len(c.contract_requests) == 3
    c.contracts_error = api_error(403, "forbidden")
    got = b.option_chain("SPY", date(2026, 10, 13), date(2026, 10, 13))
    assert len(got) == 5 and all(k.open_interest is None for k in got)


def test_option_chain_drops_missing_and_insane_fields():
    b, c, data, _ = _full()
    data.chain = {OCC: {"latestQuote": {"bp": None, "ap": 0, "t": "bad"}, "impliedVolatility": float("inf")}}
    (k,) = b.option_chain("SPY", date(2026, 10, 13), date(2026, 10, 13))
    assert k.bid is None and k.ask is None and k.mid is None and k.iv is None and k.as_of is None
    assert k.volume is None and k.last is None and k.delta is None


def test_adjusted_contract_carries_underlying_and_size():
    b, c, data, _ = _full()
    adj = "SPY1261013C00580000"
    data.chain = {adj: snap()}
    c.contracts = [contract(adj, size="50", underlying="SPY")]
    (k,) = b.option_chain("SPY", date(2026, 10, 13), date(2026, 10, 13))
    assert k.underlying == "SPY" and k.multiplier == 50 and k.occ[:-15] == "SPY1"


def test_option_chain_empty_range_and_no_keys():
    b, c, data, _ = _full()
    assert b.option_chain("SPY", date(2026, 10, 14), date(2026, 10, 13)) == []
    nokeys, _, _ = _broker()
    assert nokeys.option_chain("SPY", date(2026, 10, 13), date(2026, 10, 14)) == []
    assert nokeys.option_quote(OCC) is None
    assert nokeys.stock_quote("SPY") is None
    assert nokeys.minute_bars("SPY", NOW, NOW).empty


def test_option_quote_caches_open_interest_per_day():
    b, c, data, _ = _full()
    data.snapshots = {OCC: snap()}
    c.contracts = [contract(OCC, oi="777")]
    k = b.option_quote(OCC)
    assert k.open_interest == 777 and k.bid == 1.0
    b.option_quote(OCC)
    assert c.single_calls == [OCC]
    assert b.option_quote("SPY261013C00999000") is None  # no snapshot
    with pytest.raises(ValueError):
        b.option_quote("SPY")


def test_option_quote_open_interest_failure_is_none():
    b, c, data, _ = _full()
    data.snapshots = {OCC: snap()}
    c.contracts_error = api_error(404, "not found")
    k = b.option_quote(OCC)
    assert k is not None and k.open_interest is None


@pytest.mark.parametrize("feed", ["opra", "indicative"])
def test_option_feed_is_passed_when_configured(feed):
    b, c, data, _ = _full(option_feed=feed)
    data.snapshots = {OCC: snap()}
    c.contracts = [contract(OCC)]
    b.option_quote(OCC)
    assert data.requests[0].feed == OptionsFeed(feed)


def test_option_feed_from_env_and_validation():
    data = OptData()
    b = AlpacaBroker(OptClient(), environ={"ALPACA_OPTION_FEED": "OPRA"}, option_data_client=data)
    assert b.option_feed == "opra"
    with pytest.raises(ValueError):
        AlpacaBroker(OptClient(), environ={}, option_feed="sip")


def test_option_feed_defaults_to_indicative_when_unset(caplog):
    with caplog.at_level("INFO", logger="stockmoney.desk.execution.alpaca"):
        b = AlpacaBroker(OptClient(), environ={}, option_data_client=OptData())
    assert b.option_feed == "indicative"
    assert "using indicative" in caplog.text


def test_option_minute_bars():
    b, c, data, _ = _full()
    data.bars = {OCC: [
        {"t": "2026-10-13T13:30:00Z", "o": 1.0, "h": 1.2, "l": 0.9, "c": 1.1, "v": 10, "n": 3, "vw": 1.05},
        {"t": "2026-10-13T13:31:00Z", "o": 1.1, "h": 1.3, "l": 1.0, "c": 1.2, "v": 5, "n": 2, "vw": 1.15},
        {"t": "2026-10-13T13:45:00Z", "o": 1.1, "h": 1.3, "l": 1.0, "c": 1.2, "v": 5, "n": 2, "vw": 1.15},
    ]}
    start = datetime(2026, 10, 13, 13, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 13, 13, 31, tzinfo=timezone.utc)
    df = b.minute_bars(OCC, start, end)
    assert list(df.columns) == ["open", "high", "low", "close", "volume", "vwap"]
    assert list(df["close"]) == [1.1, 1.2] and str(df.index.tz) == "UTC"
    assert df.index[0] == pd.Timestamp(start)


def test_stock_minute_bars_iex_and_no_vwap_column_when_absent():
    b, c, _, stock = _full()
    stock.barset = BarSet({"SPY": [
        {"t": "2026-10-13T13:30:00Z", "o": 580, "h": 581, "l": 579, "c": 580.5, "v": 1000, "n": 9, "vw": None},
        {"t": "2026-10-13T13:31:00Z", "o": 580.5, "h": 582, "l": 580, "c": 581.5, "v": 900, "n": 8, "vw": None},
    ]})
    df = b.minute_bars("SPY", datetime(2026, 10, 13, 13, 30, tzinfo=timezone.utc),
                       datetime(2026, 10, 13, 13, 40, tzinfo=timezone.utc))
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert list(df["volume"]) == [1000.0, 900.0]
    assert stock.requests[0].feed == DataFeed.IEX


def test_stock_quote_requires_sane_two_sided_quote():
    b, c, _, stock = _full()
    stock.quotes = {"SPY": Quote("SPY", {"t": "2026-10-13T13:59:59Z", "bp": 580.0, "ap": 580.02, "bs": 1, "as": 1})}
    assert b.stock_quote("SPY") == (580.0, 580.02)
    stock.quotes = {"SPY": Quote("SPY", {"t": "2026-10-13T13:59:59Z", "bp": 0.0, "ap": 580.02, "bs": 0, "as": 1})}
    assert b.stock_quote("SPY") is None
    stock.quotes = {"SPY": Quote("SPY", {"t": "2026-10-13T13:59:59Z", "bp": 581.0, "ap": 580.0, "bs": 1, "as": 1})}
    assert b.stock_quote("SPY") is None
    stock.quotes = {}
    assert b.stock_quote("SPY") is None


def test_live_guard_still_applies_to_options():
    with pytest.raises(al.LiveTradingNotEnabled):
        AlpacaBroker(OptClient(), environ={"ALPACA_ENV": "live"}, option_data_client=OptData())


def test_ts_parser():
    assert al._ts("2026-10-13T13:59:58.123456789Z").microsecond == 123456
    assert al._ts("nope") is None and al._ts(None) is None
    assert al._num(float("nan")) is None and al._num("1.5") == 1.5 and math.isinf(float("inf"))


def test_minute_bars_forbidden_is_empty_but_other_errors_raise():
    b, c, data, stock = _full()

    def forbidden(req):
        raise api_error(403, "OPRA agreement is not signed")
    data.get_option_bars = forbidden
    assert b.minute_bars(OCC, NOW, NOW).empty
    stock.get_stock_bars = forbidden
    assert b.minute_bars("SPY", NOW, NOW).empty

    def broken(req):
        raise api_error(400, "bad request")
    data.get_option_bars = broken
    with pytest.raises(al.APIError):
        b.minute_bars(OCC, NOW, NOW)
