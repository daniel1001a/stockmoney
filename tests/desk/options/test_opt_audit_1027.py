"""2026-10-27 audit, stop-the-bleeding step 1: orb15 out of live (orb15_5m
shadow-only), exits triggered on the bid, the exit latch, market stops and
one take_profit limit, the slippage report, the param_changes rows. All
through FakeOptionsBroker (no real orders)."""
import json
from datetime import timedelta

import pytest

from stockmoney.desk import store
from stockmoney.desk.options import report as orep
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options._iface import OrderStatus, OrderType, Side, occ_symbol
from stockmoney.desk.options.config import RULE_PLAYBOOKS, SHADOW_ONLY_SOURCES, OptionsConfig

from opt_fakes import DAY, NEXT, et
from test_opt_engine import Rig

C601N = occ_symbol("SPY", NEXT, "C", 601)  # the live contract (entry 0.97 = mid 0.95 + half spread)


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        yield c


def entered(tmp_path, con, **kw):
    r = Rig(tmp_path, con, **kw)
    r.run(et(9, 52))
    lp = r.pos("live")[0]
    assert lp["state"] == "ENTERED" and lp["entry_price"] == pytest.approx(0.97) and r.broker.positions[C601N] == 10
    return r, lp


def exits(r, pid):
    return [o for o in ostore.orders_for(r.con, pid) if o["kind"] == "exit"]


def stick_first_sell(r):
    """The next sell sent stays NEW at the broker (never fills)."""
    real, first = r.broker.submit, []

    def submit(intent):
        if intent.side == Side.SELL and not first:
            first.append(intent)
            r.broker.stuck.add(intent.client_order_id)
        return real(intent)
    r.broker.submit = submit
    return first


# ------------------------------------------------------------------ orb15 out of live
def test_production_live_sources_exclude_orb15_and_orb15_5m_is_shadow_only(tmp_path, con):
    cfg = OptionsConfig()
    assert "orb15" not in cfg.live_sources and set(cfg.live_sources) == {"gap_go", "grok_plan"}
    assert "orb15_5m" in RULE_PLAYBOOKS and "orb15_5m" in SHADOW_ONLY_SOURCES
    assert dict(cfg.close_inv_minutes)["orb15_5m"] == 5
    r = Rig(tmp_path, con, live_sources=cfg.live_sources)
    r.run(et(9, 52))
    assert r.pos("shadow") and not r.pos("live")  # the orb15 breakout trades in shadow only
    assert r.pos("shadow", source="orb15_5m") and not r.pos("live", source="orb15_5m")
    assert not any(o.side == Side.BUY for o in r.broker.orders.values())


def test_orb15_5m_never_live_even_if_listed(tmp_path, con):
    r = Rig(tmp_path, con, live_sources=("orb15_5m",))
    assert r.eng._live_note("orb15_5m", "SPY", False, set()) == "shadow-only variant"
    r.run(et(9, 52))
    assert r.pos("shadow", source="orb15_5m") and not r.pos("live", source="orb15_5m")


# ------------------------------------------------------------------ stops on the bid, market
def test_premium_stop_triggers_on_the_bid_and_sells_at_market(tmp_path, con):
    r, lp = entered(tmp_path, con)
    r.broker.mult[C601N] = 0.67  # mid 0.6365 above the -35% line (0.6305), bid ~0.6165 below it
    q = r.broker.option_quote(C601N)
    assert q.mid > 0.97 * 0.65 > q.bid
    r.run(et(9, 53), step=5)
    ex = exits(r, lp["position_id"])
    assert ex and ex[0]["reason"] == "premium_stop" and ex[0]["order_type"] == "market"
    p = ostore.position(con, lp["position_id"])
    assert p["state"] == "EXITED" and p["exit_reason"] == "premium_stop" and p["pending_exit"] is None
    assert C601N not in r.broker.positions


