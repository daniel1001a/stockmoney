"""Registry boundaries and persistence, against the real desk store."""
import json
from dataclasses import fields

import pytest

from stockmoney.desk import profiles, store
from stockmoney.desk.learn import registry as r
from stockmoney.desk.options.config import OptionsConfig
from stockmoney.desk.strategies import REGISTRY


def test_all_strategy_bounds_and_defaults(con):
    for name, cls in REGISTRY.items():
        for param, bound in cls.bounds.items():
            key = f"{name}.{param}"
            assert r.REGISTRY[key].bound == bound
            assert r.current(con, key) == cls.params[param]


def test_protected_keys(con):
    keys = [f"profiles.{f.name}" for f in fields(profiles.Profile) if f.name != "name"]
    keys += ["options.max_premium_per_trade", "options.sleeve_pct", "options.max_open_positions",
             "options.force_flat", "risk.dd_halt", "risk.halt_file", "ALPACA_ENV",
             "ALPACA_LIVE_ENABLED", "DESK_OPTIONS_ORDERS"]
    for key in keys:
        assert not r.REGISTRY[key].ai_tunable
        with pytest.raises(ValueError, match="not AI tunable"):
            r.change(con, key, 1, "must refuse", account_kind="paper")
    assert con.execute("SELECT count(*) FROM param_changes").fetchone()[0] == 0


@pytest.mark.parametrize("key,value", [("unknown", 1), ("options.orb_min_day_rvol", 1.9),
    ("options.orb_min_day_rvol", float("nan")), ("options.orb_min_day_rvol", float("inf")),
    ("options.orb_min_day_rvol", True), ("options.orb_min_day_rvol", "1.5"),
    ("options.vwap_hold_bars", 2.5), ("options.entry_window_start", 570)])
def test_invalid_value_has_no_side_effects(con, key, value):
    with pytest.raises(ValueError):
        r.change(con, key, value, "invalid", account_kind="paper")
    assert con.execute("SELECT count(*) FROM param_changes").fetchone()[0] == 0
    assert store.get_setting(con, "options_params") == ""


def test_round_trip_and_audit(con):
    r.change(con, "options.orb_min_day_rvol", 1.75, "test proposal", account_kind="paper")
    r.change(con, "spreads.take_profit_frac", 0.6, "spread proposal", account_kind="paper")
    assert r.current(con, "options.orb_min_day_rvol") == 1.75
    assert r.current(con, "spreads.take_profit_frac") == 0.6
    rows = con.execute("SELECT strategy,param,old,new,reason FROM param_changes ORDER BY id").fetchall()
    assert tuple(rows[0]) == ("options", "orb_min_day_rvol", "1.5", "1.75", "test proposal")
    cfg = OptionsConfig()
    assert r.options_config(con, cfg, paper=True).orb_min_day_rvol == 1.75
    assert r.options_config(con, cfg, paper=False) is cfg
    assert r.options_config(con, cfg, paper=True, sleeve="spreads").spread_take_profit_frac == 0.6
    assert cfg.orb_min_day_rvol == 1.5


def test_stock_writer_and_cross_parameter_validation(con):
    r.change(con, "pullback.trail_atr", 3.5, "test", account_kind="paper")
    assert store.strategy_params(con)["pullback"]["trail_atr"] == 3.5
    store.change_param(con, "rotation", "keep_rank", 5, "fixture")
    with pytest.raises(ValueError, match="keep_rank"):
        r.change(con, "rotation.top_n", 6, "invalid combination", account_kind="paper")


def test_nonpaper_write_refused(con):
    with pytest.raises(ValueError, match="paper-only"):
        r.change(con, "options.orb_min_day_rvol", 1.75, "test", account_kind="live")
    assert r.current(con, "options.orb_min_day_rvol") == 1.5


def test_clock_override(con):
    r.change(con, "options.entry_window_start", 580, "later entry", account_kind="paper")
    assert r.options_config(con, OptionsConfig(), paper=True).entry_window_start == (9, 40)


def test_tampered_settings_cannot_override_risk(con):
    store.set_setting(con, "options_params", json.dumps({"options.max_premium_per_trade": 9999}))
    with pytest.raises(ValueError, match="invalid stored"):
        r.options_config(con, OptionsConfig(), paper=True)


def test_cli_registry(tmp_path, capsys):
    from scripts.desk_maintainer import main

    main(["--registry", "--db", str(tmp_path / "registry.sqlite")])
    output = capsys.readouterr().out
    assert "options.orb_min_day_rvol\toptions\t1.5\t[1, 3]\t0.25\tTrue" in output
    assert "ALPACA_LIVE_ENABLED\tdesk\t" in output


def test_audit_failure_rolls_back_setting(con):
    con.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON param_changes BEGIN SELECT RAISE(ABORT, 'test'); END")
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        r.change(con, "options.orb_min_day_rvol", 1.75, "test", account_kind="paper")
    assert r.current(con, "options.orb_min_day_rvol") == 1.5


def test_switch_states_follow_runtime_defaults(con, monkeypatch):
    monkeypatch.delenv("DESK_OPTIONS_ORDERS", raising=False)
    assert r.current(con, "DESK_OPTIONS_ORDERS") == "paper"
    monkeypatch.setenv("DESK_OPTIONS_ORDERS", "off")
    assert r.current(con, "DESK_OPTIONS_ORDERS") == "off"
    monkeypatch.setenv("ALPACA_LIVE_ENABLED", "not-the-confirmation")
    assert r.current(con, "ALPACA_LIVE_ENABLED") is False


@pytest.mark.parametrize("blob", ['[]', '{', '{"options.max_premium_per_trade":9999}',
                                  '{"options.orb_min_day_rvol":0.5}', '{"spreads.stop_frac":"bad"}'])
def test_invalid_shared_document_refused_by_writer_but_exits_can_load(con, blob):
    store.set_setting(con, "options_params", blob)
    with pytest.raises((ValueError, TypeError)):
        r.change(con, "options.orb_min_day_rvol", 1.75, "test", account_kind="paper")
    assert store.get_setting(con, "options_params") == blob
    assert con.execute("SELECT count(*) FROM param_changes").fetchone()[0] == 0
    reports = []
    cfg = OptionsConfig()
    for _ in range(2):
        assert r.safe_options_config(con, cfg, paper=True, on_error=lambda *a: reports.append(a)) is cfg
    assert len(reports) == 1
