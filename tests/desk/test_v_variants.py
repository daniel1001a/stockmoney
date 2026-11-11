"""Derived P1 shadow filters V03/V05/V06 (learn/v_variants.py): forward-only
registration, the filter rules themselves, V06's 1-contract rescale, shared
family size with the main gate, and isolation from trading code. Offline."""
import json
import os
import pathlib
import subprocess
import sys

import pytest

from stockmoney.desk import store
from stockmoney.desk.learn import v_variants as vv
from stockmoney.desk.learn import variants as va
from stockmoney.desk.options import store as ostore


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        ostore.ensure_schema(c)
        yield c


def _pos(con, pid, *, source="vwap_reclaim", mode="shadow", day="2026-11-11", state="EXITED", realized=100.0,
        fees=0.6, ev="A", dte="1-3dte", reason="take_profit", qty=1, entry_time="2026-11-11T13:40:00+00:00",
        uclass="mega_cap", entry_price=1.0, entry_bid=0.98, entry_ask=1.02, setup=None):
    con.execute("INSERT INTO options_positions (position_id, setup_id, day, mode, source, dte_label, state, "
                "realized_usd, fees_usd, exit_variant, exit_reason, entry_price, entry_bid, entry_ask, qty_initial, "
                "entry_time, uclass, flags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, setup, day, mode, source, dte, state, realized, fees, ev, reason, entry_price, entry_bid,
                 entry_ask, qty, entry_time, uclass, json.dumps({})))


# entry_time 13:40 UTC = 09:40 ET on 2026-11-11 (EST, UTC-5): before the 10:00 ET cutoff
BEFORE_CUTOFF = "2026-11-11T13:40:00+00:00"
AFTER_CUTOFF = "2026-11-11T15:40:00+00:00"   # 10:40 ET


def test_v03_needs_before_cutoff_and_not_high_vol(con):
    _pos(con, "ok", entry_time=BEFORE_CUTOFF, uclass="mega_cap")
    _pos(con, "late", entry_time=AFTER_CUTOFF, uclass="mega_cap")
    _pos(con, "highvol", entry_time=BEFORE_CUTOFF, uclass="high_vol")
    rs = {r["name"]: r for r in vv.rows(con)}
    assert rs["v03_cutoff_highvol"]["n"] == 1


def test_v05_needs_spread_within_5pct(con):
    _pos(con, "tight", entry_bid=0.98, entry_ask=1.02)   # spread 4%
    _pos(con, "wide", entry_bid=0.90, entry_ask=1.10)     # spread 20%
    rs = {r["name"]: r for r in vv.rows(con)}
    assert rs["v05_spread_cap_5pct"]["n"] == 1


def test_v06_needs_cheap_premium_and_rescales_to_one_contract(con):
    _pos(con, "cheap", entry_price=2.0, qty=10, realized=100.0, fees=0.6)   # 2.0 * 100 = 200 <= 250
    _pos(con, "pricey", entry_price=5.0, qty=10)                           # 5.0 * 100 = 500 > 250
    rs = {r["name"]: r for r in vv.rows(con)}
    v06 = rs["v06_10k_scale"]
    assert v06["n"] == 1
    assert v06["total"] == pytest.approx((100.0 - 0.6) / 10)  # rescaled to 1 contract, not 10


def test_before_registration_is_not_counted(con):
    _pos(con, "early", day="2026-11-09", entry_time="2026-11-09T13:40:00+00:00")  # before 2026-11-10
    _pos(con, "ok", day="2026-11-11", entry_time=BEFORE_CUTOFF)
    rs = {r["name"]: r for r in vv.rows(con)}
    assert rs["v03_cutoff_highvol"]["n"] == 1
    assert rs["v05_spread_cap_5pct"]["n"] == 1
    assert rs["v06_10k_scale"]["n"] == 1  # entry_price 1.0 * 100 = 100 <= 250, both qualify, only "ok" after registration


def test_rows_share_the_main_gate_family_size(con):
    _pos(con, "a", entry_time=BEFORE_CUTOFF)
    rs = vv.rows(con)
    assert all(r["k"] == va.family_size() for r in rs)
    assert {r["name"] for r in rs} == set(vv.NAMES)


def test_report_shows_k_and_verdicts(con):
    _pos(con, "a", entry_time=BEFORE_CUTOFF)
    text = vv.report(con)
    assert f"K = {va.family_size()}" in text
    for name in vv.NAMES:
        assert name in text


def test_cli_reads_without_writing(con, tmp_path, capsys):
    _pos(con, "a", entry_time=BEFORE_CUTOFF)
    con.commit()
    before = (tmp_path / "desk.sqlite").read_bytes()
    assert vv.main(["v_variants", "--db", str(tmp_path / "desk.sqlite")]) == 0
    assert "v03_cutoff_highvol" in capsys.readouterr().out
    assert (tmp_path / "desk.sqlite").read_bytes() == before


def test_missing_or_malformed_entry_time_does_not_qualify_v03(con):
    _pos(con, "none", entry_time=None)
    _pos(con, "naive", entry_time="not-a-date")
    rs = {r["name"]: r for r in vv.rows(con)}
    assert rs["v03_cutoff_highvol"]["n"] == 0


def test_trading_code_does_not_read_v_variants():
    root = pathlib.Path("src/stockmoney/desk")
    paths = [root / "bridge" / "policy.py", *(root / "execution").rglob("*.py"), *(root / "options").rglob("*.py"),
             *(root / "risk").rglob("*.py"), root / "run.py", root / "veto.py", root / "engine.py", root / "live.py",
             root / "intraday.py"]
    assert len(paths) > 8
    for p in paths:
        if p.exists():
            assert "v_variants" not in p.read_text(), p
    code = ("import sys; import stockmoney.desk.bridge.policy, stockmoney.desk.run, stockmoney.desk.options.engine; "
            "assert 'stockmoney.desk.learn.v_variants' not in sys.modules")
    subprocess.run([sys.executable, "-c", code], check=True, env={**os.environ, "PYTHONPATH": "src"})
