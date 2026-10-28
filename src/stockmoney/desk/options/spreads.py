"""Defined-risk spread shadow (EDGE_PLAN H6). SHADOW ONLY: no order method
exists here and nothing here is read by the options session or the stock jobs.

Question: does a bull call debit spread, opened only on tailwind days, turn the
desk's daily long ideas into a positive expectancy after realistic costs? Risk
per spread is the debit paid, known at entry.

Entry (each trading day D, 09:45 to 11:00 ET, first slot with usable quotes):
- only if the wind state logged for the last session before D is ``tailwind``
  (``wind.state_before``: what a morning decision could know);
- candidates: the index ETFs (source ``wind``) and the stock signals of the
  previous session whose symbol is in the options universe (source
  ``signal:<strategy>``); at most one open spread per underlying;
- expiry: the call expiry 7 to 21 calendar days out closest to 14 days;
- legs: long call delta nearest 0.50 (0.40 to 0.60), short call delta nearest
  0.30 (0.20 to 0.40) at a higher strike, both with a fresh two-sided quote
  (bid > 0, ask > bid, quote age <= ``max_quote_age_s``);
- fill: long at the ask plus SLIP, short at the bid minus SLIP (SLIP = $0.03
  per leg, or ``shadow_slippage_pct`` of the price when larger); the debit
  must be below the strike width; size = whole spreads within
  ``max_premium_per_trade``.

Exit, checked every slot from the next session on (fixed a priori, not fitted):
take profit when the closing value reaches the debit plus half the maximum
profit; stop when it falls to half the debit; time exit at 2 calendar days or
fewer to expiry. The closing value is long bid minus SLIP less short ask plus
SLIP, clamped to [0, width] (what the spread can be worth); a slot with a stale
leg is not marked. On expiry day from EXPIRY_SETTLE ET, a spread whose legs
have no fresh quote settles at intrinsic from the stock mid; one still open
after expiry (no quote at all that day) settles at the next stock mid and is
labelled ``expiry_late``; with no stock mid either it closes at its last mark
as ``expiry_unpriced``. Both are excluded from the H6 statistics.

Every skip reason is retried each slot of the entry window and written only
on its last slot; ``stale_quotes`` means the expiry had contracts but none with
a fresh two-sided quote. Network reads happen before any write so a slow chain
never holds the database lock.

    python -m stockmoney.desk.options.spreads tick      # one slot: open, mark, close
    python -m stockmoney.desk.options.spreads report

Run from scripts/desk_job.sh after each intraday_shadow slot (every 10 minutes
09:45 to 15:55 ET). Writes only ``options_spreads_shadow``. Under MIN_SAMPLES
closed spreads per source nothing is concluded.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from stockmoney.desk import wind
from stockmoney.desk.calendar import close_time, is_trading_day
from stockmoney.desk.options.config import OptionsConfig
from stockmoney.desk.options.shadow import contracts_for

log = logging.getLogger("desk.options.spreads")
ET = ZoneInfo("America/New_York")
DB_PATH = Path("data/desk/desk.sqlite")

SLIP = 0.03
DTE_MIN, DTE_MAX, DTE_TARGET = 7, 21, 14
LONG_DELTA, LONG_BAND = 0.50, (0.40, 0.60)
SHORT_DELTA, SHORT_BAND = 0.30, (0.20, 0.40)
TAKE_PROFIT_FRAC = 0.5   # of the maximum profit
STOP_FRAC = 0.5          # value <= this x debit
TIME_EXIT_DTE = 2
ENTRY_WINDOW = (time(9, 45), time(11, 0))
WIND_SYMBOLS = ("SPY", "QQQ")
MIN_SAMPLES = 30
MAX_CANDIDATES = 6  # index ETFs plus the top-scored signals: bounds chain downloads per slot
EXPIRY_SETTLE = time(15, 30)
TICK_TIMEOUT_S = 240

SCHEMA = """
CREATE TABLE IF NOT EXISTS options_spreads_shadow (
  id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, underlying TEXT, source TEXT, wind TEXT,
  status TEXT, skip_reason TEXT, expiry TEXT, long_occ TEXT, short_occ TEXT,
  long_strike REAL, short_strike REAL, width REAL, qty INTEGER,
  long_ask REAL, short_bid REAL, long_delta REAL, short_delta REAL, entry_debit REAL,
  entry_time TEXT, last_value REAL, last_mark_time TEXT, marks INTEGER DEFAULT 0,
  exit_time TEXT, exit_value REAL, exit_reason TEXT, fees_usd REAL, pnl_usd REAL,
  UNIQUE(day, underlying));
