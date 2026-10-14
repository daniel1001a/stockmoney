"""Regression tests for the 2026-10-12 adversarial review of the options
engine (findings H1, H2, M1, M2, L1-L3, N1-N4). Each test asserts the
CORRECT behavior of a scenario the reviewer reproduced as a bug. Fake
brokers and gates only: no real order is ever sent."""
import math
import sqlite3
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from stockmoney.desk import store
from stockmoney.desk.calendar import EARLY_CLOSES, close_time, is_trading_day
from stockmoney.desk.options import _iface
from stockmoney.desk.options import run as orun
from stockmoney.desk.options import select as sel
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options._iface import (AssetClass, BrokerPosition, OptionContract, OrderIntent, OrderStatus,
                                            OrderType, RiskDecision, Side, TIF, occ_symbol)
from stockmoney.desk.options.config import OptionsConfig
from stockmoney.desk.options.engine import Engine, market_close

from opt_fakes import DAY, NEXT, Clock, FakeOptionsBroker, FnGate, allow_gate, chain, et, orb_breakout_day, spy_cfg
from test_opt_engine import Rig

ET = ZoneInfo("America/New_York")
C601N = occ_symbol("SPY", NEXT, "C", 601)
DAY2 = date(2026, 10, 15)  # the trading day after NEXT


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        yield c


def exits(con, pid):
    return [o for o in ostore.orders_for(con, pid) if o["kind"] == "exit"]


def titles(r):
    return [a[0] for a in r.sent]


def entered_rig(tmp_path, con, **kw):
    r = Rig(tmp_path, con, **kw)
    r.run(et(9, 52))
    lp = r.pos("live")[0]
    assert lp["state"] == "ENTERED" and r.broker.positions[C601N] == 10
    return r, lp


# ------------------------------------------------------------------ H2 leftovers
def test_h2_overnight_live_position_sold_at_market_from_the_open(tmp_path, con):
    r, lp = entered_rig(tmp_path, con)
    # the session dies before 15:45; next trading day, a new engine:
    r.clock.set(et(9, 20, day=NEXT))
    r.eng = r.new_engine()
    r.run(et(9, 29, 40, day=NEXT))
    assert not exits(con, lp["position_id"])           # never before the open
    assert any("前一日部位未平" in t for t in titles(r))  # paged at the first tick
    r.run(et(9, 31, day=NEXT), step=2)
    ex = exits(con, lp["position_id"])
    assert ex and ex[0]["order_type"] == "market" and ex[0]["reason"] == "carry_over"
    assert ex[0]["submitted_at"] >= ostore.iso(et(9, 30, day=NEXT))
    p = ostore.position(con, lp["position_id"])
    assert p["state"] == "EXITED" and p["exit_reason"] == "carry_over"
    assert C601N not in r.broker.positions
    # the old day's shadow positions were closed too, and its setup rolled
    assert not ostore.positions(con, str(DAY), "shadow", ("ENTERED",))
    assert all(s["state"] != "ENTERED" for s in ostore.setups(con, str(DAY)))


def test_h2_leftover_pending_entry_resolved_and_partial_fill_sold(tmp_path, con):
    r = Rig(tmp_path, con)
    r.broker.fill = "never"
    r.run(et(9, 51, 5))
    lp = r.pos("live")[0]
    o = r.broker.orders[ostore.cid(lp["position_id"], "e")]
    # the session died with a partly filled entry; Alpaca expires the DAY order overnight
    o.status, o.filled_qty, o.filled_avg_price, o.filled_at = OrderStatus.EXPIRED, 3, 0.97, r.clock.now
    r.broker.positions[C601N] = 3
    r.broker.fill = "immediate"
    r.clock.set(et(9, 25, day=NEXT))
    r.eng = r.new_engine()
    r.run(et(9, 31, day=NEXT), step=2)
    p = ostore.position(con, lp["position_id"])
    assert p["state"] == "EXITED" and p["qty_initial"] == 3 and p["exit_reason"] == "carry_over"
    assert C601N not in r.broker.positions


