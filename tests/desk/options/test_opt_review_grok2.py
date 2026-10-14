"""Grok Build round-2 review of the options engine: G1 (an exit that cannot
be canceled), G2 (exit qty above the broker position), G3 (stale marks and
the daily loss stop), and its #4 partial-entry P&L scenario. Fake brokers
only; no real order is ever sent."""
from dataclasses import replace
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import pytest

from stockmoney.desk import store
from stockmoney.desk.options import _iface
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options._iface import (AccountSnapshot, AssetClass, BrokerPosition, OptionContract,
                                            OrderIntent, OrderStatus, OrderType, Side, TIF, occ_symbol)

from opt_fakes import DAY, NEXT, et
from test_opt_engine import Rig

C601N = occ_symbol("SPY", NEXT, "C", 601)
C600N = occ_symbol("SPY", NEXT, "C", 600)


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        yield c


def exits(con, pid):
    return [o for o in ostore.orders_for(con, pid) if o["kind"] == "exit"]


def sells(r):
    return [o for o in r.broker.orders.values() if o.side == Side.SELL]


def titles(r):
    return [a[0] for a in r.sent]


def entered(tmp_path, con, early_close=False, **kw):
    r = Rig(tmp_path, con, time_stop_minutes=600, **kw)
    if early_close:  # the broker calendar says DAY closes at 13:00 (the 2026-11-27 case): flat 12:45
        class Cal:
            def get_calendar(self, req):
                return [SimpleNamespace(date=DAY, close=datetime(2026, 10, 13, 13, 0))]
        r.broker.client = Cal()
        r.eng = r.new_engine()
    r.run(et(9, 52))
    lp = r.pos("live")[0]
    assert lp["state"] == "ENTERED" and r.broker.positions[C601N] == 10
    return r, lp


def flat_of(early):
    return (12, 45) if early else (15, 45)


def to_flat_with_unfillable_limit(r, early):
    """Run to the forced flat; the limit exit is sent but cannot fill (the
    quote drops below the limit right after it is placed)."""
    hh, mm = flat_of(early)
    r.run(et(hh, mm - 1), step=60)
    r.clock.set(et(hh, mm))
    real = r.broker.submit

    def submit(intent):
        if intent.order_type != OrderType.LIMIT:
            return real(intent)
        r.broker.fill = "never"
        o = real(intent)
        r.broker.fill = "immediate"
        r.broker.mult[C601N] = 0.5  # the bid falls under the limit: it stays NEW
        return o
    r.broker.submit = submit
    r.eng.tick()
    lim = [o for o in sells(r) if o.order_type == OrderType.LIMIT]
    assert lim and lim[0].status == OrderStatus.NEW
    return lim[0]


# ------------------------------------------------------------------ G1 exit that cannot be canceled
@pytest.mark.parametrize("early", [False, True])
def test_g1_cancel_fails_and_order_gone_from_open_orders_is_not_replaced_blind(tmp_path, con, early):
    # (revised by the Grok round-3 review, H1): "absent from open_orders" is not proof the order
    # is done; while get_order still shows it working, nothing more is sold and the owner is paged
    r, lp = entered(tmp_path, con, early_close=early)
    lim = to_flat_with_unfillable_limit(r, early)

    def boom(cid):
        raise ConnectionError("422 order is not cancelable")
    r.broker.cancel = boom
    real_open = r.broker.open_orders
    r.broker.open_orders = lambda: [o for o in real_open() if o.client_order_id != lim.client_order_id]
    hh, mm = flat_of(early)
    r.run(et(hh, mm + 8), step=2)
    assert [o.order_type for o in sells(r)] == [OrderType.LIMIT]
    assert sum("出場單撤不掉" in t for t in titles(r)) >= 2
    lim.status = OrderStatus.CANCELED  # the broker finally confirms it: replaced in the same pass
    r.run(et(hh, mm + 9), step=2)
    assert [o.order_type for o in sells(r)] == [OrderType.LIMIT, OrderType.MARKET] and C601N not in r.broker.positions


