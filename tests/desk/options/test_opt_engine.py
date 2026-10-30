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

from opt_fakes import (DAY, NEXT, Clock, FakeOptionsBroker, FnGate, allow_gate, chain, day_bars, et, orb_breakout_day,
                       spy_cfg, with_history)

C601 = occ_symbol("SPY", DAY, "C", 601)    # 0DTE: shadow variant only
C601N = occ_symbol("SPY", NEXT, "C", 601)  # 1-3 DTE: shadow variant and the live order


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        yield c


class Rig:
    def __init__(self, tmp_path, con, bars=None, gate=None, live=True, flags=None, history=True, **cfg_kw):
        self.clock = Clock(et(9, 20))
        b = bars if bars is not None else orb_breakout_day()
        self.broker = FakeOptionsBroker(self.clock, {"SPY": with_history(b) if history else b})
        self.broker.templates = {**chain(), **chain(expiry=NEXT)}
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
    assert {(p["dte_label"], p["exit_variant"]) for p in shadow} == {("0dte", "A"), ("0dte", "B"), ("1-3dte", "A"),
                                                                    ("1-3dte", "B")}
    assert all(p["band_label"] == "d40-55" and p["state"] == "ENTERED" for p in shadow)
    assert {p["occ"] for p in shadow} == {C601, C601N}
    live = r.pos("live")
    assert len(live) == 1 and live[0]["occ"] == C601N and live[0]["state"] == "ENTERED"
    assert live[0]["dte_label"] == "1-3dte" and live[0]["exit_variant"] == "A"
    assert live[0]["inv_close"] == pytest.approx(600.95) and live[0]["invalidation"] == pytest.approx(599.95)
    intent, contract, _ = r.gate_calls[0]
    assert intent.asset_class == AssetClass.OPTION and intent.side == Side.BUY
    assert intent.order_type == OrderType.LIMIT and intent.limit_price == 0.97 and intent.qty == 10
    assert contract.occ == C601N and contract.as_of is not None
    assert r.gate.reserved == [(C601N, 10, 0.97)] and r.gate.released == [C601N]
    assert r.gate.booked[0]["side"] == Side.BUY and r.gate.booked[0]["qty"] == 10


@pytest.mark.parametrize("action,qty", [("skip", 0), ("shrink:0.5", 5)])
def test_rule_filters_preserve_options_risk_gate(tmp_path, con, action, qty):
    rule = {"name": "late_entry", "sleeve": "options", "strategies": ["orb15"],
            "when": {"field": "minutes_since_open", "op": ">=", "value": 15}, "action": action}
    con.execute("INSERT INTO rule_filters (json,status) VALUES (?, 'active')", (json.dumps(rule),))
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    if qty:
        assert r.gate_calls[0][0].qty == qty
        assert r.pos("live")[0]["qty_initial"] == qty
        assert all(p["qty_initial"] == qty for p in r.pos("shadow"))
    else:
        assert not r.gate_calls and not r.pos()
        assert con.execute("SELECT count(*) FROM options_filtered WHERE reason LIKE '%late_entry%'").fetchone()[0] > 0


def test_shadow_buys_ask_sells_bid_take_profit_then_breakeven(tmp_path, con):
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    p = next(p for p in r.pos("shadow") if p["occ"] == C601 and p["exit_variant"] == "A")
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
    r.broker.mult[C601N] = 1.5
    r.run(et(9, 54))
    lp = r.pos("live")[0]
    exits = [o for o in r.orders() if o["kind"] == "exit"]
    assert exits and exits[0]["qty"] == 5 and exits[0]["side"] == "sell" and exits[0]["order_type"] == "limit"
    assert lp["qty_open"] == 5 and lp["tp_done"] == 1
    assert r.broker.positions[C601N] == 5


def test_premium_stop(tmp_path, con):
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    r.broker.mult[C601] = r.broker.mult[C601N] = 0.6  # mid 0.57 <= 0.97 * 0.65 (A) and 0.97 * 0.70 (B)
    r.run(et(9, 54))
    ps = r.pos()
    assert len(ps) == 5 and all(p["state"] == "EXITED" and p["exit_reason"] == "premium_stop" for p in ps)
    assert C601N not in r.broker.positions


def test_premium_stop_differs_by_exit_variant(tmp_path, con):
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    r.broker.mult[C601] = 0.70  # mid 0.665, bid ~0.645 (stops use the bid): below B's -30% (0.679), above A's -35% (0.6305)
    r.run(et(9, 54))
    st = {p["exit_variant"]: (p["state"], p["exit_reason"]) for p in r.pos("shadow") if p["occ"] == C601}
    assert st == {"A": ("ENTERED", None), "B": ("EXITED", "premium_stop")}


