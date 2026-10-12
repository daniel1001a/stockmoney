"""OptionsRiskEngine and OptionsBook: every rule of OPTIONS_PLAN.md section 3."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from stockmoney.desk import store
from stockmoney.desk.execution.broker import FakeBroker
from stockmoney.desk.execution.ledger import Ledger
from stockmoney.desk.execution.options_risk import (
    HALT_KEY, OptionsBook, OptionsBookError, OptionsLimits, OptionsRiskEngine, et_day_bounds, muse_blocked,
    options_mismatches, sleeve_state,
)
from stockmoney.desk.execution.types import (
    TIF, AccountSnapshot, AssetClass, BrokerOrder, BrokerPosition, OptionContract, OrderIntent, OrderStatus,
    OrderType, Side,
)

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 13, 10, 0, tzinfo=ET)  # a Tuesday, inside the entry window
OCC = "SPY261013C00580000"
OCC2 = "SPY261013P00570000"
OCC3 = "QQQ261013C00500000"
OCC4 = "NVDA261013C00230000"


def k(occ=OCC, bid=2.00, ask=2.10, oi=1000, vol=500, age_s=5.0, expiry=date(2026, 10, 13), mult=100,
      underlying=None, **kw) -> OptionContract:
    from stockmoney.desk.execution.symbols import parse_occ
    p = parse_occ(occ)
    return OptionContract(occ, underlying or p.root, p.right, p.strike, expiry, bid=bid, ask=ask, last=2.05,
                          volume=vol, open_interest=oi, delta=0.5, iv=0.3,
                          as_of=None if age_s is None else NOW - timedelta(seconds=age_s), multiplier=mult, **kw)


def buy(qty=2, occ=OCC, cid="b1", **kw) -> OrderIntent:
    kw.setdefault("order_type", OrderType.LIMIT)
    if kw["order_type"] == OrderType.LIMIT:
        kw.setdefault("limit_price", 2.10)
    return OrderIntent(cid, occ, AssetClass.OPTION, Side.BUY, qty=qty, strategy="orb15", **kw)


def sell(qty=2, occ=OCC, cid="s1", **kw) -> OrderIntent:
    return OrderIntent(cid, occ, AssetClass.OPTION, Side.SELL, qty=qty, strategy="orb15", **kw)


def snap(*positions, blocked=False) -> AccountSnapshot:
    return AccountSnapshot(100_000, 90_000, 90_000, list(positions), NOW, trading_blocked=blocked)


def opos(occ=OCC, qty=2, avg=2.0, price=2.05) -> BrokerPosition:
    return BrokerPosition(occ, AssetClass.OPTION, qty, avg, qty * price * 100, price)


@pytest.fixture
def eng(con, tmp_path):
    def _make(**kw):
        return OptionsRiskEngine(OptionsLimits(halt_file=tmp_path / "HALT", **kw), con)
    return _make


def chk(e, intent=None, contract="default", s=None, now=NOW, **kw):
    kw.setdefault("blocked", {})
    return e.check(intent or buy(), k() if contract == "default" else contract, s if s is not None else snap(),
                   now, **kw)


def book_open(e, occ=OCC, qty=2, price=2.0, cid="seed-b", at=NOW):
    e.book.apply_fill(client_order_id=cid, occ=occ, strategy="orb15", side=Side.BUY, qty=qty, price=price,
                      filled_at=at)


# ---- limits ------------------------------------------------------------------

def test_default_limits_match_the_plan():
    l = OptionsLimits()
    assert l.sleeve_budget == 5_000 and l.max_trade_premium == 1_000
    assert l.daily_loss_stop == 1_000 and l.dd_halt == 1_750 and l.max_positions == 3
    assert (l.entry_start, l.entry_end, l.flat_by) == (time(9, 30), time(11, 30), time(15, 45))
    assert l.halt_file.as_posix().endswith("data/desk/HALT")
    assert l.problems() == []


def test_raising_sleeve_pct_scales_dollar_limits():
    l = OptionsLimits(sleeve_pct=0.30)
    assert l.sleeve_budget == 15_000 and l.max_trade_premium == 3_000 and l.dd_halt == 5_250


@pytest.mark.parametrize("kw", [dict(sleeve_pct=0.31), dict(sleeve_pct=float("nan")), dict(max_trade_pct=0),
                                dict(min_mid=-1), dict(max_positions=-1), dict(entry_end=time(16, 0))])
def test_bad_limits_block_every_entry(eng, kw):
    d = chk(eng(**kw))
    assert not d.allow and "invalid limits" in d.reason


# ---- the happy path and validation ----------------------------------------------

def test_clean_entry_is_allowed(eng):
    d = chk(eng())
    assert d.allow and d.reason == "ok" and d.intent == buy()


def test_market_entry_priced_at_ask(eng):
    d = chk(eng(), buy(qty=4, order_type=OrderType.MARKET))
    assert d.allow and d.intent.qty == 4  # 4 x 2.10 x 100 = 840 <= 1000


@pytest.mark.parametrize("intent", [
    OrderIntent("x", OCC, AssetClass.EQUITY, Side.BUY, qty=1),
    OrderIntent("x", "SPY", AssetClass.OPTION, Side.BUY, qty=1),
    OrderIntent("x", "SPY   261013C00580000", AssetClass.OPTION, Side.BUY, qty=1),
    OrderIntent("x", OCC, AssetClass.OPTION, Side.BUY, notional=100.0),
    OrderIntent("x", OCC, AssetClass.OPTION, Side.BUY, qty=1.5),
    OrderIntent("x", OCC, AssetClass.OPTION, Side.BUY, qty=0),
    OrderIntent("x", OCC, AssetClass.OPTION, Side.BUY, qty=float("nan")),
    OrderIntent("x", OCC, AssetClass.OPTION, Side.BUY, qty=float("inf")),
    OrderIntent("x", OCC, AssetClass.OPTION, Side.BUY, qty=1, tif=TIF.GTC),
    OrderIntent("x", OCC, AssetClass.OPTION, Side.BUY, qty=1, order_type=OrderType.STOP, stop_price=1.0),
    OrderIntent("x", OCC, AssetClass.OPTION, Side.BUY, qty=1, order_type=OrderType.LIMIT),
    OrderIntent("x", OCC, AssetClass.OPTION, Side.BUY, qty=1, order_type=OrderType.LIMIT, limit_price=float("nan")),
    OrderIntent("x", OCC, AssetClass.OPTION, Side.BUY, qty=1, limit_price=2.0),
    OrderIntent("x", OCC, AssetClass.OPTION, Side.SELL, qty=1, tif=TIF.GTC),
])
def test_invalid_intents_blocked_both_sides(eng, intent):
    d = chk(eng(), intent, s=snap(opos()))
    assert not d.allow and "invalid option intent" in d.reason


def test_naive_now_blocked(eng):
    assert not chk(eng(), now=datetime(2026, 10, 13, 10, 0)).allow


def test_quote_for_another_contract_blocked(eng):
    d = chk(eng(), contract=k(occ=OCC2))
    assert not d.allow and "quote is for" in d.reason


# ---- switches -----------------------------------------------------------------------

def test_halt_file_blocks_entries_not_exits(eng, tmp_path):
    e = eng()
    (tmp_path / "HALT").write_text("x")
    assert "kill switch" in chk(e).reason
    book_open(e)
    assert chk(e, sell(), s=snap(opos())).allow


@pytest.mark.parametrize("key", ["halt_new_entries", "options_halt_new_entries"])
def test_settings_block_entries(eng, con, key):
    store.set_setting(con, key, "1")
    d = chk(eng())
    assert not d.allow and key in d.reason


def test_trading_blocked_and_missing_snapshot(eng):
    e = eng()
    assert "trading_blocked" in chk(e, s=snap(blocked=True)).reason
    assert "no broker snapshot" in e.check(buy(), k(), None, NOW, blocked={}).reason


# ---- time ------------------------------------------------------------------------------

@pytest.mark.parametrize("hh, mm, ok", [(9, 29, False), (9, 30, True), (11, 29, True), (11, 30, False),
                                        (15, 44, False), (15, 45, False), (16, 30, False), (8, 0, False)])
def test_entry_window(eng, hh, mm, ok):
    now = datetime(2026, 10, 13, hh, mm, tzinfo=ET)
    d = eng().check(buy(), replace(k(), as_of=now), snap(), now, blocked={})
    assert d.allow is ok, d.reason


def test_window_uses_et_whatever_the_input_zone(eng):
    now = datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
    assert eng().check(buy(), replace(k(), as_of=now), snap(), now, blocked={}).allow


def test_forced_flat_time_reason(eng):
    now = datetime(2026, 10, 13, 15, 50, tzinfo=ET)
    d = eng(entry_end=time(15, 45)).check(buy(), replace(k(), as_of=now), snap(), now, blocked={})
    assert not d.allow and "forced-flat" in d.reason


def test_weekend_blocked(eng):
    now = datetime(2026, 10, 17, 10, 0, tzinfo=ET)
    assert "not a trading day" in eng().check(buy(), replace(k(), as_of=now), snap(), now, blocked={}).reason


def test_exits_allowed_outside_window_and_after_flat(eng):
    e = eng()
    book_open(e)
    for hh, mm in ((15, 50), (8, 0), (16, 10)):
        now = datetime(2026, 10, 13, hh, mm, tzinfo=ET)
        assert e.check(sell(order_type=OrderType.MARKET), None, snap(opos()), now).allow


# ---- contract checks ---------------------------------------------------------------------

def test_expired_and_far_dated_contracts(eng):
    e = eng()
    assert "expired" in chk(e, contract=k(expiry=date(2026, 10, 12))).reason
    assert "days out" in chk(e, contract=k(expiry=date(2026, 10, 21))).reason
    assert chk(e, contract=k(expiry=date(2026, 10, 16))).allow


def test_non_standard_contract_blocked(eng):
    e = eng()
    assert "non-standard" in chk(e, contract=k(mult=50)).reason
    adj = "SPY1261013C00580000"
    assert "non-standard" in chk(e, buy(occ=adj), contract=k(occ=adj, underlying="SPY")).reason


def test_missing_quote_blocks_entry(eng):
    assert "no quote" in chk(eng(), contract=None).reason


# ---- liquidity ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw, word", [
    (dict(bid=None), "two-sided"), (dict(ask=None), "two-sided"), (dict(bid=0.0, ask=0.05), "two-sided"),
    (dict(bid=2.2, ask=2.1), "two-sided"), (dict(bid=float("nan")), "two-sided"),
    (dict(bid=1.80, ask=2.10), "spread"), (dict(bid=0.10, ask=0.11), "mid"),
    (dict(oi=None), "open interest"), (dict(oi=99), "open interest"),
    (dict(vol=None), "volume"), (dict(vol=49), "volume"),
    (dict(age_s=None), "timestamp"), (dict(age_s=61), "old"), (dict(age_s=-30), "old"),
])
def test_liquidity_filters(eng, kw, word):
    d = chk(eng(), buy(limit_price=1.0) if "mid" == word else buy(), contract=k(**kw))
    assert not d.allow and word in d.reason, d.reason


def test_price_band_vs_mid(eng):
    e = eng()
    assert "deviates" in chk(e, buy(limit_price=2.40)).reason
    assert "deviates" in chk(e, buy(limit_price=1.80)).reason
    assert chk(e, buy(limit_price=2.25)).allow  # within 10% of mid 2.05


# ---- Muse ---------------------------------------------------------------------------------

def test_muse_blocked_underlying(eng):
    d = chk(eng(), blocked={"spy": "Muse ALERT elevated: probe"})
    assert not d.allow and "Muse" in d.reason
    assert chk(eng(), blocked={"QQQ": "x"}).allow


def test_muse_flags_read_from_bridge_when_not_passed(eng, monkeypatch):
    from stockmoney.desk.bridge import policy
    monkeypatch.setattr(policy, "active_flags",
                        lambda con, now, held=(): policy.BridgeFlags(blocked={"SPY": "Muse ALERT critical: x"}))
    d = eng().check(buy(), k(), snap(), NOW)
    assert not d.allow and "Muse" in d.reason


def test_muse_failure_loses_the_veto_only(eng, monkeypatch, con):
    from stockmoney.desk.bridge import policy

    def boom(*a, **kw):
        raise RuntimeError("db gone")
    monkeypatch.setattr(policy, "active_flags", boom)
    assert muse_blocked(con, NOW) == {}
    assert eng().check(buy(), k(), snap(), NOW).allow


def test_muse_real_tables_without_messages(con):
    assert muse_blocked(con, NOW) == {}


# ---- reconcile with the broker ---------------------------------------------------------------

def test_book_broker_mismatch_blocks_entries_but_not_exits(eng):
    e = eng()
    d = chk(e, buy(occ=OCC2), contract=k(occ=OCC2), s=snap(opos()))  # broker holds OCC, book does not
    assert not d.allow and "does not match" in d.reason
    assert chk(e, sell(), s=snap(opos())).allow


def test_options_mismatches(con):
    b = OptionsBook(con)
    b.apply_fill(client_order_id="b", occ=OCC, strategy="x", side=Side.BUY, qty=2, price=1.0)
    assert options_mismatches(b, snap(opos(qty=2))) == {}
    assert OCC in options_mismatches(b, snap(opos(qty=3)))
    assert OCC in options_mismatches(b, snap())
    eq = BrokerPosition("AAPL", AssetClass.EQUITY, 10, 100, 1000, 100)
    assert options_mismatches(b, snap(opos(qty=2), eq)) == {}  # stock positions are not the sleeve's business


# ---- P&L stops ------------------------------------------------------------------------------

def test_daily_loss_stop_realized(eng):
    e = eng()
    book_open(e, qty=5, price=4.0, cid="x1")
    e.book.apply_fill(client_order_id="x2", occ=OCC, strategy="orb15", side=Side.SELL, qty=5, price=2.0,
                      filled_at=NOW)  # -1000
    d = chk(e)
    assert not d.allow and "daily loss" in d.reason


def test_daily_loss_stop_counts_unrealized(eng):
    e = eng()
    book_open(e, occ=OCC2, qty=4, price=5.0)
    # marked at 2.40: unrealized (2.40 - 5.0) * 4 * 100 = -1040
    d = chk(e, s=snap(opos(occ=OCC2, qty=4, avg=5.0, price=2.40)))
    assert not d.allow and "daily loss" in d.reason


def test_yesterdays_loss_does_not_count_today(eng):
    e = eng()
    y = NOW - timedelta(days=1)
    book_open(e, qty=5, price=4.0, cid="y1", at=y)
    e.book.apply_fill(client_order_id="y2", occ=OCC, strategy="orb15", side=Side.SELL, qty=5, price=2.0,
                      filled_at=y)
    assert chk(e).allow


def test_drawdown_halt_latches_until_rearmed(eng):
    e = eng(daily_loss_pct=1.0)  # isolate the drawdown rule
    for i in range(2):  # two days of -900: drawdown 1800 >= 1750
        at = NOW - timedelta(days=2 - i)
        book_open(e, qty=3, price=5.0, cid=f"d{i}b", at=at)
        e.book.apply_fill(client_order_id=f"d{i}s", occ=OCC, strategy="orb15", side=Side.SELL, qty=3, price=2.0,
                          filled_at=at)
    d = chk(e)
    assert not d.allow and "drawdown halt latched" in d.reason
    assert e.halted()
    e2 = eng(daily_loss_pct=1.0)
    assert "re-armed" in chk(e2).reason  # latched in the table, survives a new engine
    st = sleeve_state(e2.con, e2.limits, {}, NOW)
    e2.rearm(st)
    assert e2.halted() is None and chk(e2).allow


def test_peak_rises_with_profits(eng):
    e = eng(daily_loss_pct=1.0)
    book_open(e, qty=4, price=1.0, cid="p1", at=NOW - timedelta(days=3))
    e.book.apply_fill(client_order_id="p2", occ=OCC, strategy="orb15", side=Side.SELL, qty=4, price=6.0,
                      filled_at=NOW - timedelta(days=3))  # +2000
    assert chk(e).allow
    assert e.peak() == pytest.approx(7_000)


def test_unmarked_position_blocks_entries(eng):
    e = eng()
    book_open(e, occ=OCC2)
    bad = BrokerPosition(OCC2, AssetClass.OPTION, 2, 2.0, 400.0, float("nan"))
    d = chk(e, s=snap(bad))
    assert not d.allow and "mark" in d.reason
    assert chk(e, s=snap(bad), marks={OCC2: 2.0}).allow  # an explicit mark fixes it


# ---- positions and premium ------------------------------------------------------------------

def test_per_trade_premium_resizes_in_whole_contracts(eng):
    d = chk(eng(), buy(qty=10))
    assert d.allow and d.intent.qty == 4  # floor(1000 / 210)
    assert "per-trade premium" in d.reason and "4" in d.intent.reason


def test_one_contract_over_the_cap_is_blocked(eng):
    occ = "SPY261013C00500000"
    d = chk(eng(), buy(occ=occ, limit_price=10.5), contract=k(occ=occ, bid=10.0, ask=10.5))
    assert not d.allow and "per-trade premium" in d.reason


def test_budget_counts_open_premium_working_buys_and_reservations(eng):
    e = eng(max_positions=5)
    book_open(e, occ=OCC2, qty=4, price=10.0)  # 4000 at risk
    s = snap(opos(occ=OCC2, qty=4, avg=10.0, price=10.0))
    d = chk(e, buy(qty=4), s=s)
    assert d.allow and d.intent.qty == 4  # room 1000 -> 4 contracts at 210
    working = BrokerOrder("w", "w1", OCC3, Side.BUY, OrderStatus.NEW, 1, 0, None, NOW, None, OrderType.LIMIT,
                          TIF.DAY, None, 5.0, AssetClass.OPTION)  # 500 more
    d = chk(e, buy(qty=4), s=s, open_orders=[working])
    assert d.allow and d.intent.qty == 2
    e.reserve(buy(qty=2, occ=OCC4, cid="r"), 2.5)  # another 500: the budget is full
    d = chk(e, buy(qty=4), s=s, open_orders=[working])
    assert not d.allow and "sleeve budget" in d.reason


def test_unknown_broker_option_counts_toward_budget(eng):
    e = eng()
    book_open(e, occ=OCC2, qty=1, price=1.0)
    s = snap(opos(occ=OCC2, qty=1, avg=1.0, price=1.0))
    assert chk(e, s=s).allow


def test_max_concurrent_positions(eng):
    e = eng()
    occs = [OCC2, OCC3, OCC4]
    for i, o in enumerate(occs):
        book_open(e, occ=o, qty=1, price=1.0, cid=f"m{i}")
    s = snap(*[opos(occ=o, qty=1, avg=1.0, price=1.0) for o in occs])
    d = chk(e, s=s)
    assert not d.allow and "max 3" in d.reason
    d = chk(e, s=snap(*[opos(occ=o, qty=1, avg=1.0, price=1.0) for o in occs[:2]]))
    assert "does not match" in d.reason  # the book still holds 3


def test_one_entry_per_contract(eng):
    e = eng()
    book_open(e, qty=1, price=2.0)
    d = chk(e, s=snap(opos(qty=1)))
    assert not d.allow and "already held" in d.reason


def test_one_entry_per_contract_reserved(eng):
    e2 = eng()
    e2.reserve(buy(cid="first"), 2.10)
    assert "already held" in chk(e2, buy(cid="second")).reason
    e2.release(OCC)
    assert chk(e2, buy(cid="second")).allow


# ---- exits ----------------------------------------------------------------------------------------

def test_exit_needs_held_contracts(eng):
    e = eng()
    d = chk(e, sell())
    assert not d.allow and "no sell to open" in d.reason
    d = chk(e, sell(qty=3), s=snap(opos(qty=2)))
    assert not d.allow and "exceeds held" in d.reason
    assert chk(e, sell(qty=2), s=snap(opos(qty=2))).allow


def test_exit_counts_working_sells(eng):
    w = BrokerOrder("w", "w1", OCC, Side.SELL, OrderStatus.NEW, 1, 0, None, NOW, None, OrderType.LIMIT,
                    TIF.DAY, None, 3.0, AssetClass.OPTION)
    d = chk(eng(), sell(qty=2), s=snap(opos(qty=2)), open_orders=[w])
    assert not d.allow and "working sells" in d.reason
    assert chk(eng(), sell(qty=1), s=snap(opos(qty=2)), open_orders=[w]).allow


def test_exit_ignores_halts_and_stops(eng, tmp_path, con):
    e = eng()
    (tmp_path / "HALT").write_text("x")
    store.set_setting(con, "options_halt_new_entries", "1")
    e.book.set_state(HALT_KEY, "latched")
    assert chk(e, sell(order_type=OrderType.MARKET), contract=None, s=snap(opos())).allow


def test_limit_exit_fat_finger_and_no_quote(eng):
    e = eng()
    s = snap(opos())
    assert "fat finger" in chk(e, sell(order_type=OrderType.LIMIT, limit_price=0.50), s=s).reason
    assert chk(e, sell(order_type=OrderType.LIMIT, limit_price=1.10), s=s).allow
    d = chk(e, sell(order_type=OrderType.LIMIT, limit_price=1.10), contract=None, s=s)
    assert not d.allow and "market sell" in d.reason


def test_exit_without_snapshot_uses_book(eng):
    e = eng()
    book_open(e, qty=2)
    assert e.check(sell(order_type=OrderType.MARKET), None, None, NOW).allow
    assert not e.check(sell(qty=3, order_type=OrderType.MARKET), None, None, NOW).allow


# ---- book -------------------------------------------------------------------------------------------

def test_book_round_trip_pnl_and_idempotency(con):
    b = OptionsBook(con)
    OptionsBook(con)  # schema creation is idempotent
    assert b.apply_fill(client_order_id="b1", occ=OCC, strategy="orb15", side=Side.BUY, qty=3, price=2.0,
                        fee=0.06, filled_at=NOW)
    assert not b.apply_fill(client_order_id="b1", occ=OCC, strategy="orb15", side=Side.BUY, qty=3, price=2.0)
    assert b.position(OCC).qty == 3 and b.position(OCC).cost == pytest.approx(600)
    b.apply_fill(client_order_id="s1", occ=OCC, strategy="orb15", side=Side.SELL, qty=1, price=3.0, fee=0.02,
                 filled_at=NOW)
    b.apply_fill(client_order_id="s2", occ=OCC, strategy="orb15", side=Side.SELL, qty=2, price=1.5, filled_at=NOW)
    assert b.position(OCC) is None
    assert b.realized() == pytest.approx(-0.06 + (100 - 0.02) + (-100))


def test_book_partial_fills_cumulative(con):
    b = OptionsBook(con)
    assert b.apply_fill(client_order_id="b1", occ=OCC, strategy="x", side=Side.BUY, qty=1, price=2.0,
                        cumulative=True)
    assert b.apply_fill(client_order_id="b1", occ=OCC, strategy="x", side=Side.BUY, qty=3, price=2.2,
                        cumulative=True)
    assert not b.apply_fill(client_order_id="b1", occ=OCC, strategy="x", side=Side.BUY, qty=3, price=2.2,
                            cumulative=True)
    p = b.position(OCC)
    assert p.qty == 3 and p.avg_price == pytest.approx(2.2)


@pytest.mark.parametrize("kw", [
    dict(qty=1.5), dict(qty=0), dict(qty=float("nan")), dict(price=0), dict(price=float("inf")),
    dict(fee=-1), dict(fee=float("nan")), dict(occ="SPY"), dict(client_order_id=""),
])
def test_book_rejects_bad_fills(con, kw):
    base = dict(client_order_id="b", occ=OCC, strategy="x", side=Side.BUY, qty=1, price=1.0)
    base.update(kw)
    with pytest.raises(OptionsBookError):
        OptionsBook(con).apply_fill(**base)
    assert con.execute("SELECT COUNT(*) FROM options_fills").fetchone()[0] == 0


def test_book_rules(con):
    b = OptionsBook(con)
    with pytest.raises(OptionsBookError):
        b.apply_fill(client_order_id="s", occ=OCC, strategy="x", side=Side.SELL, qty=1, price=1.0)
    b.apply_fill(client_order_id="b", occ=OCC, strategy="x", side=Side.BUY, qty=1, price=1.0)
    with pytest.raises(OptionsBookError):
        b.apply_fill(client_order_id="b2", occ=OCC, strategy="other", side=Side.BUY, qty=1, price=1.0)
    with pytest.raises(OptionsBookError):
        b.apply_fill(client_order_id="s2", occ=OCC, strategy="x", side=Side.SELL, qty=2, price=1.0)


def test_book_never_touches_stock_ledger(con):
    led = Ledger(con)
    b = OptionsBook(con)
    b.apply_fill(client_order_id="b", occ=OCC, strategy="x", side=Side.BUY, qty=5, price=3.0)
    b.apply_fill(client_order_id="s", occ=OCC, strategy="x", side=Side.SELL, qty=5, price=1.0)
    assert led.positions() == {} and led.realized_pnl() == 0.0
    assert led.system_equity(50_000, {}) == 50_000


def test_sleeve_state(con):
    l = OptionsLimits()
    b = OptionsBook(con)
    b.apply_fill(client_order_id="b", occ=OCC, strategy="x", side=Side.BUY, qty=2, price=2.0, filled_at=NOW)
    b.apply_fill(client_order_id="b2", occ=OCC2, strategy="x", side=Side.BUY, qty=1, price=1.0, filled_at=NOW)
    b.apply_fill(client_order_id="s2", occ=OCC2, strategy="x", side=Side.SELL, qty=1, price=1.5, filled_at=NOW)
    st = sleeve_state(con, l, {OCC: 2.5}, NOW)
    assert st.realized_today == pytest.approx(50) and st.unrealized == pytest.approx(100)
    assert st.daily_pnl == pytest.approx(150) and st.equity == pytest.approx(5_150)
    assert st.open_premium == pytest.approx(400) and st.n_open == 1 and st.unmarked == ()
    st = sleeve_state(con, l, {}, NOW)
    assert st.unmarked == (OCC,) and st.unrealized == 0.0


def test_et_day_bounds():
    s, e = et_day_bounds(date(2026, 10, 13))
    assert s == "2026-10-13T04:00:00+00:00" and e == "2026-10-14T04:00:00+00:00"


# ---- end to end with FakeBroker -------------------------------------------------------------------

def test_engine_with_fake_broker_round_trip(eng):
    e = eng()
    b = FakeBroker(cash=100_000)
    b.set_option(k())
    intent = buy(qty=10)
    d = e.check(intent, b.option_quote(OCC), b.account(), NOW, open_orders=b.open_orders(), blocked={})
    assert d.allow and d.intent.qty == 4
    o = b.submit(d.intent)
    e.reserve(d.intent, 2.10)
    e.book.apply_fill(client_order_id=o.client_order_id, occ=OCC, strategy="orb15", side=Side.BUY,
                      qty=o.filled_qty, price=o.filled_avg_price, filled_at=NOW)
    e.release(OCC)
    assert options_mismatches(e.book, b.account()) == {}
    later = NOW + timedelta(hours=6)  # after flat_by: exits still go out
    x = sell(qty=4, order_type=OrderType.MARKET)
    d = e.check(x, b.option_quote(OCC), b.account(), later, open_orders=b.open_orders())
    assert d.allow
    o = b.submit(d.intent)
    e.book.apply_fill(client_order_id=o.client_order_id, occ=OCC, strategy="orb15", side=Side.SELL,
                      qty=o.filled_qty, price=o.filled_avg_price, filled_at=later)
    assert e.book.realized() == pytest.approx((2.00 - 2.10) * 4 * 100)  # the spread is the cost
    assert b.account().positions == []
