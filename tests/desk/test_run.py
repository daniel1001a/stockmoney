"""End-to-end orchestrator cycle against FakeBroker on synthetic bars."""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from stockmoney.desk import run, store
from stockmoney.desk.execution.broker import FakeBroker
from stockmoney.desk.execution.ledger import Ledger
from stockmoney.desk.execution.reconcile import reconcile
from stockmoney.desk.execution.risk import Limits
from stockmoney.desk.execution.types import OrderStatus, OrderType

from test_desk import synthetic_bars


@pytest.fixture(autouse=True)
def no_push(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    for k in ("DESK_MAIL_ADDRESS", "DESK_MAIL_APP_PASSWORD", "MUSE_ADDRESS", "MUSE_SHARED_TOKEN"):
        monkeypatch.delenv(k, raising=False)


def _at(day: pd.Timestamp, h: int, m: int) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=run.ET)


def _simulate(tmp_path, n_days=130):
    bars = synthetic_bars(n=560, seed=7)
    broker = FakeBroker(cash=100_000)  # broker holds more than the system's 50k budget, like Alpaca paper
    limits = Limits(halt_file=tmp_path / "HALT")
    days = bars.dates[-n_days:]
    log = []
    with store.connect(tmp_path / "desk.sqlite") as con:
        for i, t in enumerate(days):
            if i > 0:
                broker.prices.update({s: float(v) for s, v in bars.open.loc[t].items()})
                log.append(("pre", t, run.premarket(con, broker, _at(t, 8, 45), limits)))
                run.postopen(con, broker, _at(t, 9, 50), limits)
                for s, v in bars.low.loc[t].items():  # intraday low triggers resting stops
                    broker.tick(s, float(v))
            broker.prices.update({s: float(v) for s, v in bars.close.loc[t].items()})
            log.append(("post", t, run.postclose(con, broker, _at(t, 17, 0), bars=bars.upto(t), limits=limits)))
            problems = reconcile(Ledger(con), broker.account())
            assert problems == [], (t, problems)
        return con, broker, log


def test_full_cycle_trades_and_stays_reconciled(tmp_path):
    con, broker, log = _simulate(tmp_path)
    with store.connect(tmp_path / "desk.sqlite") as con:
        fills = con.execute("SELECT side, COUNT(*) FROM fills GROUP BY side").fetchall()
        counts = {r[0]: r[1] for r in fills}
        assert counts.get("buy", 0) >= 5 and counts.get("sell", 0) >= 3, counts
        assert store.get_setting(con, "halt_new_entries", "0") == "0"
        # every open equity position has exactly one live protective stop at the ledger price
        ledger = Ledger(con)
        stops = {o.symbol: o for o in broker.open_orders() if o.order_type == OrderType.STOP}
        for sym, pos in ledger.positions().items():
            assert sym in stops, f"{sym} has no stop"
            assert stops[sym].stop_price == pytest.approx(pos.stop, abs=0.01)
        assert len(stops) == len(ledger.positions())
        # stops only ever ratchet up: the latest ledger stop >= its initial stop
        for r in con.execute("SELECT p.symbol, p.stop, m.initial_stop FROM sleeve_positions p "
                             "JOIN position_meta m USING(symbol)").fetchall():
            assert r[1] >= r[2] - 1e-6


def test_rerunning_jobs_is_idempotent(tmp_path):
    con, broker, log = _simulate(tmp_path, n_days=60)
    t = log[-1][1]
    with store.connect(tmp_path / "desk.sqlite") as con:
        before = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        n_orders = len(broker.orders)
        run.postclose(con, broker, _at(t, 17, 30), bars=synthetic_bars(n=560, seed=7).upto(t), limits=Limits(halt_file=tmp_path / "HALT"))
        run.postopen(con, broker, _at(t, 17, 31), Limits(halt_file=tmp_path / "HALT"))
        assert con.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == before
        assert len(broker.orders) == n_orders


def test_reconcile_mismatch_halts(tmp_path):
    con, broker, log = _simulate(tmp_path, n_days=60)
    with store.connect(tmp_path / "desk.sqlite") as con:
        ledger = Ledger(con)
        assert ledger.positions(), "need a position to corrupt"
        sym = next(iter(ledger.positions()))
        broker.positions[sym].qty += 3  # someone traded in the account by hand
        t = log[-1][1]
        out = run.postopen(con, broker, _at(t + timedelta(days=1), 9, 50), Limits(halt_file=tmp_path / "HALT"))
        assert "對帳不一致" in out
        assert store.get_setting(con, "halt_new_entries") == "1"


