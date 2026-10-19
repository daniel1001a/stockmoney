"""H1 / H2 shadow comparison reports: verdict rule, DRAFT record, no live rule
change, daily advance of the earn7 book. Offline, fabricated shadow accounts."""
import json

import numpy as np
import pandas as pd
import pytest

from stockmoney.desk import store
from stockmoney.desk.broker import Account
from stockmoney.desk.data import Bars
from stockmoney.desk.indicators import compute
from stockmoney.desk.learn import h_reports, shadow, tables
from stockmoney.desk.run import ensure_schema as run_schema
from tests.desk.test_learn import make_bars


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "d.sqlite") as c:
        run_schema(c)
        tables.ensure_schema(c)
        yield c


def _trades(rets, sym="AAA"):
    return [{"symbol": sym, "strategy": "breakout", "shares": 10, "entry_date": "2026-01-05",
             "entry_price": 100.0, "exit_date": "2026-01-12", "exit_price": 100 * (1 + r), "pnl": 1000 * r,
             "ret": r, "r_multiple": r / 0.05, "exit_reason": "stop", "signal_id": f"s{i}"}
            for i, r in enumerate(rets)]


def _seed(con, name, rets, equity):
    acct = Account(name, 25_000.0, 25_000.0)
    acct.trades = _trades(rets)
    store.save_account(con, acct, "shadow", "2026-03-31")
    for i, e in enumerate(equity):
        con.execute("INSERT OR REPLACE INTO shadow_equity VALUES (?,?,?)",
                    (name, str((pd.Timestamp("2026-01-01") + pd.Timedelta(days=i)).date()), float(e)))


def _rets(n, mean, sd=0.01):
    r = np.random.default_rng(3).normal(0, 1, n)
    r = (r - r.mean()) / r.std() * sd + mean
    return list(r)


CURVE_OK = [25_000, 25_500, 26_000, 26_500]
CURVE_DEEP = [25_000, 26_000, 22_000, 23_000]


def test_expectancy_lower_bound_matches_formula():
    s = h_reports.trade_stats(_trades([0.1, 0.0, 0.2, 0.1]))
    mean, sd = 0.1, float(np.std([0.1, 0.0, 0.2, 0.1], ddof=1))
    assert s["n"] == 4 and s["mean"] == pytest.approx(mean)
    assert s["lcb"] == pytest.approx(mean - 1.96 * sd / 2)


def test_under_30_trades_is_insufficient_sample_not_fail(con):
    _seed(con, "shadow:combined", _rets(29, 0.01), CURVE_OK)
    _seed(con, "shadow:combined_t35", _rets(40, 0.05), CURVE_OK)
    res = h_reports.compare(con, "H1")
    assert res["verdict"] == "INSUFFICIENT"
    assert "樣本不足" in h_reports.render(res)


def test_pass_needs_higher_lower_bound_and_not_larger_drawdown(con):
    _seed(con, "shadow:combined", _rets(40, 0.00), CURVE_DEEP)
    _seed(con, "shadow:combined_earn7", _rets(40, 0.03), CURVE_OK)
    assert h_reports.compare(con, "H2")["verdict"] == "PASS"


def test_better_expectancy_but_deeper_drawdown_fails(con):
    _seed(con, "shadow:combined", _rets(40, 0.00), CURVE_OK)
    _seed(con, "shadow:combined_t35", _rets(40, 0.03), CURVE_DEEP)
    res = h_reports.compare(con, "H1")
    assert res["verdict"] == "FAIL" and "最大回撤" in res["reason"]


def test_lower_expectancy_fails(con):
    _seed(con, "shadow:combined", _rets(40, 0.03), CURVE_OK)
    _seed(con, "shadow:combined_t35", _rets(40, 0.00), CURVE_OK)
    assert h_reports.compare(con, "H1")["verdict"] == "FAIL"


def test_draft_row_recorded_without_touching_live_params(con):
    store.set_setting(con, "strategy_params", json.dumps({"breakout": {"trail_atr": 3.0}}))
    _seed(con, "shadow:combined", _rets(40, 0.00), CURVE_OK)
    _seed(con, "shadow:combined_t35", _rets(40, 0.03), CURVE_OK)
    h_reports.run(con, "H1")
    rows = con.execute("SELECT * FROM param_changes WHERE strategy='H1'").fetchall()
    assert len(rows) == 1 and rows[0]["reason"].startswith("DRAFT H1 PASS")
    data = json.loads(rows[0]["reason"].split(h_reports.DATA_SEP, 1)[1])
    assert data["status"] == "needs_owner_approval"
    assert json.loads(store.get_setting(con, "strategy_params", "{}")) == {"breakout": {"trail_atr": 3.0}}


def test_draft_not_duplicated_for_same_state_and_no_record_flag(con):
    _seed(con, "shadow:combined", _rets(40, 0.00), CURVE_OK)
    _seed(con, "shadow:combined_earn7", _rets(40, 0.03), CURVE_OK)
    h_reports.run(con, "H2")
    h_reports.run(con, "H2")
    assert con.execute("SELECT COUNT(*) FROM param_changes WHERE strategy='H2'").fetchone()[0] == 1
    h_reports.run(con, "H1", record=False)
    assert con.execute("SELECT COUNT(*) FROM param_changes WHERE strategy='H1'").fetchone()[0] == 0


def test_missing_accounts_report_without_crashing(con):
    out = h_reports.run_all(con)
    assert "樣本不足" in out


def test_mfe_mae_from_bars():
    idx = pd.bdate_range("2026-01-05", periods=4)
    hi = pd.DataFrame({"AAA": [101.0, 110.0, 108.0, 120.0]}, index=idx)
    lo = pd.DataFrame({"AAA": [99.0, 95.0, 100.0, 90.0]}, index=idx)
    bars = Bars(hi, hi, lo, hi, hi)
    t = {"symbol": "AAA", "entry_date": "2026-01-05", "exit_date": "2026-01-07", "entry_price": 100.0, "ret": 0.05}
    m = h_reports.mfe_mae([t], bars)
    assert m["n"] == 1
    assert m["mfe"] == pytest.approx(0.10) and m["mae"] == pytest.approx(-0.05)
    assert m["capture"] == pytest.approx(0.5)
    assert h_reports.mfe_mae([t], None)["n"] == 0


def test_shadow_step_advances_earn7_book_daily(con):
    bars = make_bars(n=320)
    first = bars.dates[260]
    ran1 = shadow.step(con, bars.upto(first), compute(bars.upto(first)), first, start=str(bars.dates[250].date()))
    assert ran1["shadow:combined_earn7"] > 0
    nxt = bars.dates[262]
    ran2 = shadow.step(con, bars.upto(nxt), compute(bars.upto(nxt)), nxt)
    assert ran2["shadow:combined_earn7"] == 2
    _, kind, last = store.load_accounts(con)["shadow:combined_earn7"]
    assert kind == "shadow" and last == str(nxt.date())
    eq = shadow.equity_table(con)
    assert eq["shadow:combined_earn7"].dropna().index[-1] == str(nxt.date())
