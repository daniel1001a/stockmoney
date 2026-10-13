"""Regression tests for the round-2 review of the options engine (R1-R6).
Each scenario was reproduced as a bug by the reviewer; these assert the
correct behavior. Fake brokers only: no real order is ever sent."""
import fcntl
from datetime import date, datetime, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from stockmoney.desk import store
from stockmoney.desk.execution.options_risk import options_mismatches
from stockmoney.desk.options import _iface
from stockmoney.desk.options import report as oreport
from stockmoney.desk.options import run as orun
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options._iface import OrderStatus, Side, occ_symbol
from stockmoney.desk.options.engine import Engine

from opt_fakes import DAY, NEXT, Clock, FakeOptionsBroker, allow_gate, et, spy_cfg
from test_opt_engine import Rig

ET = ZoneInfo("America/New_York")
C601N = occ_symbol("SPY", NEXT, "C", 601)
DAY2 = date(2026, 10, 15)
DAY3 = date(2026, 10, 16)


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        yield c


def exits(con, pid):
    return [o for o in ostore.orders_for(con, pid) if o["kind"] == "exit"]


def entered_line_a(tmp_path, con, **kw):
    r = Rig(tmp_path, con, **kw)
    r.gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.eng = r.new_engine()
    r.run(et(9, 52))
    lp = r.pos("live")[0]
    assert lp["state"] == "ENTERED" and C601N in _iface.OptionsBook(con).positions()
    return r, lp


def next_day_shadow(r, con, day=NEXT):
    """The next day's session with DESK_OPTIONS_ORDERS=off (what make_session_engine builds)."""
    r.clock.set(et(9, 20, day=day))
    r.gate = _iface.make_shadow_gate(con, r.broker, r.cfg)
    r.live = False
    r.eng = r.new_engine()


# ------------------------------------------------------------------ R1 shadow-only mode keeps line A's exit checks
def test_r1_shadow_session_sells_leftover_and_books_it(tmp_path, con):
    r, lp = entered_line_a(tmp_path, con)
    next_day_shadow(r, con)
    r.run(et(9, 35, day=NEXT), step=2)
    p = ostore.position(con, lp["position_id"])
    assert p["state"] == "EXITED" and p["exit_reason"] == "carry_over" and C601N not in r.broker.positions
    assert not _iface.OptionsBook(con).positions()          # the sale reached line A's book
    assert not options_mismatches(_iface.OptionsBook(con), r.broker.account())
    assert r.gate.engine.book.realized() == pytest.approx(p["realized_usd"])


def test_r1_shadow_session_never_sells_what_the_broker_lacks(tmp_path, con):
    r, lp = entered_line_a(tmp_path, con)
    del r.broker.positions[C601N]  # closed by hand overnight
    next_day_shadow(r, con)
    r.run(et(9, 35, day=NEXT), step=2)
    assert not [o for o in r.broker.orders.values() if o.side == Side.SELL]
    assert r.broker.positions.get(C601N, 0) == 0


def test_r1_shadow_gate_refuses_buys(tmp_path, con):
    from stockmoney.desk.options._iface import AssetClass, OrderIntent, OrderType, TIF
    g = _iface.make_shadow_gate(con, FakeOptionsBroker(Clock(et(10, 0))), spy_cfg(tmp_path))
    buy = OrderIntent("b", C601N, AssetClass.OPTION, Side.BUY, qty=1, order_type=OrderType.LIMIT, tif=TIF.DAY,
                      limit_price=1.0)
    assert not g.check(buy, None, et(10, 0)).allow


def test_r1_session_off_mode_uses_shadow_gate(tmp_path, monkeypatch):
    b = FakeOptionsBroker(Clock(et(9, 20)))
    monkeypatch.setattr(_iface, "make_trading_broker", lambda: b)
    monkeypatch.setenv("DESK_OPTIONS_ORDERS", "off")
    with store.connect(tmp_path / "d.sqlite") as c:
        eng, mode = orun.make_session_engine(c, spy_cfg(tmp_path))
        assert mode == "shadow" and isinstance(eng.risk_gate, _iface.ShadowGate)


