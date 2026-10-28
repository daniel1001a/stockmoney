import sqlite3
from dataclasses import asdict
from types import SimpleNamespace

import pandas as pd
import pytest

from stockmoney.desk import wind
from stockmoney.desk.indicators import compute
from stockmoney.desk.learn import shadow
from stockmoney.desk.learn.h_reports import HYPOTHESES
from tests.desk.test_desk import synthetic_bars


@pytest.fixture(scope="module")
def bars():
    return synthetic_bars(n=600, seed=3)


@pytest.fixture(scope="module")
def ind(bars):
    return compute(bars)


def _con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    return con


def test_wind_has_no_lookahead(bars, ind):
    for t in bars.dates[[300, 450, 599]]:
        full = asdict(wind.wind(ind, t))
        trunc = asdict(wind.wind(compute(bars.upto(t)), t))
        for k in ("vol20", "vol_pct", "spy_ret21", "breadth"):
            assert full.pop(k) == pytest.approx(trunc.pop(k), rel=1e-9, nan_ok=True)
        assert full == trunc


def _fake(monkeypatch, regime, breadth, vol_pct, spy21):
    monkeypatch.setattr(wind, "market_state", lambda ind, t: SimpleNamespace(regime=regime, breadth=breadth))
    monkeypatch.setattr(wind, "_vol_frames", lambda ind: (pd.Series({T: 0.15}), pd.Series({T: vol_pct})))
    monkeypatch.setattr(wind, "leaders", lambda ind, t: [])
    return SimpleNamespace(ret21=pd.DataFrame({wind.MARKET: [spy21]}, index=[T]))


T = pd.Timestamp("2026-10-27")


@pytest.mark.parametrize("regime,breadth,vol_pct,spy21,state,score", [
    ("risk_on", 0.65, 0.2, 0.02, "tailwind", 4),
    ("risk_on", 0.50, 0.6, 0.0, "neutral", 1),
    ("risk_off", 0.65, 0.2, 0.02, "headwind", 2),  # risk_off always headwind
    ("neutral", 0.30, 0.9, -0.03, "headwind", -3),
    ("neutral", 0.65, 0.2, 0.0, "tailwind", 2),
])
def test_state_rules(monkeypatch, regime, breadth, vol_pct, spy21, state, score):
    w = wind._wind(_fake(monkeypatch, regime, breadth, vol_pct, spy21), T)
    assert (w.state, w.score) == (state, score)


def test_record_first_write_sticks_and_state_before(ind):
    con = _con()
    d = ind.close.index
    t1, t2 = d[400], d[401]
    wind.record(con, ind, t1, "first")
    wind.record(con, ind, t1, "second")
    rows = con.execute("SELECT recorded_at FROM wind_daily").fetchall()
    assert [r[0] for r in rows] == ["first"]
    assert wind.state_before(con, str(t2.date())) == wind.wind(ind, t1).state
    assert wind.state_before(con, str(t1.date())) is None


def test_backfill_fills_gap(ind, monkeypatch):
    con = _con()
    d = ind.close.index
    monkeypatch.setattr(wind, "BACKFILL_FROM", str(d[500].date()))
    w = wind.backfill(con, ind, d[510], "x")
    assert w.date == str(d[510].date())
    assert con.execute("SELECT COUNT(*) FROM wind_daily").fetchone()[0] == 11
    wind.backfill(con, ind, d[515], "y")
    assert con.execute("SELECT COUNT(*) FROM wind_daily").fetchone()[0] == 16


def test_report_without_options_table(ind):
    con = _con()
    assert wind.report(con) == ""
    wind.record(con, ind, ind.close.index[400], "x")
    out = wind.report(con)
    assert "風向層" in out and "期權已平倉單" not in out


def test_report_groups_options_by_prior_wind(ind):
    con = _con()
    d = ind.close.index
    wind.record(con, ind, d[400], "x")
    con.execute("CREATE TABLE options_positions (day TEXT, mode TEXT, dte_label TEXT, source TEXT,"
                " exit_variant TEXT, state TEXT, direction TEXT, realized_usd REAL, fees_usd REAL, exit_time TEXT)")
    day = str(d[401].date())
    for row in [("shadow", "1-3dte", "orb15", "A", "EXITED", "call", 120.0, 2.0, "t"),
                ("shadow", "1-3dte", "orb15", "B", "EXITED", "call", 90.0, 2.0, "t"),  # variant B: excluded
                ("live", "1-3dte", "orb15", "A", "ABANDONED", "call", 0.0, 0.0, "t"),  # never filled: excluded
                ("shadow", "1-3dte", "orb15", "A", "OPEN", "call", 50.0, 1.0, None)]:  # open: excluded
        con.execute("INSERT INTO options_positions VALUES (?,?,?,?,?,?,?,?,?,?)", (day, *row))
    df = wind.options_by_wind(con)
    assert len(df) == 1 and df.pnl.iloc[0] == 118.0 and df.wind.iloc[0] == wind.wind(ind, d[400]).state
    assert "| shadow | 1-3dte | orb15 |" in wind.report(con)


class _Inner:
    name, params = "x", {}

    def candidates(self, ctx):
        return ["sig"]

    def exits(self):
        return "exits"


def test_wind_guard(monkeypatch):
    state = {"s": "headwind"}
    monkeypatch.setattr(wind, "wind", lambda ind, t: SimpleNamespace(state=state["s"]))
    g = shadow.WindGuard(_Inner(), frozenset({"tailwind"}))
    ctx = SimpleNamespace(ind=None, t=T)
    assert g.candidates(ctx) == []
    state["s"] = "tailwind"
    assert g.candidates(ctx) == ["sig"]
    assert g.exits() == "exits" and g.name == "x"


def test_registered():
    assert {"shadow:combined_wind", "shadow:combined_nohead"} <= set(shadow.VARIANTS)
    assert HYPOTHESES["H5"]["candidate"] == "shadow:combined_wind"
    assert HYPOTHESES["H5b"]["candidate"] == "shadow:combined_nohead"