def test_h2_unknown_broker_option_position_paged_never_sold(tmp_path, con):
    r = Rig(tmp_path, con)
    hand = "AAPL261120C00200000"
    r.broker.extra_positions = [BrokerPosition(hand, AssetClass.OPTION, 1, 1.0, 100.0, 1.0)]
    r.eng.tick()
    assert any("未知期權部位" in t for t in titles(r))
    assert sum("未知期權部位" in t for t in titles(r)) == 1
    r.run(et(9, 25))
    assert sum("未知期權部位" in t for t in titles(r)) == 1  # once a day
    assert not any(o.symbol == hand for o in r.broker.orders.values())


def test_h2_not_flat_page_at_1550(tmp_path, con):
    r, lp = entered_rig(tmp_path, con, time_stop_minutes=600)
    r.run(et(15, 44), step=60)
    r.broker.fill = "never"  # the forced exit never fills
    r.run(et(15, 49, 50), step=10)
    assert not any("收盤前未平倉" in t for t in titles(r))
    r.run(et(15, 50, 20), step=10)
    hits = [a for a in r.sent if "收盤前未平倉" in a[0]]
    assert len(hits) == 1 and hits[0][2] == _iface_critical()


def _iface_critical():
    from stockmoney.desk import notify
    return notify.CRITICAL


def _leftover_db(tmp_path):
    """A desk.sqlite with a live position of DAY still ENTERED (the session
    died); returns (db path, broker)."""
    db = tmp_path / "desk.sqlite"
    with store.connect(db) as c:
        r, lp = entered_rig(tmp_path, c)
        c.commit()
    return db, r.broker, lp["position_id"]


def _recover_factory(broker, sent):
    def make(con, cfg, clock):
        broker.clock = clock
        return Engine(con, broker, cfg, clock=clock, risk_gate=allow_gate(), live=True,
                      notifier=lambda *a: sent.append(a)), "paper"
    return make


def test_h2_recover_subcommand_sells_leftovers_in_market_hours(tmp_path, monkeypatch):
    monkeypatch.setattr(orun.notify, "send", lambda *a, **k: None)
    db, broker, pid = _leftover_db(tmp_path)
    clock = Clock(et(10, 5, day=NEXT))
    sent = []
    rc = orun.recover(spy_cfg(tmp_path), db, clock=clock, engine_factory=_recover_factory(broker, sent),
                      sleep=lambda s: clock.advance(s))
    assert rc == 0 and C601N not in broker.positions
    with store.connect(db) as c:
        p = ostore.position(c, pid)
        assert p["state"] == "EXITED" and p["exit_reason"] == "carry_over"
        assert exits(c, pid)[0]["order_type"] == "market"


def test_h2_recover_outside_market_hours_only_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(orun.notify, "send", lambda *a, **k: None)
    db, broker, pid = _leftover_db(tmp_path)
    n = len(broker.submit_calls)
    clock = Clock(datetime(2026, 10, 14, 7, 0, tzinfo=ET))  # boot before the open
    sent = []
    rc = orun.recover(spy_cfg(tmp_path), db, clock=clock, engine_factory=_recover_factory(broker, sent),
                      sleep=lambda s: clock.advance(s))
    assert rc == 1 and len(broker.submit_calls) == n and broker.positions[C601N] == 10
    assert any("前一日部位未平" in a[0] for a in sent)


def test_h2_recover_does_nothing_while_a_session_holds_the_lock(tmp_path):
    import fcntl
    cfg = spy_cfg(tmp_path)
    held = orun._lock(cfg.lock_path)
    try:
        assert orun.recover(cfg, tmp_path / "d.sqlite", clock=Clock(et(10, 0)),
                            engine_factory=lambda *a: pytest.fail("must not build an engine")) == 0
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()


def test_h2_recover_job_is_opt_in_and_wired():
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("desk_schedule", root / "scripts" / "desk_schedule.py")
    sch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sch)
    assert "options-recover" in sch.JOBS and "options-recover" not in sch.DEFAULT_JOBS
    assert sch.plist("options-recover", sch.JOBS["options-recover"])["RunAtLoad"] is True
    sh = (root / "scripts" / "desk_job.sh").read_text()
    assert "options.run recover" in sh
    assert orun.main.__code__  # the subcommand parses
    with pytest.raises(SystemExit):
        orun.main(["run", "recover", "--bogus"])


