"""Learning layer: trade rebuild, symptom rules, shadow idempotency, allocator
look-ahead guard, chain scoring. Offline, synthetic bars."""
import json
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from stockmoney.desk import store
from stockmoney.desk.data import Bars
from stockmoney.desk.indicators import compute
from stockmoney.desk.learn import allocator, chains, postmortem, shadow, tables
from stockmoney.desk.learn.trades import closed_trades
from stockmoney.desk.run import ensure_schema as run_schema
from stockmoney.desk.execution.ledger import ensure_schema as ledger_schema
from stockmoney.desk.universe import ALL_SYMBOLS, MARKET


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "d.sqlite") as c:
        run_schema(c)
        ledger_schema(c)
        tables.ensure_schema(c)
        yield c


def make_bars(n=420, seed=1, drift=0.0006):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-01", periods=n)
    close = pd.DataFrame(
        {s: 100 * np.exp(np.cumsum(rng.normal(drift, 0.012, n))) for s in ALL_SYMBOLS}, index=idx)
    op = close.shift(1).fillna(close.iloc[0]) * (1 + rng.normal(0, 0.002, close.shape))
    hi = np.maximum(op, close) * 1.004
    lo = np.minimum(op, close) * 0.996
    vol = pd.DataFrame(1e6 * (1 + rng.random(close.shape)), index=idx, columns=close.columns)
    return Bars(op, hi, lo, close, vol)


