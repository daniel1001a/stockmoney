"""vwap_reclaim debit-spread shadow variant and stressed-stop flags, driven
through the engine Rig (FakeOptionsBroker, no real orders). The fixture day is
an orb15 breakout, so the variant's source list is pointed at orb15 here."""
import json

import pytest

from stockmoney.desk.options import shadow as sh
from stockmoney.desk.options import spread_variant as sv
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options._iface import occ_symbol
from stockmoney.desk.options.config import OptionsConfig

from opt_fakes import DAY, NEXT, et
from test_opt_engine import C601, C601N, Rig, con  # noqa: F401  (con is a fixture)

C602 = occ_symbol("SPY", DAY, "C", 602)
C602N = occ_symbol("SPY", NEXT, "C", 602)


@pytest.fixture
def rig(tmp_path, con, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sv, "SOURCES", ("orb15",))
    return Rig(tmp_path, con)


def spread(con, occ):  # noqa: F811
    return next(dict(r) for r in con.execute("SELECT * FROM options_spread_variants WHERE long_occ=?", (occ,)))


def test_spread_opens_with_short_nearest_030_delta(rig, con):  # noqa: F811
    rig.run(et(9, 52))
    rows = [dict(r) for r in con.execute("SELECT * FROM options_spread_variants")]
    assert {(r["long_occ"], r["short_occ"]) for r in rows} == {(C601, C602), (C601N, C602N)}  # 602 is d0.35, 604 d0.20
    s = spread(con, C601)
    assert s["variant"] == "vwap_reclaim_spread" and s["mode"] == "shadow" and s["status"] == "OPEN"
    assert s["position_id"].endswith(":shadow:0dte:A") and s["short_delta"] == pytest.approx(0.35)
    # long pays its ask 0.97, short receives its bid 0.83 (mid 0.85)
    assert s["long_price"] == 0.97 and s["short_price"] == pytest.approx(0.83)
    assert s["entry_debit"] == pytest.approx(0.14) and s["width"] == 1 and s["qty"] == 71  # floor(1000 / 14)
    assert s["fees_usd"] == pytest.approx(2 * 0.03 * 71)


def test_spread_mirrors_take_profit_and_stop_of_the_single_leg(rig, con):  # noqa: F811
    rig.run(et(9, 52))
    rig.broker.mult[C601] = 1.5  # single leg sells half at bid 1.40
    rig.run(et(9, 54))
    s = spread(con, C601)
    assert s["status"] == "OPEN" and s["open_frac"] == pytest.approx(0.5)
    assert s["realized_usd"] == pytest.approx((1.40 - 0.87 - 0.14) * 35.5 * 100)  # short bought back at ask 0.87
    rig.broker.mult[C601] = 1.0  # rest stops at cost, bid 0.93
    rig.run(et(9, 56))
    s = spread(con, C601)
    leg = ostore.position(con, s["position_id"])
    assert leg["state"] == "EXITED" and s["status"] == "EXITED" and s["exit_reason"] == leg["exit_reason"]
    realized = (1.40 - 0.87 - 0.14) * 35.5 * 100 + (0.93 - 0.87 - 0.14) * 35.5 * 100
    fees = 2 * 0.03 * 71 * 2
    assert s["realized_usd"] == pytest.approx(realized) and s["pnl_usd"] == pytest.approx(realized - fees)


def test_spread_closes_by_the_flat_and_finish(rig, con, tmp_path):  # noqa: F811
    rig.cfg = OptionsConfig(**{**rig.cfg.__dict__, "time_stop_minutes": 600})
    rig.eng = rig.new_engine()
    rig.run(et(15, 47), step=60)
    assert rig.eng.finish() is True
    assert all(r["status"] == "EXITED" and r["pnl_usd"] is not None
               for r in con.execute("SELECT * FROM options_spread_variants"))


def test_spread_failure_never_touches_the_single_leg(rig, con, monkeypatch):  # noqa: F811
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(sv, "pick_short", boom)
    rig.run(et(9, 52))
    assert len(rig.pos("shadow")) == 4 and len(rig.pos("live")) == 1
    assert con.execute("SELECT COUNT(*) FROM options_spread_variants").fetchone()[0] == 0


def test_stressed_flags_on_every_position(rig, con):  # noqa: F811
    rig.run(et(9, 52))
    for p in rig.pos():
        st = json.loads(p["flags_json"])["stressed"]
        stop = 0.30 if p["exit_variant"] == "B" else 0.35
        # entry 0.97 (shadow fill / live limit), bid 0.93 ask 0.97, fees 0.03 x 2 legs
        assert st["cost"] == pytest.approx(0.97 * stop * 100 + 2 + 0.06) and st["budget"] == 1000
        assert st["qty"] == (27 if stop == 0.35 else 32) and st["actual_qty"] == 10


def test_further_otm_put_side():
    from stockmoney.desk.options._iface import OptionContract
    from datetime import date
    mk = lambda k: OptionContract(occ_symbol("SPY", date(2026, 11, 6), "P", k), "SPY", "P", float(k),  # noqa: E731
                                  date(2026, 11, 6), 1.0, 1.1)
    got = sv.further_otm([mk(k) for k in (596, 598, 600, 602)], mk(600))
    assert [c.strike for c in got] == [598, 596]
    assert sv.pick_short([mk(598), mk(596)], mk(600), 600.0, et(10, 0)) is not None
    assert sh.contracts_for(0.14, OptionsConfig()) == 71