# ------------------------------------------------------------------ H1 early closes
def test_h1_early_close_list_and_broker_calendar():
    assert close_time(date(2026, 11, 27)) == (13, 0) and close_time(date(2026, 12, 24)) == (13, 0)
    assert close_time(date(2027, 11, 26)) == (13, 0) and close_time(DAY) == (16, 0)
    assert all(is_trading_day(d) for d in EARLY_CLOSES)
    assert market_close(FakeOptionsBroker(Clock(et(9, 0))), date(2026, 11, 27)) == (13, 0)

    class Cal:
        def get_calendar(self, req):
            return [SimpleNamespace(date=DAY, close=datetime(2026, 10, 13, 14, 0))]
    assert market_close(SimpleNamespace(client=Cal()), DAY) == (14, 0)  # the broker knows an unlisted early close

    class Broken:
        def get_calendar(self, req):
            raise ConnectionError("down")
    assert market_close(SimpleNamespace(client=Broken()), date(2026, 11, 27)) == (13, 0)


def test_h1_half_day_flat_at_1245_and_gate_flat_by(tmp_path, con):
    half = date(2026, 11, 27)
    clock = Clock(datetime.combine(half, time(9, 20), ET))
    gate = allow_gate()
    eng = Engine(con, FakeOptionsBroker(clock, {}), spy_cfg(tmp_path), clock=clock, risk_gate=gate, live=True,
                 flags_fn=lambda n: {}, notifier=lambda *a: None)
    eng.tick()
    assert eng._flat() == datetime.combine(half, time(12, 45), ET)
    assert eng.session_end() == datetime.combine(half, time(13, 0), ET)
    assert gate.flat_by == [time(12, 45)]
    clock.set(datetime.combine(half, time(12, 46), ET))
    eng._begin(clock.now)
    eng._spots = {"SPY": 600.0}
    p = {"day": str(half), "qty_open": 3, "underlying": "SPY", "invalidation": None, "direction": "long",
         "inv_close": None, "inv_vwap": 0, "entry_time": datetime.combine(half, time(11, 0), ET).isoformat(),
         "max_hold_min": 180, "exit_variant": "A", "tp_done": 0, "trail_ref": None, "entry_price": 1.0,
         "stop_price": None}
    assert eng._exit_decision(p, None, clock.now) == ("force_flat", 3)
    # line A's gate takes the same flat_by
    lg = _iface.make_risk_gate(con, FakeOptionsBroker(clock), spy_cfg(tmp_path))
    lg.set_flat_by(time(12, 45))
    assert lg.engine.limits.flat_by == time(12, 45) and not lg.engine.limits.problems()


def test_h1_live_entry_blocked_near_flat_and_time_stop_capped(tmp_path, con):
    # a forced flat at 10:30 leaves 39 minutes at the 09:51 trigger: < 90, so no live order,
    # and the shadow positions' time stop is capped at the flat
    r = Rig(tmp_path, con, force_flat=(10, 30))
    r.run(et(9, 52))
    assert not r.pos("live") and not r.broker.submit_calls
    assert con.execute("SELECT COUNT(*) FROM options_filtered WHERE reason='too_close_to_flat'").fetchone()[0] == 1
    assert {p["max_hold_min"] for p in r.pos("shadow")} == {39}


def test_n4_session_loop_stops_at_an_early_close(tmp_path, monkeypatch):
    monkeypatch.setattr(orun.notify, "send", lambda *a, **k: None)
    half = date(2026, 11, 27)
    clock = Clock(datetime.combine(half, time(9, 0), ET))
    seen = []

    def make(con, cfg, clk):
        eng = Engine(con, FakeOptionsBroker(clk, {}), cfg, clock=clk, risk_gate=allow_gate(), live=True,
                     notifier=lambda *a: None)
        real = eng.tick
        eng.tick = lambda fast=False: (seen.append(clk.now), real(fast=fast))[1]
        return eng, "paper"
    assert orun.session(spy_cfg(tmp_path), tmp_path / "d.sqlite", clock=clock, engine_factory=make,
                        sleep=lambda s: clock.advance(max(s, 60))) == 0
    assert max(seen) <= datetime.combine(half, time(13, 0), ET)
    assert max(seen) >= datetime.combine(half, time(12, 59), ET)


