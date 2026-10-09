"""End-to-end orchestrator cycle against FakeBroker on synthetic bars."""
from datetime import datetime, timedelta

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


def test_muse_alert_blocks_planned_entry(tmp_path):
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
        body = ('{"msg_id":"alert0001","sent_at":"%s","type":"ALERT","ticker":"%s","severity":"elevated",'
                '"headline":"probe","source_urls":["https://example.com/x"],"observed_at":"%s"}'
                % (_at(nxt, 7, 0).isoformat(), target, _at(nxt, 6, 0).isoformat()))
        box.inject("muse@example.com", build_subject("ALERT", "alert0001", cfg.shared_token), body)
        inbox.poll(con, box, cfg, now=_at(nxt, 8, 0))
        broker.prices.update({s: float(v) for s, v in bars.open.loc[nxt].items()})
        out = run.premarket(con, broker, _at(nxt, 8, 45), limits)
        row = con.execute("SELECT status, note FROM planned_orders WHERE client_order_id=?",
                          (planned[0]["client_order_id"],)).fetchone()
        assert row["status"] == "blocked", out
        assert target not in {o.symbol for o in broker.orders.values() if o.status == OrderStatus.FILLED}
