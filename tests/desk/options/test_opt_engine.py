"""Engine state machine, exits, idempotency, restarts and safety gates, all
through FakeOptionsBroker and injected clock / risk gate (no real orders)."""
import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

from stockmoney.desk import store
from stockmoney.desk.options import plan as oplan
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options._iface import AssetClass, BrokerPosition, OrderType, RiskDecision, Side, occ_symbol
from stockmoney.desk.options.engine import Engine

from opt_fakes import DAY, Clock, FakeOptionsBroker, FnGate, allow_gate, chain, day_bars, et, orb_breakout_day, spy_cfg

C601 = occ_symbol("SPY", DAY, "C", 601)


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        yield c


class Rig:
    def __init__(self, tmp_path, con, bars=None, gate=None, live=True, flags=None, **cfg_kw):
        self.clock = Clock(et(9, 20))
        self.broker = FakeOptionsBroker(self.clock, {"SPY": bars if bars is not None else orb_breakout_day()})
        self.broker.templates = chain()
        self.cfg = spy_cfg(tmp_path, **cfg_kw)
        self.con = con
        self.gate_calls = []
        self.sent = []
        self.gate = gate or allow_gate(self.gate_calls)
        self.live = live
        self.flags = flags or {}
        self.eng = self.new_engine()

    def new_engine(self):
        return Engine(self.con, self.broker, self.cfg, clock=self.clock, risk_gate=self.gate, live=self.live,
                      flags_fn=lambda now: self.flags, notifier=lambda *a: self.sent.append(a))

    def run(self, until, step=20):
        while self.clock.now < until.astimezone(self.clock.now.tzinfo):
            self.eng.tick()
            self.clock.advance(step)

    def pos(self, mode=None, source="orb15"):
        return [p for p in ostore.positions(self.con, str(DAY), mode) if p["source"] == source]

    def orders(self):
        return [dict(r) for r in self.con.execute("SELECT * FROM options_orders ORDER BY submitted_at, client_order_id")]


def test_trigger_opens_shadow_variants_and_one_live_order(tmp_path, con):
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    setup = next(s for s in ostore.setups(con, str(DAY)) if s["source"] == "orb15")
    assert setup["state"] == "ENTERED" and json.loads(setup["signal_json"])["direction"] == "long"
    shadow = r.pos("shadow")
    assert {p["band_label"] for p in shadow} == {"d30-40", "d40-50", "d50-60"}
    assert all(p["dte_label"] == "0dte" and p["state"] == "ENTERED" for p in shadow)
    live = r.pos("live")
    assert len(live) == 1 and live[0]["occ"] == C601 and live[0]["state"] == "ENTERED"
    intent, contract, _ = r.gate_calls[0]
    assert intent.asset_class == AssetClass.OPTION and intent.side == Side.BUY
    assert intent.order_type == OrderType.LIMIT and intent.limit_price == 0.97 and intent.qty == 10
    assert contract.occ == C601 and contract.as_of is not None
    assert r.gate.reserved == [(C601, 10, 0.97)] and r.gate.released == [C601]
    assert r.gate.booked[0]["side"] == Side.BUY and r.gate.booked[0]["qty"] == 10
    # every 1-3 DTE variant was logged as a filtered opportunity (no expiry in the fake chain)
    n = con.execute("SELECT COUNT(*) FROM options_filtered WHERE reason='no_expiry_in_bucket'").fetchone()[0]
    assert n == 3


def test_shadow_buys_ask_sells_bid_take_profit_then_breakeven(tmp_path, con):
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    p = next(p for p in r.pos("shadow") if p["occ"] == C601)
    assert p["entry_price"] == 0.97 and p["qty_initial"] == 10  # bought at the ask (mid 0.95 + 0.02)
    r.broker.mult[C601] = 1.5  # mid 1.425 >= 0.97 * 1.4
    r.run(et(9, 54))
    p = ostore.position(con, p["position_id"])
    assert p["tp_done"] == 1 and p["qty_open"] == 5 and p["stop_price"] == 0.97
    fills = ostore.fills(con, p["position_id"])
    assert fills[-1]["side"] == "sell" and fills[-1]["price"] == pytest.approx(1.40)  # the bid (1.425 - 0.02, rounded), not the mid
    r.broker.mult[C601] = 1.0  # back to mid 0.95 <= cost
    r.run(et(9, 56))
    p = ostore.position(con, p["position_id"])
    assert p["state"] == "EXITED" and p["exit_reason"] == "breakeven_stop"
    assert p["realized_usd"] == pytest.approx(5 * (1.40 - 0.97) * 100 + 5 * (0.93 - 0.97) * 100)
    assert p["max_bid"] == pytest.approx(1.40)