def test_variant_b_trails_underlying_after_take_profit(tmp_path, con):
    b = orb_breakout_day()
    late = b.index >= pd.Timestamp(et(10, 5))
    b.loc[late, ["open", "high", "low", "close"]] = 601.6  # pulls back ~0.6, still above the range high
    r = Rig(tmp_path, con, bars=b)
    r.run(et(9, 52))
    r.broker.mult[C601] = 1.5
    r.run(et(10, 8))
    st = {p["exit_variant"]: p for p in r.pos("shadow") if p["occ"] == C601}
    assert st["A"]["tp_done"] == 1 and st["A"]["state"] == "ENTERED"  # A: rest stops at cost, mid still 1.425
    assert st["B"]["state"] == "EXITED" and st["B"]["exit_reason"] == "atr_trail"
    assert st["B"]["trail_ref"] > 601.6


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
    assert len(r.pos("shadow")) == 4


def test_risk_gate_deny_shrink_and_grow(tmp_path, con):
    deny = FnGate(lambda i, c, n: RiskDecision(False, "daily loss stop"))
    r = Rig(tmp_path, con, gate=deny)
    r.run(et(9, 52))
    assert not r.pos("live") and len(r.pos("shadow")) == 4 and not r.broker.submit_calls
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
        r2.broker.mult[C601N] = 0.6
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


