"""Per-source credibility scoreboard: in_play open-to-close scoring and session
choice, market_risk / block / options observations, rolling windows, trend
and 樣本不足 labels, idempotent recompute, absence tolerance, the learn job
hook, and isolation from trading code. Offline."""
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

import pandas as pd
import pytest

from stockmoney.desk import store
from stockmoney.desk.bridge import inbox
from stockmoney.desk.data import Bars
from stockmoney.desk.execution.ledger import ensure_schema as ledger_schema
from stockmoney.desk.learn import scoreboard as sb, tables
from stockmoney.desk.options import store as ostore
from stockmoney.desk.run import ensure_schema as run_schema


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "d.sqlite") as c:
        run_schema(c)
        ledger_schema(c)
        tables.ensure_schema(c)
        ostore.ensure_schema(c)
        inbox.ensure_schema(c)
        yield c


def make_bars(n=80):
    """AAPL opens 100 closes 101 (+1%), NVDA opens 100 closes 98 (-2%), SPY flat 400."""
    idx = pd.bdate_range("2026-07-01", periods=n)
    op = pd.DataFrame({"AAPL": 100.0, "NVDA": 100.0, "SPY": 400.0}, index=idx)
    close = pd.DataFrame({"AAPL": 101.0, "NVDA": 98.0, "SPY": 400.0}, index=idx)
    return Bars(op, close + 1, op - 3, close, pd.DataFrame(1e6, index=idx, columns=op.columns))


def grok_daily(con, mid, received_at, in_play=None, source="grok_verified"):
    body = {"type": "GROK_DAILY", "summary": "s"}
    if in_play is not None:
        body["in_play"] = in_play
    con.execute("INSERT INTO bridge_messages (msg_id, type, sender, sent_at, received_at, raw_body, parsed_json, source) "
                "VALUES (?,?,?,?,?,?,?,?)", (mid, "GROK_DAILY", "g", received_at, received_at, "{}",
                                             json.dumps(body), source))


def item(ticker, direction, confirmed=True, confidence=0.8):
    return {"ticker": ticker, "direction": direction, "catalyst": "c", "catalyst_kind": "earnings",
            "catalyst_time": "premarket", "confirmed": confirmed, "urls": [], "confidence": confidence}


