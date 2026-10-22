"""Intraday shadow (desk/intraday.py): offline, crafted daily history plus a fake
minute-bar client. Nothing here touches the network or a broker."""
import ast
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stockmoney.desk import intraday
from stockmoney.desk.data import FIELDS, Bars
from stockmoney.desk.intraday import ET
from stockmoney.desk.universe import ALL_SYMBOLS

DAY = date(2026, 10, 21)        # Wednesday, regular session
PREV = date(2026, 10, 20)
NEXT = date(2026, 10, 22)
FAST = ("NVDA", "SMH")          # the stock that breaks out and its (leading) sector ETF
JUMP_AT = time(10, 30)          # NVDA breaks out from this minute on
SRC = Path(intraday.__file__)


def et(h, m, d=DAY):
    return datetime.combine(d, time(h, m), ET)


def daily_history(end=PREV, n=300) -> Bars:
    """Steady uptrend for everything, faster for XLK and NVDA, flat volume
    (vol_ratio 1.0 so nothing breaks out without an intraday volume surge)."""
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    i = np.arange(n)[:, None]
    slope = np.array([0.002 if s in FAST else 0.001 for s in ALL_SYMBOLS])
    close = pd.DataFrame(100 * np.exp(slope * i), index=idx, columns=ALL_SYMBOLS)
    return Bars(close * 0.999, close * 1.005, close * 0.995, close,
                pd.DataFrame(1e6, index=idx, columns=ALL_SYMBOLS))


def minute_day(prev_close: float, surge: bool) -> pd.DataFrame:
    """Full regular session of minute bars. Flat at the prior close; with
    ``surge`` the price is +2% and volume heavy from JUMP_AT on."""
    idx = pd.date_range(et(9, 30), et(15, 59), freq="1min")
    late = np.array([t.time() >= JUMP_AT for t in idx]) if surge else np.zeros(len(idx), bool)
    c = np.where(late, prev_close * 1.02, prev_close)
    v = np.where(late, 100_000.0, 1_000.0)
    return pd.DataFrame({"open": c, "high": c * 1.001, "low": c * 0.999, "close": c, "volume": v}, index=idx)


class FakeBars:
    """Returns the WHOLE day regardless of ``end`` (a client that leaks future
    bars), so the module's own cut-off is what is under test."""

    def __init__(self, daily: Bars, feed="iex"):
        self.feed = feed
        self.calls = []
        last = daily.close.iloc[-1]
        self.day = {s: minute_day(float(last[s]), surge=(s == "NVDA")) for s in daily.close.columns}

    def minute_bars_many(self, symbols, start, end):
        self.calls.append((start, end))
        return {s: self.day[s] for s in symbols}


class SingleBars(FakeBars):
    """Per-symbol surface only (the options sleeve's minute_bars shape)."""
    minute_bars_many = None

    def minute_bars(self, symbol, start, end):
        self.calls.append((start, end))
        return self.day[symbol]


class Exploding:
    feed = "iex"

    def minute_bars_many(self, *a):
        raise AssertionError("client must not be called")


@pytest.fixture
def db(tmp_path):
    con = sqlite3.connect(tmp_path / "desk.sqlite")
    yield con
    con.close()


@pytest.fixture(scope="module")
def daily():
    return daily_history()


def rows(con):
    return con.execute("SELECT day, symbol, strategy, first_signal_at, signal_px, feed, bar_count, data_through"
                       " FROM intraday_shadow ORDER BY strategy, symbol").fetchall()


# ------------------------------------------------------------------ partial bar / look-ahead

def test_partial_bar_ignores_bars_after_cutoff(daily):
    m = minute_day(100.0, surge=True)
    bar, n = intraday.partial_bar(m, DAY, et(10, 30))
    assert n == 60  # 09:30 .. 10:29, each ended by 10:30
    assert bar["close"] == 100.0 and bar["high"] == pytest.approx(100.1)
    assert bar["volume"] == 60 * 1_000.0
    assert bar["through"] == et(10, 30)
    # a bar that starts at the cut-off minute has not ended: still excluded
    bar2, n2 = intraday.partial_bar(m, DAY, et(10, 30) + timedelta(seconds=59))
    assert n2 == 60 and bar2 == bar


