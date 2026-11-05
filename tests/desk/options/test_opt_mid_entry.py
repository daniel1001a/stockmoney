"""Mid-price entry shadow (options/mid_entry.py, owner 2026-11-05): unit rules
for resolve, plus engine runs on the Rig (FakeOptionsBroker, no real orders).
Fixture C601: template mid 0.95, half spread 0.02 -> bid 0.93 / ask 0.97."""
from datetime import timedelta

import pytest

from stockmoney.desk.options import mid_entry as me
from stockmoney.desk.options import report
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options._iface import OptionContract
from stockmoney.desk.options.config import OptionsConfig

from opt_fakes import DAY, et
from test_opt_engine import C601, Rig, con  # noqa: F401  (con is a fixture)

CFG = OptionsConfig()


def _q(bid, ask):
    return OptionContract(C601, "SPY", "C", 601.0, DAY, bid, ask, (bid + ask) / 2, 1000, 1000, 0.5, 0.2, et(9, 40))


def _row(con, t0):  # noqa: F811
    ostore.ensure_schema(con)
    linked = {"position_id": "orb15:shadow:0dte:A", "day": str(DAY), "source": "orb15", "underlying": "SPY",
              "uclass": "index", "dte_label": "0dte"}
    assert me.open_row(con, linked, _q(0.93, 0.97), t0) is True
    return me.get(con, linked["position_id"])


def test_mid_limit():
    assert me.mid_limit(0.93, 0.97) == 0.95
    assert me.mid_limit(None, 0.97) is None and me.mid_limit(0.0, 0.1) is None and me.mid_limit(1.0, 0.9) is None


def test_filled_when_ask_touches_the_mid(con):  # noqa: F811
    t0 = et(9, 40)
    row = _row(con, t0)
    assert row["mid_limit"] == 0.95 and row["status"] == me.PENDING
    assert me.resolve(con, CFG, row, _q(0.93, 0.95), True, t0) == me.PENDING  # same tick as entry: waited 0
    assert me.resolve(con, CFG, row, _q(0.90, 0.95), True, t0 + timedelta(seconds=20)) == me.FILLED
    r = me.get(con, row["position_id"])
    assert r["fill_price"] == 0.95 and r["saved_per_contract"] == pytest.approx(0.02) and r["waited_s"] == 20


def test_stale_quote_never_fills(con):  # noqa: F811
    t0 = et(9, 40)
    row = _row(con, t0)
    assert me.resolve(con, CFG, row, _q(0.90, 0.92), False, t0 + timedelta(seconds=20)) == me.PENDING


def test_fallback_at_the_ask_after_the_wait(con):  # noqa: F811
    t0 = et(9, 40)
    row = _row(con, t0)
    assert me.resolve(con, CFG, row, _q(0.93, 0.97), True, t0 + timedelta(seconds=40)) == me.PENDING
    assert me.resolve(con, CFG, row, _q(0.96, 1.00), True, t0 + timedelta(seconds=60)) == me.FALLBACK
    r = me.get(con, row["position_id"])
    assert r["fill_price"] == 1.00 and r["saved_per_contract"] == pytest.approx(-0.03)


def test_touch_after_the_wait_is_a_fallback_at_that_ask(con):  # noqa: F811
    t0 = et(9, 40)
    row = _row(con, t0)
    assert me.resolve(con, CFG, row, _q(0.90, 0.94), False, t0 + timedelta(seconds=30)) == me.PENDING  # stale
    assert me.resolve(con, CFG, row, _q(0.90, 0.94), True, t0 + timedelta(seconds=80)) == me.FALLBACK
    r = me.get(con, row["position_id"])
    assert r["fill_price"] == 0.94 and r["saved_per_contract"] == pytest.approx(0.03)


def test_no_quote_after_twice_the_wait_or_exit(con):  # noqa: F811
    t0 = et(9, 40)
    row = _row(con, t0)
    assert me.resolve(con, CFG, row, None, False, t0 + timedelta(seconds=119)) == me.PENDING
    assert me.resolve(con, CFG, row, None, False, t0 + timedelta(seconds=120)) == me.NO_QUOTE
    assert me.get(con, row["position_id"])["fill_price"] is None
    con.execute("DELETE FROM options_mid_entry")
    row = _row(con, t0)
    assert me.resolve(con, CFG, row, None, False, t0 + timedelta(seconds=5), exited=True) == me.NO_QUOTE


