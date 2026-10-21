"""Counterfactual log of blocked stock buys: classification, next-open entry,
5/20-day returns, excess vs SPY, honored stop, pending state, idempotency,
event dedupe, report, and isolation from the blocking code. Offline."""
import os
import pathlib
import subprocess
import sys

import pandas as pd
import pytest

from stockmoney.desk import store
from stockmoney.desk.broker import SLIPPAGE
from stockmoney.desk.data import Bars
from stockmoney.desk.learn import counterfactual as cf, tables
from stockmoney.desk.run import ensure_schema as run_schema


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "d.sqlite") as c:
        run_schema(c)
        tables.ensure_schema(c)
        yield c


def make_bars(n=30, aapl_close=None, aapl_low=None):
    """AAPL opens at 100 and closes at 100 + day index; SPY flat open 400,
    closes 400 * (1 + 0.001 * i). Lows default to 1 below min(open, close)."""
    idx = pd.bdate_range("2026-10-01", periods=n)
    a_close = pd.Series(aapl_close if aapl_close is not None else [100.0 + i for i in range(n)], index=idx)
    a_open = pd.Series(100.0, index=idx)
    s_close = pd.Series([400.0 * (1 + 0.001 * i) for i in range(n)], index=idx)
    s_open = pd.Series(400.0, index=idx)
    op = pd.DataFrame({"AAPL": a_open, "SPY": s_open})
    close = pd.DataFrame({"AAPL": a_close, "SPY": s_close})
    low = pd.DataFrame({"AAPL": aapl_low if aapl_low is not None else op["AAPL"].combine(close["AAPL"], min) - 1,
                        "SPY": s_open - 1}, index=idx)
    high = pd.concat([op, close]).groupby(level=0).max() + 1
    vol = pd.DataFrame(1e6, index=idx, columns=["AAPL", "SPY"])
    return Bars(op, high, low, close, vol)