@pytest.mark.parametrize("severity, filing, blocked", [
    ("critical", False, True),   # critical blocks on Muse alone
    ("elevated", False, False),  # Muse downgrade 2026-10-21: elevated alone passes, logged for the counterfactual
    ("info", False, False),      # info is logged only
    ("elevated", True, True),    # elevated plus an independent 8-K: veto.check blocks (2 sources)
])
def test_muse_alert_on_planned_entry(tmp_path, severity, filing, blocked):
    from stockmoney.desk.bridge import inbox
    from stockmoney.desk.bridge.mailbox import BridgeConfig, FakeMailbox
    from stockmoney.desk.bridge.outbox import build_subject

    bars = synthetic_bars(n=560, seed=7)
    broker = FakeBroker(cash=100_000)
    limits = Limits(halt_file=tmp_path / "HALT")
    with store.connect(tmp_path / "desk.sqlite") as con:
        planned = []
        for t in bars.dates[-130:]:
            broker.prices.update({s: float(v) for s, v in bars.close.loc[t].items()})
            run.postclose(con, broker, _at(t, 17, 0), bars=bars.upto(t), limits=limits)
            planned = con.execute("SELECT * FROM planned_orders WHERE status='planned' AND side='buy'").fetchall()
            if planned:
                break
        assert planned
        target = planned[0]["symbol"]
        nxt = bars.dates[bars.dates.get_loc(t) + 1]
        cfg = BridgeConfig(desk_address="desk@example.com", muse_address="muse@example.com",
                           shared_token="t" * 32)
        box = FakeMailbox()
        body = ('{"msg_id":"alert0001","sent_at":"%s","type":"ALERT","ticker":"%s","severity":"%s",'
                '"headline":"probe","source_urls":["https://example.com/x"],"observed_at":"%s"}'
                % (_at(nxt, 7, 0).isoformat(), target, severity, _at(nxt, 6, 0).isoformat()))
        box.inject("muse@example.com", build_subject("ALERT", "alert0001", cfg.shared_token), body)
        # veto.check runs on the wall clock, so a two-source case is received "now"
        inbox.poll(con, box, cfg, now=datetime.now(timezone.utc) if filing else _at(nxt, 8, 0))
        if filing:
            from stockmoney.desk import veto
            veto.ensure_schema(con)
            con.execute("INSERT INTO research_flags (symbol, source, kind, severity, headline, url, event_date, "
                        "collected_at) VALUES (?, 'filings', '8-K', 'elevated', 'h', 'https://sec.example/1', ?, ?)",
                        (target, str(nxt.date()), datetime.now(timezone.utc).isoformat(timespec="seconds")))
        broker.prices.update({s: float(v) for s, v in bars.open.loc[nxt].items()})
        out = run.premarket(con, broker, _at(nxt, 8, 45), limits)
        row = con.execute("SELECT status, note FROM planned_orders WHERE client_order_id=?",
                          (planned[0]["client_order_id"],)).fetchone()
        from stockmoney.desk.bridge.policy import legacy_rows
        legacy = legacy_rows(con)
        if blocked:
            assert row["status"] == "blocked", out
            assert target not in {o.symbol for o in broker.orders.values() if o.status == OrderStatus.FILLED}
        else:
            assert row["status"] == "submitted", out
            assert [r["client_order_id"] for r in legacy] == [planned[0]["client_order_id"]]
            assert "舊規則會擋" in out
        if severity == "critical":
            assert legacy == []  # really blocked, nothing to log as legacy-only


@pytest.fixture
def planned_book(tmp_path):
    """Cycles postclose until a buy is planned, then yields the next session's date."""
    bars = synthetic_bars(n=560, seed=7)
    broker = FakeBroker(cash=100_000)
    limits = Limits(halt_file=tmp_path / "HALT")
    with store.connect(tmp_path / "desk.sqlite") as con:
        for t in bars.dates[-130:]:
            broker.prices.update({s: float(v) for s, v in bars.close.loc[t].items()})
            run.postclose(con, broker, _at(t, 17, 0), bars=bars.upto(t), limits=limits)
            if con.execute("SELECT 1 FROM planned_orders WHERE status='planned' AND side='buy'").fetchone():
                nxt = bars.dates[bars.dates.get_loc(t) + 1]
                broker.prices.update({s: float(v) for s, v in bars.open.loc[nxt].items()})
                yield con, broker, nxt, limits
                return
    raise AssertionError("no plan produced")


@pytest.mark.parametrize("hour,minute,tif", [(8, 45, "opg"), (9, 40, "day")])
def test_premarket_order_type_depends_on_clock(planned_book, hour, minute, tif):
    con, broker, nxt, limits = planned_book
    run.premarket(con, broker, _at(nxt, hour, minute), limits)
    entries = [o for o in broker.orders.values() if o.order_type == OrderType.MARKET]
    assert entries and {o.tif.value for o in entries} == {tif}
    if tif == "day":  # a late run fills at once and must protect the fills right away
        stops = {o.symbol for o in broker.open_orders() if o.order_type == OrderType.STOP}
        assert stops == {o.symbol for o in entries}


def test_premarket_drops_entries_after_cutoff(planned_book):
    con, broker, nxt, limits = planned_book
    out = run.premarket(con, broker, _at(nxt, 11, 0), limits)
    assert not broker.orders
    assert "錯過進場" in out
    rows = con.execute("SELECT status FROM planned_orders WHERE side='buy' AND plan_date < ?", (str(nxt.date()),)).fetchall()
    assert rows and all(r[0] == "expired" for r in rows)