# ------------------------------------------------------------------ take_profit: one limit, then market
def test_take_profit_sends_one_limit_under_the_bid_then_market(tmp_path, con):
    r, lp = entered(tmp_path, con)
    r.broker.mult[C601N] = 1.5  # mid 1.425, bid 1.405: over +40% (1.358) on the bid
    bid = r.broker.option_quote(C601N).bid
    stick_first_sell(r)
    r.eng.tick()
    ex = exits(r, lp["position_id"])
    assert len(ex) == 1 and ex[0]["reason"] == "take_profit" and ex[0]["order_type"] == "limit"
    assert ex[0]["limit_price"] == pytest.approx(round(bid - 0.01, 2)) and ex[0]["qty"] == 5
    r.run(r.clock.now + timedelta(seconds=30), step=2)
    ex = exits(r, lp["position_id"])
    assert [o["order_type"] for o in ex] == ["limit", "market"]
    assert r.broker.orders[ex[0]["client_order_id"]].status == OrderStatus.CANCELED
    assert r.broker.positions[C601N] == 5
    p = ostore.position(con, lp["position_id"])
    assert p["tp_done"] == 1 and p["qty_open"] == 5 and p["pending_exit"] is None


def test_latched_take_profit_survives_a_flicker(tmp_path, con):
    r, lp = entered(tmp_path, con)
    r.broker.mult[C601N] = 1.5
    stick_first_sell(r)
    r.eng.tick()
    held = json.loads(ostore.position(con, lp["position_id"])["pending_exit"])
    assert held["reason"] == "take_profit" and held["qty"] == 5
    r.broker.mult[C601N] = 1.2  # bid 1.12: the trigger is gone, the latch is not
    r.run(r.clock.now + timedelta(seconds=30), step=2)
    ex = exits(r, lp["position_id"])
    assert [(o["order_type"], o["reason"]) for o in ex] == [("limit", "take_profit"), ("market", "take_profit")]
    assert r.broker.positions[C601N] == 5
    assert ostore.position(con, lp["position_id"])["pending_exit"] is None


def test_take_profit_limit_working_at_the_flat_is_replaced_by_market(tmp_path, con):
    r, lp = entered(tmp_path, con, time_stop_minutes=600)
    r.run(et(15, 44), step=60)
    r.clock.set(et(15, 44, 50))
    r.broker.mult[C601N] = 1.5
    stick_first_sell(r)
    r.eng.tick()
    assert [o["order_type"] for o in exits(r, lp["position_id"])] == ["limit"]
    r.clock.set(et(15, 45))
    r.run(et(15, 45, 10), step=2)
    ex = exits(r, lp["position_id"])
    assert [o["order_type"] for o in ex] == ["limit", "market"] and ex[1]["reason"] == "force_flat"
    assert C601N not in r.broker.positions
    assert ostore.position(con, lp["position_id"])["state"] == "EXITED"


# ------------------------------------------------------------------ slippage report (read only)
def _pos(con, pid, source, entry, qty, mode="live"):
    con.execute("INSERT INTO options_positions (position_id, day, mode, source, underlying, entry_price, qty_initial)"
                " VALUES (?,?,?,?,?,?,?)", (pid, str(DAY), mode, source, "SPY", entry, qty))


