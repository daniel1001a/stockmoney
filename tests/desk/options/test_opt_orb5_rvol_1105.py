"""2026-11-05 owner approval (EDGE_PLAN 7.4 item 4): the stocks-in-play scan
list and the shadow-only orb5_rvol playbook. All through FakeOptionsBroker
(no real orders)."""
import json

import pytest

from stockmoney.desk import store
from stockmoney.desk.options import playbooks as pb
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options.config import (RULE_PLAYBOOKS, SCAN_CANDIDATES, SHADOW_ONLY_SOURCES,
                                            OptionsConfig)

from opt_fakes import DAY, day_bars, et, profile, with_history
from test_opt_engine import Rig

CFG = OptionsConfig()
UP5 = [600.2, 600.4, 600.3, 600.5, 600.6]      # 09:30 open 600.0, 09:34 close 600.6: bar up, high 600.65
DOWN5 = [599.8, 599.6, 599.7, 599.5, 599.4]    # bar down, low 599.35


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        yield c


def ctx(top=("SPY",), prof=None):
    return {"orb5_top": {s: {"rank": i + 1, "rvol5": 2.0} for i, s in enumerate(top)},
            "profile": prof if prof is not None else profile()}


def sig(closes, now, **kw):
    return pb.orb5_rvol(day_bars(closes), now, 600.0, "SPY", CFG, ctx(**kw))


# ------------------------------------------------------------------ playbook
def test_profile_has_first5_mean_and_old_json_still_loads():
    p = profile()
    assert p.first5_mean == pytest.approx(5 * 50_000) and p.atr_daily == pytest.approx(1.0, abs=0.01)
    d = dict(p.to_json())
    assert pb.VolProfile.from_json(d).first5_mean == p.first5_mean
    d.pop("first5_mean")  # a profile stored before 2026-11-05
    assert pb.VolProfile.from_json(d).first5_mean is None


def test_bull_bar_breaks_high_long_with_tenth_atr_stop():
    s = sig(UP5 + [600.5, 600.9], et(9, 37))
    assert s is not None and s.source == "orb5_rvol" and s.direction == "long"
    assert s.at == et(9, 37) and s.price == pytest.approx(600.9)
    assert s.meta["range_hi"] == pytest.approx(600.65)
    assert s.invalidation == pytest.approx(600.65 - 0.1 * s.meta["atr_daily"])


def test_bear_bar_breaks_low_short():
    s = sig(DOWN5 + [599.5, 599.1], et(9, 37))
    assert s is not None and s.direction == "short"
    assert s.invalidation == pytest.approx(599.35 + 0.1 * s.meta["atr_daily"])


def test_doji_bar_no_trade():
    assert sig([600.2, 600.4, 599.8, 600.1, 600.0, 601.5, 602.0], et(9, 38)) is None


def test_not_in_top_or_no_atr_no_trade():
    assert sig(UP5 + [600.5, 600.9], et(9, 37), top=("QQQ",)) is None
    p = profile()
    no_atr = pb.VolProfile(p.sessions, p.cum, p.pm_mean, None, p.first5_mean)
    assert sig(UP5 + [600.5, 600.9], et(9, 37), prof=no_atr) is None


def test_no_look_ahead():
    closes = UP5 + [600.5, 600.9]
    assert sig(closes, et(9, 36)) is None  # the 09:36 breakout bar is not complete yet
    assert sig(UP5, et(9, 34)) is None     # the opening 5-minute bar is not complete yet
    assert pb.first5_volume(day_bars(UP5), et(9, 34), CFG) is None
    assert pb.first5_volume(day_bars(UP5), et(9, 35), CFG) == pytest.approx(5 * 100_000)


# ------------------------------------------------------------------ config and audit
def test_orb5_is_a_shadow_only_rule_and_scan_list_is_new_names():
    assert "orb5_rvol" in RULE_PLAYBOOKS and "orb5_rvol" in SHADOW_ONLY_SOURCES
    assert "orb5_rvol" not in CFG.live_sources
    sc = CFG.scan_candidates
    assert sc == SCAN_CANDIDATES and 25 <= len(sc) <= 35 and len(set(sc)) == len(sc)
    assert not set(sc) & set(CFG.starting_universe())


def test_1105_param_rows_idempotent(con):
    ostore.ensure_schema(con)
    ostore.record_param_changes(con)
    assert ostore.record_param_changes(con) == 0
    rows = con.execute("SELECT param, new FROM param_changes WHERE reason=?", (ostore.AUDIT_20261105,)).fetchall()
    got = {r[0]: json.loads(r[1]) for r in rows}
    assert len(rows) == 9 and got["orb5_top_n"] == 10 and got["scan_candidates"] == list(SCAN_CANDIDATES)
    assert got["shadow_only_sources"] == ["orb15_5m", "orb5_rvol"]