def test_h1_filled_exit_with_failing_cancel_and_lagging_account_never_sells_twice(tmp_path, con):
    r, lp = entered(tmp_path, con)
    lim = to_flat_with_unfillable_limit(r, False)
    # the exit fills at the broker; cancel's own get_order times out; open_orders no longer lists
    # it; the account lags and still shows 10 contracts
    lim.status, lim.filled_qty, lim.filled_avg_price, lim.filled_at = OrderStatus.FILLED, 10, 0.45, r.clock.now

    def down(cid):
        raise TimeoutError("read timed out")
    real_get = r.broker.get_order
    r.broker.get_order = down
    r.broker.cancel = down
    r.run(et(15, 52), step=2)
    assert [o.order_type for o in sells(r)] == [OrderType.LIMIT]  # no second 10-lot sell
    assert any("出場單撤不掉" in t for t in titles(r))
    r.broker.get_order = real_get
    del r.broker.positions[C601N]  # the account catches up
    r.run(et(15, 53), step=2)
    p = ostore.position(con, lp["position_id"])
    assert p["state"] == "EXITED" and p["qty_open"] == 0 and len(sells(r)) == 1


def test_h1_booking_more_than_held_is_clamped_and_paged(tmp_path, con):
    r, lp = entered(tmp_path, con)
    p = ostore.position(con, lp["position_id"])
    r.eng._begin(r.clock.now)
    r.eng._book_exit(p, 12, 0.5, 0.49, 0.51, "late_fill", r.clock.now, client_order_id="late#12")
    p = ostore.position(con, lp["position_id"])
    assert p["qty_open"] == 0 and p["state"] == "EXITED"
    assert any("賣出口數超過持有" in t for t in titles(r))


@pytest.mark.parametrize("early", [False, True])
def test_g1_cancel_fails_and_order_still_working_never_double_sells_pages_repeatedly(tmp_path, con, early):
    r, lp = entered(tmp_path, con, early_close=early)
    lim = to_flat_with_unfillable_limit(r, early)
    tried = []

    def boom(cid):
        tried.append(r.clock.now)
        raise ConnectionError("broker unavailable")
    r.broker.cancel = boom
    hh, mm = flat_of(early)
    r.run(et(hh, mm + 14), step=2)
    assert [o.order_type for o in sells(r)] == [OrderType.LIMIT]  # no second sell while the first works
    pages = [a for a in r.sent if "出場單撤不掉" in a[0]]
    assert len(pages) >= 2  # escalation: re-paged every 5 minutes, not once
    assert len(tried) < 60  # cancel retried with backoff, not every 2-second tick
    r.eng.flatten_now("session_end")
    assert [o.order_type for o in sells(r)] == [OrderType.LIMIT]


@pytest.mark.parametrize("early", [False, True])
def test_g1_terminal_on_the_last_tick_replaced_in_the_same_tick(tmp_path, con, early):
    r, lp = entered(tmp_path, con, early_close=early)
    lim = to_flat_with_unfillable_limit(r, early)
    end = (13, 0) if early else (16, 0)
    r.clock.set(et(*end) - timedelta(seconds=2))
    lim.status = OrderStatus.CANCELED  # the broker finally canceled it
    r.eng.tick()  # the session's last tick
    mk = [o for o in sells(r) if o.order_type == OrderType.MARKET]
    assert len(mk) == 1 and C601N not in r.broker.positions
    if early:
        assert r.eng.session_end() == et(13, 0) and r.eng._flat() == et(12, 45)


# ------------------------------------------------------------------ G2 exit qty vs broker position
def test_g2_broker_holds_fewer_sell_what_is_held_and_write_off_the_rest(tmp_path, con):
    # (revised by the Grok round-3 review, H2): the missing contracts are a loss, not erased at cost
    from stockmoney.desk import store as dstore
    r = Rig(tmp_path, con, time_stop_minutes=600)
    r.gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.eng = r.new_engine()
    r.run(et(9, 52))
    lp = r.pos("live")[0]
    e = lp["entry_price"]
    r.broker.positions[C601N] = 4  # the owner sold 6 by hand (at 0.20, which the sleeve cannot see)
    r.clock.set(et(15, 45))
    r.run(et(15, 46), step=2)
    assert [o.qty for o in sells(r)] == [4] and C601N not in r.broker.positions
    p = ostore.position(con, lp["position_id"])
    assert p["state"] == "EXITED" and p["qty_open"] == 0 and "written off at 0" in p["note"]
    wo = [f for f in ostore.fills(con, lp["position_id"]) if f["reason"] == "broker_shortfall"]
    assert [(f["qty"], f["price"]) for f in wo] == [(6, 0.0)]
    assert any("券商口數少於資料庫" in t for t in titles(r))
    assert dstore.get_setting(con, "options_halt_new_entries", "0") == "1"  # new buys latched off
    assert not _iface.OptionsBook(con).positions()
    assert r.gate.engine.book.realized() == pytest.approx(p["realized_usd"])  # the loss reaches the stop
    sold4 = next(o for o in sells(r)).filled_avg_price
    assert p["realized_usd"] == pytest.approx((sold4 - e) * 4 * 100 + (0.0 - e) * 6 * 100)


