"""Daily broker-vs-plan reconcile summary: slippage, missed/partial orders, stop differences,
zero-diff streak, alert threshold, postclose hook and CLI. Read-only: never halts, never
touches an order. Offline; rows are inserted directly with literal values."""
import importlib.util
import sys
from pathlib import Path

import pytest

from stockmoney.desk import run, store
from stockmoney.desk.execution import reconcile
from stockmoney.desk.execution.broker import FakeBroker
from stockmoney.desk.execution.ledger import ensure_schema as ledger_schema
from stockmoney.desk.execution.types import TIF, BrokerOrder, OrderStatus, OrderType, Side
from stockmoney.desk.run import ensure_schema as run_schema

DAY = "2026-10-15"  # a Thursday
AT = "2026-10-15T14:35:00+00:00"  # 10:35 ET the same day


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "d.sqlite") as c:
        run_schema(c)
        ledger_schema(c)
        yield c


def _order(con, cid, kind, symbol, side, qty, status="filled", ref=None, stop=None, submitted_at=AT):
    con.execute("INSERT INTO submitted (client_order_id, kind, symbol, asset_class, strategy, side, qty, stop, "
                "submitted_at, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cid, kind, symbol, "us_equity", "breakout", side, qty, stop, submitted_at, status))
    con.execute("INSERT INTO planned_orders (client_order_id, plan_date, symbol, asset_class, side, qty, strategy, "
                "ref_price, status) VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, DAY, symbol, "us_equity", side, qty, "breakout", ref, "submitted"))


def _fill(con, cid, symbol, side, qty, price, filled_at=AT):
    con.execute("INSERT INTO fills (client_order_id, symbol, strategy, side, qty, price, filled_at) VALUES (?,?,?,?,?,?,?)",
                (cid, symbol, "breakout", side, qty, price, filled_at))


def _hold(con, symbol, qty, stop):
    con.execute("INSERT INTO sleeve_positions (symbol, strategy, qty, avg_price, opened_at, stop) VALUES (?,?,?,?,?,?)",
                (symbol, "breakout", qty, 100.0, DAY, stop))


def _stop_order(symbol, stop_price, oid="o1"):
    return BrokerOrder(broker_order_id=oid, client_order_id=f"c-{oid}", symbol=symbol, side=Side.SELL,
                       status=OrderStatus.NEW, qty=10, filled_qty=0.0, filled_avg_price=None, submitted_at=None,
                       filled_at=None, order_type=OrderType.STOP, tif=TIF.GTC, stop_price=stop_price)


def _day_row(con, day, zero_diff):
    reconcile.ensure_schema(con)
    con.execute("INSERT INTO reconcile_daily (day, zero_diff) VALUES (?,?)", (day, zero_diff))


def test_adverse_buy_fill_over_threshold_is_flagged(con):
    _order(con, "b1", "entry", "AAPL", "buy", 10, ref=100.0)
    _fill(con, "b1", "AAPL", "buy", 10, 101.5)
    s = reconcile.summarize(con, DAY)
    assert [(x["symbol"], round(x["bps"])) for x in s["slip_flags"]] == [("AAPL", 150)]
    assert s["n_fills"] == 1 and s["n_orders"] == 1 and not s["zero_diff"]


def test_small_slippage_is_not_a_difference_but_is_in_the_mean(con):
    _order(con, "b1", "entry", "AAPL", "buy", 10, ref=100.0)
    _fill(con, "b1", "AAPL", "buy", 10, 100.5)
    _order(con, "b2", "entry", "MSFT", "buy", 5, ref=200.0)
    _fill(con, "b2", "MSFT", "buy", 5, 201.5)
    s = reconcile.summarize(con, DAY)
    assert s["slip_flags"] == [] and s["zero_diff"]
    assert s["mean_slip_bps"] == pytest.approx(62.5)


def test_sell_slippage_sign_is_adverse_when_below_reference(con):
    _order(con, "s1", "exit", "AAPL", "sell", 10, ref=50.0)
    _fill(con, "s1", "AAPL", "sell", 10, 49.0)
    _order(con, "s2", "exit", "MSFT", "sell", 10, ref=50.0)
    _fill(con, "s2", "MSFT", "sell", 10, 51.0)
    s = reconcile.summarize(con, DAY)
    assert [(x["symbol"], round(x["bps"])) for x in s["slips"]] == [("AAPL", 200), ("MSFT", -200)]
    assert [x["symbol"] for x in s["slip_flags"]] == ["AAPL"]


def test_stop_fill_is_measured_against_the_ledger_stop(con):
    _order(con, "st1", "stop", "AAPL", "sell", 10, stop=100.0)
    _fill(con, "st1", "AAPL", "sell", 10, 98.0)
    s = reconcile.summarize(con, DAY)
    assert [(x["kind"], round(x["bps"])) for x in s["slip_flags"]] == [("stop", 200)]


def test_fill_on_another_et_day_is_not_counted(con):
    _order(con, "b1", "entry", "AAPL", "buy", 10, ref=100.0)
    _fill(con, "b1", "AAPL", "buy", 10, 120.0, filled_at="2026-10-16T14:35:00+00:00")
    s = reconcile.summarize(con, DAY)
    assert s["n_fills"] == 0 and s["slips"] == []


def test_late_utc_fill_belongs_to_the_et_day(con):
    _order(con, "b1", "entry", "AAPL", "buy", 10, ref=100.0)
    _fill(con, "b1", "AAPL", "buy", 10, 100.0, filled_at="2026-10-16T01:30:00+00:00")  # 21:30 ET on the 15th
    s = reconcile.summarize(con, DAY)
    assert s["n_fills"] == 1


@pytest.mark.parametrize("status", ["canceled", "expired", "rejected"])
def test_unfilled_entry_is_reported_as_missed(con, status):
    _order(con, "b1", "entry", "AAPL", "buy", 10, status=status, ref=100.0)
    s = reconcile.summarize(con, DAY)
    assert [(x["symbol"], x["status"]) for x in s["missed"]] == [("AAPL", status)]
    assert s["partial"] == [] and not s["zero_diff"]


def test_working_order_is_not_a_difference(con):
    _order(con, "b1", "entry", "AAPL", "buy", 10, status="new", ref=100.0)
    s = reconcile.summarize(con, DAY)
    assert s["missed"] == [] and s["partial"] == [] and s["zero_diff"] and s["n_orders"] == 1


def test_partial_fill_is_reported(con):
    _order(con, "b1", "entry", "AAPL", "buy", 10, status="filled", ref=100.0)
    _fill(con, "b1", "AAPL", "buy", 6, 100.0)
    s = reconcile.summarize(con, DAY)
    assert [(x["symbol"], x["qty"], x["filled"]) for x in s["partial"]] == [("AAPL", 10.0, 6.0)]
    assert s["missed"] == []


def test_canceled_after_partial_fill_counts_as_partial_not_missed(con):
    _order(con, "b1", "entry", "AAPL", "buy", 10, status="canceled", ref=100.0)
    _fill(con, "b1", "AAPL", "buy", 4, 100.0)
    s = reconcile.summarize(con, DAY)
    assert len(s["partial"]) == 1 and s["missed"] == []


def test_stop_diffs_missing_price_duplicate_and_orphan(con):
    _hold(con, "AAPL", 10, 95.0)    # no broker stop
    _hold(con, "MSFT", 10, 200.0)   # stop at the wrong price
    _hold(con, "NVDA", 10, 300.0)   # two stops
    _hold(con, "AMD", 10, 150.0)    # matching stop
    orders = [_stop_order("MSFT", 190.0, "o1"), _stop_order("NVDA", 300.0, "o2"), _stop_order("NVDA", 300.0, "o3"),
              _stop_order("AMD", 150.0, "o4"), _stop_order("TSLA", 250.0, "o5")]
    s = reconcile.summarize(con, DAY, orders)
    got = {x["symbol"]: x["problem"] for x in s["stop_diffs"]}
    assert set(got) == {"AAPL", "MSFT", "NVDA", "TSLA"}
    assert "沒有停損單" in got["AAPL"] and "停損價不同" in got["MSFT"]
    assert "2 張停損單" in got["NVDA"] and "孤兒停損" in got["TSLA"]


def test_stop_within_a_cent_is_not_a_difference(con):
    _hold(con, "AAPL", 10, 95.004)
    s = reconcile.summarize(con, DAY, [_stop_order("AAPL", 95.0)])
    assert s["stop_diffs"] == [] and s["zero_diff"] and s["stop_checked"]


def test_crypto_and_stopless_positions_are_skipped(con):
    _hold(con, "BTC/USD", 0.5, 60000.0)
    con.execute("INSERT INTO sleeve_positions (symbol, strategy, qty, avg_price, opened_at, stop) "
                "VALUES ('SOXL','lev',5,40,?,NULL)", (DAY,))
    s = reconcile.summarize(con, DAY, [])
    assert s["stop_diffs"] == []


def test_stop_check_is_skipped_without_broker_orders(con):
    _hold(con, "AAPL", 10, 95.0)
    s = reconcile.summarize(con, DAY, None)
    assert s["stop_diffs"] == [] and not s["stop_checked"]
    assert "停損單比對略過" in reconcile.render(s, {"zero": 1, "diff": 0})


def test_zero_diff_streak_counts_consecutive_days(con):
    _day_row(con, "2026-10-12", 0)
    _day_row(con, "2026-10-13", 1)
    _day_row(con, "2026-10-14", 1)
    text, alert = reconcile.daily(con, DAY)
    assert "連續零差異 3 天" in text and alert is None


def test_diff_day_resets_the_zero_streak(con):
    _day_row(con, "2026-10-13", 1)
    _day_row(con, "2026-10-14", 1)
    _order(con, "b1", "entry", "AAPL", "buy", 10, status="expired", ref=100.0)
    text, alert = reconcile.daily(con, DAY)
    assert "連續零差異 0 天" in text and "連續有差異 1 天" in text and alert is None


def test_three_consecutive_diff_days_raise_the_alert(con):
    _day_row(con, "2026-10-13", 0)
    _day_row(con, "2026-10-14", 0)
    _order(con, "b1", "entry", "AAPL", "buy", 10, status="expired", ref=100.0)
    text, alert = reconcile.daily(con, DAY)
    assert alert is not None and "連續 3 天有差異" in alert and "AAPL" in alert


def test_two_diff_days_do_not_alert(con):
    _day_row(con, "2026-10-14", 0)
    _order(con, "b1", "entry", "AAPL", "buy", 10, status="expired", ref=100.0)
    assert reconcile.daily(con, DAY)[1] is None


def test_rerun_same_day_keeps_one_row_and_streak_ignores_itself(con):
    _day_row(con, "2026-10-14", 1)
    reconcile.daily(con, DAY)
    text, _ = reconcile.daily(con, DAY)
    rows = con.execute("SELECT day, zero_diff FROM reconcile_daily ORDER BY day").fetchall()
    assert [(r["day"], r["zero_diff"]) for r in rows] == [("2026-10-14", 1), (DAY, 1)]
    assert "連續零差異 2 天" in text


def test_no_record_writes_nothing(con):
    _order(con, "b1", "entry", "AAPL", "buy", 10, status="expired", ref=100.0)
    reconcile.daily(con, DAY, record_rows=False)
    reconcile.ensure_schema(con)
    assert con.execute("SELECT COUNT(*) FROM reconcile_daily").fetchone()[0] == 0


def test_summary_does_not_touch_orders_fills_or_halt(con):
    _order(con, "b1", "entry", "AAPL", "buy", 10, status="expired", ref=100.0)
    _fill(con, "b1", "AAPL", "buy", 3, 120.0)
    before = [tuple(r) for r in con.execute("SELECT * FROM submitted")] + [tuple(r) for r in con.execute("SELECT * FROM fills")]
    reconcile.daily(con, DAY, [])
    after = [tuple(r) for r in con.execute("SELECT * FROM submitted")] + [tuple(r) for r in con.execute("SELECT * FROM fills")]
    assert before == after
    assert store.get_setting(con, "halt_new_entries", "0") == "0"


def test_missing_tables_give_an_empty_zero_diff_day(tmp_path):
    with store.connect(tmp_path / "bare.sqlite") as c:
        s = reconcile.summarize(c, DAY, [])
        assert s["n_fills"] == 0 and s["n_orders"] == 0 and s["zero_diff"]


# ---- postclose hook ----------------------------------------------------------------------------

@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(run.notify, "send", lambda title, msg, priority=3, tags=None: sent.append((title, msg)) or True)
    return sent


def test_reconcile_summary_pushes_once_per_day_at_threshold(con, pushes):
    _day_row(con, "2026-10-13", 0)
    _day_row(con, "2026-10-14", 0)
    _order(con, "b1", "entry", "AAPL", "buy", 10, status="expired", ref=100.0)
    text = run._reconcile_summary(con, FakeBroker(cash=1000), DAY)
    run._reconcile_summary(con, FakeBroker(cash=1000), DAY)
    assert "券商對帳摘要 2026-10-15" in text
    assert [t for t, _ in pushes] == ["Desk 券商對帳連續有差異"]


def test_reconcile_summary_below_threshold_sends_nothing(con, pushes):
    _order(con, "b1", "entry", "AAPL", "buy", 10, status="expired", ref=100.0)
    run._reconcile_summary(con, FakeBroker(cash=1000), DAY)
    assert pushes == []


def test_reconcile_summary_survives_a_broker_open_orders_failure(con, pushes):
    class Boom(FakeBroker):
        def open_orders(self):
            raise OSError("down")

    _hold(con, "AAPL", 10, 95.0)
    text = run._reconcile_summary(con, Boom(cash=1000), DAY)
    assert "停損單比對略過" in text


def test_reconcile_summary_failure_is_contained_and_keeps_prior_writes(con, monkeypatch, pushes):
    con.execute("INSERT INTO notified VALUES ('keep-me', 'x')")  # an uncommitted earlier postclose write

    def boom(*a, **k):
        raise RuntimeError("summary broke")

    monkeypatch.setattr(run, "reconcile_daily", boom)
    text = run._reconcile_summary(con, FakeBroker(cash=1000), DAY)
    assert "券商對帳摘要失敗:RuntimeError: summary broke" in text
    assert con.execute("SELECT COUNT(*) FROM notified WHERE key='keep-me'").fetchone()[0] == 1


def _simulate(tmp_path, n_days):
    sys.path.insert(0, str(Path(__file__).parent))
    from test_run import _simulate as sim
    return sim(tmp_path, n_days=n_days)


def test_postclose_report_carries_the_summary_and_changes_no_halt(tmp_path, pushes):
    _simulate(tmp_path, 40)
    with store.connect(tmp_path / "desk.sqlite") as c:
        last = c.execute("SELECT auto_summary FROM journal ORDER BY date DESC LIMIT 1").fetchone()[0]
        assert "## 券商對帳摘要" in last and "連續零差異" in last
        assert c.execute("SELECT COUNT(*) FROM reconcile_daily").fetchone()[0] >= 30
        assert store.get_setting(c, "halt_new_entries", "0") == "0"


def test_postclose_survives_a_failing_summary(tmp_path, monkeypatch, pushes):
    def boom(*a, **k):
        raise RuntimeError("summary broke")

    monkeypatch.setattr(run, "reconcile_daily", boom)
    _simulate(tmp_path, 40)
    with store.connect(tmp_path / "desk.sqlite") as c:
        assert c.execute("SELECT COUNT(*) FROM equity_history").fetchone()[0] == 38
        last = c.execute("SELECT auto_summary FROM journal ORDER BY date DESC LIMIT 1").fetchone()[0]
        assert "券商對帳摘要失敗" in last
        assert c.execute("SELECT COUNT(*) FROM fills").fetchone()[0] >= 1


# ---- CLI --------------------------------------------------------------------------------------

def _cli(tmp_path, monkeypatch):
    real_connect = store.connect
    monkeypatch.setattr(store, "connect", lambda path=None: real_connect(tmp_path / "d.sqlite"))
    spec = importlib.util.spec_from_file_location("desk_reconcile_cli",
                                                  Path(__file__).parents[2] / "scripts" / "desk_reconcile.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def test_cli_prints_records_and_lists(tmp_path, monkeypatch, capsys):
    with store.connect(tmp_path / "d.sqlite") as c:
        run_schema(c)
        ledger_schema(c)
        _order(c, "b1", "entry", "AAPL", "buy", 10, ref=100.0)
        _fill(c, "b1", "AAPL", "buy", 10, 101.5)
    cli = _cli(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["desk_reconcile.py", "--date", DAY, "--no-broker"])
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert "券商對帳摘要 2026-10-15" in out and "滑價 AAPL entry buy" in out
    monkeypatch.setattr(sys, "argv", ["desk_reconcile.py", "--list"])
    assert cli.main() == 0
    assert "2026-10-15 fills 1 orders 1 slip 1 missed 0 partial 0 stop 0 zero_diff 0" in capsys.readouterr().out


def test_cli_no_record_writes_no_row(tmp_path, monkeypatch, capsys):
    with store.connect(tmp_path / "d.sqlite") as c:
        run_schema(c)
        ledger_schema(c)
    cli = _cli(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["desk_reconcile.py", "--date", DAY, "--no-broker", "--no-record"])
    assert cli.main() == 0
    assert "今日沒有差異" in capsys.readouterr().out
    with store.connect(tmp_path / "d.sqlite") as c:
        reconcile.ensure_schema(c)
        assert c.execute("SELECT COUNT(*) FROM reconcile_daily").fetchone()[0] == 0


def test_reconcile_module_does_not_submit_orders():
    src = Path(reconcile.__file__).read_text()
    for banned in (".submit(", "cancel_order", "make_broker", "set_setting"):
        assert banned not in src