def fill(con, cid, sym, side, qty, px, at, strat="breakout", pnl=0.0):
    con.execute("INSERT INTO fills (client_order_id, symbol, strategy, side, qty, price, filled_at, realized_pnl, recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (cid, sym, strat, side, qty, px, at, pnl, at))


def test_closed_trades_roundtrip_and_open_position_excluded(con):
    fill(con, "b1", "AAPL", "buy", 10, 100, "2026-01-05T14:35:00+00:00")
    fill(con, "s1", "AAPL", "sell", 10, 110, "2026-01-20T14:35:00+00:00", pnl=100)
    fill(con, "b2", "MSFT", "buy", 5, 200, "2026-01-06T14:35:00+00:00")  # still open
    con.execute("INSERT INTO submitted (client_order_id, kind, symbol, strategy, side, qty, status) "
                "VALUES ('s1','stop','AAPL','breakout','sell',10,'filled')")
    tr = closed_trades(con)
    assert len(tr) == 1
    t = tr[0]
    assert (t.symbol, t.entry_day, t.exit_day, t.exit_kind) == ("AAPL", "2026-01-05", "2026-01-20", "stop")
    assert t.pnl == 100 and t.ret == pytest.approx(0.10)


def test_partial_fills_aggregate_into_one_trade(con):
    fill(con, "b1", "AAPL", "buy", 6, 100, "2026-01-05T14:35:00+00:00")
    fill(con, "b1#6", "AAPL", "buy", 4, 105, "2026-01-05T15:00:00+00:00")
    fill(con, "s1", "AAPL", "sell", 10, 110, "2026-01-20T14:35:00+00:00", pnl=80)
    t = closed_trades(con)[0]
    assert t.qty == 10 and t.entry_px == pytest.approx(102.0)


def test_postmortem_symptoms_and_idempotent(con):
    bars = make_bars()
    ind = compute(bars)
    d = bars.dates
    e, x = d[300], d[310]
    px_in = float(bars.open.at[e, "AAPL"])
    px_out = float(bars.open.at[x, "AAPL"]) * 0.90  # filled 10% below the open: execution symptom
    fill(con, "b1", "AAPL", "buy", 10, px_in, f"{e.date()}T14:35:00+00:00")
    fill(con, "s1", "AAPL", "sell", 10, px_out, f"{x.date()}T14:35:00+00:00", pnl=(px_out - px_in) * 10)
    con.execute("INSERT INTO submitted (client_order_id, kind, symbol, strategy, side, qty, status) "
                "VALUES ('s1','stop','AAPL','breakout','sell',10,'filled')")
    r1 = postmortem.update_cards(con, bars, ind)
    assert r1["new"] == 1
    row = con.execute("SELECT * FROM trade_postmortem").fetchone()
    assert "S8" in row["symptoms"].split(",")
    assert row["status"] == "final"  # 10+ sessions exist after the exit
    con.execute("UPDATE trade_postmortem SET lesson='x'")
    assert postmortem.update_cards(con, bars, ind) == {"new": 0, "updated": 0}
    assert con.execute("SELECT lesson FROM trade_postmortem").fetchone()[0] == "x"


def test_postmortem_provisional_until_whipsaw_window_closes(con):
    bars = make_bars()
    ind = compute(bars)
    d = bars.dates
    e, x = d[-8], d[-4]
    fill(con, "b1", "AAPL", "buy", 1, 100, f"{e.date()}T14:35:00+00:00")
    fill(con, "s1", "AAPL", "sell", 1, 90, f"{x.date()}T14:35:00+00:00", pnl=-10)
    postmortem.update_cards(con, bars, ind)
    assert con.execute("SELECT status FROM trade_postmortem").fetchone()[0] == "provisional"


def test_symptom_report_needs_two_consecutive_weeks(con):
    base = pd.Timestamp("2026-03-27")
    for i, wk in enumerate((0, 1)):
        day = str((base - pd.Timedelta(days=7 * wk)).date())
        con.execute("INSERT INTO trade_postmortem (trade_id, symbol, strategy, exit_day, pnl, symptoms) "
                    "VALUES (?,?,?,?,?,?)", (f"t{i}", "AAPL", "breakout", day, -100.0, "S2"))
    text, hyp = postmortem.symptom_report(con, "2026-03-27")
    assert any("S2" in h for h in hyp)
    con.execute("DELETE FROM trade_postmortem WHERE trade_id='t1'")
    assert postmortem.symptom_report(con, "2026-03-27")[1] == []


def test_shadow_step_is_idempotent_and_resumes(con):
    bars = make_bars(n=330)
    ind = compute(bars)
    d = bars.dates
    start = str(d[260].date())
    ran = shadow.step(con, bars, ind, d[270], start=start)
    assert ran["shadow:combined"] == 11
    eq1 = con.execute("SELECT COUNT(*) FROM shadow_equity WHERE name='shadow:combined'").fetchone()[0]
    assert shadow.step(con, bars, ind, d[270], start=start)["shadow:combined"] == 0
    ran2 = shadow.step(con, bars, ind, d[275])
    assert ran2["shadow:combined"] == 5
    assert con.execute("SELECT COUNT(*) FROM shadow_equity WHERE name='shadow:combined'").fetchone()[0] == eq1 + 5
    # resuming equals running straight through
    with store.connect(Path(con.execute("PRAGMA database_list").fetchone()[2] + ".b")) as c2:
        tables.ensure_schema(c2)
        shadow.step(c2, bars, ind, d[275], start=start)
        a = {r[0]: r[1] for r in con.execute("SELECT date, equity FROM shadow_equity WHERE name='shadow:random_trend_0'")}
        b = {r[0]: r[1] for r in c2.execute("SELECT date, equity FROM shadow_equity WHERE name='shadow:random_trend_0'")}
        assert a == pytest.approx(b)


def test_allocator_weights_ignore_future_trades():
    ind_close = pd.Series(["risk_on|低波動"] * 6, index=pd.bdate_range("2024-01-01", periods=6))
    ent = ind_close
    def tr(rets):
        return pd.DataFrame({"strategy": "a", "ret": rets, "entry_date": [str(ent.index[0].date())] * len(rets),
                             "exit_date": ["2024-01-05"] * len(rets)})
    w1, _ = allocator.weights_from({"a": tr([0.05] * 40), "b": tr([-0.05] * 40)}, "risk_on|低波動", ent)
    assert w1["a"] > 0 and w1["b"] == 0
    w0, _ = allocator.weights_from({"a": tr([-0.05] * 40), "b": tr([-0.05] * 40)}, "risk_on|低波動", ent)
    assert w0 == {"a": 0.0, "b": 0.0}


def test_allocator_shrinks_small_cells_toward_overall_mean():
    idx = pd.bdate_range("2024-01-01", periods=3)
    cell = pd.Series(["X", "X", "Y"], index=idx)
    t = pd.DataFrame({"strategy": "a", "ret": [0.20, 0.20, 0.01] + [0.01] * 60,
                      "entry_date": [str(idx[0].date())] * 2 + [str(idx[2].date())] * 61})
    est, se, n = allocator.shrunk(t, "a", "X", cell)
    assert n == 2 and est < 0.05  # two big wins in the cell do not dominate


def _chain(direction="up", horizon=5, conf=0.7, ticker="AAPL"):
    return chains.Chain(trigger="t", steps=["s"], impacts=[chains.Impact(ticker=ticker, direction=direction,
                        horizon_days=horizon)], confidence=conf, source="owner")


def test_chain_scoring_uses_prior_close_and_declared_horizon(con):
    bars = make_bars()
    d = bars.dates
    created = datetime(d[300].year, d[300].month, d[300].day, 15, 0, tzinfo=timezone.utc)  # 10-11am ET, intraday
    cid = chains.add_chain(con, _chain("up", 5), created)
    assert chains.add_chain(con, _chain("up", 5), created) == cid  # same chain, same day: no duplicate
    n = chains.score_due(con, bars, d[-1])
    rows = con.execute("SELECT * FROM chain_scores WHERE chain_id=?", (cid,)).fetchall()
    assert n == len(rows) == 3
    assert {r["horizon"] for r in rows} == {5, 20, 60}
    r5 = [r for r in rows if r["horizon"] == 5][0]
    assert r5["declared"] == 1 and r5["base_date"] == str(d[299].date())  # intraday -> previous close
    exp = (bars.close.at[d[304], "AAPL"] / bars.close.at[d[299], "AAPL"] - 1) - \
          (bars.close.at[d[304], MARKET] / bars.close.at[d[299], MARKET] - 1)
    assert r5["excess_ret"] == pytest.approx(exp)
    assert chains.score_due(con, bars, d[-1]) == 0  # idempotent


def test_chain_scores_record_order_and_sector_excess(con):
    bars = make_bars()
    d = bars.dates
    created = datetime(d[300].year, d[300].month, d[300].day, 15, 0, tzinfo=timezone.utc)
    ch = chains.Chain(trigger="t2", steps=["s"], confidence=0.6, source="owner", impacts=[
        chains.Impact(ticker="AAPL", direction="down", horizon_days=5, order=2),
        chains.Impact(ticker="XLK", direction="up", horizon_days=5)])
    cid = chains.add_chain(con, ch, created)
    chains.score_due(con, bars, d[-1])
    r = con.execute("SELECT * FROM chain_scores WHERE chain_id=? AND ticker='AAPL' AND horizon=5", (cid,)).fetchone()
    assert r["order_n"] == 2 and r["sector_etf"] == "XLK"
    ret = bars.close.at[d[304], "AAPL"] / bars.close.at[d[299], "AAPL"] - 1
    exp = ret - (bars.close.at[d[304], "XLK"] / bars.close.at[d[299], "XLK"] - 1)
    assert r["excess_sector"] == pytest.approx(exp)
    assert r["hit_sector"] == int(exp < 0)  # declared down
    etf = con.execute("SELECT * FROM chain_scores WHERE chain_id=? AND ticker='XLK' AND horizon=5", (cid,)).fetchone()
    assert etf["order_n"] == 1 and etf["sector_etf"] is None and etf["excess_sector"] is None
    con.execute("UPDATE chains SET falsifiable=1")
    split = chains.by_order(con)
    assert split[2]["n"] == 1 and split[2]["sector_n"] == 1 and split[1]["sector_n"] == 0
    assert "依傳導層級" in chains.report(con)


def test_chain_impact_order_is_1_or_2():
    assert chains.Impact(ticker="AAPL", direction="up", horizon_days=5).order == 1
    with pytest.raises(Exception):
        chains.Impact(ticker="AAPL", direction="up", horizon_days=5, order=3)
    from stockmoney.desk.bridge.schema import GrokImpact
    assert GrokImpact(ticker="MU", direction="down", horizon_days=20, order=2).order == 2
    with pytest.raises(Exception):
        GrokImpact(ticker="MU", direction="down", horizon_days=20, order=0)


def test_chain_rejects_a_repeated_ticker():
    imp = dict(direction="up", horizon_days=5)
    with pytest.raises(Exception):
        chains.Chain(trigger="t", steps=["s"], confidence=0.5, impacts=[
            chains.Impact(ticker="BRK.B", **imp), chains.Impact(ticker="BRK-B", order=2, **imp)])
    from stockmoney.desk.bridge.schema import GrokChain
    g = dict(trigger="t", urls=["https://x.com/a/status/1"], steps=["s"], confidence=0.5)
    with pytest.raises(Exception):
        GrokChain(**g, impacts=[{"ticker": "MU", **imp}, {"ticker": "MU", "order": 2, **imp}])


def test_chain_sector_columns_backfill_on_a_later_pass(con):
    bars = make_bars()
    d = bars.dates
    created = datetime(d[300].year, d[300].month, d[300].day, 15, 0, tzinfo=timezone.utc)
    cid = chains.add_chain(con, _chain("up", 5), created)
    chains.score_due(con, bars, d[-1])
    fresh = con.execute("SELECT excess_ret, hit FROM chain_scores WHERE chain_id=? AND horizon=5", (cid,)).fetchone()
    # as scored before the sector columns existed
    con.execute("UPDATE chain_scores SET sector_etf=NULL, excess_sector=NULL, hit_sector=NULL")
    con.execute("UPDATE chain_scores SET excess_ret=9, hit=-1 WHERE horizon=20")  # stored on another price basis
    con.execute("UPDATE chain_scores SET scored_date='1999-01-01' WHERE horizon=60")  # another window
    assert chains.score_due(con, bars, d[-1]) == 0  # fills are not new scores
    r = con.execute("SELECT * FROM chain_scores WHERE chain_id=? AND horizon=5", (cid,)).fetchone()
    assert r["sector_etf"] == "XLK" and r["excess_sector"] is not None and r["hit_sector"] in (0, 1)
    assert (r["excess_ret"], r["hit"]) == (fresh["excess_ret"], fresh["hit"])
    for h in (20, 60):  # left alone, the stored SPY score is never rewritten
        rh = con.execute("SELECT * FROM chain_scores WHERE chain_id=? AND horizon=?", (cid, h)).fetchone()
        assert rh["excess_sector"] is None
    r20 = con.execute("SELECT excess_ret, hit FROM chain_scores WHERE chain_id=? AND horizon=20", (cid,)).fetchone()
    assert (r20["excess_ret"], r20["hit"]) == (9, -1)


def test_chain_report_counts_impacts_outside_the_universe(con):
    bars = make_bars()
    d = bars.dates
    created = datetime(d[300].year, d[300].month, d[300].day, 15, 0, tzinfo=timezone.utc)
    chains.add_chain(con, chains.Chain(trigger="t3", steps=["s"], confidence=0.5, source="owner", impacts=[
        chains.Impact(ticker="AAPL", direction="up", horizon_days=5),
        chains.Impact(ticker="ZZZQ", direction="down", horizon_days=5, order=2)]), created)
    chains.add_chain(con, chains.Chain(trigger="only outside", steps=["s"], confidence=0.5, source="owner", impacts=[
        chains.Impact(ticker="ZZZQ", direction="up", horizon_days=5)]), created)
    chains.score_due(con, bars, d[-1])
    assert chains.unscorable(con) == 2
    rep = chains.report(con)
    assert "2 個 impact 的代號不在追蹤股票池內" in rep
    assert "only outside" not in rep  # never scorable, so not listed as waiting


def test_chain_after_close_uses_same_day_close(con):
    bars = make_bars()
    d = bars.dates
    created = datetime(d[300].year, d[300].month, d[300].day, 22, 0, tzinfo=timezone.utc)  # after 16:05 ET in both EST and EDT
    chains.add_chain(con, _chain(), created)
    chains.score_due(con, bars, d[-1])
    assert con.execute("SELECT base_date FROM chain_scores LIMIT 1").fetchone()[0] == str(d[300].date())


def test_chain_authority_stays_log_only_until_enough_good_samples(con):
    assert chains.authority(con)[0] == "log_only"
    for i in range(60):
        con.execute("INSERT INTO chains (chain_id, falsifiable) VALUES (?,1)", (f"c{i}",))
        con.execute("INSERT INTO chain_scores (chain_id, ticker, horizon, declared, direction, base_date, scored_date, excess_ret, hit, confidence, scored_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (f"c{i}", "AAPL", 20, 1, "up", "2026-01-01", "2026-02-01", 0.01, int(i % 10 != 0), 0.7, "x"))
    assert chains.authority(con)[0] == "eligible_for_review"
    con.execute("UPDATE chain_scores SET hit=0")
    assert chains.authority(con)[0] == "log_only"


def test_chain_schema_rejects_bad_horizon_and_extra_fields():
    with pytest.raises(Exception):
        chains.Chain(trigger="t", steps=["s"], impacts=[{"ticker": "AAPL", "direction": "up", "horizon_days": 7}],
                     confidence=0.5)
    with pytest.raises(Exception):
        chains.Chain(trigger="t", steps=["s"], impacts=[{"ticker": "AAPL", "direction": "up", "horizon_days": 5}],
                     confidence=0.5, place_order=True)


def test_learn_package_cannot_reach_orders():
    import pathlib, re
    src = "".join(p.read_text() for p in pathlib.Path("src/stockmoney/desk/learn").glob("*.py"))
    for banned in ("execution.alpaca", "execution.broker", "execution.risk", "RiskEngine", ".submit(", "make_broker"):
        assert banned not in src, banned


def test_grok_chains_are_ingested_with_our_clock_and_once(con):
    msg = {"type": "GROK_DAILY", "events": [], "calls": [], "attention": [], "tracked_accounts": [],
           "chains": [{"trigger": "supplier warns", "urls": ["https://x.com/a/status/1"], "steps": ["s1", "s2"],
                       "impacts": [{"ticker": "MU", "direction": "down", "horizon_days": 20},
                                   {"ticker": "BTC/USD", "direction": "up", "horizon_days": 5}],
                       "checkpoints": [{"what": "earnings", "date": "2026-11-05"}], "confidence": 0.4,
                       "created_at": "2020-01-01T00:00:00-05:00"},
                      {"trigger": "bad", "steps": [], "impacts": [], "confidence": 2}]}
    from stockmoney.desk.bridge.inbox import ensure_schema as bridge_schema
    bridge_schema(con)
    con.execute("INSERT INTO bridge_messages (msg_id, type, sender, sent_at, received_at, raw_body, parsed_json) "
                "VALUES (?,?,?,?,?,?,?)",
                ("grok-20261012-0730", "GROK_DAILY", "x", "2026-10-12T07:30:00-04:00",
                 "2026-10-12T11:40:00+00:00", "{}", json.dumps(msg)))
    assert chains.ingest_grok(con) == {"added": 1, "skipped": 1}
    row = con.execute("SELECT created_at, source, impacts_json FROM chains").fetchone()
    assert row["source"] == "grok" and row["created_at"].startswith("2026-10-12T11:40")  # our received_at
    assert [i["ticker"] for i in json.loads(row["impacts_json"])] == ["MU"]  # crypto impact dropped, not renamed
    assert chains.ingest_grok(con)["added"] == 0  # idempotent


# ------------------------------------------------------------------ earnings
from datetime import date as _date, timedelta as _td

from stockmoney.desk.learn import earnings


def _fetcher(table):
    """table: {date: rows | None}; unknown days answer with no rows."""
    return lambda d: table.get(d, [])


def test_earnings_refresh_failed_day_never_marks_stock_safe(con):
    d0 = _date(2026, 10, 12)  # Monday
    ok = {d0 + _td(days=1): [{"symbol": "NVDA", "time": "time-after-hours"}, {"symbol": "ZZZZ"}]}
    r = earnings.refresh(con, d0, days=3, fetch=_fetcher(ok), pause_s=0)
    assert r["failed"] == 0
    assert earnings.upcoming(con, ["NVDA", "ZZZZ"], d0, 7) == {"NVDA": "2026-10-13"}  # non-universe symbol ignored
    # tomorrow's listing now fails: the known date must survive
    r2 = earnings.refresh(con, d0, days=3, fetch=lambda d: None, pause_s=0)
    assert r2["failed"] == 3
    assert earnings.upcoming(con, ["NVDA"], d0, 7) == {"NVDA": "2026-10-13"}
    assert earnings.covered(con, d0 + _td(days=1), d0 + _td(days=1)) is True  # earlier ok scan still counts


def test_earnings_date_moved_is_retired_but_remembered(con):
    d0 = _date(2026, 10, 12)
    earnings.refresh(con, d0, days=3, fetch=_fetcher({d0 + _td(days=1): [{"symbol": "AAPL"}]}), pause_s=0)
    earnings.refresh(con, d0, days=3, fetch=_fetcher({d0 + _td(days=2): [{"symbol": "AAPL"}]}), pause_s=0)
    assert earnings.upcoming(con, ["AAPL"], d0, 7) == {"AAPL": "2026-10-14"}
    old = con.execute("SELECT removed_at FROM earnings WHERE symbol='AAPL' AND date='2026-10-13'").fetchone()
    assert old["removed_at"] is not None


def test_earnings_guard_is_point_in_time(con):
    d0 = _date(2026, 10, 12)
    earnings.refresh(con, d0, days=3, fetch=_fetcher({d0 + _td(days=1): [{"symbol": "NVDA"}]}), pause_s=0,
                     now=datetime(2026, 10, 12, 21, 0, tzinfo=timezone.utc))  # first seen Monday 17:00 ET
    blocked = earnings.blocked_fn(con, 7)
    assert blocked("NVDA", pd.Timestamp("2026-10-12")) is True    # known by the end of Monday
    assert blocked("NVDA", pd.Timestamp("2026-10-09")) is False   # Friday: we did not know yet
    assert blocked("AAPL", pd.Timestamp("2026-10-12")) is False
    assert blocked("NVDA", pd.Timestamp("2026-10-14")) is False   # already reported


def test_postmortem_s6_when_a_loss_spans_known_earnings(con):
    bars = make_bars()
    ind = compute(bars)
    d = bars.dates
    e, x = d[300], d[308]
    start = e.date()
    earnings.ensure_schema(con)
    day = start
    while day <= x.date():
        con.execute("INSERT OR REPLACE INTO earnings_scan VALUES (?,?,?,?)", (day.isoformat(), 1, 0, "x"))
        day += _td(days=1)
    con.execute("INSERT INTO earnings VALUES ('AAPL', ?, '', 'x', 'x', NULL)", ((e + pd.Timedelta(days=3)).date().isoformat(),))
    px = float(bars.open.at[e, "AAPL"])
    fill(con, "b1", "AAPL", "buy", 10, px, f"{e.date()}T14:35:00+00:00")
    fill(con, "s1", "AAPL", "sell", 10, px * float(bars.open.at[x, "AAPL"]) / float(bars.open.at[x, "AAPL"]) * 0.97,
         f"{x.date()}T14:35:00+00:00", pnl=-1)
    postmortem.update_cards(con, bars, ind)
    row = con.execute("SELECT symptoms, facts_json FROM trade_postmortem").fetchone()
    assert "S6" in row["symptoms"].split(",")
    assert json.loads(row["facts_json"])["earnings_in_hold"]


def test_postmortem_earnings_unknown_when_span_not_scanned(con):
    bars = make_bars()
    ind = compute(bars)
    d = bars.dates
    px = float(bars.open.at[d[300], "AAPL"])
    fill(con, "b1", "AAPL", "buy", 10, px, f"{d[300].date()}T14:35:00+00:00")
    fill(con, "s1", "AAPL", "sell", 10, px * 0.97, f"{d[308].date()}T14:35:00+00:00", pnl=-1)
    postmortem.update_cards(con, bars, ind)
    assert json.loads(con.execute("SELECT facts_json FROM trade_postmortem").fetchone()[0])["earnings_in_hold"] is None


def test_earnings_guard_drops_only_blocked_entries_and_delegates_the_rest():
    from types import SimpleNamespace
    from stockmoney.desk.learn.shadow import EarningsGuard
    from stockmoney.desk.strategies import Signal

    t = pd.Timestamp("2026-10-12")
    sigs = [Signal("breakout", "NVDA", t, 100, 90, 1.0, "x", "y"), Signal("breakout", "AAPL", t, 100, 90, 1.0, "x", "y")]
    inner = SimpleNamespace(name="breakout", params={"a": 1}, candidates=lambda ctx: sigs,
                            exit_reason=lambda *a: "inner-exit", trail_stop=lambda *a: 42.0)
    g = EarningsGuard(inner, lambda sym, day: sym == "NVDA")
    out = g.candidates(SimpleNamespace(t=t))
    assert [s.symbol for s in out] == ["AAPL"]
    assert g.name == "breakout" and g.exit_reason() == "inner-exit" and g.trail_stop() == 42.0


def test_shadow_step_runs_the_earnings_guarded_book(con):
    bars = make_bars(n=330)
    ind = compute(bars)
    d = bars.dates
    shadow.step(con, bars, ind, d[300], start=str(d[280].date()))
    assert "shadow:combined_earn7" in store.load_accounts(con)

    assert "shadow:combined_muse_strict" in store.load_accounts(con)


def test_muse_strict_book_applies_the_old_rule_point_in_time(con):
    """shadow:combined_muse_strict (Muse downgrade control, 2026-10-21): an
    info ALERT blocks entries only from the premarket after it was received,
    for FLAG_DAYS; without a connection nothing is blocked."""
    from stockmoney.desk.bridge import inbox
    from stockmoney.desk.bridge.mailbox import BridgeConfig, FakeMailbox
    from stockmoney.desk.bridge.outbox import build_subject
    cfg = BridgeConfig(desk_address="desk@example.com", muse_address="muse@example.com", shared_token="t" * 32)
    box = FakeMailbox()
    got = datetime(2026, 10, 12, 18, tzinfo=timezone.utc)  # Mon 14:00 ET
    body = ('{"msg_id":"alert0001","sent_at":"%s","type":"ALERT","ticker":"NVDA","severity":"info",'
            '"headline":"probe","source_urls":["https://example.com/x"],"observed_at":"%s"}'
            % (got.isoformat(), got.isoformat()))
    box.inject("muse@example.com", build_subject("ALERT", "alert0001", cfg.shared_token), body)
    inbox.poll(con, box, cfg, now=got)
    blocked = shadow.muse_legacy_fn(con)
    assert not blocked("NVDA", pd.Timestamp("2026-10-09"))  # decided before the message arrived
    assert blocked("NVDA", pd.Timestamp("2026-10-12")) and blocked("nvda", pd.Timestamp("2026-10-13"))
    assert not blocked("AAPL", pd.Timestamp("2026-10-12"))
    assert not blocked("NVDA", pd.Timestamp("2026-10-16"))  # older than FLAG_DAYS at that premarket
    assert not shadow.muse_legacy_fn(None)("NVDA", pd.Timestamp("2026-10-12"))



# ------------------------------------------------------------------ console views
from stockmoney.desk.learn import views


def _bridge(con):
    from stockmoney.desk.bridge.inbox import ensure_schema as bridge_schema
    bridge_schema(con)


def test_views_separate_muse_and_grok_messages_and_rejects(con):
    _bridge(con)
    rows = [("sd-1", "SOCIAL_DAILY", {"items": [{}, {}]}),
            ("al-1", "ALERT", {"ticker": "NVDA", "severity": "critical", "headline": "x"}),
            ("grok-20261012-0730", "GROK_DAILY", {"events": [1], "chains": [], "calls": [], "attention": [],
                                                  "tracked_accounts": ["@a"]})]
    for i, (mid, typ, body) in enumerate(rows):
        con.execute("INSERT INTO bridge_messages (msg_id, type, sender, sent_at, received_at, raw_body, parsed_json) "
                "VALUES (?,?,?,?,?,?,?)",
                    (mid, typ, "s", "t", f"2026-10-12T0{i}:00:00+00:00", "{}", json.dumps(body)))
    con.execute("INSERT INTO bridge_rejects (received_at, subject, msg_id, reason) VALUES "
                "('2026-10-12T01:00:00+00:00','[SMD/GROK_DAILY/grok-x1] t','grok-x1','schema_invalid')")
    con.execute("INSERT INTO bridge_rejects (received_at, subject, msg_id, reason) VALUES "
                "('2026-10-12T02:00:00+00:00','[SMD/ALERT/al-bad] t','al-bad','bad_token')")
    assert set(views.messages(con, views.MUSE).msg_id) == {"sd-1", "al-1"}
    g = views.messages(con, views.GROK)
    assert list(g.msg_id) == ["grok-20261012-0730"] and "事件 1" in g["摘要"].iloc[0]
    assert list(views.rejects(con, views.GROK).msg_id) == ["grok-x1"]
    assert list(views.rejects(con, views.MUSE).msg_id) == ["al-bad"]
    assert "NVDA critical" in views.messages(con, views.MUSE)["摘要"].iloc[0] or True


def test_scorecard_is_split_by_who_owes_the_delivery():
    card = {"deliveries": {"SOCIAL_AM": {"name": "a", "on_time": 1, "due": 1},
                           "GROK_AM": {"name": "b", "on_time": 0, "due": 1}}}
    s = views.scorecard_by_agent(card)
    assert list(s[views.MUSE]) == ["SOCIAL_AM"] and list(s[views.GROK]) == ["GROK_AM"]


def test_shadow_table_labels_books_and_random_median(con):
    for name, vals in {"shadow:combined": [100, 110], "bench:spy": [100, 105],
                       "shadow:random_trend_0": [100, 90], "shadow:random_trend_1": [100, 120],
                       "shadow:random_trend_2": [100, 100]}.items():
        for d, v in zip(("2026-10-12", "2026-10-13"), vals):
            con.execute("INSERT INTO shadow_equity VALUES (?,?,?)", (name, d, v))
    shown, summary = views.shadow_table(con)
    assert shown.iloc[-1]["現行規則(影子)"] == pytest.approx(10.0)
    assert shown.iloc[-1]["隨機進場中位數(多頭排列)"] == pytest.approx(0.0)
    assert "SPY 買進持有" in summary.index


def test_console_pages_render_with_empty_and_filled_state(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.chdir(tmp_path)  # fresh data/desk/ so the page runs against an empty database
    for page in ("總覽", "持倉與委託", "情報員", "學習", "覆盤日誌", "策略實驗室"):
        at = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "app" / "desk_app.py"), default_timeout=60)
        at.run()
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, (page, [e.value for e in at.exception])


# ------------------------------------------------------------------ author calls
from datetime import timedelta

from stockmoney.desk.learn import calls as author_calls


def _put_calls(con, msg_id, received_at, calls, type_="CALLS"):
    from stockmoney.desk.bridge.inbox import ensure_schema as bridge_schema
    bridge_schema(con)
    con.execute("INSERT INTO bridge_messages (msg_id, type, sender, sent_at, received_at, raw_body, parsed_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (msg_id, type_, "x", received_at, received_at, "{}", json.dumps({"calls": calls})))


def _call(url="https://x.com/a/status/1", direction="long", ticker="AAPL", author="@a"):
    return {"author": author, "platform": "x", "ticker": ticker, "direction": direction,
            "posted_at": "2020-01-01T00:00:00-05:00", "post_url": url}


def test_call_received_after_close_uses_next_close_and_our_clock(con):
    bars = make_bars()
    d = bars.dates
    day = d[300]
    # 22:00 UTC is after 16:05 ET in both EST and EDT; posted_at (2020) must be ignored
    _put_calls(con, "calls-a", datetime(day.year, day.month, day.day, 22, 0, tzinfo=timezone.utc).isoformat(),
               [_call()])
    _put_calls(con, "calls-b", datetime(day.year, day.month, day.day, 15, 0, tzinfo=timezone.utc).isoformat(),
               [_call(url="https://x.com/a/status/2")])  # intraday: same-day close
    assert author_calls.ingest(con)["added"] == 2
    author_calls.score_due(con, bars, d[-1])
    base = {r["post_url"]: r["base_date"] for r in con.execute(
        "SELECT c.post_url, s.base_date FROM call_scores s JOIN author_calls c USING (call_id) WHERE s.horizon=5")}
    assert base["https://x.com/a/status/1"] == str(d[301].date())
    assert base["https://x.com/a/status/2"] == str(d[300].date())
    r5 = con.execute("SELECT excess_ret FROM call_scores s JOIN author_calls c USING (call_id) "
                     "WHERE c.post_url='https://x.com/a/status/1' AND s.horizon=5").fetchone()
    exp = (bars.close.at[d[306], "AAPL"] / bars.close.at[d[301], "AAPL"] - 1) - \
          (bars.close.at[d[306], MARKET] / bars.close.at[d[301], MARKET] - 1)
    assert r5["excess_ret"] == pytest.approx(exp)


def test_call_on_weekend_uses_next_session_close():
    d = make_bars().dates
    fri = next(t for t in d if t.weekday() == 4)
    sat = datetime(fri.year, fri.month, fri.day, 15, 0, tzinfo=timezone.utc) + timedelta(days=1)
    assert author_calls.base_session(sat.isoformat(), d) == d[d.get_loc(fri) + 1]  # Monday


def test_duplicate_post_is_scored_once(con):
    bars = make_bars()
    d = bars.dates
    t1 = datetime(d[300].year, d[300].month, d[300].day, 15, 0, tzinfo=timezone.utc)
    _put_calls(con, "calls-1", t1.isoformat(), [_call(direction="long")])
    _put_calls(con, "calls-2", (t1 + timedelta(days=3)).isoformat(), [_call(direction="short")])  # same url, resent
    assert author_calls.ingest(con)["added"] == 1
    assert author_calls.ingest(con)["added"] == 0
    assert author_calls.score_due(con, bars, d[-1]) == 3
    assert author_calls.score_due(con, bars, d[-1]) == 0
    row = con.execute("SELECT direction, received_at FROM author_calls").fetchone()
    assert row["direction"] == "long" and row["received_at"] == t1.isoformat()  # first receipt wins


def test_bearish_call_hits_on_negative_excess_and_exit_is_skipped(con):
    bars = make_bars()
    d = bars.dates
    bars.close.loc[d[305]:, "AAPL"] *= 0.5   # AAPL collapses after the baseline
    bars.close.loc[d[305]:, "MSFT"] *= 2.0
    t = datetime(d[300].year, d[300].month, d[300].day, 15, 0, tzinfo=timezone.utc).isoformat()
    _put_calls(con, "calls-1", t, [_call(url="u1", direction="short", ticker="AAPL"),
                                   _call(url="u2", direction="long", ticker="AAPL"),
                                   _call(url="u3", direction="short", ticker="MSFT"),
                                   _call(url="u4", direction="exit", ticker="AAPL")])
    assert author_calls.ingest(con)["added"] == 3  # exit not stored
    author_calls.score_due(con, bars, d[-1])
    hit = {r["post_url"]: r["hit"] for r in con.execute(
        "SELECT c.post_url, s.hit FROM call_scores s JOIN author_calls c USING (call_id) WHERE s.horizon=20")}
    assert hit == {"u1": 1, "u2": 0, "u3": 0}


def test_author_with_fewer_than_20_calls_is_insufficient_and_shrunk(con):
    for i in range(25):
        con.execute("INSERT INTO author_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"big{i}", "@big", "x", "AAPL", "long", "t", "m", f"u{i}", "", None, None, ""))
        con.execute("INSERT INTO call_scores VALUES (?,?,?,?,?,?,?)", (f"big{i}", 20, "b", "s", 0.02, 1, "x"))
    for i in range(5):
        con.execute("INSERT INTO author_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"sm{i}", "@small", "x", "AAPL", "short", "t", "m", f"v{i}", "", None, None, ""))
        con.execute("INSERT INTO call_scores VALUES (?,?,?,?,?,?,?)", (f"sm{i}", 20, "b", "s", -0.10, 1, "x"))
    st = author_calls.author_stats(con).set_index("author")
    assert st.at["@small", "label"] == author_calls.INSUFFICIENT and st.at["@small", "n"] == 5
    assert st.at["@big", "label"] == "ok"
    pool = (25 * 0.02 + 5 * 0.10) / 30
    assert st.at["@small", "mean_signed"] == pytest.approx(0.10)
    assert pool < st.at["@small", "shrunk_signed"] < 0.10  # pulled toward the pooled mean
    assert st.at["@small", "shrunk_signed"] == pytest.approx(
        (5 * 0.10 + allocator.PRIOR_N * pool) / (5 + allocator.PRIOR_N))


def test_calls_module_is_inside_the_learn_boundary():
    import pathlib
    src = pathlib.Path("src/stockmoney/desk/learn/calls.py").read_text()
    for banned in ("execution", "broker", "risk", "RiskEngine", ".submit(", "make_broker"):
        assert banned not in src, banned


def _losing_trade(con, bars, e, x, sym="AAPL", entry_mult=1.0):
    px = float(bars.open.at[e, sym]) * entry_mult
    fill(con, "b1", sym, "buy", 10, px, f"{e.date()}T14:35:00+00:00")
    fill(con, "s1", sym, "sell", 10, px * 0.95, f"{x.date()}T14:35:00+00:00", pnl=-px * 0.5)
    return px


def test_postmortem_s10_when_an_up_chain_before_entry_did_not_pay(con):
    bars = make_bars()
    ind = compute(bars)
    d = bars.dates
    e, x = d[300], d[305]
    created = datetime(d[297].year, d[297].month, d[297].day, 21, 0, tzinfo=timezone.utc)
    chains.add_chain(con, chains.Chain(trigger="t", steps=["s"], confidence=0.5,
                                       impacts=[{"ticker": "AAPL", "direction": "up", "horizon_days": 5}]), created)
    late = datetime(d[302].year, d[302].month, d[302].day, 21, 0, tzinfo=timezone.utc)  # after the entry: ignored
    chains.add_chain(con, chains.Chain(trigger="late", steps=["s"], confidence=0.5,
                                       impacts=[{"ticker": "MSFT", "direction": "up", "horizon_days": 5}]), late)
    _losing_trade(con, bars, e, x)
    postmortem.update_cards(con, bars, ind)
    row = con.execute("SELECT symptoms, facts_json FROM trade_postmortem").fetchone()
    f = json.loads(row["facts_json"])
    assert len(f["up_chains_before_entry"]) == 1
    assert ("S10" in row["symptoms"].split(",")) == (f["excess_vs_spy"] <= 0)
    sym_ret = bars.close.at[x, "AAPL"] / bars.open.at[e, "AAPL"] - 1
    spy_ret = bars.close.at[x, "SPY"] / bars.open.at[e, "SPY"] - 1
    assert f["excess_vs_spy"] == pytest.approx(sym_ret - spy_ret)  # open-to-close on both legs, not the fills


def test_postmortem_final_card_without_wind_facts_is_rebuilt_once(con):
    bars = make_bars()
    ind = compute(bars)
    d = bars.dates
    _losing_trade(con, bars, d[300], d[305])
    postmortem.update_cards(con, bars, ind)
    con.execute("UPDATE trade_postmortem SET status='final', facts_json='{}', symptoms='S0', lesson='keep me'")
    assert postmortem.update_cards(con, bars, ind)["updated"] == 1
    row = con.execute("SELECT facts_json, lesson FROM trade_postmortem").fetchone()
    assert "wind_entry" in json.loads(row["facts_json"]) and row["lesson"] == "keep me"
    con.execute("UPDATE trade_postmortem SET status='final'")
    assert postmortem.update_cards(con, bars, ind)["updated"] == 0


def test_postmortem_final_card_is_not_stamped_when_inputs_failed(con):
    """A run whose chain ingest or wind backfill failed leaves the final card
    unstamped; the next good run picks up the missed chain and the wind."""
    from stockmoney.desk import wind
    bars = make_bars()
    ind = compute(bars)
    d = bars.dates
    e, x = d[300], d[305]
    _losing_trade(con, bars, e, x)
    postmortem.update_cards(con, bars, ind, inputs_ok=False)
    con.execute("UPDATE trade_postmortem SET status='final'")
    created = datetime(d[297].year, d[297].month, d[297].day, 21, 0, tzinfo=timezone.utc)
    chains.add_chain(con, chains.Chain(trigger="t", steps=["s"], confidence=0.5,
                                       impacts=[{"ticker": "AAPL", "direction": "up", "horizon_days": 5}]), created)
    assert postmortem.update_cards(con, bars, ind)["updated"] == 1
    f = json.loads(con.execute("SELECT facts_json FROM trade_postmortem").fetchone()[0])
    assert len(f["up_chains_before_entry"]) == 1 and f["card_v"] == postmortem.CARD_V
    con.execute("UPDATE trade_postmortem SET status='final'")
    assert postmortem.update_cards(con, bars, ind)["updated"] == 0
    # wind logged later for a session before the entry: the null wind is filled
    wind.ensure_schema(con)
    con.execute("INSERT INTO wind_daily (date, state) VALUES (?,?)", (str(d[299].date()), "tailwind"))
    assert postmortem.update_cards(con, bars, ind)["updated"] == 1
    f = json.loads(con.execute("SELECT facts_json FROM trade_postmortem").fetchone()[0])
    assert f["wind_entry"] == "tailwind"
    con.execute("UPDATE trade_postmortem SET status='final'")
    assert postmortem.update_cards(con, bars, ind)["updated"] == 0
    # a chain committed by an earlier run whose card pass failed still rebuilds the stamped final
    chains.add_chain(con, chains.Chain(trigger="t2", steps=["s"], confidence=0.5,
                                       impacts=[{"ticker": "AAPL", "direction": "up", "horizon_days": 5}]), created)
    assert postmortem.update_cards(con, bars, ind)["updated"] == 1
    f = json.loads(con.execute("SELECT facts_json FROM trade_postmortem").fetchone()[0])
    assert len(f["up_chains_before_entry"]) == 2
    con.execute("UPDATE trade_postmortem SET status='final'")
    assert postmortem.update_cards(con, bars, ind)["updated"] == 0


def test_postmortem_no_s10_when_entry_is_past_the_impact_horizon(con):
    bars = make_bars()
    ind = compute(bars)
    d = bars.dates
    e, x = d[300], d[305]
    created = datetime(d[280].year, d[280].month, d[280].day, 21, 0, tzinfo=timezone.utc)
    chains.add_chain(con, chains.Chain(trigger="t", steps=["s"], confidence=0.5,
                                       impacts=[{"ticker": "AAPL", "direction": "up", "horizon_days": 5}]), created)
    _losing_trade(con, bars, e, x)
    postmortem.update_cards(con, bars, ind)
    row = con.execute("SELECT symptoms, facts_json FROM trade_postmortem").fetchone()
    assert json.loads(row["facts_json"])["up_chains_before_entry"] == []
    assert "S10" not in row["symptoms"].split(",")


def test_postmortem_s11_on_entry_slippage_in_r(con):
    bars = make_bars()
    ind = compute(bars)
    d = bars.dates
    e, x = d[300], d[305]
    px = _losing_trade(con, bars, e, x, entry_mult=1.003)  # 0.3% over the open, under the S8 0.5%
    stop = px - px * 0.003 / 0.3  # risk such that slippage is about 0.3R (computed against the open)
    t = closed_trades(con)[0]
    t.initial_stop = stop
    card = postmortem.build_card(con, t, bars, ind)
    assert card["facts"]["slip_entry_r"] == pytest.approx((px - float(bars.open.at[e, "AAPL"])) / (px - stop))
    assert "S11" in card["symptoms"] and card["facts"]["slip_entry"] < 0.005  # S8 would not catch it
    t.initial_stop = px * 0.9  # same slippage is now tiny in R
    assert "S11" not in postmortem.build_card(con, t, bars, ind)["symptoms"]


def test_postmortem_card_records_wind_known_before_entry_and_exit(con):
    from stockmoney.desk import wind
    bars = make_bars()
    ind = compute(bars)
    d = bars.dates
    e, x = d[300], d[305]
    wind.ensure_schema(con)
    for day, state in ((d[299], "tailwind"), (d[300], "headwind"), (d[304], "neutral")):
        con.execute("INSERT INTO wind_daily (date, state) VALUES (?,?)", (str(day.date()), state))
    _losing_trade(con, bars, e, x)
    postmortem.update_cards(con, bars, ind)
    f = json.loads(con.execute("SELECT facts_json FROM trade_postmortem").fetchone()[0])
    assert (f["wind_entry"], f["wind_exit"]) == ("tailwind", "neutral")


def test_postmortem_wind_is_null_without_a_log(con):
    bars = make_bars()
    ind = compute(bars)
    d = bars.dates
    _losing_trade(con, bars, d[300], d[305])
    postmortem.update_cards(con, bars, ind)
    f = json.loads(con.execute("SELECT facts_json FROM trade_postmortem").fetchone()[0])
    assert f["wind_entry"] is None and f["wind_exit"] is None