# ------------------------------------------------------------------ R2 phantom leftover
def test_r2_phantom_leftover_reconciled_over_three_days_with_real_gate(tmp_path, con):
    r, lp = entered_line_a(tmp_path, con)
    del r.broker.positions[C601N]  # owner closed it by hand / it expired while the Mac was off
    for d in (NEXT, DAY2, DAY3):
        r.clock.set(et(9, 20, day=d))
        r.gate = _iface.make_risk_gate(con, r.broker, r.cfg)
        r.eng = r.new_engine()
        r.run(et(9, 40, day=d), step=20)
    p = ostore.position(con, lp["position_id"])
    assert p["state"] == "EXITED" and p["exit_reason"] == "broker_flat_unreconciled"
    assert not [o for o in r.broker.orders.values() if o.side == Side.SELL]  # this path never sells
    assert not _iface.OptionsBook(con).positions()
    rec = con.execute("SELECT side, qty, realized_pnl FROM options_book_fills WHERE side='reconcile'").fetchall()
    assert [tuple(x) for x in rec] == [("reconcile", 10.0, 0.0)]
    n_live = con.execute("SELECT COUNT(*) FROM options_positions WHERE mode='live' AND state IN "
                         "('PENDING','ENTERED')").fetchone()[0]
    assert n_live == 0
    assert sum("部位已不在券商" in a[0] for a in r.sent) == 1  # paged once, not daily
    assert not any("出場被風控擋下" in a[0] for a in r.sent)
    rep = oreport.build(con, str(DAY), r.cfg)
    assert "損益未知" in rep and "未知" in rep


def test_r2_leftover_with_working_order_is_not_reconciled(tmp_path, con):
    r, lp = entered_line_a(tmp_path, con)
    del r.broker.positions[C601N]
    # a stray working order on the contract: nothing may be assumed
    from stockmoney.desk.options._iface import AssetClass, BrokerOrder, OrderType, TIF
    r.broker.orders["stray"] = BrokerOrder("99", "stray", C601N, Side.SELL, OrderStatus.NEW, 1, 0.0, None,
                                           r.clock.now, None, OrderType.LIMIT, TIF.DAY, None, 5.0, AssetClass.OPTION)
    r.clock.set(et(9, 20, day=NEXT))
    r.gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.eng = r.new_engine()
    r.run(et(9, 35, day=NEXT), step=20)
    assert ostore.position(con, lp["position_id"])["state"] == "ENTERED"


def test_r2_resolve_subcommand(tmp_path, monkeypatch):
    monkeypatch.setattr(orun.notify, "send", lambda *a, **k: None)
    db = tmp_path / "desk.sqlite"
    with store.connect(db) as c:
        r, lp = entered_line_a(tmp_path, c)
        c.commit()
    broker = r.broker
    clock = Clock(et(17, 0))

    def make(c, cfg, clk):
        return Engine(c, broker, cfg, clock=clk, risk_gate=_iface.make_risk_gate(c, broker, cfg),
                      notifier=lambda *a: None), "paper"
    cfg = spy_cfg(tmp_path)
    assert orun.resolve(lp["position_id"], cfg, db, clock, make) == 1  # the broker still holds it: refused
    assert broker.positions[C601N] == 10
    del broker.positions[C601N]
    assert orun.resolve(lp["position_id"], cfg, db, clock, make) == 0
    assert orun.resolve("nope", cfg, db, clock, make) == 1
    with store.connect(db) as c:
        p = ostore.position(c, lp["position_id"])
        assert p["state"] == "EXITED" and p["exit_reason"] == "manual_resolved"
        assert not _iface.OptionsBook(c).positions()
    assert not [o for o in broker.orders.values() if o.side == Side.SELL]