# ------------------------------------------------------------------ M2 partial entry stuck in PENDING
def _stuck_partial(tmp_path, con, **kw):
    r = Rig(tmp_path, con, **kw)
    r.broker.fill = "never"
    r.run(et(9, 51, 5))
    lp = r.pos("live")[0]
    o = r.broker.orders[ostore.cid(lp["position_id"], "e")]
    o.status, o.filled_qty, o.filled_avg_price, o.filled_at = OrderStatus.PARTIALLY_FILLED, 4, 0.97, r.clock.now
    r.broker.positions[C601N] = 4

    def boom(cid):
        raise ConnectionError("cancel failed")
    r.broker.cancel = boom
    r.broker.fill = "immediate"  # the exit itself can fill
    r.eng = r.new_engine()
    return r, lp


def test_m2_partial_entry_filled_part_sold_at_forced_flat(tmp_path, con):
    r, lp = _stuck_partial(tmp_path, con, time_stop_minutes=600)
    r.run(et(15, 44), step=120)
    assert not exits(con, lp["position_id"])  # not due yet
    assert any("撤單一直失敗" in t for t in titles(r))  # paged as soon as cancels keep failing
    r.run(et(15, 46), step=2)
    ex = exits(con, lp["position_id"])
    assert len(ex) == 1 and ex[0]["qty"] == 4 and ex[0]["reason"] == "force_flat"
    assert C601N not in r.broker.positions
    p = ostore.position(con, lp["position_id"])
    assert p["qty_open"] == 0 and p["state"] == "PENDING"  # the entry order itself is still working
    assert r.eng.finish() is False                       # and finish says so


def test_m2_partial_entry_sold_when_time_stop_elapses(tmp_path, con):
    r, lp = _stuck_partial(tmp_path, con, time_stop_minutes=30)
    r.run(et(10, 25), step=20)
    ex = exits(con, lp["position_id"])
    assert ex and ex[0]["reason"] == "time_stop" and C601N not in r.broker.positions
    # the entry finally ends: the row closes as EXITED, not ENTERED with nothing open
    o = r.broker.orders[ostore.cid(lp["position_id"], "e")]
    o.status = OrderStatus.CANCELED
    r.run(et(10, 26), step=20)
    p = ostore.position(con, lp["position_id"])
    assert p["state"] == "EXITED" and p["qty_initial"] == 4 and p["exit_reason"] == "time_stop"


# ------------------------------------------------------------------ M1 exits through the risk layer
def test_m1_every_exit_passes_the_gate(tmp_path, con):
    gate = allow_gate()
    r, lp = entered_rig(tmp_path, con, gate=gate)
    r.broker.mult[C601N] = 0.6
    r.run(et(9, 54))
    assert ostore.position(con, lp["position_id"])["exit_reason"] == "premium_stop"
    assert gate.exit_calls and all(i.side == Side.SELL for i, _, _ in gate.exit_calls)


def test_m1_refused_limit_falls_back_to_market(tmp_path, con):
    gate = FnGate(lambda i, c, n: RiskDecision(True, "ok", i),
                  exit_fn=lambda i, c, n: RiskDecision(i.order_type == OrderType.MARKET, "band"))
    r, lp = entered_rig(tmp_path, con, gate=gate)
    r.broker.mult[C601N] = 0.6
    r.run(et(9, 54))
    ex = exits(con, lp["position_id"])
    assert ex[0]["order_type"] == "market" and C601N not in r.broker.positions


def test_m1_gate_refusal_sends_nothing_and_pages(tmp_path, con):
    gate = FnGate(lambda i, c, n: RiskDecision(True, "ok", i), exit_fn=lambda i, c, n: RiskDecision(False, "no"))
    r, lp = entered_rig(tmp_path, con, gate=gate)
    r.broker.mult[C601N] = 0.6
    r.run(et(9, 54))
    assert not exits(con, lp["position_id"]) and r.broker.positions[C601N] == 10
    assert sum("出場被風控擋下" in t for t in titles(r)) == 1
    # a gate that RAISES a sell, or changes anything but lowering its qty, is a refusal too
    gate.exit_fn = lambda i, c, n: RiskDecision(True, "bigger", replace(i, qty=i.qty + 1))
    r.run(et(9, 55))
    assert not exits(con, lp["position_id"])
    gate.exit_fn = lambda i, c, n: RiskDecision(True, "other contract", replace(i, symbol="SPY261014C00602000"))
    r.run(et(9, 56))
    assert not exits(con, lp["position_id"])
    gate.exit_fn = lambda i, c, n: RiskDecision(True, "smaller", replace(i, qty=3))  # lowering is allowed
    r.run(et(9, 56, 30))
    assert exits(con, lp["position_id"])[0]["qty"] == 3