def test_no_lookahead_trigger_only_from_bars_up_to_now(db, daily):
    client = FakeBars(daily)
    early = intraday.tick(db, et(10, 15), client, daily)  # surge in the returned data, but after now
    assert early.status == "ok" and early.recorded == []
    assert rows(db) == []
    late = intraday.tick(db, et(11, 0), client, daily)
    assert ("breakout", "NVDA") in late.recorded
    r = {x[2]: x for x in rows(db)}["breakout"]
    assert r[1] == "NVDA" and r[0] == str(DAY) and r[5] == "iex"
    assert r[6] == 90  # 09:30 .. 10:59
    assert r[7] == et(11, 0).astimezone(intraday.timezone.utc).isoformat(timespec="seconds")
    assert r[4] == pytest.approx(float(daily.close["NVDA"].iloc[-1]) * 1.02)
    # nothing else broke out (flat names have vol_ratio far below 1.3)
    assert {x[1] for x in rows(db)} == {"NVDA"}


def test_sip_reads_sixteen_minutes_behind(db, daily):
    client = FakeBars(daily, feed="sip")
    res = intraday.tick(db, et(10, 40), client, daily)  # cut-off 10:24: before the surge
    assert res.recorded == [] and client.calls[-1][1] == et(10, 24)
    res = intraday.tick(db, et(11, 0), client, daily)   # cut-off 10:44
    assert ("breakout", "NVDA") in res.recorded
    r = {x[2]: x for x in rows(db)}["breakout"]
    assert r[5] == "sip" and r[6] == 74  # 09:30 .. 10:43


def test_per_symbol_client_surface(db, daily):
    res = intraday.tick(db, et(11, 0), SingleBars(daily), daily)
    assert ("breakout", "NVDA") in res.recorded


def test_first_trigger_recorded_once_per_day(db, daily):
    client = FakeBars(daily)
    first = intraday.tick(db, et(11, 0), client, daily)
    assert ("breakout", "NVDA") in first.recorded
    again = intraday.tick(db, et(12, 0), client, daily)
    assert again.status == "ok" and again.recorded == []
    hits = db.execute("SELECT first_signal_at, bar_count FROM intraday_shadow"
                      " WHERE symbol='NVDA' AND strategy='breakout'").fetchall()
    assert len(hits) == 1 and hits[0][1] == 90  # the first trigger's data is kept


# ------------------------------------------------------------------ refusals

@pytest.mark.parametrize("now", [
    et(12, 0, date(2026, 10, 24)),   # Saturday
    et(9, 29),                       # before the open
    et(16, 0),                       # at the close
    et(20, 0),
])
def test_refuses_outside_regular_hours(db, daily, now):
    res = intraday.tick(db, now, Exploding(), daily)
    assert res.status == "outside_hours"
    assert db.execute("SELECT COUNT(*) FROM intraday_shadow").fetchone()[0] == 0


def test_cli_refuses_outside_hours_before_any_io(tmp_path, capsys):
    def boom(*a, **k):
        raise AssertionError("no client outside hours")
    db_path = tmp_path / "x.sqlite"
    rc = intraday.main(["intraday", "tick", "--db", str(db_path), "--data-dir", str(tmp_path / "none")],
                       clock=lambda: et(8, 0), client_factory=boom)
    assert rc == 0
    assert "非正常交易時段" in capsys.readouterr().err
    assert not db_path.exists()


def test_cli_skips_slot_when_db_locked(tmp_path, monkeypatch, caplog):
    """Grok review of step 7c: the options session can hold desk.sqlite past the 30s
    timeout; the scheduled tick logs and skips that slot instead of crashing."""
    def locked(path):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(intraday, "_connect", locked)
    rc = intraday.main(["intraday", "tick", "--db", str(tmp_path / "x.sqlite")],
                       clock=lambda: et(11, 0), client_factory=lambda feed: Exploding())
    assert rc == 0
    assert "資料庫忙碌" in caplog.text


def test_cli_other_db_errors_still_raise(tmp_path, monkeypatch):
    def broken(path):
        raise sqlite3.OperationalError("no such table: intraday_shadow")
    monkeypatch.setattr(intraday, "_connect", broken)
    with pytest.raises(sqlite3.OperationalError):
        intraday.main(["intraday", "tick", "--db", str(tmp_path / "x.sqlite")],
                      clock=lambda: et(11, 0), client_factory=lambda feed: Exploding())


def test_refuses_stale_daily_cache(db):
    stale = daily_history(end=date(2026, 10, 19))
    res = intraday.tick(db, et(11, 0), Exploding(), stale)
    assert res.status == "stale_daily"


def test_todays_row_in_cache_is_ignored(db, daily):
    """A cache that already holds a row for today (e.g. a partial yfinance bar)
    must not leak into the evaluation: the history is cut before today."""
    full = scored_daily(daily)
    res = intraday.tick(db, et(10, 15), FakeBars(daily), Bars(*(getattr(full, f).loc[:pd.Timestamp(DAY)]
                                                                for f in FIELDS)))
    assert res.status == "ok" and res.recorded == []


# ------------------------------------------------------------------ broker isolation