def block(con, cid, day, sym="AAPL", note="Muse ALERT info: headline", stop=90.0, side="buy"):
    con.execute("INSERT INTO planned_orders (client_order_id, plan_date, symbol, asset_class, side, qty, strategy, "
                "stop, ref_price, reason, signal_id, status, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, day, sym, "us_equity", side, 10, "pullback", stop, 100.0, "r", "s", "blocked", note, day))


def test_classify_real_note_shapes():
    assert cf.classify("Muse ALERT info: a | Muse ALERT elevated: b") == ("muse_alert", "elevated")
    assert cf.classify("Muse ALERT critical: x") == ("muse_alert", "critical")
    assert cf.classify("Muse risk flags: lawsuit | Muse ALERT info: upgrade") == ("muse_risk_flags", "info")
    assert cf.classify("Muse risk flags: lawsuit") == ("muse_risk_flags", None)
    assert cf.classify("Muse something new") == ("muse_other", None)
    assert cf.classify("對帳不一致,暫停新進場") == ("other", None)
    assert cf.classify(None) == ("other", None)


def test_entry_is_next_open_after_plan_date_and_returns():
    bars = make_bars()
    d = bars.dates
    o = cf.outcome(bars, "AAPL", str(d[2].date()), stop=None)
    entry = 100.0 * (1 + SLIPPAGE)
    assert o["entry_day"] == str(d[3].date()) and o["entry_px"] == pytest.approx(entry)
    # day 5 after entry is index 8: close 108; SPY open 400 -> close 400 * 1.008
    assert o["ret_5"] == pytest.approx(108.0 / entry - 1)
    assert o["excess_5"] == pytest.approx(108.0 / entry - 1 - 0.008)
    assert o["ret_20"] == pytest.approx(123.0 / entry - 1)
    assert o["excess_20"] == pytest.approx(123.0 / entry - 1 - 0.023)
    assert o["status"] == "done" and o["stop_hit"] is None and o["stop_ret_5"] is None


def test_plan_date_on_a_non_session_uses_the_next_session():
    bars = make_bars()
    o = cf.outcome(bars, "AAPL", "2026-10-03")  # a Saturday; the next session is Monday 10-05
    assert o["entry_day"] == "2026-10-05"


def test_stop_honored_exits_at_stop_or_gap_open():
    bars = make_bars()
    low = bars.low["AAPL"].copy()
    low.iloc[5] = 94.0  # entry at index 3, stop 95 crossed on day 2 after entry
    bars.low["AAPL"] = low
    o = cf.outcome(bars, "AAPL", str(bars.dates[2].date()), stop=95.0)
    entry = 100.0 * (1 + SLIPPAGE)
    stop_ret = 95.0 * (1 - SLIPPAGE) / entry - 1
    assert o["stop_hit"] == 1
    assert o["stop_ret_5"] == pytest.approx(stop_ret) and o["stop_ret_20"] == pytest.approx(stop_ret)
    assert o["ret_5"] == pytest.approx(108.0 / entry - 1)  # the unstopped path is kept too
    # a gap below the stop exits at the open, not the stop
    bars.open.loc[bars.dates[5], "AAPL"] = 93.0
    o = cf.outcome(bars, "AAPL", str(bars.dates[2].date()), stop=95.0)
    assert o["stop_ret_5"] == pytest.approx(93.0 * (1 - SLIPPAGE) / (100.0 * (1 + SLIPPAGE)) - 1)


def test_stop_after_horizon_keeps_plain_return_for_that_horizon():
    bars = make_bars()
    bars.low.loc[bars.dates[10], "AAPL"] = 80.0  # day 7 after entry
    o = cf.outcome(bars, "AAPL", str(bars.dates[2].date()), stop=90.0)
    assert o["stop_ret_5"] == pytest.approx(o["ret_5"])
    assert o["stop_ret_20"] == pytest.approx(90.0 * (1 - SLIPPAGE) / (100.0 * (1 + SLIPPAGE)) - 1)
    assert o["stop_hit"] == 1


def test_pending_and_partial_until_enough_bars():
    bars = make_bars()
    d = bars.dates
    o = cf.outcome(bars.upto(d[6]), "AAPL", str(d[2].date()), stop=90.0)
    assert o["status"] == "pending" and o["entry_day"] == str(d[3].date())
    assert o["ret_5"] is None and o["excess_5"] is None and o["stop_hit"] is None
    o = cf.outcome(bars.upto(d[10]), "AAPL", str(d[2].date()), stop=90.0)
    assert o["status"] == "partial" and o["ret_5"] is not None and o["ret_20"] is None
    assert o["stop_hit"] is None  # not hit yet, window still open
    assert cf.outcome(bars, "AAPL", str(d[-1].date()))["entry_day"] is None
    assert cf.outcome(bars, "MSFT", str(d[2].date()))["status"] == "pending"


def test_refresh_is_idempotent_and_updates_as_bars_arrive(con):
    bars = make_bars()
    d = bars.dates
    block(con, "c1", str(d[2].date()))
    block(con, "sell1", str(d[2].date()), side="sell")
    r = cf.refresh(con, bars, d[8])
    assert r == {"added": 1, "updated": 0, "rows": 1}
    assert cf.refresh(con, bars, d[8]) == {"added": 0, "updated": 0, "rows": 1}
    row = con.execute("SELECT * FROM blocked_counterfactual").fetchone()
    assert row["status"] == "partial" and row["block_class"] == "muse_alert" and row["severity"] == "info"
    assert cf.refresh(con, bars, d[-1]) == {"added": 0, "updated": 1, "rows": 1}
    assert con.execute("SELECT status FROM blocked_counterfactual").fetchone()[0] == "done"


def test_refresh_without_planned_orders_table(tmp_path):
    with store.connect(tmp_path / "e.sqlite") as c:
        tables.ensure_schema(c)
        assert cf.refresh(c, make_bars(), make_bars().dates[-1]) == {"added": 0, "updated": 0, "rows": 0}


def test_first_block_dedupes_repeated_blocks_of_one_event(con):
    bars = make_bars()
    # 10-09 Fri, 10-12 Mon (3 days, same event), 10-13 (chained), 10-19 (6 days later: new event)
    for cid, day in (("a", "2026-10-09"), ("b", "2026-10-12"), ("c", "2026-10-13"), ("d", "2026-10-19")):
        block(con, cid, day)
    block(con, "m", "2026-10-12", sym="MSFT")
    cf.refresh(con, bars, bars.dates[-1])
    got = dict(con.execute("SELECT client_order_id, first_block FROM blocked_counterfactual").fetchall())
    assert got == {"a": 1, "b": 0, "c": 0, "d": 1, "m": 1}


def test_report_groups_and_labels(con):
    bars = make_bars()
    block(con, "a", "2026-10-01", note="Muse ALERT elevated: x")
    block(con, "b", "2026-10-02", note="Muse ALERT elevated: x")
    block(con, "c", "2026-10-01", sym="MSFT", note="Muse risk flags: y")
    block(con, "d", "2026-10-01", sym="NVDA", note="對帳不一致,暫停新進場")
    cf.refresh(con, bars, bars.dates[-1])
    text = cf.report(con)
    assert "反事實(未成交,僅供參考)" in text
    line = next(l for l in text.splitlines() if l.startswith("- Muse ALERT elevated"))
    assert "2 筆,獨立事件 1 件" in line and "停損觸發率(已滿 20 日)0/1" in line
    assert any(l.startswith("- Muse 風險旗標") for l in text.splitlines())
    assert any(l.startswith("- 非 Muse") for l in text.splitlines())
    assert "尚無" in cf.report(con.execute("DELETE FROM blocked_counterfactual").connection)


def test_blocking_code_does_not_import_counterfactual():
    root = pathlib.Path("src/stockmoney/desk")
    for p in [root / "bridge" / "policy.py", *(root / "execution").rglob("*.py"), root / "run.py", root / "veto.py"]:
        assert "counterfactual" not in p.read_text(), p
    code = ("import sys; import stockmoney.desk.bridge.policy, stockmoney.desk.run; "
            "assert 'stockmoney.desk.learn.counterfactual' not in sys.modules")
    subprocess.run([sys.executable, "-c", code], check=True, env={**os.environ, "PYTHONPATH": "src"})