def test_resolved_row_is_final_and_open_is_once(con):  # noqa: F811
    t0 = et(9, 40)
    row = _row(con, t0)
    me.resolve(con, CFG, row, _q(0.90, 0.94), True, t0 + timedelta(seconds=20))
    r = me.get(con, row["position_id"])
    assert me.resolve(con, CFG, r, _q(0.99, 1.01), True, t0 + timedelta(seconds=90)) == me.FILLED
    assert me.open_row(con, {"position_id": row["position_id"], "day": str(DAY), "source": "orb15",
                             "underlying": "SPY", "uclass": "index", "dte_label": "0dte"},
                       _q(0.93, 0.97), t0) is False


@pytest.fixture
def rig(tmp_path, con):  # noqa: F811
    return Rig(tmp_path, con)


def _rows(con):  # noqa: F811
    return [dict(r) for r in con.execute("SELECT * FROM options_mid_entry ORDER BY position_id")]


def _until_entry(rig, con):  # noqa: F811
    for _ in range(200):
        rig.eng.tick()
        rig.clock.advance(20)
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='options_mid_entry'").fetchone() and \
                con.execute("SELECT 1 FROM options_mid_entry WHERE occ=?", (C601,)).fetchone():
            return con.execute("SELECT position_id FROM options_mid_entry WHERE occ=?", (C601,)).fetchone()[0]
    raise AssertionError("no entry")


def test_engine_records_live_exit_shadow_entries_only(rig, con):  # noqa: F811
    rig.run(et(9, 52))
    rows = _rows(con)
    assert rows and all(r["position_id"].endswith(":" + rig.cfg.live_exit) for r in rows)
    assert all(":shadow:" in r["position_id"] for r in rows)
    r = next(r for r in rows if r["occ"] == C601)
    assert r["signal_bid"] == 0.93 and r["signal_ask"] == 0.97 and r["mid_limit"] == 0.95
    assert r["uclass"] and r["source"] == "orb15"


def test_engine_fills_when_the_ask_drops_and_sends_no_extra_order(rig, con):  # noqa: F811
    pid = _until_entry(rig, con)
    n_orders = len(rig.orders())
    rig.broker.mult[C601] = 0.97  # mid 0.9215 -> ask 0.94 <= limit 0.95, inside the 60s wait
    rig.run(rig.clock.now + timedelta(seconds=60))
    r = me.get(con, pid)
    assert r["status"] == me.FILLED and r["fill_price"] == 0.95
    assert n_orders >= 0 and all(":live:" in o["position_id"] for o in rig.orders())  # the variant never submits


def test_engine_fallback_and_finish_leave_nothing_pending(rig, con):  # noqa: F811
    rig.cfg = OptionsConfig(**{**rig.cfg.__dict__, "time_stop_minutes": 600})
    rig.eng = rig.new_engine()
    rig.run(et(15, 47), step=60)
    assert rig.eng.finish() is True
    rows = _rows(con)
    assert rows and all(r["status"] != me.PENDING for r in rows)
    assert any(r["status"] == me.FALLBACK for r in rows)


def test_failure_never_blocks_the_entry(rig, con, monkeypatch):  # noqa: F811
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(me, "open_row", boom)
    rig.run(et(9, 52))
    assert len(rig.pos("shadow")) == 4 and len(rig.pos("live")) == 1
    assert con.execute("SELECT COUNT(*) FROM options_mid_entry").fetchone()[0] == 0


def test_summary_and_report_section(rig, con):  # noqa: F811
    _until_entry(rig, con)
    rig.broker.mult[C601] = 0.97
    rig.run(rig.clock.now + timedelta(seconds=60))
    s = me.summary(con)
    assert s and all(not x["ready"] for x in s)
    text = report.build(con, str(DAY))
    assert "中價進場影子" in text and "不足" in text