"""


def ensure_schema(con) -> None:
    con.executescript(SCHEMA)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def slip(price: float, cfg: OptionsConfig) -> float:
    return max(SLIP, price * cfg.shadow_slippage_pct)


def fresh(c, now: datetime, cfg: OptionsConfig) -> bool:
    """Usable two-sided quote no older than ``max_quote_age_s``."""
    if c is None or c.bid is None or c.ask is None or c.as_of is None:
        return False
    if not (c.bid > 0 and c.ask > c.bid):
        return False
    return 0 <= (now - c.as_of).total_seconds() <= cfg.max_quote_age_s


def entry_debit(long_ask: float, short_bid: float, cfg: OptionsConfig) -> float:
    return (long_ask + slip(long_ask, cfg)) - (short_bid - slip(short_bid, cfg))


def close_value(long_bid: float, short_ask: float, width: float, cfg: OptionsConfig) -> float:
    """Exit value of the spread, clamped to what a call debit spread can be worth."""
    raw = (long_bid - slip(long_bid, cfg)) - (short_ask + slip(short_ask, cfg))
    return min(max(raw, 0.0), width)


@dataclass
class Pick:
    long: object
    short: object
    debit: float
    width: float


def pick(chain: list, day: date, now: datetime, cfg: OptionsConfig) -> tuple[Pick | None, str]:
    """Choose expiry and legs from a call chain; (None, reason) when nothing fits."""
    calls = [c for c in chain if c.right == "C" and DTE_MIN <= (c.expiry - day).days <= DTE_MAX]
    if not calls:
        return None, "no_expiry"
    expiry = min({c.expiry for c in calls}, key=lambda e: (abs((e - day).days - DTE_TARGET), e))
    legs = [c for c in calls if c.expiry == expiry and c.delta is not None and fresh(c, now, cfg)]
    if not legs:
        return None, "stale_quotes"
    longs = [c for c in legs if LONG_BAND[0] <= c.delta <= LONG_BAND[1]]
    if not longs:
        return None, "no_long_leg"
    lg = min(longs, key=lambda c: (abs(c.delta - LONG_DELTA), c.strike))
    shorts = [c for c in legs if c.strike > lg.strike and SHORT_BAND[0] <= c.delta <= SHORT_BAND[1]]
    if not shorts:
        return None, "no_short_leg"
    sh = min(shorts, key=lambda c: (abs(c.delta - SHORT_DELTA), c.strike))
    width = sh.strike - lg.strike
    debit = entry_debit(lg.ask, sh.bid, cfg)
    if not 0 < debit < width:
        return None, "debit_not_below_width"
    return Pick(lg, sh, debit, width), ""


def candidates(con, day: date, cfg: OptionsConfig) -> list[tuple[str, str]]:
    """(underlying, source) for ``day``: index ETFs plus the previous session's
    stock signals in the options universe, one row per underlying."""
    prev = day - timedelta(days=1)
    while not is_trading_day(prev):
        prev -= timedelta(days=1)
    universe = set(cfg.index) | set(cfg.mega_cap) | set(cfg.high_vol)
    out = {s: "wind" for s in WIND_SYMBOLS}
    rows = con.execute("SELECT symbol, strategy FROM signals WHERE date = ? ORDER BY score DESC",
                       (prev.isoformat(),)).fetchall()
    for sym, strat in rows:
        if len(out) >= MAX_CANDIDATES:
            break
        if sym in universe and sym not in out:
            out[sym] = f"signal:{strat}"
    return sorted(out.items())


def _skip(con, day: str, und: str, source: str, state: str | None, reason: str) -> None:
    con.execute("INSERT OR IGNORE INTO options_spreads_shadow (day, underlying, source, wind, status, skip_reason)"
                " VALUES (?,?,?,?, 'SKIPPED', ?)", (day, und, source, state, reason))


def open_new(con, data, now: datetime, cfg: OptionsConfig) -> list[str]:
    """Open spreads for today's candidates (entry window, tailwind only)."""
    et = now.astimezone(ET)
    day = et.date()
    if not ENTRY_WINDOW[0] <= et.time() < ENTRY_WINDOW[1]:
        return []
    state = wind.state_before(con, day.isoformat())
    if state != "tailwind":
        return []
    done = {r[0] for r in con.execute("SELECT underlying FROM options_spreads_shadow WHERE day = ?",
                                      (day.isoformat(),))}
    held = {r[0] for r in con.execute("SELECT underlying FROM options_spreads_shadow WHERE status = 'OPEN'")}
    # the 10:55 slot only: a 10:45 run delayed by the intraday tick's alarm must not write skips
    last_slot = et.time() >= (datetime.combine(day, ENTRY_WINDOW[1]) - timedelta(minutes=5)).time()
    picks = []  # network first: no write (and so no lock) is held while chains download
    for und, source in candidates(con, day, cfg):
        if und in done or und in held:
            continue
        try:
            chain = data.option_chain(und, day + timedelta(days=DTE_MIN), day + timedelta(days=DTE_MAX))
        except Exception as e:  # noqa: BLE001 - one failed chain must not stop the others
            log.warning("spreads: chain %s failed: %s", und, e)
            picks.append((und, source, None, "chain_error"))
            continue
        picks.append((und, source, *pick(chain, day, now, cfg)))
    opened = []
    for und, source, p, reason in picks:
        if p is None:
            if last_slot:  # every reason may clear at a later slot of the window
                _skip(con, day.isoformat(), und, source, state, reason)
            continue
        qty = contracts_for(p.debit, cfg)
        if qty == 0:
            if last_slot:  # a cheaper quote may come later in the window
                _skip(con, day.isoformat(), und, source, state, "too_expensive")
            continue
        con.execute(
            "INSERT OR IGNORE INTO options_spreads_shadow (day, underlying, source, wind, status, expiry, long_occ,"
            " short_occ, long_strike, short_strike, width, qty, long_ask, short_bid, long_delta, short_delta,"
            " entry_debit, entry_time, fees_usd) VALUES (?,?,?,?, 'OPEN', ?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (day.isoformat(), und, source, state, p.long.expiry.isoformat(), p.long.occ, p.short.occ,
             p.long.strike, p.short.strike, p.width, qty, p.long.ask, p.short.bid, p.long.delta, p.short.delta,
             p.debit, _iso(now), 2 * qty * cfg.fee_per_contract))
        opened.append(und)
    return opened