def test_m1_real_gate_refuses_a_sell_to_open(tmp_path, con):
    r = Rig(tmp_path, con)
    r.gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.eng = r.new_engine()
    r.run(et(9, 52))
    lp = r.pos("live")[0]
    assert lp["state"] == "ENTERED"
    del r.broker.positions[C601N]  # the broker no longer holds it (closed by hand)
    r.clock.set(et(15, 45))
    r.run(et(15, 48), step=5)  # a shortfall is confirmed on a second look 120 s later
    assert not exits(con, lp["position_id"]) and C601N not in r.broker.positions  # never a sell to open
    from stockmoney.desk.options._iface import OrderIntent as OI
    sell = OI("s-test", C601N, AssetClass.OPTION, Side.SELL, qty=1, order_type=OrderType.MARKET, tif=TIF.DAY)
    assert not r.gate.check_exit(sell, None, r.clock.now).allow
    # (Grok round-3 H2) the missing contracts are written off as a loss, not left open
    p = ostore.position(con, lp["position_id"])
    assert p["state"] == "EXITED" and p["exit_reason"] == "broker_shortfall"


def test_m1_real_gate_normal_day_round_trip(tmp_path, con):
    r = Rig(tmp_path, con)
    r.gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.broker.extra_positions = [BrokerPosition("AAPL", AssetClass.EQUITY, 100, 150.0, 15_000.0, 150.0)]
    r.eng = r.new_engine()
    r.run(et(9, 52))
    assert r.pos("live")[0]["state"] == "ENTERED"
    r.run(et(15, 47), step=60)
    assert r.pos("live")[0]["state"] == "EXITED" and C601N not in r.broker.positions


def test_m1_deny_all_gate_refuses_everything():
    g = _iface.DenyAllGate()
    now = et(10, 0)
    sell = OrderIntent("s", C601N, AssetClass.OPTION, Side.SELL, qty=1, order_type=OrderType.MARKET, tif=TIF.DAY)
    assert not g.check_exit(sell, None, now).allow
    assert not g.check(replace(sell, side=Side.BUY), None, now).allow


# ------------------------------------------------------------------ L1 non-finite quotes
def test_l1_nan_and_inf_quotes_never_pass_liquidity():
    cfg = OptionsConfig()
    now = et(10, 0)
    base = OptionContract(C601N, "SPY", "C", 601.0, NEXT, 1.0, 1.02, None, 100, 1000, 0.5, None, now)
    assert sel.liquidity_reason(base, now, cfg) is None
    for bad in (dict(bid=float("nan")), dict(ask=float("nan")), dict(ask=float("inf")), dict(bid=float("-inf"))):
        assert sel.liquidity_reason(replace(base, **bad), now, cfg) == "no_two_sided_quote", bad
        assert sel.mid(replace(base, **bad)) is None
    assert sel.liquidity_reason(replace(base, bid=1.1, ask=1.0), now, cfg) == "no_two_sided_quote"
    assert sel.liquidity_reason(replace(base, bid=0.0), now, cfg) == "no_two_sided_quote"


def test_l1_nan_bid_is_never_fresh_and_never_priced(tmp_path, con):
    r, lp = entered_rig(tmp_path, con)
    real = r.broker.option_quote

    def nanq(occ):
        q = real(occ)
        return replace(q, bid=float("nan")) if q is not None and occ == C601N else q
    r.broker.option_quote = nanq
    r.run(et(9, 55))
    p = ostore.position(con, lp["position_id"])
    assert p["last_bid"] is not None and not math.isnan(p["last_bid"])
    assert not r.eng._fresh(nanq(C601N), r.clock.now)
    r.clock.set(et(15, 45))
    r.eng.tick()
    ex = exits(con, lp["position_id"])
    assert ex[0]["order_type"] == "market" and ex[0]["limit_price"] is None