def test_g2_broker_holds_more_never_sells_more_than_open(tmp_path, con):
    r, lp = entered(tmp_path, con)
    r.broker.positions[C601N] = 15  # the owner holds 5 of the same contract
    r.clock.set(et(15, 45))
    r.run(et(15, 46), step=2)
    assert sum(o.filled_qty for o in sells(r)) == 10 and r.broker.positions[C601N] == 5
    assert ostore.position(con, lp["position_id"])["state"] == "EXITED"


def test_g2_broker_holds_zero_today_sends_nothing_and_writes_off(tmp_path, con):
    r, lp = entered(tmp_path, con)
    del r.broker.positions[C601N]
    r.clock.set(et(15, 45))
    r.run(et(15, 46), step=2)
    p = ostore.position(con, lp["position_id"])
    assert not sells(r) and p["state"] == "EXITED" and p["exit_reason"] == "broker_shortfall"
    assert p["realized_usd"] == pytest.approx(-10 * p["entry_price"] * 100)
    assert any("券商口數少於資料庫" in t for t in titles(r))


def test_h2_daily_stop_sees_a_hand_sale_and_reappearing_contracts_are_never_sold(tmp_path, con):
    r = Rig(tmp_path, con, time_stop_minutes=600, capital_budget=10_000.0)  # daily stop 200
    r.gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.eng = r.new_engine()
    r.run(et(9, 52))
    lp = r.pos("live")[0]
    r.broker.positions[C601N] = 4  # the owner sold 6 by hand
    r.clock.set(et(10, 0))
    r.eng.tick()
    p = ostore.position(con, lp["position_id"])
    assert p["qty_open"] == 4 and p["state"] == "ENTERED"
    st = _iface.sleeve_state(con, r.gate.engine.limits, {C601N: 0.95}, r.clock.now)
    assert st.realized_today == pytest.approx(-6 * lp["entry_price"] * 100) and st.daily_pnl <= -200
    k = OptionContract(C600N, "SPY", "C", 600.0, NEXT, 1.03, 1.07, None, 1000, 5000, 0.55, None, r.clock.now)
    buy = OrderIntent("t", C600N, AssetClass.OPTION, Side.BUY, qty=1, order_type=OrderType.LIMIT, tif=TIF.DAY,
                      limit_price=1.07)
    assert not r.gate.check(buy, k, r.clock.now, marks={C601N: 0.95}, blocked={}).allow
    # the 4 are sold at the flat; then 6 of the contract reappear at the broker
    r.clock.set(et(15, 45))
    r.run(et(15, 46), step=2)
    assert C601N not in r.broker.positions
    r.broker.positions[C601N] = 6
    r.eng = r.new_engine()  # e.g. a restart: the start-of-day check runs again
    r.run(et(15, 48), step=2)
    assert any("未知期權部位" in t for t in titles(r))
    assert sum(o.filled_qty for o in sells(r)) == 4 and r.broker.positions[C601N] == 6


def test_g2_fresh_fill_is_not_mistaken_for_a_shortfall(tmp_path, con):
    # a broker position read that lags the fill must not reconcile the store
    r = Rig(tmp_path, con, time_stop_minutes=600)
    r.run(et(9, 51, 1))
    lp = r.pos("live")[0]
    assert lp["state"] == "ENTERED"
    r.broker.positions[C601N] = 4
    r.broker.mult[C601N] = 0.6  # premium stop right away, within 60 s of the entry fill
    r.clock.set(et(9, 51, 20))
    r.eng.tick()
    p = ostore.position(con, lp["position_id"])
    assert "gone" not in (p["note"] or "") and not any("口數與資料庫不符" in t for t in titles(r))


