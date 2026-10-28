"""Defined-risk spread shadow (options/spreads.py, EDGE_PLAN H6)."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from stockmoney.desk import wind
from stockmoney.desk.options import spreads
from stockmoney.desk.options.config import OptionsConfig

SRC = Path(spreads.__file__)
DAY = date(2026, 10, 28)  # Wednesday
EXP = DAY + timedelta(days=16)  # 2026-11-13, a Friday
CFG = OptionsConfig()


def et(d: date, h: int, m: int) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=spreads.ET)


@dataclass
class C:
    occ: str
    strike: float
    delta: float | None
    bid: float | None
    ask: float | None
    as_of: datetime | None
    expiry: date = EXP
    right: str = "C"
    underlying: str = "SPY"


def chain(now: datetime, und: str = "SPY", expiry: date = EXP) -> list[C]:
    q = now - timedelta(seconds=5)
    return [C(f"{und}-{k}", k, d, b, a, q, expiry, underlying=und) for k, d, b, a in (
        (95, 0.70, 6.0, 6.2), (100, 0.52, 3.0, 3.1), (105, 0.31, 1.2, 1.3), (110, 0.15, 0.4, 0.5))]


class FakeData:
    def __init__(self, chains: dict[str, list] | None = None, quotes: dict[str, C] | None = None,
                 stock: dict[str, tuple[float, float]] | None = None):
        self.chains, self.quotes, self.stock = chains or {}, quotes or {}, stock or {}
        self.chain_calls = []

    def option_chain(self, und, lo, hi):
        self.chain_calls.append(und)
        return self.chains.get(und, [])

    def option_quote(self, occ):
        return self.quotes.get(occ)

    def stock_quote(self, sym):
        return self.stock.get(sym)


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    wind.ensure_schema(c)
    spreads.ensure_schema(c)
    c.execute("CREATE TABLE signals (signal_id TEXT, date TEXT, strategy TEXT, symbol TEXT, score REAL)")
    return c


def set_wind(con, day: str, state: str) -> None:
    con.execute("INSERT OR REPLACE INTO wind_daily (date, state) VALUES (?, ?)", (day, state))


def test_pick_legs_debit_and_width():
    now = et(DAY, 10, 0)
    p, reason = spreads.pick(chain(now), DAY, now, CFG)
    assert reason == ""
    assert (p.long.strike, p.short.strike, p.width) == (100, 105, 5)
    # long at the ask + 0.03, short at the bid - 0.03
    assert p.debit == pytest.approx((3.1 + 0.03) - (1.2 - 0.03))


def test_pick_expiry_closest_to_14_days_within_7_to_21():
    now = et(DAY, 10, 0)
    near, far, out = DAY + timedelta(days=9), DAY + timedelta(days=16), DAY + timedelta(days=30)
    p, _ = spreads.pick(chain(now, expiry=near) + chain(now, expiry=far) + chain(now, expiry=out), DAY, now, CFG)
    assert p.long.expiry == far  # |16-14| < |9-14|; 30 days is outside the window
    p, reason = spreads.pick(chain(now, expiry=DAY + timedelta(days=3)), DAY, now, CFG)
    assert p is None and reason == "no_expiry"


def test_pick_rejects_stale_and_one_sided_quotes():
    now = et(DAY, 10, 0)
    ch = chain(now)
    ch[1].as_of = now - timedelta(seconds=CFG.max_quote_age_s + 1)  # long leg stale
    p, reason = spreads.pick(ch, DAY, now, CFG)
    assert p is None and reason == "no_long_leg"
    ch = chain(now)
    ch[2].bid = 0.0  # short leg one-sided
    p, reason = spreads.pick(ch, DAY, now, CFG)
    assert p is None and reason == "no_short_leg"


def test_pick_debit_must_be_below_width():
    now = et(DAY, 10, 0)
    ch = chain(now)
    ch[1].ask, ch[1].bid = 6.5, 6.4  # debit 6.53 - 1.17 = 5.36 > width 5
    p, reason = spreads.pick(ch, DAY, now, CFG)
    assert p is None and reason == "debit_not_below_width"


def test_open_only_on_prior_session_tailwind(con):
    now = et(DAY, 10, 0)
    data = FakeData({"SPY": chain(now), "QQQ": chain(now, "QQQ")})
    set_wind(con, DAY.isoformat(), "tailwind")  # today's own row must not count
    set_wind(con, "2026-10-27", "neutral")
    assert spreads.open_new(con, data, now, CFG) == []
    assert data.chain_calls == []
    con.execute("UPDATE wind_daily SET state = 'tailwind' WHERE date = '2026-10-27'")
    assert spreads.open_new(con, data, now, CFG) == ["QQQ", "SPY"]


def test_open_window_and_candidates(con):
    set_wind(con, "2026-10-27", "tailwind")
    con.executemany("INSERT INTO signals VALUES (?,?,?,?,?)", [
        ("a", "2026-10-27", "pullback", "NVDA", 0.3),   # in the options universe
        ("b", "2026-10-27", "pullback", "TMO", 0.2),    # not in the universe
        ("c", "2026-10-26", "breakout", "AAPL", 0.5)])  # older session
    assert spreads.candidates(con, DAY, CFG) == [("NVDA", "signal:pullback"), ("QQQ", "wind"), ("SPY", "wind")]
    early = et(DAY, 9, 40)
    assert spreads.open_new(con, FakeData({"SPY": chain(early)}), early, CFG) == []
    late = et(DAY, 11, 0)
    assert spreads.open_new(con, FakeData({"SPY": chain(late)}), late, CFG) == []


def test_open_records_row_and_sizes_within_premium_cap(con):
    set_wind(con, "2026-10-27", "tailwind")
    now = et(DAY, 10, 0)
    spreads.open_new(con, FakeData({"SPY": chain(now)}), now, CFG)
    row = con.execute("SELECT * FROM options_spreads_shadow WHERE underlying = 'SPY'").fetchone()
    assert row["status"] == "OPEN" and row["wind"] == "tailwind" and row["source"] == "wind"
    assert row["qty"] == 5  # floor(1000 / (1.96 * 100))
    assert row["entry_debit"] * 100 * row["qty"] <= CFG.max_premium_per_trade
    # QQQ had no chain: retried each slot, the skip is written only on the last one
    q = "SELECT status, skip_reason FROM options_spreads_shadow WHERE underlying = 'QQQ'"
    assert con.execute(q).fetchone() is None
    # a later slot the same day opens nothing new
    data = FakeData({"SPY": chain(now)})
    assert spreads.open_new(con, data, et(DAY, 10, 55), CFG) == []
    assert data.chain_calls == ["QQQ"]
    assert tuple(con.execute(q).fetchone()) == ("SKIPPED", "no_expiry")


def test_stale_legs_retry_until_the_last_slot(con):
    set_wind(con, "2026-10-27", "tailwind")
    now = et(DAY, 10, 0)
    stale = chain(now)
    for c in stale:
        c.as_of = now - timedelta(minutes=5)
    spreads.open_new(con, FakeData({"SPY": stale}), now, CFG)
    assert con.execute("SELECT COUNT(*) FROM options_spreads_shadow WHERE underlying = 'SPY'").fetchone()[0] == 0
    last = et(DAY, 10, 55)
    for c in stale:
        c.as_of = last - timedelta(minutes=5)
    spreads.open_new(con, FakeData({"SPY": stale}), last, CFG)
    row = con.execute("SELECT status, skip_reason FROM options_spreads_shadow WHERE underlying = 'SPY'").fetchone()
    assert tuple(row) == ("SKIPPED", "stale_quotes")


def test_too_expensive_and_delayed_runs_do_not_skip_early(con):
    set_wind(con, "2026-10-27", "tailwind")
    now = et(DAY, 10, 0)
    dear = chain(now)
    dear[1].bid, dear[1].ask = 13.0, 13.1  # debit about 11.97: below a 15 width, above the $1000 cap
    dear[2].strike, dear[3].strike = 115, 120
    spreads.open_new(con, FakeData({"SPY": dear}), now, CFG)
    q = "SELECT status, skip_reason FROM options_spreads_shadow WHERE underlying = 'SPY'"
    assert con.execute(q).fetchone() is None
    late_run = et(DAY, 10, 50)  # the 10:45 slot pushed back by a slow intraday tick
    spreads.open_new(con, FakeData({"SPY": [], "QQQ": []}), late_run, CFG)
    assert con.execute("SELECT COUNT(*) FROM options_spreads_shadow").fetchone()[0] == 0
    last = et(DAY, 10, 55)
    assert spreads.open_new(con, FakeData({"SPY": chain(last)}), last, CFG) == ["SPY"]


def test_last_slot_keeps_its_slot_when_delayed(con):
    set_wind(con, "2026-10-27", "tailwind")
    now = et(DAY, 11, 0)  # the 10:55 fire after the intraday alarm used its 300s
    assert spreads.open_new(con, FakeData({"SPY": chain(now)}), now, CFG) == []
    data = FakeData({"SPY": chain(now)})
    assert spreads.open_new(con, data, now, CFG, slot=et(DAY, 10, 55)) == ["SPY"]
    q = "SELECT status, skip_reason FROM options_spreads_shadow WHERE underlying = 'QQQ'"
    assert tuple(con.execute(q).fetchone()) == ("SKIPPED", "no_expiry")


def test_main_passes_the_slot_and_ignores_an_implausible_one(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(spreads, "tick", lambda con, data, now, slot=None: seen.append(slot) or ([], []))
    now = et(DAY, 11, 0).astimezone(timezone.utc)
    db = str(tmp_path / "d.sqlite")
    fire = int(et(DAY, 10, 55).timestamp())
    assert spreads.main(["x", "tick", "--db", db, "--slot-at", str(fire)], clock=lambda: now,
                        data_factory=object) == 0
    assert spreads.main(["x", "tick", "--db", db, "--slot-at", str(fire - 3600)], clock=lambda: now,
                        data_factory=object) == 0
    assert seen == [et(DAY, 10, 55).astimezone(timezone.utc), None]


def test_candidates_are_capped(con):
    set_wind(con, "2026-10-27", "tailwind")
    syms = ["NVDA", "AMD", "AAPL", "MSFT", "META", "AMZN", "GOOGL"]
    con.executemany("INSERT INTO signals VALUES (?,?,?,?,?)",
                    [(s, "2026-10-27", "pullback", s, 1.0 - i / 10) for i, s in enumerate(syms)])
    got = spreads.candidates(con, DAY, OptionsConfig(index=("SPY", "QQQ"), mega_cap=tuple(syms)))
    assert len(got) == spreads.MAX_CANDIDATES
    assert {"SPY", "QQQ", "NVDA", "AMD"} <= {s for s, _ in got}


def _opened(con):
    set_wind(con, "2026-10-27", "tailwind")
    now = et(DAY, 10, 0)
    spreads.open_new(con, FakeData({"SPY": chain(now)}), now, CFG)
    return con.execute("SELECT * FROM options_spreads_shadow WHERE underlying = 'SPY'").fetchone()


def _quotes(now, long_bid, long_ask, short_bid, short_ask):
    q = now - timedelta(seconds=5)
    return {"SPY-100": C("SPY-100", 100, 0.5, long_bid, long_ask, q),
            "SPY-105": C("SPY-105", 105, 0.3, short_bid, short_ask, q)}


def test_no_exit_on_entry_day_but_marked(con):
    _opened(con)
    now = et(DAY, 15, 0)
    data = FakeData(quotes=_quotes(now, 0.5, 0.6, 0.4, 0.5))  # value far below the stop
    assert spreads.mark_and_exit(con, data, now, CFG) == []
    row = con.execute("SELECT status, marks, last_value FROM options_spreads_shadow WHERE underlying = 'SPY'").fetchone()
    assert row["status"] == "OPEN" and row["marks"] == 1
    assert row["last_value"] == 0.0  # raw (0.5 - 0.03) - (0.5 + 0.03) < 0 is clamped


def test_mark_is_clamped_to_width(con):
    row = _opened(con)
    nxt = et(date(2026, 10, 29), 10, 0)
    spreads.mark_and_exit(con, FakeData(quotes=_quotes(nxt, 9.0, 9.1, 0.01, 0.05)), nxt, CFG)
    r = con.execute("SELECT exit_value FROM options_spreads_shadow WHERE id = ?", (row["id"],)).fetchone()
    assert r["exit_value"] == row["width"]


def test_take_profit_stop_and_time_exits(con):
    row = _opened(con)
    debit = row["entry_debit"]
    nxt = et(date(2026, 10, 29), 10, 0)
    # value 4.0 - 0.03 - (0.4 + 0.03) = 3.54 >= debit + 0.5 * (5 - debit) = 3.48
    out = spreads.mark_and_exit(con, FakeData(quotes=_quotes(nxt, 4.0, 4.1, 0.3, 0.4)), nxt, CFG)
    assert out == [("SPY", "take_profit")]
    r = con.execute("SELECT * FROM options_spreads_shadow WHERE underlying = 'SPY'").fetchone()
    assert r["status"] == "CLOSED"
    fees = 4 * r["qty"] * CFG.fee_per_contract
    assert r["pnl_usd"] == pytest.approx((r["exit_value"] - debit) * 100 * r["qty"] - fees)

    con.execute("DELETE FROM options_spreads_shadow")
    row = _opened(con)
    out = spreads.mark_and_exit(con, FakeData(quotes=_quotes(nxt, 1.0, 1.1, 0.01, 0.05)), nxt, CFG)
    assert out == [("SPY", "stop")]  # 0.97 - 0.08 = 0.89 <= 0.98

    con.execute("DELETE FROM options_spreads_shadow")
    _opened(con)
    late = et(EXP - timedelta(days=2), 10, 0)
    out = spreads.mark_and_exit(con, FakeData(quotes=_quotes(late, 2.0, 2.1, 0.8, 0.9)), late, CFG)
    assert out == [("SPY", "time")]


def test_stale_mark_is_skipped_and_expired_settles_at_intrinsic(con):
    _opened(con)
    nxt = et(date(2026, 10, 29), 10, 0)
    q = _quotes(nxt, 4.0, 4.1, 0.3, 0.4)
    q["SPY-105"].as_of = nxt - timedelta(minutes=10)
    assert spreads.mark_and_exit(con, FakeData(quotes=q), nxt, CFG) == []
    after = et(EXP + timedelta(days=3), 10, 0)
    out = spreads.mark_and_exit(con, FakeData(stock={"SPY": (103.0, 103.2)}), after, CFG)
    assert out == [("SPY", "expiry_late")]
    r = con.execute("SELECT exit_value FROM options_spreads_shadow WHERE underlying = 'SPY'").fetchone()
    assert r["exit_value"] == pytest.approx(3.1)


def test_expiry_day_settles_from_same_day_stock_quote(con):
    _opened(con)
    early = et(EXP, 15, 0)
    assert spreads.mark_and_exit(con, FakeData(stock={"SPY": (103.0, 103.2)}), early, CFG) == []
    close = et(EXP, 15, 30)
    out = spreads.mark_and_exit(con, FakeData(stock={"SPY": (103.0, 103.2)}), close, CFG)
    assert out == [("SPY", "expiry")]


def test_expired_without_any_price_closes_at_last_mark(con):
    _opened(con)
    con.execute("UPDATE options_spreads_shadow SET last_value = 7.0")  # clamped to the width
    after = et(EXP + timedelta(days=1), 10, 0)
    assert spreads.mark_and_exit(con, FakeData(), after, CFG) == [("SPY", "expiry_unpriced")]
    r = con.execute("SELECT status, exit_value FROM options_spreads_shadow").fetchone()
    assert r["status"] == "CLOSED" and r["exit_value"] == 5.0
    assert "到期後才結算(不計入)1 筆" in spreads.report(con)


def test_report_says_too_few_samples(con):
    _opened(con)
    text = spreads.report(con)
    assert "持有中 1 筆" in text and "樣本不足" in text and "wind 0" in text


def test_report_shows_lower_bound_per_source(con):
    rows = [(f"2026-10-{d:02d}", "SPY", "wind", "tailwind", "CLOSED", 2.0, 1, 100.0 if d % 2 else -50.0,
             "take_profit") for d in range(1, 21)]
    con.executemany("INSERT INTO options_spreads_shadow (day, underlying, source, wind, status, entry_debit,"
                    " qty, pnl_usd, exit_reason) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    text = spreads.report(con)
    assert "wind:已平倉 20 筆" in text and "下緣" in text and "單日最大 +100" in text
    assert "樣本不足 30(每個來源),不評估:wind 20" in text


def test_main_refuses_outside_session(tmp_path):
    called = []
    sat = datetime(2026, 10, 31, 14, 0, tzinfo=timezone.utc)
    assert spreads.main(["x", "tick", "--db", str(tmp_path / "d.sqlite")], clock=lambda: sat,
                        data_factory=lambda: called.append(1)) == 0
    assert called == [] and not (tmp_path / "d.sqlite").exists()


def test_module_never_loads_broker_or_execution():
    env = {**os.environ, "PYTHONPATH": str(SRC.parents[3])}
    code = ("import sys, stockmoney.desk.options.spreads\n"
            "bad = [m for m in sys.modules if m.startswith(('stockmoney.desk.execution', 'stockmoney.desk.broker',"
            " 'alpaca'))]\n"
            "print(','.join(bad))")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""


def test_source_has_no_order_words():
    text = SRC.read_text()
    for word in ("submit", "place_", "OrderIntent", "TradingClient", "cancel_"):
        assert word not in text