# ------------------------------------------------------------------ engine
def events(con, kind):
    return [dict(r) for r in con.execute("SELECT * FROM options_events WHERE kind=? ORDER BY id", (kind,))]


def test_orb5_shadow_never_live_even_if_listed(tmp_path, con):
    r = Rig(tmp_path, con, scan_candidates=(), live_sources=("gap_go", "orb15", "grok_plan", "orb5_rvol"))
    r.run(et(9, 55))
    top = json.loads(events(con, "orb5_rank")[0]["message"])["top"]
    assert top == {"SPY": {"rank": 1, "rvol5": 2.0}}
    shadow = r.pos("shadow", source="orb5_rvol")
    assert shadow and all(p["state"] == "ENTERED" for p in shadow)
    assert r.pos("live", source="orb5_rvol") == []
    assert len(r.pos("live", source="orb15")) == 1  # the other playbooks unchanged


@pytest.mark.parametrize("kw", [{"orb5_min_rvol": 3.0}, {"orb5_top_n": 0}])
def test_rank_filters_skip_setup(tmp_path, con, kw):
    r = Rig(tmp_path, con, scan_candidates=(), **kw)
    r.run(et(9, 55))
    assert json.loads(events(con, "orb5_rank")[0]["message"])["top"] == {}
    rows = con.execute("SELECT reason, detail_json FROM options_filtered WHERE underlying='SPY' "
                       "AND reason='orb5_not_in_play'").fetchall()
    assert len(rows) == 1 and json.loads(rows[0][1])["rvol5"] == pytest.approx(2.0)
    assert r.pos(source="orb5_rvol") == []
    assert len(r.pos("live", source="orb15")) == 1


def test_restart_reloads_the_days_rank(tmp_path, con):
    r = Rig(tmp_path, con, scan_candidates=())
    r.run(et(9, 40))
    assert len(events(con, "orb5_rank")) == 1
    r.eng = r.new_engine()
    r.run(et(9, 55))
    assert len(events(con, "orb5_rank")) == 1
    assert r.eng._orb5_top == {"SPY": {"rank": 1, "rvol5": 2.0}}
    assert r.pos("shadow", source="orb5_rvol")


def test_scan_failures_never_block_the_session(tmp_path, con):
    r = Rig(tmp_path, con, scan_candidates=("NOSUCH", "BOOM"))
    real = r.broker.minute_bars

    def bars(symbol, start, end):
        if symbol == "BOOM":
            raise RuntimeError("feed down")
        return real(symbol, start, end)
    r.broker.minute_bars = bars
    r.run(et(9, 55))
    assert any("BOOM" in e["message"] for e in events(con, "scan"))
    assert "SPY" in {u["symbol"] for u in ostore.universe(con, str(DAY))}
    assert len(r.pos("live", source="orb15")) == 1
    assert r.pos("shadow", source="orb5_rvol")


def test_scan_hit_joins_the_universe(tmp_path, con):
    gapper = with_history(day_bars([50.0] * 30, prev_close=45.0, pm_close=50.0, pm_volume=200_000),
                          px=45.0)
    r = Rig(tmp_path, con, scan_candidates=("GAPR",))
    r.broker.bars["GAPR"] = gapper
    r.run(et(9, 31))
    # an 11% premarket gap on heavy premarket volume: the scan adds it after the starting names
    assert [u["symbol"] for u in ostore.universe(con, str(DAY))] == ["SPY", "GAPR"]
    assert events(con, "scan") == []


def test_orb5_exception_does_not_block_other_playbooks(tmp_path, con, monkeypatch):
    def boom(*a, **k):
        raise ValueError("orb5 broke")
    monkeypatch.setitem(pb.RULES, "orb5_rvol", boom)
    r = Rig(tmp_path, con, scan_candidates=())
    r.run(et(9, 55))
    assert any("orb5 broke" in e["message"] for e in events(con, "orb5_rvol"))
    assert len(r.pos("live", source="orb15")) == 1
    assert r.pos("shadow", source="orb15")


def test_orb5_shadow_flat_by_forced_flat(tmp_path, con):
    r = Rig(tmp_path, con, scan_candidates=())
    r.run(et(15, 50), step=60)
    ps = r.pos("shadow", source="orb5_rvol")
    assert ps and all(p["state"] == "EXITED" for p in ps)