# ------------------------------------------------------------------ G3 stale marks
def test_g3_stale_marks_never_hide_the_daily_loss(tmp_path, con):
    # sleeve budget 1,000 -> daily stop 200; 10 contracts bought near 0.97
    r = Rig(tmp_path, con, capital_budget=10_000.0)
    r.gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.eng = r.new_engine()
    r.run(et(9, 52))
    lp = r.pos("live")[0]
    assert lp["state"] == "ENTERED" and lp["last_bid"] > 0.9

    def down(occ):
        raise ConnectionError("quote feed down")
    r.broker.option_quote = down
    snap = AccountSnapshot(0, 0, 0, [BrokerPosition(C601N, AssetClass.OPTION, 10, 0, 200.0, 0.20)], r.clock.now)
    r.broker.account = lambda: snap
    r.clock.set(et(10, 0))
    r.eng.tick()
    marks = r.eng._live_marks()
    assert C601N not in marks  # the stored last bid / ask (about 0.95) is not passed on
    k = OptionContract(C600N, "SPY", "C", 600.0, NEXT, 1.03, 1.07, None, 1000, 5000, 0.55, None, r.clock.now)
    buy = OrderIntent("t", C600N, AssetClass.OPTION, Side.BUY, qty=1, order_type=OrderType.LIMIT, tif=TIF.DAY,
                      limit_price=1.07)
    dec = r.gate.check(buy, k, r.clock.now, marks=marks, blocked={})
    assert not dec.allow and "daily loss" in dec.reason
    # what the old marks would have shown: the loss hidden, no daily stop
    stale = {C601N: (lp["last_bid"] + lp["last_ask"]) / 2}
    assert "daily loss" not in (r.gate.check(buy, k, r.clock.now, marks=stale, blocked={}).reason or "")


# ------------------------------------------------------------------ Grok #4: partial entry re-average
def test_grok4_partial_entry_sold_between_fills_realized_equals_book(tmp_path, con):
    r = Rig(tmp_path, con, time_stop_minutes=30, max_premium_per_trade=200.0)
    r.gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.broker.fill = "never"
    r.eng = r.new_engine()
    r.run(et(9, 51, 5))
    pid = r.pos("live")[0]["position_id"]
    eo = r.broker.orders[ostore.cid(pid, "e")]
    assert eo.qty == 2
    eo.status, eo.filled_qty, eo.filled_avg_price, eo.filled_at = OrderStatus.PARTIALLY_FILLED, 1, 1.00, r.clock.now
    r.broker.positions[C601N] = 1

    def nocancel(cid):
        raise ConnectionError("cancel failed")
    r.broker.cancel = nocancel
    r.broker.fill = "immediate"
    r.broker.mult[C601N] = 0.52 / 0.95  # bid 0.50
    r.eng = r.new_engine()
    r.eng.tick()
    r.clock.set(et(10, 22))
    r.run(et(10, 23), step=2)  # time stop sells the first contract @ 0.50
    assert [(o.filled_qty, o.filled_avg_price) for o in sells(r)] == [(1, 0.5)]
    eo.filled_qty, eo.filled_avg_price = 2, 1.50  # the second contract @ 2.00
    r.broker.positions[C601N] = 1
    r.run(et(10, 24), step=2)
    eo.status = OrderStatus.CANCELED
    r.run(et(10, 26), step=2)
    p = ostore.position(con, pid)
    assert p["state"] == "EXITED" and C601N not in r.broker.positions
    assert p["realized_usd"] == pytest.approx(-200.0)
    assert r.gate.engine.book.realized() == pytest.approx(-200.0)