def _close(con, row, now: datetime, value: float, reason: str, cfg: OptionsConfig) -> None:
    fees = (row["fees_usd"] or 0.0) + 2 * row["qty"] * cfg.fee_per_contract
    pnl = (value - row["entry_debit"]) * cfg.contract_multiplier * row["qty"] - fees
    con.execute("UPDATE options_spreads_shadow SET status = 'CLOSED', exit_time = ?, exit_value = ?,"
                " exit_reason = ?, fees_usd = ?, pnl_usd = ? WHERE id = ?",
                (_iso(now), value, reason, fees, pnl, row["id"]))


def mark_and_exit(con, data, now: datetime, cfg: OptionsConfig) -> list[tuple[str, str]]:
    """Mark every open spread; close on the fixed rules. Returns (underlying, reason)."""
    et = now.astimezone(ET)
    day = et.date()
    rows = con.execute("SELECT * FROM options_spreads_shadow WHERE status = 'OPEN'").fetchall()
    quotes = {}  # network first, writes after
    for row in rows:
        expiry = date.fromisoformat(row["expiry"])
        lg = sh = None
        if day <= expiry:
            lg, sh = data.option_quote(row["long_occ"]), data.option_quote(row["short_occ"])
        stock = None
        if day > expiry or (day == expiry and et.time() >= EXPIRY_SETTLE
                            and not (fresh(lg, now, cfg) and fresh(sh, now, cfg))):
            stock = data.stock_quote(row["underlying"])
        quotes[row["id"]] = (lg, sh, stock)
    closed = []
    for row in rows:
        expiry = date.fromisoformat(row["expiry"])
        lg, sh, stock = quotes[row["id"]]
        if day >= expiry and stock is not None and not (fresh(lg, now, cfg) and fresh(sh, now, cfg)):
            px = sum(stock) / 2
            value = min(max(px - row["long_strike"], 0.0), row["width"])
            reason = "expiry" if day == expiry else "expiry_late"
            _close(con, row, now, value, reason, cfg)
            closed.append((row["underlying"], reason))
            continue
        if day > expiry:  # no price at all after expiry: never leave the name blocked
            value = min(max(row["last_value"] or 0.0, 0.0), row["width"])
            _close(con, row, now, value, "expiry_unpriced", cfg)
            closed.append((row["underlying"], "expiry_unpriced"))
            continue
        if not (fresh(lg, now, cfg) and fresh(sh, now, cfg)):
            continue
        value = close_value(lg.bid, sh.ask, row["width"], cfg)
        con.execute("UPDATE options_spreads_shadow SET last_value = ?, last_mark_time = ?, marks = marks + 1"
                    " WHERE id = ?", (value, _iso(now), row["id"]))
        if day <= date.fromisoformat(row["day"]):
            continue  # exits start the next session
        debit = row["entry_debit"]
        reason = ("take_profit" if value >= debit + TAKE_PROFIT_FRAC * (row["width"] - debit)
                  else "stop" if value <= STOP_FRAC * debit
                  else "time" if (expiry - day).days <= TIME_EXIT_DTE else None)
        if reason:
            _close(con, row, now, value, reason, cfg)
            closed.append((row["underlying"], reason))
    return closed