# ------------------------------------------------------------------ L2 unreadable exit status
def test_l2_unreadable_exit_never_replaced_blind_and_pages_until_readable(tmp_path, con):
    # (revised by the Grok round-3 review, H1: a replacement only after the old order is read and booked)
    r, lp = entered_rig(tmp_path, con, time_stop_minutes=600)
    r.run(et(15, 44), step=60)
    r.broker.fill = "never"
    r.run(et(15, 45, 10), step=2)
    assert exits(con, lp["position_id"])
    n_sells = len([o for o in r.broker.orders.values() if o.side == Side.SELL])

    def bad(cid):
        raise ConnectionError("down")
    r.broker.get_order = bad
    r.broker.fill = "immediate"
    r.run(et(15, 52), step=2)
    assert len([o for o in r.broker.orders.values() if o.side == Side.SELL]) == n_sells  # nothing sent blind
    assert sum("出場單撤不掉" in t for t in titles(r)) >= 2  # re-paged every 5 minutes
    del r.broker.get_order  # the broker recovers: booked, the rest replaced in the same pass
    r.run(et(15, 53), step=2)
    p = ostore.position(con, lp["position_id"])
    assert p["state"] == "EXITED" and p["qty_open"] == 0 and C601N not in r.broker.positions
    assert sum(o.filled_qty for o in r.broker.orders.values() if o.side == Side.SELL) == 10


def test_l2_forced_flat_survives_submit_errors(tmp_path, con):
    r, lp = entered_rig(tmp_path, con, time_stop_minutes=600)
    r.run(et(15, 44), step=60)
    real = r.broker.submit

    def bad(intent):
        raise ConnectionError("down")
    r.broker.submit = bad
    r.run(et(15, 48), step=2)
    r.broker.submit = real
    r.run(et(15, 52), step=2)
    assert r.pos("live")[0]["state"] == "EXITED" and C601N not in r.broker.positions


# ------------------------------------------------------------------ L3 plan times
def test_l3_unicode_digits_rejected_in_plan_times():
    from pydantic import ValidationError
    from stockmoney.desk.options.plan import Trigger
    base = {"kind": "break_above", "level": 600.0, "confirm_closes": 1, "earliest": "09:40", "latest": "11:00"}
    assert Trigger.model_validate(base).earliest == "09:40"
    with pytest.raises(ValidationError):
        Trigger.model_validate({**base, "earliest": "09:٠٥"})
    with pytest.raises(ValidationError):
        Trigger.model_validate({**base, "latest": "١١:00"})


# ------------------------------------------------------------------ N1 no write lock across I/O
class LockCheckingBroker(FakeOptionsBroker):
    """Fails the test if any broker call happens inside an open write transaction."""
    con = None
    violations: list

    def _chk(self, what):
        if self.con is not None and self.con.in_transaction:
            self.violations.append(what)

    def minute_bars(self, *a):
        self._chk("minute_bars")
        return super().minute_bars(*a)

    def stock_quote(self, *a):
        self._chk("stock_quote")
        return super().stock_quote(*a)

    def option_quote(self, *a):
        self._chk("option_quote")
        return super().option_quote(*a)

    def option_chain(self, *a):
        self._chk("option_chain")
        return super().option_chain(*a)

    def submit(self, *a):
        self._chk("submit")
        return super().submit(*a)

    def get_order(self, *a):
        self._chk("get_order")
        return super().get_order(*a)

    def account(self):
        self._chk("account")
        return super().account()


def test_n1_engine_never_holds_the_write_lock_across_broker_calls(tmp_path, con):
    from opt_fakes import with_history
    r = Rig(tmp_path, con)
    b = LockCheckingBroker(r.clock, {"SPY": with_history(orb_breakout_day())})
    b.templates = r.broker.templates
    b.violations = []
    r.broker = b
    r.gate = _iface.make_risk_gate(con, b, r.cfg)  # line A's gate reads the broker too
    r.eng = r.new_engine()
    b.con = con
    notified = []
    r.eng.notifier = lambda *a: notified.append(con.in_transaction)
    r.run(et(9, 55))
    b.mult[C601N] = 0.6
    r.run(et(10, 0))
    r.eng.finish()
    assert r.pos("live") and r.pos("live")[0]["state"] == "EXITED"
    assert b.violations == []
    assert notified and not any(notified)