def _ord(con, cid, pid, kind, side, otype, qty, px, bid, ask, at):
    con.execute("INSERT INTO options_orders (client_order_id, position_id, kind, side, order_type, reason, status,"
                " filled_qty, avg_price, ref_bid, ref_ask, submitted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, pid, kind, side, otype, kind, "filled", qty, px, bid, ask, at))


def test_slippage_numbers_match_the_fixture(con):
    ostore.ensure_schema(con)
    _pos(con, "P1", "orb15", 1.00, 10)
    _ord(con, "P1-e", "P1", "entry", "buy", "limit", 10, 1.00, 0.90, 1.00, "2026-10-27T09:50:00")   # mid 0.95
    _ord(con, "P1-x0", "P1", "exit", "sell", "market", 10, 0.60, 0.62, 0.70, "2026-10-27T10:10:00")  # mid 0.66
    _pos(con, "P2", "gap_go", 2.00, 5)
    _ord(con, "P2-e", "P2", "entry", "buy", "limit", 5, 2.00, None, None, "2026-10-27T09:40:00")  # no quote
    _pos(con, "S1", "orb15", 1.00, 10, mode="shadow")
    _ord(con, "S1-e", "S1", "entry", "buy", "limit", 10, 1.50, 0.90, 1.00, "2026-10-27T09:50:00")  # not live
    _ord(con, "P1-x1", "P1", "exit", "sell", "limit", 0, None, 0.62, 0.70, "2026-10-27T10:09:00")  # never filled
    rows, by_pb = orep.slippage(con, str(DAY), 100)
    assert [r["client_order_id"] for r in rows] == ["P1-e", "P1-x0"]
    e, x = rows
    assert e["mid"] == pytest.approx(0.95) and e["slip_pct"] == pytest.approx(0.05 / 0.95)
    assert e["slip_usd"] == pytest.approx(50.0)
    assert x["mid"] == pytest.approx(0.66) and x["slip_pct"] == pytest.approx(0.06 / 0.66)
    assert x["slip_usd"] == pytest.approx(60.0)
    o = by_pb["orb15"]
    assert (o["fills"], o["unmeasured"]) == (2, 0)
    assert o["slip_usd"] == pytest.approx(110.0) and o["premium_usd"] == pytest.approx(1000.0)
    assert o["cost_pct"] == pytest.approx(0.11)
    g = by_pb["gap_go"]
    assert (g["fills"], g["unmeasured"], g["cost_pct"]) == (1, 1, None)
    text = "\n".join(orep._slippage_section(con, str(DAY), 100))
    assert "| orb15 | 2 | 0 | +110 | 1000 | +11.0% |" in text


# ------------------------------------------------------------------ param_changes
def test_param_changes_written_once(con):
    ostore.ensure_schema(con)
    assert ostore.record_param_changes(con) == len(ostore.PARAM_CHANGES) == 8
    assert ostore.record_param_changes(con) == 0
    rows = con.execute("SELECT param, old, new, reason FROM param_changes WHERE strategy='options'").fetchall()
    assert len(rows) == 8 and {r[3] for r in rows} == {"2026-10-27 audit: stop-the-bleeding step 1"}
    got = {r[0]: (json.loads(r[1]), json.loads(r[2])) for r in rows}
    assert got["live_sources"] == (["gap_go", "orb15", "grok_plan"], ["gap_go", "grok_plan"])
    assert got["exit_max_limit_attempts"] == (3, 1)


def test_restart_after_live_sources_change_keeps_frozen_setup_out_of_live(tmp_path, con):
    """live_eligible is written on the setup row; a restart with orb15 removed
    from live_sources must not open live from that stale row (Grok review)."""
    import dataclasses
    r = Rig(tmp_path, con, live_sources=("orb15",))
    r.run(et(9, 46))
    setup = next(s for s in ostore.setups(con, str(DAY)) if s["source"] == "orb15")
    assert setup["live_eligible"] and not r.pos("live")
    r.cfg = dataclasses.replace(r.cfg, live_sources=("gap_go", "grok_plan"))
    r.eng = r.new_engine()
    r.run(et(9, 52))
    assert r.pos("shadow") and not r.pos("live")
    assert not any(o.side == Side.BUY for o in r.broker.orders.values())


@pytest.mark.parametrize("change", ["untrusted", "shadow_only"])
def test_restart_after_plan_loses_trust_keeps_frozen_grok_setup_out_of_live(tmp_path, con, change):
    """grok_plan setups built under a plan that is no longer the trusted plan in
    force (backfilled grok_legacy, or a verified resend after the cutoff) must
    not open live after a same-day restart (Grok review)."""
    from stockmoney.desk.options import plan as oplan
    from test_opt_engine import _plan
    oplan.ingest(con, _plan(), et(8, 0))
    r = Rig(tmp_path, con, max_live_per_underlying=2)
    r.run(et(9, 40))
    setup = next(s for s in ostore.setups(con, str(DAY)) if s["source"] == "grok_plan")
    assert setup["live_eligible"] and setup["state"] == "WAITING"
    if change == "untrusted":
        con.execute("UPDATE options_plans SET source='grok_legacy'")
    else:
        con.execute("UPDATE options_plans SET shadow_only=1")
    con.commit()
    r.eng = r.new_engine()
    r.run(et(10, 50))
    assert not [p for p in r.pos(source="grok_plan") if p["mode"] == "live"]
    if change == "untrusted":
        s = next(s for s in ostore.setups(con, str(DAY)) if s["source"] == "grok_plan")
        assert s["state"] == "SUPERSEDED" and not r.pos(source="grok_plan")
