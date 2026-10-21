"""Request budget (round-3 review F3): market-data calls stay well under
Alpaca's Basic-plan 200 / min, fast polls make no market-data calls,
low-priority work is what gets skipped, and exits never wait for it."""
import logging
from collections import Counter

import pytest

from stockmoney.desk import store
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options.config import OptionsConfig
from stockmoney.desk.options.engine import Engine
from stockmoney.desk.options._iface import occ_symbol

from opt_fakes import NEXT, Clock, FakeOptionsBroker, allow_gate, chain, et, orb_breakout_day, with_history

DATA = ("minute_bars", "stock_quote", "option_quote", "option_chain")


class Counting:
    """Counts the outermost market-data calls of a fake broker (the fake's
    stock_quote reads its own bars; that inner call is not a request)."""

    def __init__(self, b):
        self.n = Counter()
        self.depth = 0
        for name in DATA:
            setattr(b, name, self._wrap(getattr(b, name), name))

    def _wrap(self, f, name):
        def w(*a, **k):
            if self.depth == 0:
                self.n[name] += 1
            self.depth += 1
            try:
                return f(*a, **k)
            finally:
                self.depth -= 1
        return w

    def total(self):
        return sum(self.n.values())


def full_universe(tmp_path, con, **kw):
    cfg = OptionsConfig(halt_file=tmp_path / "H", lock_path=tmp_path / "l", report_dir=tmp_path / "r", **kw)
    clock = Clock(et(9, 20))
    day = with_history(orb_breakout_day())
    b = FakeOptionsBroker(clock, {s: day for s in cfg.starting_universe()})
    for s in cfg.starting_universe():
        b.templates.update(chain(s))
        b.templates.update(chain(s, expiry=NEXT))
    sent = []
    eng = Engine(con, b, cfg, clock=clock, risk_gate=allow_gate(), live=True, notifier=lambda *a: sent.append(a))
    return cfg, clock, b, eng, sent


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        yield c


def test_normal_ticks_17_names_stay_under_120_per_minute(tmp_path, con):
    cfg, clock, b, eng, _ = full_universe(tmp_path, con)
    assert len(cfg.starting_universe()) == 17
    c = Counting(b)
    while clock.now < et(9, 55):
        eng.tick()
        clock.advance(20)
    summary = eng.summary()
    assert summary.get("live_entered") and summary.get("shadow_entered", 0) > 30  # a busy book
    c.n.clear()
    while clock.now < et(10, 5):
        eng.tick()
        clock.advance(20)
    assert c.total() / 10 < 120
    assert c.n["minute_bars"] / 10 <= 17  # each name's bars at most once a minute


def test_fast_ticks_make_no_market_data_calls(tmp_path, con):
    cfg, clock, b, eng, _ = full_universe(tmp_path, con)
    while clock.now < et(9, 55):
        eng.tick()
        clock.advance(20)
    c = Counting(b)
    for i in range(60):  # two minutes of 2-second polls, a full tick every 20 s
        eng.tick(fast=(i % 10 != 0))
        clock.advance(2)
    assert c.total() / 2 < 120
    c.n.clear()
    for _ in range(9):
        eng.tick(fast=True)
        clock.advance(2)
    assert c.total() == 0


def test_exits_run_when_low_priority_work_is_starved(tmp_path, con):
    cfg, clock, b, eng, _ = full_universe(tmp_path, con, max_data_calls_per_min=1)
    while clock.now < et(9, 55):
        eng.tick()
        clock.advance(20)
    live = [p for p in ostore.positions(con, "2026-10-13", "live") if p["state"] == "ENTERED"]
    assert live
    for p in live:
        b.mult[p["occ"]] = 0.5  # premium stop
    for _ in range(6):
        eng.tick()
        clock.advance(20)
    assert all(ostore.position(con, p["position_id"])["state"] == "EXITED" for p in live)
    assert eng.data_calls_last_minute() >= 1


def test_rate_limit_backs_off_low_priority_only(tmp_path, con):
    cfg, clock, b, eng, _ = full_universe(tmp_path, con)
    while clock.now < et(9, 55):
        eng.tick()
        clock.advance(20)
    live = {p["occ"] for p in ostore.positions(con, "2026-10-13", "live") if p["state"] == "ENTERED"}
    real = b.option_quote

    def limited(occ):
        if occ not in live:
            raise RuntimeError("alpaca option_snapshot failed after 3 attempts: 429 Too Many Requests")
        return real(occ)
    b.option_quote = limited
    eng.tick()
    assert eng._backoff_until is not None and eng._backoff_until > clock.now
    for occ in live:
        b.mult[occ] = 0.5
    clock.advance(5)
    eng.tick()  # inside the backoff window: live exits still decided and sent
    assert all(any(o["kind"] == "exit" for o in ostore.orders_for(con, p["position_id"]))
               for p in ostore.positions(con, "2026-10-13", "live") if p["occ"] in live)


def test_session_logs_budget_and_feeds(tmp_path, monkeypatch, caplog):
    from stockmoney.desk.options import run as orun
    from opt_fakes import spy_cfg
    monkeypatch.setattr(orun.notify, "send", lambda *a, **k: None)
    clock = Clock(et(9, 0))

    def make(c, cfg, clk):
        b = FakeOptionsBroker(clk, {"SPY": orb_breakout_day()})
        b.stock_feed, b.option_feed = "iex", "indicative"
        return Engine(c, b, cfg, clock=clk, risk_gate=allow_gate(), live=True, notifier=lambda *a: None), "paper"
    with caplog.at_level(logging.INFO, logger="desk.options.run"):
        orun.session(spy_cfg(tmp_path), tmp_path / "d.sqlite", clock=clock, engine_factory=make,
                     sleep=lambda s: clock.advance(max(s, 600)))
    line = next(r.message for r in caplog.records if "data budget" in r.message)
    assert "110 calls/min" in line and "stock feed iex" in line and "indicative" in line
