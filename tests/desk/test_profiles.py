import pytest

from stockmoney.desk import profiles


def test_default_is_standard():
    p = profiles.active({})
    assert (p.risk_per_trade, p.max_positions, p.max_per_sector) == (0.0075, 8, 2)


def test_aggressive_on_paper():
    p = profiles.active({"DESK_PROFILE": "paper_aggressive", "ALPACA_ENV": "paper"})
    assert p.risk_per_trade == 0.015 and p.limits().max_position_pct == 0.25


def test_aggressive_refused_on_live():
    with pytest.raises(ValueError, match="paper-only"):
        profiles.active({"DESK_PROFILE": "paper_aggressive", "ALPACA_ENV": "live"})


def test_unknown_profile():
    with pytest.raises(ValueError):
        profiles.active({"DESK_PROFILE": "yolo"})


def test_wide_on_paper():
    p = profiles.active({"DESK_PROFILE": "paper_wide", "ALPACA_ENV": "paper"})
    assert p == profiles.Profile("paper_wide", 0.009, 20, 4, 0.25, 0.30, 0.40)
    assert p.risk_per_trade * p.max_positions == pytest.approx(0.18)


@pytest.mark.parametrize("env", ["live", "unknown"])
def test_wide_refused_outside_paper(env):
    with pytest.raises(ValueError, match="paper-only"):
        profiles.active({"DESK_PROFILE": "paper_wide", "ALPACA_ENV": env})


def test_profile_observations_logged_once(con, monkeypatch):
    import json
    from stockmoney.desk import run, store

    for name in ("standard", "standard", "paper_wide", "paper_wide", "standard"):
        monkeypatch.setattr(run, "_PROFILE", profiles.PROFILES[name])
        run.record_profile(con)
    rows = con.execute("SELECT * FROM param_changes WHERE strategy='desk' AND param='profile' ORDER BY id").fetchall()
    assert [(json.loads(r["old"]), json.loads(r["new"])) for r in rows] == [
        (None, "standard"), ("standard", "paper_wide"), ("paper_wide", "standard")]
    assert all(r["changed_at"] for r in rows)
    assert store.strategy_params(con) == {}