def regular_hours(now: datetime) -> bool:
    et = now.astimezone(ET)
    if not is_trading_day(et.date()):
        return False
    return time(9, 30) <= et.time() < time(*close_time(et.date()))


def tick(con, data, now: datetime, cfg: OptionsConfig | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    cfg = cfg or OptionsConfig()
    ensure_schema(con)
    wind.ensure_schema(con)
    closed = mark_and_exit(con, data, now, cfg)
    con.commit()
    opened = open_new(con, data, now, cfg)
    con.commit()
    return opened, closed


def report(con) -> str:
    """Closed spreads per source (H6 evidence) plus what is open."""
    ensure_schema(con)
    from stockmoney.desk.learn.h_reports import trade_stats  # lazy: keep the tick import light

    lines = ["== 價差影子(H6,只記錄) =="]
    sources = [r[0] for r in con.execute(
        "SELECT DISTINCT source FROM options_spreads_shadow WHERE status != 'SKIPPED' ORDER BY source")]
    short = []
    for src in sources:
        closed = con.execute(
            "SELECT day, pnl_usd, pnl_usd / (entry_debit * 100 * qty) AS ret FROM options_spreads_shadow"
            " WHERE status = 'CLOSED' AND source = ? AND exit_reason NOT IN ('expiry_late', 'expiry_unpriced')", (src,)).fetchall()
        st = trade_stats([{"ret": r["ret"]} for r in closed])
        if st["n"] < MIN_SAMPLES:
            short.append(f"{src} {st['n']}")
        if not st["n"]:
            continue
        pnl = sum(r["pnl_usd"] for r in closed)
        by_day: dict[str, float] = {}
        for r in closed:
            by_day[r["day"]] = by_day.get(r["day"], 0.0) + r["pnl_usd"]
        top = max(by_day.values(), key=abs)
        lcb = "n/a" if st["lcb"] is None else f"{st['lcb']:+.1%}"
        lines.append(f"{src}:已平倉 {st['n']} 筆,勝率 {st['win_rate']:.0%},淨損益 {pnl:+,.0f},"
                     f"每筆報酬(對成本)平均 {st['mean']:+.1%} 下緣 {lcb},單日最大 {top:+,.0f}")
    if not sources:
        lines.append("尚無開倉")
    n_open = con.execute("SELECT COUNT(*) FROM options_spreads_shadow WHERE status = 'OPEN'").fetchone()[0]
    skips = con.execute("SELECT skip_reason, COUNT(*) FROM options_spreads_shadow WHERE status = 'SKIPPED'"
                        " GROUP BY skip_reason ORDER BY 2 DESC").fetchall()
    lines.append(f"持有中 {n_open} 筆" + (";略過 " + ", ".join(f"{k} {v}" for k, v in skips) if skips else ""))
    late = con.execute("SELECT COUNT(*) FROM options_spreads_shadow WHERE exit_reason IN ('expiry_late', 'expiry_unpriced')").fetchone()[0]
    if late:
        lines.append(f"到期後才結算(不計入){late} 筆")
    if short or not sources:
        lines.append(f"樣本不足 {MIN_SAMPLES}(每個來源),不評估" + (":" + ", ".join(short) if short else ""))
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI

class TickTimeout(BaseException):
    """Outer alarm. BaseException so no retry layer can swallow it."""


def main(argv: list[str], *, clock=lambda: datetime.now(timezone.utc), data_factory=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="python -m stockmoney.desk.options.spreads")
    ap.add_argument("cmd", choices=("tick", "report"))
    ap.add_argument("--db", type=Path, default=DB_PATH)
    a = ap.parse_args(argv[1:])
    now = clock()
    if a.cmd == "tick" and not regular_hours(now):
        return 0  # holiday or outside the session: nothing to quote

    def _timeout(*_):
        raise TickTimeout(f"spreads {a.cmd} exceeded {TICK_TIMEOUT_S}s")
    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(TICK_TIMEOUT_S)
    try:
        con = sqlite3.connect(a.db, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            if a.cmd == "report":
                print(report(con))
                return 0
            if data_factory is None:
                from stockmoney.desk.options.marketdata import AlpacaOptionsData
                data_factory = AlpacaOptionsData
            opened, closed = tick(con, data_factory(), now)
            for und in opened:
                print(f"價差影子開倉 {und}")
            for und, reason in closed:
                print(f"價差影子平倉 {und} {reason}")
            return 0
        finally:
            con.close()
    except TickTimeout as e:
        log.error("%s", e)
        return 1
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "locked" not in msg and "busy" not in msg:
            raise
        log.warning("價差影子:資料庫忙碌,略過本時段:%s", e)
        return 0
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