def test_n1_stock_sync_fills_treats_a_locked_db_as_retryable(tmp_path, monkeypatch):
    from stockmoney.desk import run as drun
    from stockmoney.desk.execution.ledger import Ledger
    with store.connect(tmp_path / "s.sqlite") as c:
        drun.ensure_schema(c)
        c.execute("INSERT INTO submitted (client_order_id, kind, symbol, asset_class, strategy, side, qty, "
                  "submitted_at, status, recorded) VALUES ('x1','entry','AAPL','equity','s','buy',1,?, 'new', 0)",
                  (datetime.now(ET).isoformat(),))
        c.commit()

        def locked(*a, **k):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(drun, "_sync_one", locked)
        ev = drun.sync_fills(c, object(), Ledger(c), "2026-10-13")
        assert ev and "資料庫忙碌" in ev[0]
        assert store.get_setting(c, "halt_new_entries", "0") != "1"


# ------------------------------------------------------------------ N2 partial exit fills priced per increment
def test_n2_partial_exit_fills_booked_at_their_own_price(tmp_path, con):
    r = Rig(tmp_path, con)
    r.gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.eng = r.new_engine()
    r.run(et(9, 52))
    lp = r.pos("live")[0]
    e = lp["entry_price"]
    r.broker.fill = "never"
    r.broker.mult[C601N] = 0.6
    while not exits(con, lp["position_id"]):
        r.eng.tick()
        r.clock.advance(2)
    ex = exits(con, lp["position_id"])
    o = r.broker.orders[ex[0]["client_order_id"]]
    o.status, o.filled_qty, o.filled_avg_price, o.filled_at = OrderStatus.PARTIALLY_FILLED, 4, 0.50, r.clock.now
    r.broker.positions[C601N] = 6
    r.eng.tick()
    r.clock.advance(2)
    o.status, o.filled_qty, o.filled_avg_price = OrderStatus.FILLED, 10, 0.56  # the last 6 at 0.60
    del r.broker.positions[C601N]
    r.eng.tick()
    sells = [f for f in ostore.fills(con, lp["position_id"]) if f["side"] == "sell"]
    assert [(f["qty"], round(f["price"], 6)) for f in sells] == [(4, 0.5), (6, 0.6)]
    p = ostore.position(con, lp["position_id"])
    want = ((0.50 - e) * 4 + (0.60 - e) * 6) * 100
    assert p["state"] == "EXITED" and p["realized_usd"] == pytest.approx(want)
    # the daily-loss stop reads line A's book: the same realized number
    assert r.gate.engine.book.realized() == pytest.approx(want)


# ------------------------------------------------------------------ N3 never a second sell while one works
def test_n3_flatten_now_does_not_double_sell_and_flat_replaces_limit(tmp_path, con):
    r, lp = entered_rig(tmp_path, con, time_stop_minutes=600)
    r.run(et(15, 44), step=60)
    r.broker.fill = "never"
    r.run(et(15, 45, 1), step=1)
    ex = exits(con, lp["position_id"])
    assert len(ex) == 1 and ex[0]["order_type"] == "limit"
    n = len(r.broker.submit_calls)
    r.eng.flatten_now("session_end")
    # (Grok round-3 H4) the working limit is canceled, confirmed, then replaced in the same call:
    # one replacement, never two sells working at once
    assert len(r.broker.submit_calls) == n + 1
    sells_ = [o for o in r.broker.orders.values() if o.side == Side.SELL]
    assert sum(o.status == OrderStatus.NEW for o in sells_) == 1
    r.run(et(15, 45, 8), step=2)
    ex = exits(con, lp["position_id"])
    assert r.broker.orders[ex[0]["client_order_id"]].status == OrderStatus.CANCELED  # limit replaced at once
    assert ex[-1]["order_type"] == "market" and len(ex) == 2