def test_stock_entries_default_off_is_logged_once(con, monkeypatch):
    monkeypatch.setattr(run, "STOCK_ENTRIES_DEFAULT", False)
    assert run.stock_entries_enabled(con) is False
    assert run.stock_entries_enabled(con) is False
    rows = con.execute("SELECT * FROM param_changes WHERE param=?", (run.STOCK_ENTRIES_KEY,)).fetchall()
    assert len(rows) == 1 and rows[0]["strategy"] == "desk" and rows[0]["new"] == "false"
    assert store.get_setting(con, run.STOCK_ENTRIES_KEY) == "0"


def test_advisory_only_sends_no_stock_buys_but_keeps_stops(tmp_path):
    """Owner decision 2026-10-27: stock sleeves only advise. Holdings keep their stops."""
    bars = synthetic_bars(n=560, seed=7)
    broker = FakeBroker(cash=100_000)
    limits = Limits(halt_file=tmp_path / "HALT")
    days = bars.dates[-130:]
    with store.connect(tmp_path / "desk.sqlite") as con:
        held_from = None
        for i, t in enumerate(days):
            if i > 0:
                broker.prices.update({s: float(v) for s, v in bars.open.loc[t].items()})
                if held_from is not None:
                    n_buys = sum(1 for o in broker.orders.values() if o.side.value == "buy")
                    out = run.premarket(con, broker, _at(t, 8, 45), limits)
                    run.postopen(con, broker, _at(t, 9, 50), limits)
                    assert sum(1 for o in broker.orders.values() if o.side.value == "buy") == n_buys, out
                else:
                    run.premarket(con, broker, _at(t, 8, 45), limits)
                    run.postopen(con, broker, _at(t, 9, 50), limits)
                for s, v in bars.low.loc[t].items():
                    broker.tick(s, float(v))
            broker.prices.update({s: float(v) for s, v in bars.close.loc[t].items()})
            if held_from is None and Ledger(con).positions():
                store.set_setting(con, run.STOCK_ENTRIES_KEY, "0")  # switch off with holdings on the book
                con.commit()
                held_from = t
            out = run.postclose(con, broker, _at(t, 17, 0), bars=bars.upto(t), limits=limits)
            if held_from is not None and "跳過規劃" not in out:
                assert "**中長期建議(不自動下單)**" in out and "不自動下單" in out
        assert held_from is not None
        assert con.execute("SELECT COUNT(*) FROM planned_orders WHERE side='buy' AND status='advisory'").fetchone()[0] > 0
        assert con.execute("SELECT COUNT(*) FROM planned_orders WHERE side='buy' AND status='planned' "
                           "AND plan_date>=?", (str(held_from.date()),)).fetchone()[0] == 0
        ledger = Ledger(con)
        stops = {o.symbol: o for o in broker.open_orders() if o.order_type == OrderType.STOP}
        for sym, pos in ledger.positions().items():
            assert sym in stops and stops[sym].stop_price == pytest.approx(pos.stop, abs=0.01)
        assert reconcile(ledger, broker.account()) == []


def test_stock_entries_production_default_is_off():
    """The autouse conftest fixture patches STOCK_ENTRIES_DEFAULT on for legacy
    tests, so read the shipped value from the source instead (owner decision
    2026-10-27: mid/long-term stocks are advisory only)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(run))
    (val,) = [n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
              and any(getattr(t, "id", None) == "STOCK_ENTRIES_DEFAULT" for t in n.targets)]
    assert isinstance(val, ast.Constant) and val.value is False


def test_plan_buy_converts_between_advisory_and_planned_only(con):
    """The cid has no status in it: switching STOCK_ENTRIES between two
    postclose runs converts the unsent row in place instead of leaving the
    old status while the report claims the new one (Grok review)."""
    run.ensure_schema(con)
    day = "2026-10-28"

    def status():
        return [r[0] for r in con.execute("SELECT status FROM planned_orders WHERE symbol='AAPL'")]

    assert run._plan_buy(con, day, "AAPL", 10, "trend", 90.0, 100.0, "t", "sig1", run.ADVISORY)
    assert status() == [run.ADVISORY]
    assert run._plan_buy(con, day, "AAPL", 12, "trend", 91.0, 101.0, "t2", "sig2", "planned")
    r = con.execute("SELECT status, qty, stop, ref_price, signal_id FROM planned_orders WHERE symbol='AAPL'").fetchone()
    assert tuple(r) == ("planned", 12, 91.0, 101.0, "sig2")
    assert run._plan_buy(con, day, "AAPL", 12, "trend", 91.0, 101.0, "t2", "sig2", run.ADVISORY)
    assert status() == [run.ADVISORY]
    con.execute("UPDATE planned_orders SET status='submitted'")
    assert not run._plan_buy(con, day, "AAPL", 12, "trend", 91.0, 101.0, "t2", "sig2", "planned")
    assert status() == ["submitted"]