def _plan(level=601.5, plan_id="gp-20261013-0800", dte="0dte", avoid=None, playbook="orb", events=(), **extra):
    return json.dumps({
        "protocol": "1", "type": "OPTIONS_PLAN", "plan_id": plan_id, "for_date": str(DAY),
        "generated_at": "2026-10-13T07:45:00-04:00",
        "market_context": {"summary": "", "risk_tone": "mixed",
                           "key_events": [{"time": t, "what": "event"} for t in events]},
        "plans": [{"underlying": "SPY", "bias": "long", "playbook": playbook, "thesis": "t", "catalyst": "",
                   "trigger": {"kind": "break_above", "level": level, "confirm_closes": 1, "earliest": "09:35",
                               "latest": "11:00"},
                   "invalidation": {"kind": "below", "underlying_level": 599.0}, "target_underlying_pct": 1,
                   "max_hold_minutes": 45, "preferred_dte": dte, "confidence": 0.5, "evidence_urls": [], **extra}],
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
    assert book.position(C601N).qty == lp["qty_initial"] and book.position(C601N).strategy == "options:orb15"
    assert r.gate.engine._reserved == {}
    r.eng.tick()  # re-resolving the same fills books nothing twice
    assert con.execute("SELECT COUNT(*) FROM options_book_fills").fetchone()[0] == 1
    r.broker.mult[C601N] = 0.6
    r.run(et(9, 54))
    assert r.pos("live")[0]["exit_reason"] == "premium_stop"
    assert book.position(C601N) is None and not r.broker.positions


def test_real_line_a_gate_refuses_a_stale_quote(tmp_path, con):
    from stockmoney.desk.options import _iface
    from stockmoney.desk.options._iface import OrderIntent, TIF
    r = Rig(tmp_path, con)
    gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.clock.set(et(10, 0))
    r.broker.quote_age_s = r.cfg.max_quote_age_s + 5
    q = r.broker.option_quote(C601N)
    intent = OrderIntent("t1", C601N, AssetClass.OPTION, Side.BUY, qty=1, order_type=OrderType.LIMIT, tif=TIF.DAY,
                         limit_price=q.ask)
    dec = gate.check(intent, q, r.clock.now)
    assert not dec.allow and "old" in dec.reason
    r.broker.quote_age_s = 1
    assert gate.check(intent, r.broker.option_quote(C601N), r.clock.now).allow


def _filtered(con, reason):
    return con.execute("SELECT COUNT(*) FROM options_filtered WHERE reason=?", (reason,)).fetchone()[0]


def test_live_never_uses_a_contract_expiring_today(tmp_path, con):
    r = Rig(tmp_path, con)
    r.broker.templates = chain()  # only today's expiry
    r.run(et(9, 52))
    assert not r.pos("live") and {p["dte_label"] for p in r.pos("shadow")} == {"0dte"}
    assert _filtered(con, "no_live_contract") == 1
    with store.connect(tmp_path / "z.sqlite") as con2:  # even if the config pointed live at 0DTE
        r2 = Rig(tmp_path, con2, live_dte="0dte")
        r2.run(et(9, 52))
        assert not r2.pos("live") and _filtered(con2, "expires_today") == 1


def test_unknown_rvol_is_shadow_only(tmp_path, con):
    r = Rig(tmp_path, con, history=False)  # no previous sessions: RVOL unknown
    r.run(et(9, 52))
    assert not r.pos("live") and len(r.pos("shadow")) == 4
    assert _filtered(con, "rvol_unknown") == 1
    s = next(s for s in ostore.setups(con, str(DAY)) if s["source"] == "orb15")
    assert s["rvol"] is None and json.loads(s["signal_json"])["meta"]["day_rvol"] is None


def test_shadow_only_playbooks(tmp_path, con):
    r = Rig(tmp_path, con)
    r.run(et(9, 26))
    notes = {s["source"]: (s["live_eligible"], s["live_note"]) for s in ostore.setups(con, str(DAY))}
    assert notes["vwap_reclaim"] == (0, "source not live-eligible")
    assert notes["gap_fade"] == (0, "source not live-eligible")
    assert notes["orb15"] == (1, "") and notes["gap_go"] == (1, "")


def test_grok_live_needs_gap_go_or_orb_playbook(tmp_path, con):
    oplan.ingest(con, _plan(playbook="momentum"), et(8, 0))
    r = Rig(tmp_path, con, max_live_per_underlying=2)
    r.run(et(10, 5))
    g = r.pos(source="grok_plan")
    assert g and all(p["mode"] == "shadow" for p in g)
    assert _filtered(con, "grok_playbook_shadow_only") == 1


def test_grok_plan_min_rvol_raises_the_live_floor(tmp_path, con):
    oplan.ingest(con, _plan(min_rvol=5.0, regime="trend", daily_ref="event 2"), et(8, 0))
    r = Rig(tmp_path, con, max_live_per_underlying=2)
    r.run(et(10, 5))
    g = r.pos(source="grok_plan")
    assert g and all(p["mode"] == "shadow" for p in g)
    assert _filtered(con, "rvol_below_live_floor") == 1
    s = next(s for s in ostore.setups(con, str(DAY)) if s["source"] == "grok_plan")
    assert 1.5 <= s["rvol"] < 5.0


def test_macro_event_blocks_live_not_shadow(tmp_path, con):
    oplan.ingest(con, _plan(level=650.0, events=("10:15",)), et(8, 0))  # orb15 triggers 09:51: within 30 min
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    assert not r.pos("live") and len(r.pos("shadow")) == 4
    assert _filtered(con, "macro_event_window") == 1
    s = next(s for s in ostore.setups(con, str(DAY)) if s["source"] == "orb15")
    reg = json.loads(s["regime_json"])
    assert reg["macro_event_within_30m"] is True and reg["macro_events"] == ["10:15 event"]
    with store.connect(tmp_path / "m.sqlite") as con2:  # the config list counts too
        r2 = Rig(tmp_path, con2, macro_events=((str(DAY), "09:30"),))
        r2.run(et(9, 52))
        assert not r2.pos("live") and _filtered(con2, "macro_event_window") == 1
    with store.connect(tmp_path / "n.sqlite") as con3:  # 31 minutes away: allowed
        r3 = Rig(tmp_path, con3, macro_events=((str(DAY), "09:20"),))
        r3.run(et(9, 52))
        assert r3.pos("live")


def test_regime_tags_stored_per_setup(tmp_path, con):
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    s = next(s for s in ostore.setups(con, str(DAY)) if s["source"] == "orb15")
    reg = json.loads(s["regime_json"])
    assert set(reg) >= {"spy_premarket_rvol", "spy_first15_range_pct", "vix_prev_close", "macro_event_within_30m",
                        "consecutive_loss_days"}
    assert reg["vix_prev_close"] is None and reg["consecutive_loss_days"] == 0
    assert reg["spy_first15_range_pct"] == pytest.approx((600.95 - 599.95) / 600.0, abs=1e-5)
    assert reg["spy_premarket_rvol"] == pytest.approx(90 * 50_000 / 30_000, rel=1e-3)
    assert s["rvol"] == pytest.approx(2.4e6 / 1.05e6, abs=1e-3)


def test_consecutive_loss_days_counts_losing_live_variant_days(tmp_path, con):
    ostore.ensure_schema(con)
    for day, net in (("2026-10-08", -50.0), ("2026-10-09", -20.0), ("2026-10-12", -10.0), ("2026-10-07", 30.0)):
        ostore.insert_position(con, {"position_id": f"{day}:x", "setup_id": f"{day}:s", "day": day, "mode": "shadow",
                                     "state": "EXITED", "dte_label": "1-3dte", "exit_variant": "A",
                                     "realized_usd": net, "fees_usd": 0.0})
    r = Rig(tmp_path, con)
    r.run(et(9, 52))
    s = next(s for s in ostore.setups(con, str(DAY)) if s["source"] == "orb15")
    assert json.loads(s["regime_json"])["consecutive_loss_days"] == 3
    con.execute("DELETE FROM options_positions WHERE day='2026-10-09'")  # a session without trades ends the run
    r.eng._loss_days = None
    assert r.eng._consecutive_loss_days() == 1


def test_two_live_entries_in_one_tick_pass_the_real_gate(tmp_path, con):
    """The first entry's fill is booked right after its submit, so line A's
    book / broker check does not refuse the second entry of the same tick."""
    from stockmoney.desk.options import _iface
    r = Rig(tmp_path, con, index=("SPY", "QQQ"))
    r.broker.bars["QQQ"] = r.broker.bars["SPY"]
    r.broker.templates.update({**chain("QQQ"), **chain("QQQ", expiry=NEXT)})
    r.gate = _iface.make_risk_gate(con, r.broker, r.cfg)
    r.eng = r.new_engine()
    r.run(et(9, 52))
    live = [p for p in ostore.positions(con, str(DAY), "live")]
    assert {p["underlying"] for p in live} == {"SPY", "QQQ"} and all(p["state"] == "ENTERED" for p in live)
    assert _filtered(con, "risk_gate") == 0