def test_live_take_profit_sells_half_through_broker(tmp_path, con):
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    r.broker.mult[C601] = 1.5
    r.run(et(9, 54))
    lp = r.pos("live")[0]
    exits = [o for o in r.orders() if o["kind"] == "exit"]
    assert exits and exits[0]["qty"] == 5 and exits[0]["side"] == "sell" and exits[0]["order_type"] == "limit"
    assert lp["qty_open"] == 5 and lp["tp_done"] == 1
    assert r.broker.positions[C601] == 5


def test_premium_stop(tmp_path, con):
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    r.broker.mult[C601] = 0.6  # mid 0.57 <= 0.97 * 0.65
    r.run(et(9, 54))
    for p in [q for q in r.pos() if q["occ"] == C601]:
        assert p["state"] == "EXITED" and p["exit_reason"] == "premium_stop"
    assert C601 not in r.broker.positions


def test_underlying_invalidation(tmp_path, con):
    b = orb_breakout_day()
    after = b.index >= pd.Timestamp(et(10, 0))
    b.loc[after, ["open", "high", "low", "close"]] = 599.0  # collapses below the range middle 600.45
    r = Rig(tmp_path, con, bars=b)
    r.run(et(10, 2))
    ps = r.pos()
    assert ps and all(p["state"] == "EXITED" and p["exit_reason"] == "invalidation" for p in ps)


def test_time_stop(tmp_path, con):
    r = Rig(tmp_path, con, time_stop_minutes=30)
    r.run(et(10, 30))
    ps = r.pos()
    assert ps and all(p["exit_reason"] == "time_stop" for p in ps)
    entry = datetime.fromisoformat(ps[0]["entry_time"])
    assert timedelta(minutes=30) <= datetime.fromisoformat(ps[0]["exit_time"]) - entry < timedelta(minutes=31)


def test_forced_flat_at_1545_and_finish_is_flat(tmp_path, con):
    r = Rig(tmp_path, con, time_stop_minutes=600)
    r.run(et(15, 47), step=60)  # the live sell sent at 15:45 is booked on the next tick
    ps = r.pos()
    assert ps and all(p["exit_reason"] == "force_flat" for p in ps)
    assert all(datetime.fromisoformat(p["exit_time"]) >= et(15, 45) for p in ps)
    assert r.eng.finish() is True and not r.broker.positions


def test_no_entries_outside_window(tmp_path, con):
    r = Rig(tmp_path, con)
    r.clock.set(et(11, 40))
    r.eng.tick()
    assert not ostore.positions(con, str(DAY))
    assert all(s["state"] == "EXPIRED" for s in ostore.setups(con, str(DAY)))


def test_rerun_tick_and_restart_never_double_submit(tmp_path, con):
    r = Rig(tmp_path, con)
    r.run(et(9, 51, 10))
    r.eng.tick()
    r.eng.tick()  # same clock, twice
    n_pos = len(ostore.positions(con, str(DAY)))
    assert len(r.broker.submit_calls) == 1
    r.eng = r.new_engine()  # restart: state comes back from the store
    r.run(et(9, 53))
    assert len(ostore.positions(con, str(DAY))) == n_pos
    assert len(r.broker.submit_calls) == 1
    assert r.pos("live")[0]["state"] == "ENTERED"


def test_restart_after_late_trigger_does_not_chase(tmp_path, con):
    r = Rig(tmp_path, con)
    r.clock.set(et(10, 30))  # the 09:51 trigger is long past
    r.eng.tick()
    s = next(s for s in ostore.setups(con, str(DAY)) if s["source"] == "orb15")
    assert s["state"] == "MISSED" and not r.pos()


def test_submit_timeout_after_broker_accepted_is_resolved_not_resent(tmp_path, con):
    r = Rig(tmp_path, con)
    r.broker.fail_submit_once = True
    r.run(et(9, 53))
    assert len(r.broker.submit_calls) == 1
    assert r.pos("live")[0]["state"] == "ENTERED"


def test_submit_never_received_becomes_abandoned(tmp_path, con):
    r = Rig(tmp_path, con)

    def boom(intent):
        r.broker.submit_calls.append(intent.client_order_id)
        raise ConnectionError("down")
    r.broker.submit = boom
    r.run(et(9, 53))
    lp = r.pos("live")[0]
    assert lp["state"] == "ABANDONED" and lp["exit_reason"] == "entry_not_sent"
    assert len(r.broker.submit_calls) == 1
    assert r.pos("shadow")  # shadow unaffected


def test_unfilled_entry_cancelled_after_20s(tmp_path, con):
    r = Rig(tmp_path, con)
    r.broker.fill = "never"
    r.run(et(9, 51, 10), step=5)
    r.run(et(9, 52), step=5)
    lp = r.pos("live")[0]
    assert lp["state"] == "ABANDONED" and lp["exit_reason"] == "entry_unfilled"
    o = r.broker.orders[ostore.cid(lp["position_id"], "e")]
    assert o.status.value == "canceled"
    assert len(r.pos("shadow")) == 3