def option(con, pid, day, pnl, source="orb15", mode="live", state="EXITED", exit_reason="stop", fees=1.0):
    con.execute("INSERT INTO options_positions (position_id, day, mode, source, underlying, state, realized_usd, "
                "fees_usd, exit_reason) VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, day, mode, source, "SPY", state, pnl + fees, fees, exit_reason))


def scores(con, day, source, window):
    return {r["metric"]: (r["n"], r["value"]) for r in con.execute(
        "SELECT metric, n, value FROM source_scores WHERE day=? AND source=? AND window=?", (day, source, window))}


def test_session_for_premarket_same_day_after_open_next_day():
    s = make_bars().dates
    d = s[10]
    pre = f"{d.date()}T12:00:00+00:00"  # 08:00 ET
    post = f"{d.date()}T14:00:00+00:00"  # 10:00 ET
    assert sb.session_for(pre, s) == d
    assert sb.session_for(post, s) == s[11]
    sat = (d + pd.Timedelta(days=(5 - d.weekday()) % 7)).date()
    assert sb.session_for(f"{sat}T12:00:00+00:00", s).weekday() == 0


def test_in_play_open_to_close_hit_brier_and_confirmed_split(con):
    bars = make_bars()
    d = bars.dates[-5]
    grok_daily(con, "m1", f"{d.date()}T12:00:00+00:00",
               [item("AAPL", "up", True, 0.8), item("NVDA", "up", False, 0.6), item("ZZZZ", "up"),
                item("AAPL", "sideways")])
    grok_daily(con, "m2", f"{d.date()}T12:30:00+00:00", [item("AAPL", "up")])  # same claim, later mail
    grok_daily(con, "m0", f"{d.date()}T11:00:00+00:00")  # no in_play field
    obs = sb.in_play_obs(con, bars)
    main = [o for o in obs if o["source"] == "grok_in_play"]
    assert len(main) == 2 and {o["day"] for o in main} == {str(d.date())}
    by_hit = {o["hit"]: o for o in main}
    assert by_hit[1]["signed"] == pytest.approx(0.01) and by_hit[0]["signed"] == pytest.approx(-0.02)
    assert [o["hit"] for o in obs if o["source"] == "grok_in_play:confirmed"] == [1]
    assert [o["hit"] for o in obs if o["source"] == "grok_in_play:unconfirmed"] == [0]
    sb.refresh(con, bars, bars.dates[-1])
    m = scores(con, str(bars.dates[-1].date()), "grok_in_play", "all")
    assert m["hit_rate"] == (2, 0.5)
    assert m["mean_signed"][1] == pytest.approx(-0.005)
    assert m["brier"][1] == pytest.approx(((0.8 - 1) ** 2 + (0.6 - 0) ** 2) / 2)


def test_unverified_in_play_scores_apart_from_grok(con):
    """grok_legacy mail never grades Grok's authority; it scores apart."""
    bars = make_bars()
    d = bars.dates[-5]
    grok_daily(con, "m1", f"{d.date()}T12:00:00+00:00", [item("AAPL", "up")], source="grok_legacy")
    grok_daily(con, "m2", f"{d.date()}T12:30:00+00:00", [item("AAPL", "up")])
    got = sorted(o["source"] for o in sb.in_play_obs(con, bars))
    assert got == ["grok_in_play", "grok_in_play:confirmed", "grok_in_play:legacy"]


def test_in_play_down_direction_scores_the_short_side(con):
    bars = make_bars()
    d = bars.dates[-3]
    grok_daily(con, "m1", f"{d.date()}T12:00:00+00:00", [item("NVDA", "down")])
    (o,) = [o for o in sb.in_play_obs(con, bars) if o["source"] == "grok_in_play"]
    assert o["hit"] == 1 and o["signed"] == pytest.approx(0.02)


def test_market_risk_hit_definition(con):
    rows = [("a", "elevated", "2026-10-01", -0.02), ("b", "low", "2026-10-01", -0.01),
            ("c", "critical", "2026-10-02", 0.01), ("d", "low", "2026-10-02", 0.005), ("e", "low", "2026-10-03", None)]
    con.executemany("INSERT INTO grok_market_risk (msg_id, level, base_day, ret_5) VALUES (?,?,?,?)", rows)
    obs = sb.market_risk_obs(con)
    assert sorted((o["hit"], round(o["signed"], 3)) for o in obs) == [(0, -0.01), (0, -0.01), (1, 0.005), (1, 0.02)]


def test_chains_market_risk_and_calls_grade_only_verified_grok(con):
    """Unverified GROK_DAILY (grok_legacy, or a row from before the source
    column) scores under ``:legacy``, never as Grok; an unowned chain is legacy."""
    from stockmoney.desk.learn import calls as learn_calls
    learn_calls.ensure_schema(con)
    for mid, src in (("gv", "grok_verified"), ("gl", "grok_legacy"), ("gn", None)):
        con.execute("INSERT INTO bridge_messages (msg_id, type, sender, sent_at, received_at, raw_body, parsed_json, "
                    "source) VALUES (?,?,?,?,?,?,?,?)", (mid, "GROK_DAILY", "g", "2026-10-01T12:00:00+00:00",
                                                         "2026-10-01T12:00:00+00:00", "{}",
                                                         json.dumps({"chains": [{}]}), src))
    for cid, owner in (("cv", "gv"), ("cl", "gl"), ("cu", None)):
        con.execute("INSERT INTO chains (chain_id, created_at, source, falsifiable) VALUES (?,?,?,1)",
                    (cid, "2026-10-01T12:00:00+00:00", "grok"))
        con.execute("INSERT INTO chain_scores (chain_id, ticker, horizon, declared, direction, excess_ret, hit, "
                    "confidence) VALUES (?,?,?,?,?,?,?,?)", (cid, "AAPL", 5, 1, "up", 0.01, 1, 0.6))
        if owner:
            con.execute("INSERT INTO chain_owners VALUES (?,?)", (cid, owner))
    assert sorted(o["source"] for o in sb.chain_obs(con)) == ["grok_chains", "grok_chains:legacy",
                                                               "grok_chains:legacy"]
    con.executemany("INSERT INTO grok_market_risk (msg_id, level, base_day, ret_5, source) VALUES (?,?,?,?,?)",
                    [("gv", "low", "2026-10-01", 0.01, "grok_verified"), ("gl", "low", "2026-10-01", 0.01,
                                                                          "grok_legacy"),
                     ("gn", "low", "2026-10-01", 0.01, None)])
    assert sorted(o["source"] for o in sb.market_risk_obs(con)) == ["grok_market_risk", "grok_market_risk:legacy",
                                                                     "grok_market_risk:legacy"]
    con.execute("INSERT INTO bridge_messages (msg_id, type, sender, sent_at, received_at, raw_body, parsed_json, "
                "source) VALUES ('mc','CALLS','m','x','x','{}','{}','muse')")
    for cid, mid in (("k1", "gv"), ("k2", "gl"), ("k3", "gn"), ("k4", "mc")):
        con.execute("INSERT INTO author_calls (call_id, direction, source_msg) VALUES (?,?,?)", (cid, "long", mid))
        con.execute("INSERT INTO call_scores (call_id, horizon, base_date, excess_ret, hit) VALUES (?,5,?,?,1)",
                    (cid, "2026-10-01", 0.02))
    assert sorted(o["source"] for o in sb.calls_obs(con)) == ["grok_calls", "grok_calls:legacy",
                                                               "grok_calls:legacy", "muse_calls"]


def test_block_sources_and_counterfactual_hit(con):
    rows = [("1", "2026-10-01", "muse_alert", "Muse ALERT elevated: x", -0.03, 1, 0),
            ("2", "2026-10-02", "muse_alert", "Muse ALERT elevated: x", 0.05, 0, 0),  # repeat block: skipped
            ("3", "2026-10-03", "other", "buy blocked: research veto: muse + filings", 0.02, 1, 0),
            ("4", "2026-10-03", "muse_alert", "Muse ALERT elevated: y", 0.01, 1, 1),
            ("5", "2026-10-03", "other", "對帳不一致", -0.04, 1, 0),
            ("6", "2026-10-03", "muse_risk_flags", "Muse risk flags: z", None, 1, 0)]  # pending
    con.executemany("INSERT INTO blocked_counterfactual (client_order_id, plan_date, block_class, note, excess_5, "
                    "first_block, legacy_only) VALUES (?,?,?,?,?,?,?)", rows)
    got = sorted((o["source"], o["hit"], round(o["signed"], 3)) for o in sb.block_obs(con))
    assert got == [("muse_alert_block", 1, 0.03), ("muse_legacy_rule", 0, -0.01), ("research_veto_block", 0, -0.02)]


def test_options_pnl_sources_exclude_unknown_and_abandoned(con):
    option(con, "p1", "2026-10-20", 50.0)
    option(con, "p2", "2026-10-20", -30.0)
    option(con, "p3", "2026-10-20", 999.0, exit_reason="broker_flat_unreconciled")
    option(con, "p4", "2026-10-20", 999.0, state="ABANDONED")
    option(con, "p5", "2026-10-21", 20.0, source="grok_plan", mode="shadow")
    obs = sb.options_obs(con)
    assert sorted((o["source"], o["pnl"]) for o in obs) == [
        ("grok_options_plan:shadow", 20.0), ("playbook:orb15:live", -30.0), ("playbook:orb15:live", 50.0)]


def test_windows_select_on_event_day_and_metrics(con):
    bars = make_bars()
    s = bars.dates
    for i in range(30):  # one trade per session over the last 30 sessions: 20 inside 20d, all inside 60d
        option(con, f"p{i}", str(s[-30 + i].date()), 10.0 if i >= 10 else -10.0)
    sb.refresh(con, bars, s[-1])
    day = str(s[-1].date())
    w20, w60 = scores(con, day, "playbook:orb15:live", "20d"), scores(con, day, "playbook:orb15:live", "60d")
    assert w20["n"][0] == 20 and w20["pnl_per_trade"][1] == pytest.approx(10.0) and w20["win_rate"][1] == 1.0
    assert w60["n"][0] == 30 and w60["pnl_total"][1] == pytest.approx(100.0)
    assert scores(con, day, "grok_in_play", "all") == {"n": (0, 0.0)}  # fixed source, silent


def test_trend_labels():
    assert sb.trend("hit_rate", 0.70, 19, 0.50) == "樣本不足"
    assert sb.trend("hit_rate", 0.60, 20, 0.50) == "改善"
    assert sb.trend("hit_rate", 0.40, 20, 0.50) == "惡化"
    assert sb.trend("hit_rate", 0.53, 20, 0.50) == "持平"
    assert sb.trend("pnl_per_trade", 15.0, 25, 10.0) == "持平"  # within 10 USD
    assert sb.trend("pnl_per_trade", -200.0, 25, 0.0) == "惡化"
    assert sb.trend("pnl_per_trade", 320.0, 25, 300.0) == "持平"  # within 10% of |60d| (30 USD)
    assert sb.trend("pnl_per_trade", 340.0, 25, 300.0) == "改善"


def test_report_section_lists_sources_and_small_samples(con):
    bars = make_bars()
    s = bars.dates
    for i in range(25):
        option(con, f"p{i}", str(s[-25 + i].date()), 10.0)
    grok_daily(con, "m1", f"{s[-2].date()}T12:00:00+00:00", [item("AAPL", "up")])
    sb.refresh(con, bars, s[-1])
    text = sb.report(con, str(s[-1].date()))
    assert text.startswith("## 來源記分板")
    row = next(line for line in text.splitlines() if line.startswith("| Grok in_play(開盤"))
    assert "1 筆,命中 100%" in row and "樣本不足" in row
    pb = next(line for line in text.splitlines() if "規則劇本 orb15(紙上實單)" in line)
    assert "25 筆,每筆 +10 美元" in pb and pb.endswith("| 持平 |")
    assert "前 60 分鐘報酬未計算" in text
    assert "沒有算出" in sb.report(con, "2000-01-01")


def test_refresh_is_idempotent_and_keeps_earlier_days(con):
    bars = make_bars()
    option(con, "p1", str(bars.dates[-2].date()), 5.0)
    sb.refresh(con, bars, bars.dates[-2])
    a = sb.refresh(con, bars, bars.dates[-1])
    b = sb.refresh(con, bars, bars.dates[-1])
    assert a == b
    days = {r[0]: r[1] for r in con.execute("SELECT day, COUNT(*) FROM source_scores GROUP BY day")}
    assert len(days) == 2 and days[str(bars.dates[-1].date())] == a["rows"]


def test_tolerates_missing_tables(tmp_path):
    with store.connect(tmp_path / "bare.sqlite") as c:
        r = sb.refresh(c, make_bars(), make_bars().dates[-1])
        assert r["observations"] == 0 and r["rows"] == len(sb.FIXED) * len(sb.WINDOWS)


def test_learn_job_writes_scoreboard_section(con, monkeypatch, tmp_path):
    from stockmoney.desk.learn import job, links
    from test_learn import make_bars as long_bars
    monkeypatch.setattr(job.notify, "send", lambda *a, **k: True)
    monkeypatch.setattr(job, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(job.earnings, "refresh", lambda *a, **k: None)
    monkeypatch.setattr(job.filings, "refresh", lambda *a, **k: None)
    monkeypatch.setattr(links, "default_fetch", lambda url, timeout=0: (_ for _ in ()).throw(OSError("offline")))
    bars = long_bars(423)
    d = bars.dates
    option(con, "p1", str(d[-2].date()), 40.0, source="vwap_reclaim", mode="shadow")
    now = datetime(d[-1].year, d[-1].month, d[-1].day, 21, 15, tzinfo=timezone.utc)
    body = job.run(con, now, bars=bars, shadow_start=str(d[-200].date()), with_allocator=False)
    assert "## 來源記分板" in body and "規則劇本 vwap_reclaim(影子)" in body
    failed = body.split("## 本次失敗的步驟")[1] if "## 本次失敗的步驟" in body else ""
    assert "scoreboard" not in failed
    assert scores(con, str(d[-1].date()), "playbook:vwap_reclaim:shadow", "all")["pnl_total"][1] == 40.0


def test_trading_code_does_not_read_the_scoreboard():
    root = pathlib.Path("src/stockmoney/desk")
    paths = [root / "bridge" / "policy.py", *(root / "execution").rglob("*.py"), *(root / "options").rglob("*.py"),
             root / "run.py", root / "veto.py", root / "engine.py", root / "live.py", root / "intraday.py"]
    assert len(paths) > 8
    for p in paths:
        text = p.read_text()
        assert "scoreboard" not in text and "source_scores" not in text, p
    code = ("import sys; import stockmoney.desk.bridge.policy, stockmoney.desk.run; "
            "assert 'stockmoney.desk.learn.scoreboard' not in sys.modules")
    subprocess.run([sys.executable, "-c", code], check=True, env={**os.environ, "PYTHONPATH": "src"})


def test_alerts_scored_per_sender(con):
    """Grok's own-channel ALERTs score as grok_alert, not as Muse's; a Muse
    ALERT and a Grok ALERT on the same ticker and day both count."""
    bars = make_bars()
    d = bars.dates[-10]
    for mid, src in (("alert-1", "muse"), ("alert-grok-1", "grok_verified"), ("alert-grok-2", "grok_legacy"),
                     ("alert-0", None)):
        con.execute("INSERT INTO bridge_messages (msg_id, type, sender, sent_at, received_at, raw_body, parsed_json, "
                    "source) VALUES (?,?,?,?,?,?,?,?)",
                    (mid, "ALERT", "x", f"{d.date()}T12:00:00+00:00", f"{d.date()}T12:00:00+00:00", "{}",
                     json.dumps({"type": "ALERT", "ticker": "NVDA", "severity": "elevated"}), src))
    got = sorted(o["source"] for o in sb.alert_obs(con, bars))
    assert got == ["grok_alert", "grok_alert:legacy", "muse_alert"]
    assert "grok_alert" in sb.FIXED and "grok_alert" in sb.LABEL