# ------------------------------------------------------------------ R4 store realized = book realized
def test_r4_store_vs_book_realized_with_interleaved_partial_entry(tmp_path, con):
    r = Rig(tmp_path, con, time_stop_minutes=30)
    r.gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.broker.fill = "never"
    r.eng = r.new_engine()
    r.run(et(9, 51, 5))
    pid = r.pos("live")[0]["position_id"]
    eo = r.broker.orders[ostore.cid(pid, "e")]
    eo.status, eo.filled_qty, eo.filled_avg_price, eo.filled_at = OrderStatus.PARTIALLY_FILLED, 4, 1.00, r.clock.now
    r.broker.positions[C601N] = 4

    def nocancel(cid):
        raise ConnectionError("cancel failed")
    r.broker.cancel = nocancel
    r.broker.fill = "immediate"
    r.eng = r.new_engine()
    r.eng.tick()
    r.clock.set(et(10, 22))
    r.run(et(10, 23), step=2)  # time stop: the 4 are sold
    assert exits(con, pid)
    eo.filled_qty, eo.filled_avg_price = 10, 1.06  # 6 more at 1.10
    r.broker.positions[C601N] = r.broker.positions.get(C601N, 0) + 6
    r.run(et(10, 24), step=2)
    eo.status = OrderStatus.CANCELED
    r.run(et(10, 26), step=2)
    p = ostore.position(con, pid)
    sells = [o for o in r.broker.orders.values() if o.side == Side.SELL and o.filled_qty]
    truth = sum(o.filled_qty * o.filled_avg_price for o in sells) * 100 - (4 * 1.00 + 6 * 1.10) * 100
    assert p["state"] == "EXITED" and C601N not in r.broker.positions
    assert p["realized_usd"] == pytest.approx(truth, abs=0.01)
    assert r.gate.engine.book.realized() == pytest.approx(truth, abs=0.01)
    buys = [f for f in ostore.fills(con, pid) if f["side"] == "buy"]
    assert [(f["qty"], round(f["price"], 6)) for f in buys] == [(4, 1.0), (6, 1.1)]


# ------------------------------------------------------------------ R5 session waits for recover's lock
def test_r5_session_waits_for_recover_to_release_the_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(orun.notify, "send", lambda *a, **k: None)
    cfg = spy_cfg(tmp_path)
    held = [orun._lock(cfg.lock_path)]
    waited = []

    def sleep(s):
        waited.append(s)
        if sum(waited) >= 120 and held:  # recover finishes after 2 minutes
            h = held.pop()
            fcntl.flock(h.fileno(), fcntl.LOCK_UN)
            h.close()
    built = []

    def factory(c, cfg_, clk):
        built.append(1)
        raise RuntimeError("stop here")  # the session got the lock; nothing more needed
    with pytest.raises(RuntimeError):
        orun.session(cfg, tmp_path / "d.sqlite", clock=Clock(et(9, 20)), engine_factory=factory, sleep=sleep)
    assert built and 120 <= sum(waited) <= orun.RECOVER_MAX_S + orun.LOCK_WAIT_EXTRA_S


def test_r5_session_gives_up_after_the_bound(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(orun.notify, "send", lambda *a, **k: sent.append(a))
    cfg = spy_cfg(tmp_path)
    held = orun._lock(cfg.lock_path)
    waited = []
    try:
        assert orun.session(cfg, tmp_path / "d.sqlite", clock=Clock(et(9, 0)),
                            engine_factory=lambda *a: pytest.fail("no engine"), sleep=waited.append) == 1
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()
    assert sum(waited) >= orun.RECOVER_MAX_S + orun.LOCK_WAIT_EXTRA_S and "未啟動" in sent[0][0]


# ------------------------------------------------------------------ R6 calendar failures are not cached
def test_r6_calendar_failure_retried_and_paged(tmp_path, con):
    calls = {"n": 0, "ok": False}

    class Cal:
        def get_calendar(self, req):
            calls["n"] += 1
            if not calls["ok"]:
                raise ConnectionError("calendar down")
            return [SimpleNamespace(date=DAY, close=datetime(2026, 10, 13, 13, 0))]

    clock = Clock(et(9, 20))
    b = FakeOptionsBroker(clock, {})
    b.client = Cal()
    sent = []
    gate = allow_gate()
    eng = Engine(con, b, spy_cfg(tmp_path), clock=clock, risk_gate=gate, live=True, notifier=lambda *a: sent.append(a))
    for _ in range(3):
        eng.tick()
        assert eng._flat() == et(15, 45)  # unknown: the regular close, flat never later than 15:45
        clock.advance(61)
    assert calls["n"] == 3 and sum("市場日曆讀不到" in a[0] for a in sent) == 1
    calls["ok"] = True  # the calendar comes back and says 13:00 (an early close the list does not know)
    eng.tick()
    assert eng._flat() == et(12, 45) and eng.session_end() == et(13, 0)
    assert gate.flat_by[-1] == time(12, 45)  # the gate follows the corrected flat time
    eng.tick()
    assert calls["n"] == 4  # cached once read