def test_risk_gate_deny_shrink_and_grow(tmp_path, con):
    deny = FnGate(lambda i, c, n: RiskDecision(False, "daily loss stop"))
    r = Rig(tmp_path, con, gate=deny)
    r.run(et(9, 52))
    assert not r.pos("live") and len(r.pos("shadow")) == 3 and not r.broker.submit_calls
    assert con.execute("SELECT detail_json FROM options_filtered WHERE reason='risk_gate'").fetchone()[0].find("daily") > 0


def test_risk_gate_may_shrink_never_grow(tmp_path, con):
    from dataclasses import replace
    shrink = FnGate(lambda i, c, n: RiskDecision(True, "resized", replace(i, qty=2)))
    r = Rig(tmp_path, con, gate=shrink)
    r.run(et(9, 52))
    assert r.broker.orders[r.broker.submit_calls[0]].qty == 2
    with store.connect(tmp_path / "b.sqlite") as con2:
        grow = FnGate(lambda i, c, n: RiskDecision(True, "bigger", replace(i, qty=50)))
        r2 = Rig(tmp_path, con2, gate=grow)
        r2.run(et(9, 52))
        assert not r2.broker.submit_calls
        assert con2.execute("SELECT COUNT(*) FROM options_filtered WHERE reason='risk_gate_bad_intent'").fetchone()[0] == 1


def test_halt_blocks_entries_not_exits(tmp_path, con):
    r = Rig(tmp_path, con)
    r.cfg.halt_file.write_text("x")
    r.run(et(9, 52))
    assert not r.pos("live") and r.pos("shadow")
    assert any("HALT" in a[0] for a in r.sent)
    # a live position opened before HALT still exits
    (tmp_path / "x").mkdir()
    with store.connect(tmp_path / "c.sqlite") as con2:
        r2 = Rig(tmp_path / "x", con2)
        r2.run(et(9, 52))
        r2.cfg.halt_file.write_text("x")
        r2.broker.mult[C601] = 0.6
        r2.run(et(9, 54))
        assert r2.pos("live")[0]["exit_reason"] == "premium_stop"


def test_muse_flag_blocks_live(tmp_path, con):
    r = Rig(tmp_path, con, flags={"SPY": "Muse ALERT critical"})
    r.run(et(9, 52))
    assert not r.pos("live") and r.pos("shadow")
    assert con.execute("SELECT COUNT(*) FROM options_filtered WHERE reason='muse_flag'").fetchone()[0] == 1


def test_stale_option_quotes_never_enter(tmp_path, con):
    r = Rig(tmp_path, con)
    r.broker.quote_age_s = 120
    r.run(et(9, 52))
    assert not ostore.positions(con, str(DAY))
    d = con.execute("SELECT detail_json FROM options_filtered WHERE reason='no_liquid_contract'").fetchone()[0]
    assert "stale_quote" in d


def test_stale_bars_no_trigger_and_owner_told(tmp_path, con):
    b = orb_breakout_day()
    b = b[b.index < pd.Timestamp(et(9, 40))]  # feed stops at 09:40
    r = Rig(tmp_path, con, bars=b)
    r.run(et(9, 52))
    assert not ostore.positions(con, str(DAY))
    assert any("資料延遲" in a[0] for a in r.sent)


def test_data_errors_do_not_stop_the_loop(tmp_path, con):
    r = Rig(tmp_path, con)
    r.run(et(9, 40))
    r.broker.data_error = ConnectionError("feed down")
    r.run(et(9, 50))
    r.broker.data_error = None
    r.run(et(9, 53))
    assert r.pos("live") and con.execute("SELECT COUNT(*) FROM options_events WHERE level='error'").fetchone()[0] > 0


def test_live_requires_paper_broker(tmp_path, con):
    r = Rig(tmp_path, con)
    r.broker.is_paper = False
    eng = r.new_engine()
    assert eng.live is False
    r.eng = eng
    r.run(et(9, 52))
    assert not r.pos("live") and r.pos("shadow")


def test_position_caps(tmp_path, con):
    r = Rig(tmp_path, con, max_open_positions=0)
    r.run(et(9, 52))
    assert not r.pos("live")
    assert con.execute("SELECT COUNT(*) FROM options_filtered WHERE reason='max_open_positions'").fetchone()[0] == 1


def test_finish_pages_owner_when_broker_not_flat(tmp_path, con):
    r = Rig(tmp_path, con)
    r.broker.extra_positions = [BrokerPosition("SPY261013P00590000", AssetClass.OPTION, 1, 1, 1, 1)]
    r.clock.set(et(16, 0))
    assert r.eng.finish() is False
    assert any(a[2] == 5 for a in r.sent)  # notify.CRITICAL