# ------------------------------------------------------------------ Grok round 3, H3: no broker mark
def test_h3_option_without_broker_mark_is_unmarked_and_blocks_buys(tmp_path, con):
    r = Rig(tmp_path, con)
    r.gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.eng = r.new_engine()
    r.run(et(9, 52))
    assert r.pos("live")[0]["state"] == "ENTERED"
    snap = AccountSnapshot(0, 0, 0, [BrokerPosition(C601N, AssetClass.OPTION, 10, 0.97, None, None)], r.clock.now)
    assert C601N not in r.gate.engine._marks(snap, None, {})
    k = OptionContract(C600N, "SPY", "C", 600.0, NEXT, 1.03, 1.07, None, 1000, 5000, 0.55, None, r.clock.now)
    buy = OrderIntent("t", C600N, AssetClass.OPTION, Side.BUY, qty=1, order_type=OrderType.LIMIT, tif=TIF.DAY,
                      limit_price=1.07)
    dec = r.gate.engine.check(buy, k, snap, r.clock.now, open_orders=[], marks={}, blocked={})
    assert not dec.allow and "without a usable mark" in dec.reason


# ------------------------------------------------------------------ Grok round 3, H4: last-tick replacement
def test_h4_market_reprice_on_the_last_tick_is_replaced_in_the_same_tick(tmp_path, con):
    r, lp = entered(tmp_path, con)
    r.eng._sleep = lambda s: None
    r.broker.fill = "never"
    r.clock.set(et(15, 59, 38))
    r.eng.tick()  # the forced exit (a limit at the bid) ...
    r.clock.set(et(15, 59, 40))
    r.eng.tick()  # ... is canceled at once at the flat and replaced by a market sell
    mk = [o for o in sells(r) if o.order_type == OrderType.MARKET]
    assert len(mk) == 1 and mk[0].status == OrderStatus.NEW
    r.clock.set(et(15, 59, 58))  # the session's last tick: the market sell is 18 s old ...
    r.eng.tick()
    r.clock.set(et(16, 0, 0))    # ... a final pass past the reprice timeout
    r.eng.tick()
    mk = [o for o in sells(r) if o.order_type == OrderType.MARKET]
    assert len(mk) == 2 and mk[0].status == OrderStatus.CANCELED and mk[1].status == OrderStatus.NEW
    assert ostore.order(con, mk[1].client_order_id)["submitted_at"] == ostore.iso(r.clock.now)
    assert sum(o.status == OrderStatus.NEW for o in sells(r)) == 1  # never two sells working


def test_h4_not_sent_exit_is_replaced_in_the_same_call(tmp_path, con):
    r, lp = entered(tmp_path, con)
    real = r.broker.submit

    def lost(intent):
        r.broker.submit_calls.append(intent.client_order_id)
        raise ConnectionError("connection reset before the broker got it")
    r.broker.submit = lost
    r.clock.set(et(15, 45))
    r.eng.tick()
    ex = [o for o in ostore.orders_for(con, lp["position_id"]) if o["kind"] == "exit"]
    assert len(ex) == 1 and ex[0]["status"] == "pending_submit"
    r.broker.submit = real
    r.clock.set(et(15, 45, 32))  # > 30 s: the broker never got it
    r.eng.tick()
    ex = [o for o in ostore.orders_for(con, lp["position_id"]) if o["kind"] == "exit"]
    assert ex[0]["status"] == "not_sent" and len(ex) == 2 and ex[1]["order_type"] == "market"
    assert C601N not in r.broker.positions


def test_h4_flatten_now_cancels_rereads_and_replaces_in_one_call(tmp_path, con):
    r, lp = entered(tmp_path, con)
    r.eng._sleep = lambda s: None
    lim = to_flat_with_unfillable_limit(r, False)
    r.clock.advance(1)
    r.eng.flatten_now("session_end")  # one call: cancel the limit, confirm, market sell, see it filled
    assert lim.status == OrderStatus.CANCELED
    p = ostore.position(con, lp["position_id"])
    assert p["state"] == "EXITED" and C601N not in r.broker.positions


def test_h4_unconfirmed_cancel_sends_nothing_and_pages(tmp_path, con):
    r, lp = entered(tmp_path, con)
    waits = []
    r.eng._sleep = waits.append
    lim = to_flat_with_unfillable_limit(r, False)
    r.broker.cancel = lambda cid: None  # the cancel is accepted but the order never shows as canceled
    r.clock.advance(1)
    r.eng.flatten_now("session_end")
    assert [o.order_type for o in sells(r)] == [OrderType.LIMIT]
    assert any("出場單撤不掉" in t for t in titles(r))
    assert sum(waits) < 10 * 3  # bounded: under 10 s per round, 3 rounds at most