def test_module_never_loads_broker_or_execution():
    env = {**os.environ, "PYTHONPATH": str(SRC.parents[2])}
    code = ("import sys, stockmoney.desk.intraday\n"
            "bad = [m for m in sys.modules if m.startswith(('stockmoney.desk.execution', 'stockmoney.desk.broker',"
            " 'stockmoney.desk.options', 'stockmoney.desk.engine', 'stockmoney.desk.store', 'alpaca'))]\n"
            "print(','.join(bad))")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""


def test_source_has_no_order_path():
    tree = ast.parse(SRC.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any(m.startswith(("alpaca.trading", "stockmoney.desk.execution", "stockmoney.desk.broker",
                                 "stockmoney.desk.engine", "stockmoney.desk.store")) for m in imported)
    assert not any(m.endswith((".broker", ".execution")) for m in imported)
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"submit", "submit_order", "place_order", "TradingClient", "OrderIntent", "websocket"}


# ------------------------------------------------------------------ score

def scored_daily(daily: Bars) -> Bars:
    """History plus the real DAY and NEXT daily bars: NVDA closes +3%, opens
    NEXT +4% (vs the prior close); everyone else keeps trending."""
    t, n = pd.Timestamp(DAY), pd.Timestamp(NEXT)
    last = daily.close.iloc[-1]
    c_day = last * np.exp(0.001)
    c_day["NVDA"] = last["NVDA"] * 1.03
    c_next = c_day * np.exp(0.001)
    o_next = c_day * 1.0
    o_next["NVDA"] = last["NVDA"] * 1.04
    vol = pd.Series(1e6, index=last.index)
    vol["NVDA"] = 5e6
    add = {
        "open": [c_day * 0.999, o_next], "high": [c_day * 1.005, c_next * 1.005],
        "low": [c_day * 0.995, c_next * 0.995], "close": [c_day, c_next], "volume": [vol, vol],
    }
    out = [pd.concat([getattr(daily, f), pd.DataFrame(add[f], index=[t, n])]) for f in FIELDS]
    return Bars(*out)


def test_score_fills_outcomes(db, daily):
    intraday.tick(db, et(11, 0), FakeBars(daily), daily)
    assert intraday.score(db, daily) == 0  # DAY not in the cache yet: nothing to score
    full = scored_daily(daily)
    k = intraday.score(db, full)
    assert k >= 1
    r = db.execute("SELECT signal_px, close_px, next_open_px, next_day, ret_to_close, ret_vs_next_open,"
                   " daily_rule_signal_next_day, scored_at FROM intraday_shadow"
                   " WHERE symbol='NVDA' AND strategy='breakout'").fetchone()
    last = float(daily.close["NVDA"].iloc[-1])
    assert r[0] == pytest.approx(last * 1.02)
    assert r[1] == pytest.approx(last * 1.03) and r[2] == pytest.approx(last * 1.04)
    assert r[3] == str(NEXT)
    assert r[4] == pytest.approx(1.03 / 1.02 - 1) and r[5] == pytest.approx(1.04 / 1.02 - 1)
    assert r[6] == 1  # the full daily bar (5x volume, +3%) also breaks out
    assert r[7] is not None
    assert intraday.score(db, full) == 0  # idempotent


def test_score_waits_for_next_session(db, daily):
    intraday.tick(db, et(11, 0), FakeBars(daily), daily)
    full = scored_daily(daily)
    only_day = Bars(*(getattr(full, f).loc[:pd.Timestamp(DAY)] for f in FIELDS))
    assert intraday.score(db, only_day) == 0


# ------------------------------------------------------------------ report

def _seed(con, n, strategy="breakout"):
    intraday.ensure_schema(con)
    for i in range(n):
        con.execute("INSERT INTO intraday_shadow (day, symbol, strategy, signal_px, ret_to_close, ret_vs_next_open,"
                    " daily_rule_signal_next_day, scored_at) VALUES (?,?,?,?,?,?,?,?)",
                    (f"2026-01-{i % 28 + 1:02d}", f"S{i}", strategy, 100.0, 0.01, 0.02, 1, "x"))
    con.commit()


def test_report_below_threshold(db):
    _seed(db, 5)
    out = intraday.report(db)
    assert "n=5" in out and "樣本不足 30,不評估" in out
    assert "breakout: n=5" in out and "+2.00%" in out


def test_report_at_threshold(db):
    _seed(db, 20)
    _seed(db, 10, strategy="pullback")
    out = intraday.report(db)
    assert "n=30" in out and "樣本不足" not in out
    assert "pullback: n=10" in out


def test_report_empty(db):
    out = intraday.report(db)
    assert "n=0" in out and "樣本不足 30,不評估" in out
