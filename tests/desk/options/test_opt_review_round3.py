"""Round-3 review of the options engine (F1, F2): line A's book must never
roll back the engine's own store writes, a refused fill keeps its order
polled, and the sleeve's own unbooked partial sale is never reconciled away
as contracts "gone at the broker". Fakes only."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from stockmoney.desk import store
from stockmoney.desk.options import _iface
from stockmoney.desk.options import plan as oplan
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options._iface import OrderStatus, Side, occ_symbol
from stockmoney.desk.options.engine import Engine

from opt_fakes import DAY, NEXT, Clock, FakeOptionsBroker, chain, et, orb_breakout_day, spy_cfg, with_history

C601N = occ_symbol("SPY", NEXT, "C", 601)


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        yield c


class Rig:
    def __init__(self, tmp_path, con, **kw):
        self.clock = Clock(et(9, 20))
        self.broker = FakeOptionsBroker(self.clock, {"SPY": with_history(orb_breakout_day())})
        self.broker.templates = {**chain(), **chain(expiry=NEXT)}
        self.cfg = spy_cfg(tmp_path, **kw)
        self.con = con
        self.sent = []
        self.gate = _iface.make_risk_gate(con, self.broker, self.cfg)
        self.eng = self.new_engine()

    def new_engine(self):
        return Engine(self.con, self.broker, self.cfg, clock=self.clock, risk_gate=self.gate, live=True,
                      flags_fn=lambda now: {}, notifier=lambda *a: self.sent.append(a))

    def run(self, until, step=20):
        while self.clock.now < until.astimezone(timezone.utc):
            self.eng.tick()
            self.clock.advance(step)

    def live(self):
        return ostore.positions(self.con, str(DAY), "live")


def truth(broker):
    return sum((o.filled_qty * o.filled_avg_price) * (1 if o.side == Side.SELL else -1)
               for o in broker.orders.values() if o.filled_qty) * 100


def test_status_unreadable_after_partial_exit_misreconciles_own_fill(tmp_path, con):
    """An exit partly fills (4 of 10) and is then cancelled; get_order goes down
    (Alpaca's cancel also calls get_order, so cancel fails too) while account and
    open_orders still answer. The fallback must not treat our OWN unbooked sale as
    contracts 'gone at the broker'."""
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    lp = r.live()[0]
    assert lp["state"] == "ENTERED" and r.broker.positions[C601N] == 10
    r.broker.fill = "never"
    r.broker.mult[C601N] = 0.5          # premium stop -> limit sell at the bid
    r.run(et(9, 52, 10), step=2)
    ex = [o for o in ostore.orders_for(con, lp["position_id"]) if o["kind"] == "exit"]
    assert len(ex) == 1
    bo = r.broker.orders[ex[0]["client_order_id"]]
    # 4 fill at the bid, then the order is cancelled at the broker (our reprice / a broker cancel)
    q = r.broker.option_quote(C601N)
    bo.filled_qty, bo.filled_avg_price, bo.filled_at, bo.status = 4, q.bid, r.clock.now, OrderStatus.CANCELED
    r.broker.positions[C601N] = 6
    real_get, real_cancel = r.broker.get_order, r.broker.cancel

    def down(cid):
        raise ConnectionError("get_order endpoint down")
    r.broker.get_order = down
    r.broker.cancel = down
    r.broker.fill = "immediate"
    r.clock.advance(70)                  # past the 60 s "settled" window
    r.run(r.clock.now + timedelta(seconds=40), step=2)
    r.broker.get_order, r.broker.cancel = real_get, real_cancel
    r.run(r.clock.now + timedelta(seconds=60), step=2)
    p = ostore.position(con, lp["position_id"])
    book = _iface.OptionsBook(con)
    rec = con.execute("SELECT * FROM options_book_fills WHERE side='reconcile'").fetchall()
    assert not r.broker.positions and p["state"] == "EXITED" and p["qty_open"] == 0
    assert not rec, "our own partial sale was reconciled away as contracts 'gone at the broker'"
    assert abs(book.realized() - truth(r.broker)) < 0.01
    assert abs(p["realized_usd"] - truth(r.broker)) < 0.01


def test_daily_loss_stop_blocks_next_buy(tmp_path, con):
    """Real LineAGate: after a realized loss beyond the stop, the next live
    trigger of the day is refused by the gate."""
    r = Rig(tmp_path, con, time_stop_minutes=5)
    r.gate.engine.limits.daily_loss_pct = 0.02  # $100 stop for the test
    r.run(et(9, 52))
    lp = r.live()[0]
    r.broker.mult[C601N] = 0.5   # -50%: premium stop, a loss of about -$500
    r.run(et(9, 56), step=2)
    p = ostore.position(con, lp["position_id"])
    assert p["state"] == "EXITED" and p["realized_usd"] < -100
    # a Grok plan item that triggers later the same day
    plan = {"protocol": "1", "type": "OPTIONS_PLAN", "plan_id": "dlstop01", "for_date": str(DAY),
            "generated_at": et(7, 0).isoformat(),
            "market_context": {"summary": "", "risk_tone": "", "key_events": []},
            "plans": [{"underlying": "SPY", "bias": "long", "playbook": "orb", "thesis": "t", "catalyst": "",
                       "confidence": 0.5, "evidence_urls": [],
                       "trigger": {"kind": "break_above", "level": 601.5, "confirm_closes": 1,
                                   "earliest": "09:35", "latest": "11:30"},
                       "invalidation": {"kind": "below", "underlying_level": 600.0}}],
            "avoid": [], "notes": ""}
    assert oplan.ingest(con, json.dumps(plan), et(8, 0), r.cfg).status == "accepted"
    r.cfg = spy_cfg(tmp_path, time_stop_minutes=5, grok_min_rvol=0.1)
    r.eng = r.new_engine()
    r.run(et(10, 30), step=10)
    buys = [o for o in r.broker.orders.values() if o.side == Side.BUY]
    filt = con.execute("SELECT reason, detail_json FROM options_filtered WHERE stage='live'").fetchall()
    assert len(buys) == 1
    assert any("daily loss" in (f["detail_json"] or "") for f in filt)


def test_book_error_rolls_back_callers_uncommitted_store_writes(tmp_path, con):
    """LineAGate.book_fill swallows OptionsBookError, but apply_fill raises it
    INSIDE `with self.con:` on the shared connection: the engine's uncommitted
    store writes for the same fill are rolled back silently."""
    ostore.ensure_schema(con)
    con.commit()
    gate = _iface.make_risk_gate(con, FakeOptionsBroker(Clock(et(10, 0))), spy_cfg(tmp_path))
    ostore.add_fill(con, "pid-x", et(10, 0), "sell", 3, 1.0, 0.9, 1.1, "premium_stop", "cid#3")  # uncommitted
    assert con.in_transaction
    errs = []
    gate.on_error = lambda k, m: errs.append(m)
    gate.book_fill(client_order_id="cid", occ=C601N, strategy="options:orb15", side=Side.SELL, qty=3, price=1.0,
                   filled_at=et(10, 0))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM options_fills WHERE position_id='pid-x'").fetchone()[0]
    assert n == 1, "the store fill written before book_fill was rolled back by the book's error"
    assert errs  # and the refusal was reported


def test_book_refusal_keeps_the_order_polled_and_pages(tmp_path, con):
    """The book refuses a sell fill (here: it lost the position): the store
    keeps its booking, the exit order is NOT marked terminal (it is polled
    again), the position is flagged and the owner paged once; once the book
    can take the fill, the next poll books it and the order closes."""
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    lp = r.live()[0]
    book = _iface.OptionsBook(con)
    pos = book.position(C601N)
    with con:  # simulate a diverged book: it holds nothing
        con.execute("DELETE FROM options_book_positions WHERE occ=?", (C601N,))
    r.broker.mult[C601N] = 0.5
    r.run(et(9, 53), step=2)
    p = ostore.position(con, lp["position_id"])
    ex = [o for o in ostore.orders_for(con, lp["position_id"]) if o["kind"] == "exit"]
    assert p["state"] == "EXITED" and p["qty_open"] == 0 and not r.broker.positions  # store booking kept
    assert ex and ex[0]["status"] not in ("filled", "canceled", "expired", "rejected")  # still polled
    assert "reconcile needed" in p["note"]
    assert sum("帳本拒收" in a[0] for a in r.sent) == 1
