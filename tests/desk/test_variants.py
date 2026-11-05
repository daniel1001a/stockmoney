"""Variant registry (learn/variants.py, EDGE_PLAN 7.4 item 5): registration
dates, family size, the setup gate dropping pre-registration trades, the
out-of-sample column, and isolation from trading code. Offline."""
import json
import os
import pathlib
import subprocess
import sys

import pytest

from stockmoney.desk import store
from stockmoney.desk.learn import setup_gate as sg
from stockmoney.desk.learn import variants as va
from stockmoney.desk.options import spread_variant as sv
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options.config import GROK, RULE_PLAYBOOKS, SHADOW_ONLY_SOURCES, OptionsConfig


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        ostore.ensure_schema(c)
        yield c


def test_every_recorded_variant_is_registered():
    cfg = OptionsConfig()
    names = {v.name for v in va.REGISTRY}
    for s in (*RULE_PLAYBOOKS, GROK, *SHADOW_ONLY_SOURCES, *cfg.live_sources):
        assert s in names, s
    for ev in cfg.exit_variants:
        assert f"exit_{ev}" in names
    assert {sv.VARIANT, "stressed_sizing", "mid_entry"} <= names
    assert len(names) == len(va.REGISTRY)  # no duplicate names
    for v in va.REGISTRY:
        assert v.kind in va.KINDS and len(v.registered) == 10 and v.gate and v.cost_assumption


def test_registered_since_and_family_size():
    assert va.registered_since("vwap_reclaim") == "2026-10-12"
    assert va.registered_since("orb15_5m") == "2026-10-27"
    assert va.registered_since("mid_entry") == "2026-11-05"
    assert va.registered_since("nope") is None
    assert va.family_size("2026-10-11") == 0
    assert va.family_size("2026-10-12") == 7   # 5 sources + exit A/B
    assert va.family_size("2026-10-27") == 8
    assert va.family_size("2026-11-05") == len(va.REGISTRY) == va.family_size()
    assert "K = " in va.report()


def _pos(con, pid, source, day, realized=100.0):
    con.execute("INSERT INTO options_positions (position_id, day, mode, source, dte_label, state, realized_usd, "
                "fees_usd, exit_variant, exit_reason, entry_price, entry_bid, entry_ask, qty_initial, flags_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, day, "shadow", source, "1-3dte", "EXITED", realized, 0.0, "A", "time_stop", 1.0, 0.95, 1.05,
                 1, json.dumps({})))


def test_gate_drops_trades_before_registration(con):
    _pos(con, "early", "orb15_5m", "2026-10-26")    # before 2026-10-27: not its sample
    _pos(con, "ok", "orb15_5m", "2026-10-28")
    _pos(con, "unreg", "someday_new", "2026-01-02")  # unregistered: kept
    rs = {r["source"]: r for r in sg.rows(con)}
    assert rs["orb15_5m"]["n"] == 1 and rs["someday_new"]["n"] == 1
    text = sg.report(con)
    assert f"K = {va.family_size()}" in text and "後段(最後 1/3 交易日)平均" in text


def test_out_of_sample_mean_is_the_last_third_of_days():
    ts = [{"pnl": -10.0 if d < 4 else 30.0, "day": f"2026-10-{d + 1:02d}", "mode": "shadow"} for d in range(6)]
    assert sg.metrics(ts)["oos_mean"] == pytest.approx(30.0)  # days 5 and 6
    ts = [{"pnl": float(d), "day": f"2026-10-{d + 1:02d}", "mode": "shadow"} for d in range(7)]
    assert sg.metrics(ts)["oos_mean"] == pytest.approx((4 + 5 + 6) / 3)  # ceil(7 / 3) = 3 days
    assert sg.metrics([])["oos_mean"] is None


def test_trading_code_does_not_read_the_new_reports():
    root = pathlib.Path("src/stockmoney/desk")
    paths = [root / "bridge" / "policy.py", *(root / "execution").rglob("*.py"), *(root / "options").rglob("*.py"),
             *(root / "risk").rglob("*.py"), root / "run.py", root / "veto.py", root / "engine.py", root / "live.py",
             root / "intraday.py"]
    assert len(paths) > 8
    for p in paths:
        if p.exists():
            text = p.read_text()
            for mod in ("learn.variants", "learn.exit_check", "learn.process_review", "import variants",
                        "import exit_check", "import process_review"):
                assert mod not in text, (p, mod)
    code = ("import sys; import stockmoney.desk.bridge.policy, stockmoney.desk.run, stockmoney.desk.options.engine; "
            "bad = [m for m in ('variants', 'exit_check', 'process_review') "
            "if 'stockmoney.desk.learn.' + m in sys.modules]; assert not bad, bad")
    subprocess.run([sys.executable, "-c", code], check=True, env={**os.environ, "PYTHONPATH": "src"})
