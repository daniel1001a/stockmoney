"""CLI: ingest-plan, session loop (lock, HALT, finish), report, selftest, scheduling hooks."""
import fcntl
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from stockmoney.desk import store
from stockmoney.desk.options import report as oreport
from stockmoney.desk.options import run as orun
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options.engine import Engine

from opt_fakes import DAY, Clock, FakeOptionsBroker, allow_gate, chain, et, orb_breakout_day, spy_cfg
from test_opt_engine import Rig, _plan

ROOT = Path(__file__).resolve().parents[3]


def test_ingest_plan_cli(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(orun.notify, "send", lambda *a, **k: sent.append(a))
    f = tmp_path / "plan.json"
    f.write_text(_plan(), encoding="utf-8")
    db = tmp_path / "desk.sqlite"
    assert orun.ingest_plan(f, spy_cfg(tmp_path), db, now=et(8, 0)) == 0
    f.write_text(json.dumps({**json.loads(_plan()), "extra": 1}), encoding="utf-8")
    assert orun.ingest_plan(f, spy_cfg(tmp_path), db, now=et(8, 1)) == 1
    assert sent and "拒收" in sent[0][0]


def _factory(broker_holder):
    def make(con, cfg, clock):
        b = FakeOptionsBroker(clock, {"SPY": orb_breakout_day()})
        b.templates = chain()
        broker_holder.append(b)
        return Engine(con, b, cfg, clock=clock, risk_gate=allow_gate(), live=True,
                      notifier=lambda *a: None), "paper"
    return make


def test_session_runs_whole_day_and_ends_flat(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(orun.notify, "send", lambda *a, **k: sent.append(a))
    clock = Clock(et(9, 0))
    holder = []
    cfg = spy_cfg(tmp_path)
    rc = orun.session(cfg, tmp_path / "desk.sqlite", clock=clock, engine_factory=_factory(holder),
                      sleep=lambda s: clock.advance(max(s, 20)))
    assert rc == 0
    assert any("期權軌道啟動" in a[0] for a in sent)
    with store.connect(tmp_path / "desk.sqlite") as con:
        pos = ostore.positions(con, str(DAY))
        assert pos and all(p["state"] in ("EXITED", "ABANDONED") for p in pos)
    assert not holder[0].positions
    assert (cfg.report_dir / f"{DAY}.md").exists()


def test_second_session_refused_by_lock(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(orun.notify, "send", lambda *a, **k: sent.append(a))
    cfg = spy_cfg(tmp_path)
    held = orun._lock(cfg.lock_path)
    try:
        assert orun.session(cfg, tmp_path / "d.sqlite", clock=Clock(et(9, 0)), engine_factory=_factory([]),
                            sleep=lambda s: None) == 1
        assert "未啟動" in sent[0][0]
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()


def test_session_skips_non_trading_day(tmp_path):
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    sat = Clock(datetime(2026, 10, 17, 9, 0, tzinfo=ZoneInfo("America/New_York")))
    assert orun.session(spy_cfg(tmp_path), tmp_path / "d.sqlite", clock=sat, engine_factory=_factory([])) == 0


def test_session_engine_wires_line_a_broker_and_gate(tmp_path, monkeypatch):
    """The session trades through line A's broker and OptionsRiskEngine;
    DESK_OPTIONS_ORDERS=off gives a shadow-only engine with a deny-all gate."""
    from stockmoney.desk.options import _iface
    b = FakeOptionsBroker(Clock(et(9, 20)))
    monkeypatch.setattr(_iface, "make_trading_broker", lambda: b)
    monkeypatch.delenv("DESK_OPTIONS_ORDERS", raising=False)
    with store.connect(tmp_path / "d.sqlite") as con:
        eng, mode = orun.make_session_engine(con, spy_cfg(tmp_path))
        assert mode == "paper" and eng.live and isinstance(eng.risk_gate, _iface.LineAGate)
        assert eng.risk_gate.engine.limits.max_spread_pct == spy_cfg(tmp_path).max_spread_pct
        monkeypatch.setenv("DESK_OPTIONS_ORDERS", "off")
        eng, mode = orun.make_session_engine(con, spy_cfg(tmp_path))
        assert mode == "shadow" and eng.live is False and isinstance(eng.risk_gate, _iface.ShadowGate)
        b.is_paper = False
        monkeypatch.delenv("DESK_OPTIONS_ORDERS")
        eng, mode = orun.make_session_engine(con, spy_cfg(tmp_path))
        assert mode == "shadow" and eng.live is False  # never trades through a non-paper broker


def test_report_sections(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as con:
        r = Rig(tmp_path, con)
        r.run(et(10, 0))
        r.broker.mult = {k: 0.5 for k in r.broker.templates}
        r.run(et(10, 3))
        text = oreport.build(con, str(DAY), r.cfg)
    for h in ("期權短打日報", "資料限制", "紙上實單", "主變體", "依劇本", "依標的類別", "依 RVOL", "依盤勢標記",
              "出場變體", "到期變體", "Grok 劇本 vs 規則劇本", "同一 RVOL 條件", "被過濾掉的機會", "異常"):
        assert h in text
    assert "orb15" in text and "權利金停損" in text


def test_selftest_passes():
    assert orun.selftest() == 0


def test_schedule_hook_is_opt_in():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import desk_schedule as ds
    finally:
        sys.path.pop(0)
    assert ds.JOBS["options"] == {"times": [(9, 0)], "weekdays": True}
    assert "options" not in ds.DEFAULT_JOBS
    assert ds.DEFAULT_JOBS == ["premarket", "postopen", "postclose", "recover", "learn"]
    sh = (ROOT / "scripts/desk_job.sh").read_text()
    assert "stockmoney.desk.options.run session" in sh and 'stockmoney.desk.run "$1"' in sh
