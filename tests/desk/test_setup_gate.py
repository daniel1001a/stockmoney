"""Per-setup live gate (learn/setup_gate.py): metrics on synthetic rows, the
row filter, stressed sizing, and isolation from trading code. Offline."""
import json
import os
import pathlib
import subprocess
import sys

import pytest

from stockmoney.desk import store
from stockmoney.desk.learn import setup_gate as sg
from stockmoney.desk.options import shadow as sh
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options.config import OptionsConfig


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        ostore.ensure_schema(c)
        yield c


def trades(n, days, pnl=lambda i: 10.0, mode="shadow"):
    return [{"pnl": pnl(i), "day": f"2026-10-{1 + i % days:02d}", "mode": mode} for i in range(n)]


def test_pass_needs_every_condition():
    ok = sg.metrics(trades(120, 20, lambda i: 10.0 + (i % 7) - 3))
    assert ok["verdict"] == "PASS" and ok["n"] == 120 and ok["days"] == 20
    assert ok["lb"] == min(ok["lb_trade"], ok["lb_day"]) and 0 < ok["lb"] <= ok["mean"]
    assert 0 < ok["day_share"] <= 0.5
    flat = sg.metrics(trades(120, 20))  # 6 trades of +10 on each of 20 days
    assert flat["total"] == 1200 and flat["day_share"] == pytest.approx(0.05) and flat["lb"] == pytest.approx(10)
    assert sg.metrics(trades(99, 20))["verdict"] == "FAIL"                       # n < 100
    assert sg.metrics(trades(120, 14))["verdict"] == "FAIL"                      # days < 15
    assert sg.metrics(trades(120, 20, lambda i: -5.0 + (i % 3)))["verdict"] == "FAIL"  # lower bound <= 0
    lumpy = sg.metrics(trades(120, 20, lambda i: 2000.0 if i == 0 else -1.0 + (i % 3)))
    assert lumpy["day_share"] > 0.5 and lumpy["verdict"] == "FAIL"               # one day carries it


def test_day_share_na_when_total_not_positive_and_seed_is_fixed():
    m = sg.metrics(trades(120, 20, lambda i: -1.0))
    assert m["day_share"] is None and m["verdict"] == "FAIL"
    noisy = trades(150, 30, lambda i: (i * 37 % 23) - 9.0)
    assert sg.metrics(noisy) == sg.metrics(noisy)
    assert sg.metrics([])["n"] == 0 and sg.metrics([])["verdict"] == "FAIL"


def test_day_block_bound_is_more_conservative_when_days_cluster():
    # wins and losses each land on whole days: the trade bootstrap understates the spread
    m = sg.metrics(trades(200, 20, lambda i: 30.0 if i % 20 < 11 else -30.0))
    assert m["lb_day"] < m["lb_trade"] and m["lb"] == m["lb_day"]


def test_stressed_risk_formula():
    cfg = OptionsConfig()
    st = sh.stressed_risk(1.00, 0.95, 1.05, 0.35, cfg)
    assert st["cost"] == pytest.approx(35 + 5 + 0.06) and st["qty"] == 24  # floor(1000 / 40.06)
    assert sh.stressed_risk(1.00, None, None, 0.30, cfg)["cost"] == pytest.approx(30.06)


def _pos(con, pid, *, source="vwap_reclaim", mode="shadow", day="2026-11-02", state="EXITED", realized=100.0,
         fees=0.6, ev="A", dte="1-3dte", reason="take_profit", qty=10, flags=None, setup=None):
    con.execute("INSERT INTO options_positions (position_id, setup_id, day, mode, source, dte_label, state, "
                "realized_usd, fees_usd, exit_variant, exit_reason, entry_price, entry_bid, entry_ask, qty_initial, "
                "flags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, setup, day, mode, source, dte, state, realized, fees, ev, reason, 1.0, 0.95, 1.05, qty,
                 json.dumps(flags or {})))


def test_rows_filter_pnl_and_stressed(con):
    _pos(con, "a", flags={"stressed": {"qty": 50}})
    _pos(con, "b", mode="live", dte="0dte", realized=-200.0)
    _pos(con, "c", ev="B")                                   # second exit variant of the same entry: not counted
    _pos(con, "d", state="ENTERED")                          # still open
    _pos(con, "e", reason="manual_resolved")                 # unknown P&L
    _pos(con, "f", source="orb15", realized=50.0)
    rs = {r["source"]: r for r in sg.rows(con)}
    v = rs["vwap_reclaim"]
    assert v["n"] == 2 and v["live"] == 1 and v["shadow"] == 1
    assert v["total"] == pytest.approx(99.4 - 200.6)
    # a: flag qty 50 -> 9.94 * 50; b: recomputed from columns, 24 contracts
    assert v["stressed_total"] == pytest.approx(99.4 / 10 * 50 + -200.6 / 10 * 24)
    by = {(r["source"], r["dte_label"]) for r in sg.rows(con, by_dte=True)}
    assert by == {("vwap_reclaim", "0dte"), ("vwap_reclaim", "1-3dte"), ("orb15", "1-3dte")}
    text = sg.report(con)
    assert "逐策略實盤關卡" in text and "| vwap_reclaim | 全部 | 2 |" in text and "FAIL" in text


def test_live_row_and_its_shadow_twin_count_once(con):
    _pos(con, "live1", mode="live", setup="s1", realized=-100.0)
    _pos(con, "twin1", setup="s1", realized=-90.0)            # same setup and dte: dropped
    _pos(con, "other", setup="s1", dte="0dte", realized=40.0)  # other dte bucket of the setup: kept
    _pos(con, "solo", setup="s2", realized=30.0)               # shadow-only setup: kept
    v = {r["source"]: r for r in sg.rows(con)}["vwap_reclaim"]
    assert v["n"] == 3 and v["live"] == 1 and v["shadow"] == 2
    assert v["total"] == pytest.approx(-100.6 + 39.4 + 29.4)


def test_report_on_a_database_without_options_tables(tmp_path):
    with store.connect(tmp_path / "bare.sqlite") as c:
        assert sg.rows(c) == [] and "尚無已出場部位" in sg.report(c)


def test_cli_reads_without_writing(con, tmp_path, capsys):
    _pos(con, "a")
    con.commit()
    before = (tmp_path / "desk.sqlite").read_bytes()
    assert sg.main(["setup_gate", "--db", str(tmp_path / "desk.sqlite"), "--by-dte"]) == 0
    assert "| vwap_reclaim | 1-3dte | 1 |" in capsys.readouterr().out
    assert (tmp_path / "desk.sqlite").read_bytes() == before


def test_trading_code_does_not_read_the_gate():
    root = pathlib.Path("src/stockmoney/desk")
    paths = [root / "bridge" / "policy.py", *(root / "execution").rglob("*.py"), *(root / "options").rglob("*.py"),
             *(root / "risk").rglob("*.py"), root / "run.py", root / "veto.py", root / "engine.py", root / "live.py",
             root / "intraday.py"]
    assert len(paths) > 8
    for p in paths:
        if p.exists():
            assert "setup_gate" not in p.read_text(), p
    code = ("import sys; import stockmoney.desk.bridge.policy, stockmoney.desk.run, stockmoney.desk.options.engine; "
            "assert 'stockmoney.desk.learn.setup_gate' not in sys.modules")
    subprocess.run([sys.executable, "-c", code], check=True, env={**os.environ, "PYTHONPATH": "src"})