def _plan(level=601.5, plan_id="gp-20261013-0800", dte="0dte", avoid=None):
    return json.dumps({
        "protocol": "1", "type": "OPTIONS_PLAN", "plan_id": plan_id, "for_date": str(DAY),
        "generated_at": "2026-10-13T07:45:00-04:00",
        "market_context": {"summary": "", "risk_tone": "mixed", "key_events": []},
        "plans": [{"underlying": "SPY", "bias": "long", "playbook": "orb", "thesis": "t", "catalyst": "",
                   "trigger": {"kind": "break_above", "level": level, "confirm_closes": 1, "earliest": "09:30",
                               "latest": "11:00"},
                   "invalidation": {"kind": "below", "underlying_level": 599.0}, "target_underlying_pct": 1,
                   "max_hold_minutes": 45, "preferred_dte": dte, "confidence": 0.5, "evidence_urls": []}],
        "avoid": avoid or [], "notes": ""})


def test_grok_plan_live_and_max_hold(tmp_path, con):
    oplan.ingest(con, _plan(), et(8, 0))
    r = Rig(tmp_path, con, max_live_per_underlying=2)
    r.run(et(10, 50))
    g = r.pos(source="grok_plan")
    assert any(p["mode"] == "live" for p in g)
    assert all(p["max_hold_min"] == 45 and p["exit_reason"] == "time_stop" for p in g)


def test_late_plan_is_shadow_only(tmp_path, con):
    oplan.ingest(con, _plan(), et(9, 25))
    r = Rig(tmp_path, con)
    r.run(et(10, 0))
    s = next(s for s in ostore.setups(con, str(DAY)) if s["source"] == "grok_plan")
    assert s["live_eligible"] == 0 and "09:20" in s["live_note"]
    g = r.pos(source="grok_plan")
    assert g and all(p["mode"] == "shadow" for p in g)


def test_newer_plan_supersedes(tmp_path, con):
    oplan.ingest(con, _plan(level=605.0, plan_id="gp-20261013-0700"), et(7, 0))
    r = Rig(tmp_path, con)
    r.run(et(9, 26))
    oplan.ingest(con, _plan(level=601.5, plan_id="gp-20261013-0900"), et(9, 0))
    r.run(et(9, 27))
    st = {s["plan_id"]: s["state"] for s in ostore.setups(con, str(DAY)) if s["source"] == "grok_plan"}
    assert st == {"gp-20261013-0700": "SUPERSEDED", "gp-20261013-0900": "WAITING"}


def test_avoid_list_blocks_live_rule_setups(tmp_path, con):
    d = json.loads(_plan())
    d["plans"] = []
    d["avoid"] = [{"ticker": "SPY", "why": "no"}]
    oplan.ingest(con, json.dumps(d), et(8, 0))
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    assert not r.pos("live") and r.pos("shadow")


def test_real_line_a_gate_books_fills_and_releases(tmp_path, con):
    """The engine against line A's OptionsRiskEngine and OptionsBook (no
    stand-in): the entry passes every section 3 rule, every live fill is
    booked once, the reservation is released, and the book ends flat."""
    from stockmoney.desk.options import _iface
    r = Rig(tmp_path, con)
    r.gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.eng = r.new_engine()
    r.run(et(9, 52))
    lp = r.pos("live")[0]
    assert lp["state"] == "ENTERED"
    book = r.gate.engine.book
    assert book.position(C601).qty == lp["qty_initial"] and book.position(C601).strategy == "options:orb15"
    assert r.gate.engine._reserved == {}
    r.eng.tick()  # re-resolving the same fills books nothing twice
    assert con.execute("SELECT COUNT(*) FROM options_book_fills").fetchone()[0] == 1
    r.broker.mult[C601] = 0.6
    r.run(et(9, 54))
    assert r.pos("live")[0]["exit_reason"] == "premium_stop"
    assert book.position(C601) is None and not r.broker.positions


def test_real_line_a_gate_refuses_a_stale_quote(tmp_path, con):
    from stockmoney.desk.options import _iface
    from stockmoney.desk.options._iface import OrderIntent, TIF
    r = Rig(tmp_path, con)
    gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.clock.set(et(10, 0))
    r.broker.quote_age_s = r.cfg.max_quote_age_s + 5
    q = r.broker.option_quote(C601)
    intent = OrderIntent("t1", C601, AssetClass.OPTION, Side.BUY, qty=1, order_type=OrderType.LIMIT, tif=TIF.DAY,
                         limit_price=q.ask)
    dec = gate.check(intent, q, r.clock.now)
    assert not dec.allow and "old" in dec.reason
    r.broker.quote_age_s = 1
    assert gate.check(intent, r.broker.option_quote(C601), r.clock.now).allow
