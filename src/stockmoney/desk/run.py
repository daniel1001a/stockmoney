"""Orchestrator: the scheduled jobs that run the desk against a real broker
(Alpaca paper today). Every job is idempotent and safe to re-run.

    python -m stockmoney.desk.run postclose   # 16:45 ET: sync fills, reconcile, trail stops, plan tomorrow
    python -m stockmoney.desk.run premarket   # 08:45 ET: Muse flags, risk checks, submit market-on-open
    python -m stockmoney.desk.run postopen    # 09:50 ET: sync fills, deferred exits, protective stops
    python -m stockmoney.desk.run recover     # at boot (launchd RunAtLoad): sync fills, stops, orphan sweep
    python -m stockmoney.desk.run crypto      # 20:00 ET daily: crypto sleeves (Phase 1)
    python -m stockmoney.desk.run bridge      # every 15 min: poll Muse

Flow of one stock trade:
  postclose(T)  strategy signal -> planned buy (sized)          [planned_orders]
  premarket(T+1) Muse flags + risk check -> OPG market buy      [submitted]
  postopen(T+1)  fill -> ledger; GTC stop placed at broker      [ledger, position_meta]
  postclose(...) trail the stop up; rule exit -> planned sell
  premarket      cancel stop, OPG market sell (exits never expire; a failed
                 exit re-places its stop and is retried), then a stop pass
                 over every other held symbol, then entries
  postopen       an exit premarket had to defer (stop cancel not confirmed)
                 is sent as a DAY market sell; an opening sell that ended
                 unfilled gets its stop back and is re-planned for the next run; an opening
                 buy that ended short (expired/cancelled, filled < qty) gets
                 one DAY market buy for the missing shares, through the same
                 risk checks, before 10:30 ET only
The broker holds the truth. A reconcile mismatch halts new entries and pages
the owner; exits and protective stops keep running for every symbol that still
reconciles (a mismatched symbol is left alone for a human, and its exit stays
planned until it reconciles again). A halt set by reconcile is lifted by the
next clean reconcile; a manual halt (console or HALT file) stays until the
owner lifts it. Fills are booked as they arrive, partial fills included.
Postopen, postclose and recover end by cancelling any sell stop for a symbol
the ledger does not hold.

Options isolation: the intraday options sleeve trades in the same broker
account. Every job and the stop helpers wrap the broker in
execution.isolation.stock_only, so option positions and orders are invisible
here: never reconciled, counted as exposure, adopted, swept or cancelled.

Crash safety: an order is written to ``submitted`` as ``pending_submit`` and
committed before broker.submit is called (sync_fills resolves such rows by
client_order_id), and a cancel is committed right after it happens, so neither
a crash nor a later exception can lose the record of an order the broker
already holds. Jobs never run concurrently (an
exclusive lock on data/desk/run.lock), and a job that exceeds JOB_TIMEOUT_S is
aborted with JobTimeout, a BaseException that no retry layer can swallow. The
job clock starts once the lock is held, and premarket re-reads it before each
order, so a run that waited for the lock never sends a stale opening order.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import floor
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from stockmoney.desk import data, engine, notify, store
from stockmoney.desk.calendar import is_trading_day
from stockmoney.desk.execution.broker import Broker
from stockmoney.desk.execution.isolation import stock_only
from stockmoney.desk.execution.ledger import Ledger, norm
from stockmoney.desk.execution.reconcile import daily as reconcile_daily
from stockmoney.desk.execution.reconcile import mismatches
from stockmoney.desk import profiles
from stockmoney.desk.execution.risk import Limits, RiskEngine
from stockmoney.desk.execution.types import (
    AccountSnapshot, AssetClass, BrokerOrder, OrderIntent, OrderStatus, OrderType, Side, TIF,
)
from stockmoney.desk.indicators import REGIME_LABEL, compute
from stockmoney.desk.strategies import REGISTRY, Strategy
from stockmoney.desk.universe import sector_name, sector_of

log = logging.getLogger("desk.run")
ET = ZoneInfo("America/New_York")
OPG_CUTOFF = (9, 25)     # ET: from here on, market-on-open is no longer accepted
ENTRY_CUTOFF = (10, 30)  # ET: new entries from a stale plan are dropped after this
SESSION_CLOSE = (16, 0)  # ET: regular close, used when the broker has no market calendar
JOB_TIMEOUT_S = 900      # the Alpaca SDK sets no HTTP timeout; never let a job hang forever
CANCEL_WAIT_S = 20.0     # how long to wait for an asynchronous cancel or fill to settle
LOCK_PATH = Path("data/desk/run.lock")
LOCK_WAIT_S = JOB_TIMEOUT_S + 60.0  # longer than any job may hold the lock, then give up
HALT_SOURCE_KEY = "halt_source"  # who set halt_new_entries: reconcile | sync | manual ('' = unknown, sticky)
PLAN_THROUGH_KEY = "last_postclose_through"  # session date the newest valid plans were made from
MAX_EXIT_ATTEMPTS = 3    # a failing exit is retried this many times per day, then left for a human
PENDING_SUBMIT = "pending_submit"  # written before broker.submit; resolved by sync_fills
NOT_SENT = "not_sent"    # a pending_submit row the broker never received

# Stock-book sizing (backtested values; see docs/desk/OPERATING_PLAN.md)
_PROFILE = profiles.active()
RISK_PER_TRADE = _PROFILE.risk_per_trade
MAX_POSITIONS = _PROFILE.max_positions
MAX_PER_SECTOR = _PROFILE.max_per_sector
MAX_POSITION_PCT = _PROFILE.max_position_pct  # standard = same cap as the backtest Account

_TERMINAL = (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED)
_sleep = time.sleep
_monotonic = time.monotonic


class JobTimeout(BaseException):
    """Raised by the job alarm. A BaseException on purpose: requests/urllib3
    and the broker retry layer only catch Exception, so they cannot turn the
    timeout into a retryable network error."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS planned_orders (
  client_order_id TEXT PRIMARY KEY, plan_date TEXT, symbol TEXT, asset_class TEXT, side TEXT,
  qty REAL, strategy TEXT, stop REAL, ref_price REAL, reason TEXT, signal_id TEXT,
  status TEXT, note TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS submitted (
  client_order_id TEXT PRIMARY KEY, kind TEXT, symbol TEXT, asset_class TEXT, strategy TEXT,
  side TEXT, qty REAL, stop REAL, submitted_at TEXT, status TEXT, recorded INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS position_meta (
  symbol TEXT PRIMARY KEY, highest_close REAL, initial_stop REAL, entry_date TEXT,
  signal_id TEXT, stop_order_id TEXT);
CREATE TABLE IF NOT EXISTS equity_history (
  date TEXT PRIMARY KEY, system_equity REAL, broker_equity REAL, mode TEXT, n_positions INTEGER,
  recorded_at TEXT);
CREATE TABLE IF NOT EXISTS notified (key TEXT PRIMARY KEY, at TEXT);
CREATE TABLE IF NOT EXISTS plan_attempts (key TEXT PRIMARY KEY, n INTEGER);
"""


def ensure_schema(con) -> None:
    con.executescript(SCHEMA)


# ---------------------------------------------------------------- helpers

@dataclass
class LivePos:
    """Adapter so live positions reuse Strategy.trail_stop / exit_reason."""
    stop: float
    highest_close: float
    entry_date: str

    def days_held(self, t: pd.Timestamp, index=None) -> int:
        return int(pd.bdate_range(self.entry_date, t).size - 1)


def _finite(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _today(now: datetime) -> str:
    return now.astimezone(ET).date().isoformat()


def _cid(day: str, tag: str, symbol: str, strategy: str = "") -> str:
    """Compact, deterministic client_order_id (Alpaca allows 128 chars). The
    strategy is a short stable hash, so two strategies that share a name
    prefix can never collide."""
    base = f"{day.replace('-', '')[2:]}-{tag}-{symbol.replace('/', '')}"
    if not strategy:
        return base
    return f"{base}-{hashlib.sha1(strategy.encode()).hexdigest()[:8]}"


def _notify_once(con, key: str, title: str, msg: str, priority: int, day: str) -> None:
    """At most one notification per key per day: a condition that persists is
    reported again on the next day instead of only once ever."""
    key = f"{day}:{key}"
    if con.execute("SELECT 1 FROM notified WHERE key=?", (key,)).fetchone():
        return
    notify.send(title, msg, priority)
    con.execute("INSERT INTO notified VALUES (?,?)", (key, store.now()))


def _halt(con, reason: str, day: str, source: str) -> None:
    """Halt new entries and page the owner. ``source`` records who set it;
    only a 'reconcile' halt is lifted automatically, so an existing halt from
    any other source (manual, a booking failure, unknown) keeps its source."""
    halted = store.get_setting(con, "halt_new_entries", "0").strip() == "1"
    if not halted or store.get_setting(con, HALT_SOURCE_KEY, "") == "reconcile":
        store.set_setting(con, HALT_SOURCE_KEY, source)
    store.set_setting(con, "halt_new_entries", "1")
    _notify_once(con, f"halt:{reason[:80]}", "Desk 已暫停新進場", reason, notify.CRITICAL, day)


def _lift_reconcile_halt(con, day: str) -> None:
    """A clean reconcile lifts a halt that reconcile itself set. Logged in
    param_changes like a console change, and the owner is told. The check is
    the UPDATE itself, which holds the write lock from its first read, so a
    manual pause written by another process in between is never overwritten."""
    con.commit()
    claimed = con.execute(
        "UPDATE settings SET value='' WHERE key=? AND value='reconcile' AND EXISTS "
        "(SELECT 1 FROM settings WHERE key='halt_new_entries' AND TRIM(value)='1')", (HALT_SOURCE_KEY,)).rowcount
    if claimed != 1:
        con.commit()
        return
    con.execute("UPDATE settings SET value='0' WHERE key='halt_new_entries'")  # same transaction, still locked
    why = "對帳已恢復一致,自動解除對帳造成的新進場暫停"
    con.execute("INSERT INTO param_changes (strategy, param, old, new, reason, changed_at) VALUES (?,?,?,?,?,?)",
                ("desk", "halt_new_entries", "true", "false", why, store.now()))
    con.commit()
    notify.send("Desk 已解除暫停新進場", f"{day}:{why}。", notify.WARN)


def _strategies(con, day: str | None = None) -> dict[str, Strategy]:
    """Build the enabled strategies from stored parameters. A stored parameter
    the strategy rejects (stale or hand-edited setting) must not crash every
    job: that strategy falls back to its defaults and the owner is warned once
    per day."""
    params = store.strategy_params(con)
    disabled = set(json.loads(store.get_setting(con, "disabled_strategies", "[]")))
    day = day or _today(datetime.now(timezone.utc))
    out: dict[str, Strategy] = {}
    for n, cls in REGISTRY.items():
        if n in disabled:
            continue
        try:
            out[n] = cls(**params.get(n, {}))
        except (ValueError, TypeError) as e:
            log.warning("strategy %s: stored parameters rejected (%s); using defaults", n, e)
            _notify_once(con, f"badparams:{n}", f"策略參數無效:{n}",
                         f"{n} 的已存參數 {params.get(n)} 被拒絕({e}),本次改用預設參數。", notify.WARN, day)
            out[n] = cls()
    return out


def bridge_configured() -> bool:
    import os
    return all(os.environ.get(k) for k in ("DESK_MAIL_ADDRESS", "DESK_MAIL_APP_PASSWORD", "MUSE_ADDRESS", "MUSE_SHARED_TOKEN"))


def make_broker() -> Broker:
    from stockmoney.desk.execution.alpaca import AlpacaBroker
    return AlpacaBroker()


def _last_complete(bars: data.Bars, now: datetime) -> pd.Timestamp:
    from stockmoney.desk.live import last_complete_session
    return last_complete_session(bars, now.astimezone(ET))


def _broker_call(broker: Broker, fn, what: str):
    """Run a direct client call through the broker's own retry layer when it
    has one (AlpacaBroker._retry), so a transient error is retried like every
    other broker read."""
    retry = getattr(broker, "_retry", None)
    return retry(fn, what) if callable(retry) else fn()


def _last_closed_session(broker: Broker, now: datetime) -> str:
    """ISO date of the most recent regular session that has closed by ``now``.
    Uses the broker's market calendar when it has one (Alpaca), else the
    built-in NYSE calendar (FakeBroker, or a calendar outage)."""
    et = now.astimezone(ET)
    client = getattr(broker, "client", None)
    if client is not None:
        try:
            from alpaca.trading.requests import GetCalendarRequest
            req = GetCalendarRequest(start=et.date() - timedelta(days=14), end=et.date())
            cal = _broker_call(broker, lambda: client.get_calendar(req), "get_calendar")
            local = et.replace(tzinfo=None)  # the calendar's open/close are naive ET wall-clock times
            closed = [c.date for c in cal if c.close <= local]
            if closed:
                return max(closed).isoformat()
        except Exception as e:
            log.warning("market calendar unavailable, falling back to the NYSE calendar: %s", e)
    d = et.date()
    if not (is_trading_day(d) and (et.hour, et.minute) >= SESSION_CLOSE):
        d -= timedelta(days=1)
        while not is_trading_day(d):
            d -= timedelta(days=1)
    return d.isoformat()


def _check_reconcile(con, ledger: Ledger, snap: AccountSnapshot, day: str) -> dict[str, str]:
    """Mismatched symbols (normalized) -> problem. Any mismatch halts new
    entries and pages the owner; the caller keeps managing the clean symbols."""
    bad = mismatches(ledger, snap)
    if bad:
        _halt(con, "對帳不一致:" + ";".join(list(bad.values())[:5]), day, "reconcile")
        con.commit()
    else:
        _lift_reconcile_halt(con, day)
    return bad


# ---------------------------------------------------------------- fill sync

def sync_fills(con, broker: Broker, ledger: Ledger, day: str | None = None) -> list[str]:
    """Pull status of every unrecorded order and book what has filled so far,
    partial fills included (the ledger adds only the part not booked yet). A
    row is closed (recorded=1) once its order is final. Each
    row is its own transaction: a row that cannot be booked (for example a
    SleeveConflict) halts new entries and pages the owner, and the remaining
    rows are still booked. A broker that cannot be reached (network error,
    timeout, BrokerUnavailable) ends the pass without a halt; the rows stay
    open for the next run. An exit that ended unfilled while shares are still
    held is re-planned (see _reopen_exit)."""
    day = day or _today(datetime.now(timezone.utc))
    events = []
    con.commit()
    rows = con.execute("SELECT * FROM submitted WHERE recorded=0 ORDER BY submitted_at, client_order_id").fetchall()
    for r in rows:
        try:
            ev = _sync_one(con, broker, ledger, r, day)
            con.commit()
            if ev:
                events.append(ev)
        except Exception as e:
            con.rollback()
            if _unreachable(e):
                # the broker could not be read: nothing was booked, the rows stay open and
                # the next run retries them; a sticky halt here would outlive a Wi-Fi blip
                log.warning("fill sync stopped at %s, broker unreachable: %s", r["client_order_id"], e)
                events.append(f"成交同步中斷(券商連線失敗),下次執行再試:{type(e).__name__}: {e}")
                break
            if _db_busy(e):
                # another process (the options session) held the shared database past the
                # timeout: nothing was booked, same as an unreachable broker, never a halt
                log.warning("fill sync stopped at %s, database busy: %s", r["client_order_id"], e)
                events.append(f"成交同步中斷(資料庫忙碌),下次執行再試:{type(e).__name__}: {e}")
                break
            log.exception("could not book order %s", r["client_order_id"])
            why = f"成交入帳失敗 {r['symbol']}({r['client_order_id']}):{type(e).__name__}: {e}"
            _halt(con, why, day, "sync")
            con.commit()
            events.append(why)
    return events


def _db_busy(e: BaseException) -> bool:
    """SQLite could not get its lock in time (another process holds it)."""
    msg = str(e).lower()
    return isinstance(e, sqlite3.OperationalError) and ("locked" in msg or "busy" in msg)


def _unreachable(e: BaseException) -> bool:
    """A network or broker-availability failure (not a booking error)."""
    if isinstance(e, (TimeoutError, ConnectionError)):
        return True
    try:
        from requests import RequestException
        from stockmoney.desk.execution.alpaca import BrokerUnavailable
    except ImportError:
        return False
    return isinstance(e, (RequestException, BrokerUnavailable))


def _sync_one(con, broker: Broker, ledger: Ledger, r, day: str) -> str | None:
    o = broker.get_order(r["client_order_id"])
    if o is None:
        if r["status"] == PENDING_SUBMIT and _today(datetime.fromisoformat(r["submitted_at"])) < day:
            # written ahead of a submit the broker never received (the call failed or the job died
            # before it); a day later the broker would show it if it existed
            con.execute("UPDATE submitted SET status=?, recorded=1 WHERE client_order_id=?",
                        (NOT_SENT, r["client_order_id"]))
            return f"未送達券商 {r['symbol']} {r['side']}({r['client_order_id']})"
        return None
    con.execute("UPDATE submitted SET status=? WHERE client_order_id=?", (o.status.value, r["client_order_id"]))
    final = o.status in _TERMINAL
    ev = None
    if o.filled_qty and o.filled_qty > 0:
        side = Side(r["side"])
        booked = ledger.apply_fill(
            client_order_id=r["client_order_id"], symbol=r["symbol"], strategy=r["strategy"],
            side=side, qty=float(o.filled_qty), price=float(o.filled_avg_price),
            filled_at=o.filled_at.isoformat() if o.filled_at else None,
            stop=r["stop"] if side == Side.BUY else None, cumulative=True,
        )
        if booked:
            verb = "買進" if side == Side.BUY else ("停損賣出" if r["kind"] == "stop" else "賣出")
            ev = f"{verb} {r['symbol']} {o.filled_qty:g} @ {o.filled_avg_price:.2f}({r['strategy']})"
            if not final:
                ev += f"(部分成交,累計 {o.filled_qty:g}/{o.qty or 0:g},委託仍在)"
        # From the ledger state, not from ``booked``: apply_fill commits on its own, so a
        # failure right after it (e.g. a locked database) left the fill booked without its
        # position_meta; the retry (booked=False) restores or removes the row here.
        held = ledger.position(r["symbol"]) is not None
        if side == Side.BUY and held:
            con.execute(
                "INSERT INTO position_meta (symbol, highest_close, initial_stop, entry_date) VALUES (?,?,?,?) "
                "ON CONFLICT(symbol) DO NOTHING",
                (r["symbol"], float(o.filled_avg_price), r["stop"],
                 _today(o.filled_at or datetime.now(timezone.utc))))
        elif side == Side.SELL and not held:
            con.execute("DELETE FROM position_meta WHERE symbol=?", (r["symbol"],))
    elif final and (o.status != OrderStatus.CANCELED or r["kind"] != "stop"):
        ev = f"未成交 {r['symbol']} {r['side']}:{o.status.value}"
    if final and r["kind"] == "exit" and o.status != OrderStatus.FILLED and ledger.position(r["symbol"]) is not None:
        ev = ";".join(x for x in (ev, _reopen_exit(con, r, o, day)) if x)
    if final:
        con.execute("UPDATE submitted SET recorded=1 WHERE client_order_id=?", (r["client_order_id"],))
    return ev


def _reopen_exit(con, r, o: BrokerOrder, day: str) -> str | None:
    """An exit order ended (expired, cancelled, rejected) without selling every
    share while the position is still held. Its stop was cancelled before the
    sell, so the plan goes back to ``planned``: the stop pass restores the
    stop and the next exit run sends a new order (see _exit_cid). After
    MAX_EXIT_ATTEMPTS in one day the plan becomes ``error`` and the owner is
    paged."""
    cid = r["client_order_id"]
    p = con.execute("SELECT * FROM planned_orders WHERE side='sell' AND status='submitted' "
                    "AND (client_order_id=? OR ? LIKE client_order_id || '-r%')", (cid, cid)).fetchone()
    if p is None:
        return None
    sym, key = r["symbol"], f"{day}:{p['client_order_id']}"
    con.execute("INSERT INTO plan_attempts VALUES (?, 1) ON CONFLICT(key) DO UPDATE SET n=n+1", (key,))
    n = con.execute("SELECT n FROM plan_attempts WHERE key=?", (key,)).fetchone()[0]
    why = f"出場單 {cid} 結束為 {o.status.value},仍有持倉"
    if n >= MAX_EXIT_ATTEMPTS:
        _mark(con, p, "error", f"{why};出場連續失敗 {n} 次,待人工處理")
        notify.send(f"Desk 出場失敗 {sym}", f"{why}。今天第 {n} 次,不再自動重試,請人工處理。", notify.CRITICAL)
        return f"出場失敗 {sym}(第 {n} 次,已停止重試):{why}"
    _mark(con, p, "planned", f"{why};第 {n}/{MAX_EXIT_ATTEMPTS} 次,先補回停損單,下次執行再出場")
    notify.send(f"Desk 出場未成交 {sym}", f"{why}。先補回停損單,下次執行重送出場單({n}/{MAX_EXIT_ATTEMPTS})。",
                notify.WARN)
    return f"{sym} 出場未成交,保留計畫待重送(第 {n} 次)"


_UNFILLED = (OrderStatus.CANCELED.value, OrderStatus.EXPIRED.value, OrderStatus.REJECTED.value)


def _exit_cid(con, cid: str) -> str:
    """Order id for the next attempt of exit plan ``cid``: the plan id itself,
    then ``<cid>-r1``, ``-r2``... past every attempt that ended unfilled (the
    broker never accepts one client_order_id twice)."""
    k, out = 0, cid
    while (row := con.execute("SELECT status, recorded FROM submitted WHERE client_order_id=?", (out,)).fetchone()) \
            and row["recorded"] == 1 and row["status"] in _UNFILLED:
        k += 1
        out = f"{cid}-r{k}"
    return out


def _submit(con, broker: Broker, intent: OrderIntent, kind: str, stop: float | None = None, *,
            plan=None, note: str = "", stop_for: str | None = None, day: str = ""):
    """Send one order with write-ahead bookkeeping: the ``submitted`` row (and
    the stop id) is committed as ``pending_submit`` BEFORE the broker call, so a
    crash between the call and the commit cannot orphan an order the broker
    holds; sync_fills resolves the row by client_order_id. A retry with the
    same id reuses the row (the broker side is idempotent on the id).
    ``plan``: the planned_orders row to mark submitted. ``stop_for``: the
    symbol whose position_meta.stop_order_id this order becomes."""
    cid = intent.client_order_id
    con.execute(
        "INSERT INTO submitted (client_order_id, kind, symbol, asset_class, strategy, side, qty, stop, submitted_at, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(client_order_id) DO UPDATE SET qty=excluded.qty, stop=excluded.stop, "
        "submitted_at=excluded.submitted_at, status=excluded.status, recorded=0 "
        "WHERE submitted.status IN (?, ?)",
        (cid, kind, intent.symbol, intent.asset_class.value, intent.strategy,
         intent.side.value, intent.qty, stop, store.now(), PENDING_SUBMIT, PENDING_SUBMIT, NOT_SENT))
    if stop_for is not None:
        _set_stop_id(con, stop_for, cid, stop, day)
    con.commit()
    o = broker.submit(intent)
    con.execute("UPDATE submitted SET status=? WHERE client_order_id=? AND recorded=0", (o.status.value, cid))
    if plan is not None:
        _mark(con, plan, "submitted", note)
    con.commit()
    return o


def _set_stop_id(con, sym: str, cid: str | None, stop: float | None = None, day: str = "") -> None:
    if cid is None:  # clearing never creates a position_meta row
        con.execute("UPDATE position_meta SET stop_order_id=NULL WHERE symbol=?", (sym,))
        return
    con.execute(
        "INSERT INTO position_meta (symbol, highest_close, initial_stop, entry_date, stop_order_id) "
        "VALUES (?, NULL, ?, ?, ?) ON CONFLICT(symbol) DO UPDATE SET stop_order_id=excluded.stop_order_id",
        (sym, stop, day or None, cid))


def _filled_any(o: BrokerOrder | None) -> bool:
    return o is not None and (o.status == OrderStatus.FILLED or (o.filled_qty or 0) > 0)


def ensure_stops(con, broker: Broker, ledger: Ledger, risk: RiskEngine, snap: AccountSnapshot, day: str,
                 skip: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Every equity position must have exactly one live GTC stop at the ledger
    stop price. A stop whose price changed is replaced only after the broker
    confirms the old one is cancelled (a pending cancel still reserves the
    shares); an untracked stop is adopted or cancelled; a missing one is
    re-created. A recorded stop that is gone from the open orders is looked up
    first: if it filled, the fill is booked and nothing new is placed for the
    shares it sold. New stops are sized from a fresh broker position read made
    just before the submit, never from ``snap`` (kept for the call signature).
    ``skip``: normalized symbols that failed reconciliation or are being
    exited, left alone here. One symbol's failure is reported and the rest
    continue."""
    broker = stock_only(broker)
    events = []
    open_orders = broker.open_orders()
    for sym, pos in ledger.positions().items():
        if "/" in sym or norm(sym) in skip:
            continue  # crypto stops are managed by the crypto job (Phase 1)
        try:
            ev = _ensure_stop(con, broker, risk, day, sym, pos, open_orders)
        except Exception as e:
            con.commit()  # keep the record of whatever the broker already did
            log.exception("stop management failed for %s", sym)
            ev = f"停損單處理失敗 {sym}:{type(e).__name__}: {e}"
        if ev:
            events.append(ev)
    return events


def _stop_filled(con, broker: Broker, risk: RiskEngine, day: str, sym: str, depth: int) -> str:
    """A stop of ``sym`` filled (fully or partly): book it now, forget the stop
    id, and re-evaluate the position that is left (if any) once."""
    events = sync_fills(con, broker, risk.ledger, day)
    # UPDATE, not _set_stop_id: a closed position's meta row was deleted by the sync and must stay gone
    con.execute("UPDATE position_meta SET stop_order_id=NULL WHERE symbol=?", (sym,))
    con.commit()
    msg = f"{sym} 停損單已成交,已同步入帳" + ("(" + ";".join(events) + ")" if events else "")
    left = risk.ledger.position(sym)
    if left is None or depth > 0:
        return msg
    ev = _ensure_stop(con, broker, risk, day, left.symbol, left, broker.open_orders(), depth=1)
    return msg + (f";{ev}" if ev else "")


def _broker_qty(broker: Broker, sym: str) -> float:
    live = broker.account()
    return next((bp.qty for bp in live.positions if norm(bp.symbol) == norm(sym)), 0.0)


def _ensure_stop(con, broker: Broker, risk: RiskEngine, day: str, sym: str, pos,
                 open_orders: list[BrokerOrder], depth: int = 0) -> str | None:
    if not (_finite(pos.stop) and pos.stop > 0):
        return None
    qty = floor(pos.qty + 1e-9)
    if qty <= 0:
        return None
    want = round(float(pos.stop), 2)

    def matches(o: BrokerOrder) -> bool:
        return abs((o.stop_price or 0) - want) < 0.005 and abs((o.qty or 0) - qty) < 1e-9

    meta = con.execute("SELECT stop_order_id FROM position_meta WHERE symbol=?", (sym,)).fetchone()
    cur_id = meta["stop_order_id"] if meta else None
    cur = next((o for o in open_orders if cur_id and o.client_order_id == cur_id), None)
    if cur_id and cur is None:
        # not in the open orders: it may have filled since the fills were synced
        o = broker.get_order(cur_id)
        if o is not None and o.status not in _TERMINAL:
            cur = o  # still working (accepted after the open-orders read)
        elif _filled_any(o):
            return _stop_filled(con, broker, risk, day, sym, depth)
    if cur and cur.status != OrderStatus.PENDING_CANCEL and matches(cur):
        return None  # live (new, accepted, pending_new, partially filled) at the right price and size
    strays = [o for o in open_orders
              if norm(o.symbol) == norm(sym) and o.side == Side.SELL and o.client_order_id != cur_id
              and o.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)]
    if cur is None:
        for o in strays:
            if o.status != OrderStatus.PENDING_CANCEL and o.order_type == OrderType.STOP and matches(o):
                # a stop the database lost track of (e.g. a crash right after submit): adopt it
                con.execute(
                    "INSERT OR IGNORE INTO submitted (client_order_id, kind, symbol, asset_class, strategy, side, "
                    "qty, stop, submitted_at, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (o.client_order_id, "stop", sym, AssetClass.EQUITY.value, pos.strategy, Side.SELL.value,
                     o.qty, want, store.now(), o.status.value))
                _set_stop_id(con, sym, o.client_order_id, want, day)
                con.commit()
                return f"沿用既有停損單 {sym} @ {want:.2f}"
    for old in ([cur] if cur else []) + strays:
        if not _cancel_and_wait(broker, old.client_order_id):
            return f"{sym} 舊停損單取消未確認,下次再換"
        if _filled_any(broker.get_order(old.client_order_id)):
            return _stop_filled(con, broker, risk, day, sym, depth)
    if cur_id:
        _set_stop_id(con, sym, None)
        con.commit()
    # size from what the broker holds right now: a stop that fired a moment ago
    # must not be followed by a new stop for shares that are already gone
    held = _broker_qty(broker, sym)
    qty = min(qty, floor(held + 1e-9))
    if qty <= 0:
        return f"{sym} 券商已無持倉,不掛停損單"
    n = con.execute("SELECT COUNT(*) FROM submitted WHERE symbol=? AND kind='stop'", (sym,)).fetchone()[0]
    intent = OrderIntent(
        _cid(day, f"s{n}", sym), sym, AssetClass.EQUITY, Side.SELL, qty=qty,
        order_type=OrderType.STOP, tif=TIF.GTC, stop_price=want,
        strategy=pos.strategy, reason="protective stop")
    d = risk.check(intent, broker.account(), ref_price=want, orders_this_run=0)
    if not d.allow:
        return f"停損單被風控擋下 {sym}:{d.reason}"
    _submit(con, broker, d.intent, "stop", want, stop_for=sym, day=day)
    return f"停損單 {sym} @ {want:.2f}"


def sweep_orphan_stops(con, broker: Broker, ledger: Ledger, day: str) -> list[str]:
    """Cancel (and wait for) every open SELL stop at the broker for a symbol
    the ledger does not hold. Such a stop protects nothing, and on a margin
    account it could open a short when it triggers. A symbol the broker still
    holds is a reconcile mismatch and is left for a human. One order's
    failure is reported and the rest are still swept."""
    broker = stock_only(broker)
    events: list[str] = []
    try:
        held = {norm(s) for s in ledger.positions()}
        orphans = [o for o in broker.open_orders()
                   if o.side == Side.SELL and o.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)
                   and norm(o.symbol) not in held]
        at_broker = {norm(p.symbol) for p in broker.account().positions if abs(p.qty) > 0} if orphans else set()
    except Exception as e:
        con.commit()
        log.exception("orphan stop sweep failed")
        orphans = []
        events.append(f"孤兒停損單清理失敗:{type(e).__name__}: {e}")
    for o in orphans:
        try:
            if norm(o.symbol) in at_broker:
                events.append(f"{o.symbol} 帳本無持倉但券商有,停損單 {o.client_order_id} 留待人工")
                continue
            if not _cancel_and_wait(broker, o.client_order_id):
                events.append(f"孤兒停損單 {o.symbol}({o.client_order_id})取消未確認,下次再試")
                continue
            con.execute("UPDATE position_meta SET stop_order_id=NULL WHERE stop_order_id=?", (o.client_order_id,))
            con.commit()
            if _filled_any(broker.get_order(o.client_order_id)):
                events.append(f"孤兒停損單 {o.symbol}({o.client_order_id})在取消前成交,請人工確認")
            else:
                events.append(f"已取消孤兒停損單 {o.symbol}({o.client_order_id})")
        except Exception as e:
            con.commit()
            log.exception("orphan stop sweep failed for %s", o.client_order_id)
            events.append(f"孤兒停損單清理失敗 {o.symbol}({o.client_order_id}):{type(e).__name__}: {e}")
    if events:
        _notify_once(con, "orphanstops:" + ";".join(events)[:80], "Desk 孤兒停損單", "\n".join(events),
                     notify.WARN, day)
        con.commit()
    return events


# ---------------------------------------------------------------- jobs

def postclose(con, broker: Broker, now: datetime, bars: data.Bars | None = None, limits: Limits | None = None) -> str:
    ensure_schema(con)
    broker = stock_only(broker)
    today = _today(now)
    limits = limits or _PROFILE.limits()
    risk = RiskEngine(limits, con)
    ledger = risk.ledger
    events = sync_fills(con, broker, ledger, today)
    events += _expire_unretried_opg(con, broker, now)
    snap = broker.account()
    bad = _check_reconcile(con, ledger, snap, today)
    head = ("對帳不一致,已暫停新進場(出場與停損照常):\n" + "\n".join(bad.values()) + "\n\n") if bad else ""

    if bars is None:
        bars = data.to_bars(data.refresh())
    through = _last_complete(bars, now)
    # The session being managed is the last completed one, not the calendar
    # date: a run after midnight still manages yesterday's session.
    day = str(through.date())
    expected = _last_closed_session(broker, now)
    if day != expected:
        events += ensure_stops(con, broker, ledger, risk, snap, today, skip=set(bad))
        events += sweep_orphan_stops(con, broker, ledger, today)
        return head + f"{today} 非交易日或資料未更新(最新 {day},應為 {expected}),跳過規劃。" + (
            "\n" + "\n".join(events) if events else "")
    bars = bars.upto(through)
    ind = compute(bars)
    ctx = engine.day_context(ind, through)
    strats = _strategies(con, day)
    close = ind.close.loc[through]

    # manage open stock positions: trail stops, rule exits (reconciled symbols only)
    exits = []
    for sym, pos in ledger.positions().items():
        if "/" in sym or norm(sym) in bad:
            continue
        if sym not in close or pd.isna(close[sym]):
            # no bar for the managed session: the trailing stop and the exit rules cannot run,
            # but the existing stop is still maintained by ensure_stops below
            why = f"{sym} 缺少 {day} 的日線資料,今天不更新移動停損與出場判斷(既有停損單照常維護)"
            log.warning("held symbol %s has no bar for %s", sym, day)
            events.append(why)
            _notify_once(con, f"nobar:{sym}", f"Desk 持倉缺資料 {sym}", why, notify.WARN, day)
            con.commit()
            continue
        try:
            meta = con.execute("SELECT * FROM position_meta WHERE symbol=?", (sym,)).fetchone()
            prev_hc = meta["highest_close"] if meta and _finite(meta["highest_close"]) else pos.avg_price
            hc = max(float(prev_hc), float(close[sym]))
            con.execute("UPDATE position_meta SET highest_close=? WHERE symbol=?", (hc, sym))
            strat = strats.get(pos.strategy) or REGISTRY[pos.strategy]()
            lp = LivePos(pos.stop or 0.0, hc, meta["entry_date"] if meta and meta["entry_date"] else day)
            new_stop = strat.trail_stop(ctx, sym, lp)
            if not _finite(new_stop):
                log.warning("trail stop for %s is not finite (%s); keeping %s", sym, new_stop, pos.stop)
            elif new_stop > (pos.stop or 0) + 0.005:
                ledger.set_stop(sym, round(new_stop, 2))
            reason = strat.exit_reason(ctx, sym, lp)
            if reason:
                exits.append(sym)
                # an exit still waiting from an earlier session (premarket missed or retried) is kept;
                # planning a second one would make two sells compete for the same shares
                if not con.execute("SELECT 1 FROM planned_orders WHERE symbol=? AND side='sell' AND status='planned'",
                                   (sym,)).fetchone():
                    _plan(con, day, sym, Side.SELL, pos.qty, pos.strategy, None, float(close[sym]), reason, "")
        except Exception as e:
            log.exception("position management failed for %s", sym)
            events.append(f"持倉管理失敗 {sym}:{type(e).__name__}: {e}")
        con.commit()
    events += ensure_stops(con, broker, ledger, risk, snap, day, skip=set(bad))
    events += sweep_orphan_stops(con, broker, ledger, day)
    con.commit()

    # new entries for the next open; a failure here must not undo the stop bookkeeping above
    planned: list[dict] = []
    if bad:
        events.append("對帳不一致,今天不規劃新進場")
    else:
        try:
            signals = engine.scan(strats, ctx)
            for s in signals:
                store.save_signal(con, engine.signal_id(s), s)
            planned = _plan_entries(con, day, ledger, risk, snap, ctx, signals, set(exits))
            con.commit()
        except Exception as e:
            con.rollback()
            planned = []
            log.exception("planning failed")
            why = f"規劃新進場失敗:{type(e).__name__}: {e}"
            events.append(why)
            notify.send(f"Desk 規劃失敗 {day}", why[:1000], notify.CRITICAL)

    mode, why = risk.mode(snap)
    sys_eq = risk.system_equity(snap)
    con.execute(
        "INSERT INTO equity_history VALUES (?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
        "system_equity=excluded.system_equity, broker_equity=excluded.broker_equity, mode=excluded.mode, "
        "n_positions=excluded.n_positions, recorded_at=excluded.recorded_at",
        (day, sys_eq, snap.equity, mode, len(ledger.positions()), store.now()))
    store.set_setting(con, PLAN_THROUGH_KEY, day)

    report = head + _report(day, ctx, sys_eq, limits.capital_budget, mode, why, ledger, events, planned, exits)
    report += _reconcile_summary(con, broker, day)
    con.execute(
        "INSERT INTO journal (date, auto_summary, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET auto_summary=excluded.auto_summary, updated_at=excluded.updated_at",
        (day, report, store.now()))
    con.commit()
    _bridge_outbound(con, ledger, planned, report, now)
    notify.send(f"Desk 盤後 {day}", report[:3500], notify.INFO)
    return report


def _reconcile_summary(con, broker: Broker, day: str) -> str:
    """Daily broker-vs-plan comparison for the report (execution.reconcile.daily). Read-only for
    trading and isolated: any failure here costs the section, never the run."""
    con.commit()  # what postclose wrote so far must survive a rollback below
    try:
        try:
            open_orders = broker.open_orders()
        except Exception:
            log.exception("reconcile summary: open orders unavailable, stop check skipped")
            open_orders = None
        text, alert = reconcile_daily(con, day, open_orders)
        if alert:
            _notify_once(con, "reconcile_daily_diff", "Desk 券商對帳連續有差異", alert[:900], notify.WARN, day)
        con.commit()
        return "\n\n" + text
    except Exception as e:
        con.rollback()
        log.exception("reconcile summary failed")
        return f"\n\n(券商對帳摘要失敗:{type(e).__name__}: {e})"


def _plan(con, day, sym, side: Side, qty, strategy, stop, ref, reason, signal_id) -> str:
    cid = _cid(day, "b" if side == Side.BUY else "x", sym, strategy)
    con.execute(
        "INSERT OR IGNORE INTO planned_orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, day, sym, (AssetClass.CRYPTO if "/" in sym else AssetClass.EQUITY).value, side.value,
         qty, strategy, stop, ref, reason, signal_id, "planned", "", store.now()))
    return cid


def _plan_entries(con, day, ledger: Ledger, risk: RiskEngine, snap, ctx, signals, exiting: set[str]) -> list[dict]:
    """Size the next session's buys with the same rules as the backtest
    Account: risk per trade (or target weight), capped at MAX_POSITION_PCT of
    equity, the market exposure cap and available system cash. Signals are
    ranked by engine.rank_signals, and one symbol is planned at most once,
    whichever strategies signal it.

    Buys already planned for ``day`` (a rerun of the same postclose) are kept
    even when they are no longer signalled, and they use up slots, sector
    room, exposure and cash first, so a rerun never plans beyond the caps.
    Buys for ``day`` already submitted whose order is not booked yet (a rerun
    after premarket) use up the caps the same way."""
    positions = ledger.positions()
    held = {s for s in positions if "/" not in s} - exiting
    existing = [r for r in con.execute(
        "SELECT * FROM planned_orders p WHERE plan_date=? AND side='buy' AND (status='planned' OR "
        "(status='submitted' AND EXISTS (SELECT 1 FROM submitted s WHERE s.client_order_id=p.client_order_id "
        "AND s.recorded=0))) ORDER BY created_at, client_order_id", (day,)).fetchall() if r["symbol"] not in held]
    out = [{"symbol": r["symbol"], "strategy": r["strategy"], "qty": r["qty"], "ref": r["ref_price"],
            "stop": r["stop"]} for r in existing if r["status"] == "planned"]
    if ctx.market.exposure_cap <= 0 or not signals:
        return out
    equity = risk.system_equity(snap)
    if not (_finite(equity) and equity > 0):
        log.warning("system equity %s is not usable; no entries planned", equity)
        return out
    prices = {p.symbol: p.current_price for p in snap.positions}

    def value(s: str) -> float:
        px = prices.get(s)
        return positions[s].qty * (px if _finite(px) else positions[s].avg_price)

    invested = sum(value(s) for s in held)
    cash = equity - sum(value(s) for s in positions)
    slots = MAX_POSITIONS - len(held)
    per_sector: dict[str | None, int] = {}
    for s in held:
        per_sector[sector_of(s)] = per_sector.get(sector_of(s), 0) + 1
    planned_syms: set[str] = set()
    for r in existing:
        cost = float(r["qty"] or 0) * float(r["ref_price"] or 0)
        invested += cost
        cash -= cost
        slots -= 1
        per_sector[sector_of(r["symbol"])] = per_sector.get(sector_of(r["symbol"]), 0) + 1
        planned_syms.add(r["symbol"])
    for s in engine.rank_signals(signals):
        if slots <= 0:
            break
        if s.symbol in held or s.symbol in planned_syms or ledger.position(s.symbol) is not None:
            continue
        if not (_finite(s.ref_price) and s.ref_price > 0 and _finite(s.stop) and s.stop > 0):
            log.warning("skip %s (%s): ref %s / stop %s not usable", s.symbol, s.strategy, s.ref_price, s.stop)
            continue
        cid = _cid(day, "b", s.symbol, s.strategy)
        if con.execute("SELECT 1 FROM planned_orders WHERE plan_date=? AND symbol=? AND side='buy' "
                       "AND client_order_id<>? AND status IN ('planned','submitted')",
                       (day, s.symbol, cid)).fetchone():
            continue  # already planned for this session under another strategy or id
        sec = sector_of(s.symbol)
        if per_sector.get(sec, 0) >= MAX_PER_SECTOR:
            continue
        if s.target_weight is not None:
            dollars = equity * s.target_weight * ctx.market.exposure_cap
        else:
            risk_ps = s.ref_price - s.stop
            if risk_ps <= 0:
                continue
            dollars = equity * RISK_PER_TRADE / risk_ps * s.ref_price
        dollars = min(dollars, equity * MAX_POSITION_PCT, equity * ctx.market.exposure_cap - invested, cash)
        if not (_finite(dollars) and dollars > 0):
            continue
        qty = floor(dollars / s.ref_price)
        if qty <= 0:
            continue
        invested += qty * s.ref_price
        cash -= qty * s.ref_price
        slots -= 1
        per_sector[sec] = per_sector.get(sec, 0) + 1
        planned_syms.add(s.symbol)
        _plan(con, day, s.symbol, Side.BUY, qty, s.strategy, round(s.stop, 2), s.ref_price, s.thesis, engine.signal_id(s))
        out.append({"symbol": s.symbol, "strategy": s.strategy, "qty": qty, "ref": s.ref_price, "stop": s.stop})
    return out


@dataclass
class _Run:
    """Mutable state of one premarket (or postopen exit) run."""
    n: int = 0
    lines: list[str] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    exiting: set[str] = field(default_factory=set)  # normalized symbols whose exit was sent this run
    job: str = "premarket"
    late: bool = False  # once a plain market order had to be used, the late path stays on
    t0: float = field(default_factory=lambda: _monotonic())  # job clock origin (``et`` was read here)

    def clock(self, et: datetime) -> datetime:
        """``et`` advanced by the time this run has taken so far."""
        return et + timedelta(seconds=max(_monotonic() - self.t0, 0.0))

    def tif(self, et: datetime) -> TIF:
        """Market-on-open is only accepted until ~09:28 ET; re-read the clock
        before every order, a run that started in time may cross the cutoff."""
        c = self.clock(et)
        if (c.hour, c.minute) >= OPG_CUTOFF:
            self.late = True
        return TIF.DAY if self.late else TIF.OPG


def premarket(con, broker: Broker, now: datetime, limits: Limits | None = None, mailbox=None) -> str:
    """Exits first, then a protective-stop pass over every held symbol that is
    not being exited this morning, then entries. Exits never expire: a planned
    sell runs whenever its position is still held. A failed exit re-places its
    stop at once and stays planned for a rerun (MAX_EXIT_ATTEMPTS per day)."""
    ensure_schema(con)
    broker = stock_only(broker)
    st = _Run()
    today = _today(now)
    if not broker.is_equity_market_open() and not _opens_today(broker, now):
        return f"{today} 不開盤,跳過。"
    risk = RiskEngine(limits or _PROFILE.limits(), con)
    ledger = risk.ledger
    sync_fills(con, broker, ledger, today)
    snap = broker.account()
    bad = _check_reconcile(con, ledger, snap, today)

    flags = _bridge_flags(con, ledger, now, mailbox)
    plans = con.execute(
        "SELECT * FROM planned_orders WHERE status='planned' AND plan_date < ? ORDER BY side DESC, client_order_id",
        (today,)).fetchall()
    through = store.get_setting(con, PLAN_THROUGH_KEY, "")
    last_session = _last_closed_session(broker, now)
    et = now.astimezone(ET)
    # A late run (Mac was asleep, or the run waited for the lock) falls back to
    # a plain market order, and gives up on new entries after 10:30 so we never
    # chase a stale plan deep into the session. Both are re-checked per order.
    st.tif(et)
    if bad:
        st.lines.append("對帳不一致,只允許出場:" + ";".join(bad.values()))
    kw = dict(bad=bad, flags=flags, through=through, last_session=last_session, et=et, today=today)
    _send_exits(con, broker, risk, ledger, snap, plans, st, kw)
    # Every held symbol keeps a stop unless it is being sold this morning: a stop
    # replacement that postclose could not finish (unconfirmed cancel) or a stop
    # lost to a failure is restored here, before any new money goes in.
    st.lines += _premarket_stops(con, broker, risk, today, bad, st)
    for p in plans:
        if p["side"] != Side.BUY.value:
            continue
        try:
            _premarket_one(con, broker, risk, ledger, snap, p, st, **kw)
        except Exception as e:
            log.exception("premarket plan %s failed", p["client_order_id"])
            why = f"{type(e).__name__}: {e}"
            _mark(con, p, "error", why)
            con.commit()
            st.lines.append(f"送單失敗 {p['symbol']}:{why}")
            notify.send(f"Desk 送單失敗 {p['symbol']}", why[:1000], notify.CRITICAL)
    con.commit()
    if st.late and st.sent:
        st.lines += _protect_late_fills(con, broker, ledger, risk, st.sent, today)
    msg = f"{today} 盤前送單 {st.n} 筆\n" + "\n".join(st.lines)
    for note in flags.notify:
        _notify_once(con, f"muse:{note[:100]}", "Muse 警報", note, notify.WARN, today)
    if st.n:
        notify.send(f"Desk 盤前 {today}", msg, notify.INFO)
    return msg


def _send_exits(con, broker: Broker, risk: RiskEngine, ledger: Ledger, snap: AccountSnapshot, plans, st: _Run,
                kw: dict) -> None:
    """Run every planned sell in ``plans``; a failing one restores its stop
    and stays planned (see _exit_failed)."""
    for p in plans:
        if p["side"] != Side.SELL.value:
            continue
        try:
            _premarket_one(con, broker, risk, ledger, snap, p, st, **kw)
        except Exception as e:
            log.exception("%s exit %s failed", st.job, p["client_order_id"])
            _exit_failed(con, broker, risk, p, st, f"{type(e).__name__}: {e}", kw["today"])
    con.commit()


def _exit_deferred(con, p, st: _Run, why: str, today: str) -> None:
    """An exit could not be sent now and stays planned: say so once per day
    and job, so the owner knows the position is still held."""
    sym, cid = p["symbol"], p["client_order_id"]
    _mark(con, p, "planned", why)
    st.lines.append(f"{sym} 出場延後:{why}")
    _notify_once(con, f"exitdefer:{st.job}:{cid}", f"Desk 出場延後 {sym}",
                 f"{sym} 的出場計畫 {cid} 本次({st.job})未送出:{why}", notify.WARN, today)
    con.commit()


def _premarket_stops(con, broker: Broker, risk: RiskEngine, today: str, bad: dict[str, str], st: _Run) -> list[str]:
    """Stop pass over every held symbol except mismatched ones and ones with
    an exit sent or still working at the broker."""
    try:
        exiting = set(st.exiting) | {
            norm(o.symbol) for o in broker.open_orders()
            if o.side == Side.SELL and o.order_type not in (OrderType.STOP, OrderType.STOP_LIMIT)}
        return ensure_stops(con, broker, risk.ledger, risk, broker.account(), today, skip=set(bad) | exiting)
    except Exception as e:
        con.commit()
        log.exception("%s stop pass failed", st.job)
        why = f"{type(e).__name__}: {e}"
        notify.send(f"Desk 停損單檢查失敗({st.job})", why[:1000], notify.CRITICAL)
        return [f"盤前停損單檢查失敗:{why}"]


def _exit_failed(con, broker: Broker, risk: RiskEngine, p, st: _Run, why: str, today: str) -> None:
    """An exit raised somewhere after (or while) its stop was cancelled. Put
    the protective stop back from a fresh broker read right away, and leave
    the plan ``planned`` so a rerun the same morning retries it; after
    MAX_EXIT_ATTEMPTS failures in one day it becomes ``error`` and the owner
    is paged."""
    sym, cid = p["symbol"], p["client_order_id"]
    key = f"{today}:{cid}"
    con.execute("INSERT INTO plan_attempts VALUES (?, 1) ON CONFLICT(key) DO UPDATE SET n=n+1", (key,))
    n = con.execute("SELECT n FROM plan_attempts WHERE key=?", (key,)).fetchone()[0]
    con.commit()
    restored = _restore_stop(con, broker, risk, sym, _exit_cid(con, cid), today)
    if n >= MAX_EXIT_ATTEMPTS:
        _mark(con, p, "error", f"出場連續失敗 {n} 次,待人工處理:{why}")
        con.commit()
        st.lines.append(f"出場失敗 {sym}(第 {n} 次,已停止重試):{why};{restored}")
        notify.send(f"Desk 出場失敗 {sym}", f"今天第 {n} 次失敗,不再自動重試,請人工處理。\n{why}\n{restored}"[:1000],
                    notify.CRITICAL)
    else:
        _mark(con, p, "planned", f"出場失敗 {n}/{MAX_EXIT_ATTEMPTS} 次,待重跑:{why}")
        con.commit()
        st.lines.append(f"出場失敗 {sym}(第 {n} 次,保留計畫待重跑):{why};{restored}")
        notify.send(f"Desk 出場失敗 {sym}", f"第 {n}/{MAX_EXIT_ATTEMPTS} 次,重跑盤前會再試。\n{why}\n{restored}"[:1000],
                    notify.WARN)


def _restore_stop(con, broker: Broker, risk: RiskEngine, sym: str, exit_cid: str, today: str) -> str:
    """Re-place the protective stop of ``sym`` after a failed exit, unless the
    exit order did reach the broker (then it is the protection, or it filled)."""
    try:
        o = broker.get_order(exit_cid)
        if o is not None and o.status not in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            return "出場單已在券商,不補停損單"
        pos = risk.ledger.position(sym)
        if pos is None:
            return "帳本已無持倉"
        ev = _ensure_stop(con, broker, risk, today, pos.symbol, pos, broker.open_orders())
        con.commit()
        return ev or "停損單仍在"
    except Exception as e:
        con.commit()
        log.exception("could not restore the stop for %s", sym)
        why = f"停損單補掛失敗 {sym}:{type(e).__name__}: {e}"
        notify.send(f"Desk 停損單補掛失敗 {sym}", why[:1000], notify.CRITICAL)
        return why


def _premarket_one(con, broker: Broker, risk: RiskEngine, ledger: Ledger, snap: AccountSnapshot, p, st: _Run,
                   *, bad, flags, through, last_session, et, today) -> None:
    sym, side, cid = p["symbol"], Side(p["side"]), p["client_order_id"]
    if side == Side.SELL:
        cid = _exit_cid(con, cid)  # a retry after an attempt that ended unfilled gets a fresh id
    prior =con.execute("SELECT status FROM submitted WHERE client_order_id=?", (cid,)).fetchone()
    if prior and prior["status"] not in (PENDING_SUBMIT, NOT_SENT):
        # the broker already has this order (a job died after submit, before marking the plan)
        _mark(con, p, "submitted", "重跑時發現已送出")
        if side == Side.SELL:
            st.exiting.add(norm(sym))
        return
    if side == Side.BUY and (p["plan_date"] != through or through != last_session):
        # only entries from the postclose of the last completed session are valid;
        # exits never expire (a held position must still be sold)
        _mark(con, p, "expired", f"過期計畫:計畫日 {p['plan_date']},最近一次盤後規劃 {through or '無'},"
                                 f"上一個交易日 {last_session}")
        return
    if side == Side.BUY:
        c = st.clock(et)
        if (c.hour, c.minute) >= ENTRY_CUTOFF:
            _mark(con, p, "expired", f"錯過進場時間窗(執行於 {c:%H:%M} ET,超過 10:30)")
            st.lines.append(f"錯過進場 {sym}:執行太晚")
            return
        if bad:
            _mark(con, p, "blocked", "對帳不一致,暫停新進場")
            st.lines.append(f"擋下 {sym}:對帳不一致")
            return
        if sym in flags.blocked:
            _mark(con, p, "blocked", flags.blocked[sym])
            st.lines.append(f"擋下 {sym}:{flags.blocked[sym]}")
            return
        if sym in flags.legacy_blocked:  # log only: the old Muse rule would have blocked it
            from stockmoney.desk.bridge.policy import record_legacy
            record_legacy(con, cid, p["plan_date"], sym, flags.legacy_blocked[sym], et)
            st.lines.append(f"Muse 降權放行 {sym}(舊規則會擋):{flags.legacy_blocked[sym]}")
        if (why := _order_conflict(con, broker, sym, cid)):
            _mark(con, p, "blocked", why)
            st.lines.append(f"擋下 {sym}:{why}")
            return
        px = _live_price(broker, sym)
        qty = p["qty"]
        if px is not None and px > p["ref_price"]:
            qty = floor(qty * p["ref_price"] / px + 1e-9)  # never spend more than planned
        if qty <= 0:
            _mark(con, p, "blocked", f"以即時價 {px:g} 重算後股數為 0")
            return
        intent = OrderIntent(cid, sym, AssetClass(p["asset_class"]), side, qty=qty,
                             order_type=OrderType.MARKET, tif=st.tif(et), strategy=p["strategy"], reason=p["reason"])
        d = risk.check(intent, snap, ref_price=p["ref_price"], orders_this_run=st.n, market_price=px)
        if not d.allow:
            _mark(con, p, "blocked", d.reason)
            st.lines.append(f"風控擋下 {sym}:{d.reason}")
            return
        note = d.reason if px is not None else f"{d.reason}(無即時報價,只以前收盤價檢查)"
        _submit(con, broker, d.intent, "entry", p["stop"], plan=p, note=note)
        risk.reserve(d.intent, px if px is not None else p["ref_price"])
    else:
        if norm(sym) in bad:
            # exits never expire: keep it planned, it runs once the symbol reconciles again
            _exit_deferred(con, p, st, f"對帳不一致({bad[norm(sym)]}),出場暫緩,請人工確認持股;"
                                       "對帳恢復一致後自動執行", today)
            return
        if ledger.position(sym) is None:
            _mark(con, p, "cancelled", f"已無持倉(計畫日 {p['plan_date']})")
            return
        if norm(sym) in st.exiting:
            _mark(con, p, "cancelled", "同一檔的另一張出場單已送出")
            return
        dup = con.execute("SELECT client_order_id FROM submitted WHERE symbol=? AND kind='exit' AND recorded=0 "
                          "AND client_order_id<>? AND status NOT IN (?, ?)",
                          (sym, cid, PENDING_SUBMIT, NOT_SENT)).fetchone()
        if dup:
            _mark(con, p, "cancelled", f"已有未入帳的出場單 {dup[0]}")
            st.exiting.add(norm(sym))
            return
        meta = con.execute("SELECT stop_order_id FROM position_meta WHERE symbol=?", (sym,)).fetchone()
        if meta and meta["stop_order_id"]:
            stop_id = meta["stop_order_id"]
            if not _cancel_and_wait(broker, stop_id):
                _exit_deferred(con, p, st, f"停損單 {stop_id} 取消未確認,開盤後(postopen)再試", today)
                return
            _set_stop_id(con, sym, None)
            con.commit()
            if _filled_any(broker.get_order(stop_id)):
                # the stop filled (fully or partly) before the cancel landed: book it, then re-evaluate
                st.lines += sync_fills(con, broker, ledger, today)
                if ledger.position(sym) is None:
                    _mark(con, p, "cancelled", "停損單已先成交,不再賣出")
                    st.lines.append(f"{sym} 停損已成交,取消出場單")
                    return
        held = _broker_qty(broker, sym)  # size the exit from what the broker holds now
        qty = floor(held + 1e-9)
        if qty <= 0:
            _mark(con, p, "cancelled", "券商已無持倉")
            return
        intent = OrderIntent(cid, sym, AssetClass(p["asset_class"]), side, qty=qty,
                             order_type=OrderType.MARKET, tif=st.tif(et), strategy=p["strategy"], reason=p["reason"])
        d = risk.check(intent, broker.account(), ref_price=p["ref_price"], orders_this_run=st.n)
        if not d.allow:
            _mark(con, p, "blocked", d.reason)
            st.lines.append(f"風控擋下 {sym}:{d.reason}")
            return
        _submit(con, broker, d.intent, "exit", None, plan=p, note=d.reason)
        st.exiting.add(norm(sym))
    st.n += 1
    st.sent.append(d.intent.client_order_id)
    st.lines.append(f"{'買' if side == Side.BUY else '賣'} {sym} {d.intent.qty:g} 股"
                    f"({'開盤市價' if d.intent.tif == TIF.OPG else '盤中市價'})")


def _order_conflict(con, broker: Broker, sym: str, cid: str) -> str | None:
    """A buy is refused while another order for the same symbol is live at the
    broker or submitted but not yet booked: two strategies must never both
    open one symbol."""
    r = con.execute("SELECT client_order_id FROM submitted WHERE symbol=? AND recorded=0 AND client_order_id<>?",
                    (sym, cid)).fetchone()
    if r:
        return f"已有未入帳的 {sym} 委託 {r[0]}"
    for o in broker.open_orders():
        if norm(o.symbol) == norm(sym) and o.client_order_id != cid:
            return f"券商已有 {sym} 的未完成委託 {o.client_order_id}"
    return None


def _live_price(broker: Broker, sym: str) -> float | None:
    try:
        px = broker.latest_price(sym)
    except Exception as e:
        log.warning("latest price for %s unavailable: %s", sym, e)
        return None
    return float(px) if _finite(px) and px > 0 else None


def _protect_late_fills(con, broker: Broker, ledger: Ledger, risk: RiskEngine, cids: list[str], today: str) -> list[str]:
    """A late (post-09:25) run sends plain market orders that fill right away;
    without this they would sit unprotected until the postclose job. Waits
    briefly for the orders to settle, books them and places stops. Symbols
    that do not reconcile yet are skipped here (an order may still be
    filling); postopen and postclose reconcile them properly. Fills are
    booked as they stand, partial ones included, so a buy still filling gets
    a stop for what it holds now. A symbol with an exit still working is
    left alone."""
    try:
        deadline = time.monotonic() + CANCEL_WAIT_S
        while True:
            pending = [c for c in cids if (o := broker.get_order(c)) is not None and o.status not in _TERMINAL]
            if not pending or time.monotonic() >= deadline:
                break
            _sleep(1.0)
        events = sync_fills(con, broker, ledger, today)
        snap = broker.account()
        exiting = {norm(o.symbol) for o in broker.open_orders()
                   if o.side == Side.SELL and o.order_type not in (OrderType.STOP, OrderType.STOP_LIMIT)}
        events += ensure_stops(con, broker, ledger, risk, snap, today, skip=set(mismatches(ledger, snap)) | exiting)
        return events
    except Exception as e:
        log.exception("late stop placement failed")
        notify.send("Desk 盤中進場停損單失敗", f"{type(e).__name__}: {e}", notify.CRITICAL)
        return [f"盤中進場停損單失敗:{type(e).__name__}: {e}"]


def _opg_retry_key(today: str, p) -> str:
    return f"{today}:opgretry:{p['client_order_id']}"


def _opg_unretried(con) -> list[tuple[str, object]]:
    """(submit day, plan) for entry plans whose opening order ended (expired
    or cancelled) and whose one retry attempt was not spent. The broker order
    then says whether any shares are missing."""
    rows = con.execute(
        "SELECT p.*, s.submitted_at AS s_at FROM planned_orders p JOIN submitted s "
        "ON s.client_order_id=p.client_order_id WHERE p.side='buy' AND p.status='submitted' "
        "AND s.kind='entry' AND s.recorded=1 AND s.status IN (?, ?) ORDER BY p.client_order_id",
        (OrderStatus.EXPIRED.value, OrderStatus.CANCELED.value)).fetchall()
    out = []
    for r in rows:
        day = _today(datetime.fromisoformat(r["s_at"]))
        if con.execute("SELECT 1 FROM plan_attempts WHERE key=?", (_opg_retry_key(day, r),)).fetchone() is None:
            out.append((day, r))
    return out


def _opg_retry_candidates(con, today: str) -> list:
    """Today's entry plans whose opening order ended and were not retried yet."""
    return [r for day, r in _opg_unretried(con) if day == today]


def _expire_unretried_opg(con, broker: Broker, now: datetime) -> list[str]:
    """Close the retry window for opening entries whose remainder was never
    retried (the 09:50 postopen could not reach the broker before sending, and
    no run before 10:30 ET tried again): after ENTRY_CUTOFF, or on a later day,
    the attempt is recorded as spent and the plan takes the status the cutoff
    path gives it ('expired' with nothing bought, else 'submitted' for the
    held shares). Nothing is sent. An order that still cannot be read is left
    for the next run."""
    et = now.astimezone(ET)
    today, late = _today(now), (et.hour, et.minute) >= ENTRY_CUTOFF
    lines = []
    for day, p in _opg_unretried(con):
        if day == today and not late:
            continue
        try:
            o = broker.get_order(p["client_order_id"])
        except Exception:
            log.exception("opening order %s unreadable while closing its retry window", p["client_order_id"])
            continue
        con.execute("INSERT OR IGNORE INTO plan_attempts VALUES (?, 1)", (_opg_retry_key(day, p),))
        if o is None:  # a settled 404: fills unknown, keep 'submitted' and let reconcile say what is held
            why = f"開盤單 {p['client_order_id']} 在券商查無記錄(404),超過補單時間窗({day} 10:30 ET),未補單,持股以對帳為準"
            _mark(con, p, "submitted", why)
            con.commit()
            lines.append(f"{p['symbol']} {why}")
            continue
        if o.tif != TIF.OPG or o.status not in (OrderStatus.EXPIRED, OrderStatus.CANCELED):
            con.commit()
            continue
        ordered, filled = float(o.qty or 0), float(o.filled_qty or 0)
        rest = floor(ordered - filled + 1e-9)
        if rest <= 0:
            con.commit()
            continue
        why = f"超過補單時間窗({day} 10:30 ET,執行於 {et:%m-%d %H:%M} ET),剩餘 {rest} 股作廢"
        _mark(con, p, "submitted" if filled > 0 else "expired",
              f"開盤單 {p['client_order_id']} 結束為 {o.status.value},已成交 {filled:g}/{ordered:g},未補單:{why}")
        con.commit()
        lines.append(f"{p['symbol']} 開盤單未全數成交({filled:g}/{ordered:g}),未補單:{why}")
    return lines


def _retry_opg_entries(con, broker: Broker, risk: RiskEngine, snap: AccountSnapshot, plans, st: _Run, *,
                       bad, flags, et, today) -> None:
    """An opening (OPG) entry that ended with fewer shares than ordered gets
    exactly one DAY market order for the missing shares, through the same
    risk path as premarket (RiskEngine.check with the live price, bridge
    flags as of now, reconcile state, order conflicts). Never after
    ENTRY_CUTOFF, never for the part already filled, never twice for one
    plan: the attempt is recorded in plan_attempts before anything is sent,
    and the retry id ``<cid>-rt`` is checked at the broker. The price band
    and every limit are the ones premarket uses; nothing is widened."""
    for p in plans:
        for tries_left in (1, 0):
            try:
                _retry_opg_one(con, broker, risk, snap, p, st, bad=bad, flags=flags, et=et, today=today)
                break
            except Exception as e:
                log.exception("opening retry %s failed", p["client_order_id"])
                con.rollback()
                why = f"開盤單補單失敗:{type(e).__name__}: {e}"
                if con.execute("SELECT 1 FROM submitted WHERE client_order_id=?",
                               (f"{p['client_order_id']}-rt",)).fetchone() is not None:
                    _retry_opg_failed(con, broker, p, st, today, why)
                    break
                # nothing was sent (a broker read failed first): the one attempt is
                # not spent; wait once and try again in this run, since no scheduled
                # run comes before the cutoff
                if tries_left:
                    _sleep(CANCEL_WAIT_S)
                    continue
                why += (";今天沒有其他排程會在 10:30 ET 前重試,請在 10:30 ET 前手動執行 postopen,"
                        "否則收盤後剩餘股數作廢")
                st.lines.append(f"補單暫緩 {p['symbol']}:{why}")
                notify.send(f"Desk 補單暫緩 {p['symbol']}", why[:1000], notify.CRITICAL)
    con.commit()


def _retry_opg_failed(con, broker: Broker, p, st: _Run, today: str, why: str) -> None:
    """The retry was sent (its write-ahead row exists) and failed: the one
    attempt is spent."""
    con.execute("INSERT OR IGNORE INTO plan_attempts VALUES (?, 1)", (_opg_retry_key(today, p),))
    try:
        filled = _filled_any(broker.get_order(p["client_order_id"]))
    except Exception:
        log.exception("opening order %s unreadable after a failed retry", p["client_order_id"])
        filled = True  # unknown: keep the plan 'submitted' rather than call it an error
    _mark(con, p, "submitted" if filled else "error", why)
    con.commit()
    st.lines.append(f"補單失敗 {p['symbol']}:{why}")
    notify.send(f"Desk 補單失敗 {p['symbol']}", why[:1000], notify.CRITICAL)


def _retry_opg_one(con, broker: Broker, risk: RiskEngine, snap: AccountSnapshot, p, st: _Run, *,
                   bad, flags, et, today) -> None:
    sym, cid = p["symbol"], p["client_order_id"]
    o = broker.get_order(cid)
    if o is None:  # recorded as ended short, now missing: nothing sent, retried and paged like a failed read
        raise LookupError(f"開盤單 {cid} 在券商查無記錄(404)")
    if o.tif != TIF.OPG or o.status not in (OrderStatus.EXPIRED, OrderStatus.CANCELED):
        return
    ordered, filled = float(o.qty or 0), float(o.filled_qty or 0)
    rest = floor(ordered - filled + 1e-9)  # only the missing shares, never the filled part
    if rest <= 0:
        return
    rcid = f"{cid}-rt"
    # one attempt per plan, whatever its outcome; committed with the write-ahead row below
    con.execute("INSERT OR IGNORE INTO plan_attempts VALUES (?, 1)", (_opg_retry_key(today, p),))
    prior = con.execute("SELECT status FROM submitted WHERE client_order_id=?", (rcid,)).fetchone()
    if (prior and prior["status"] not in (PENDING_SUBMIT, NOT_SENT)) or broker.get_order(rcid) is not None:
        con.commit()
        return
    head = f"開盤單 {cid} 結束為 {o.status.value},已成交 {filled:g}/{ordered:g}"

    def stop_here(status: str, why: str) -> None:
        # a partly filled plan stays 'submitted' (shares are held); a plan with
        # nothing bought takes the status premarket would have given it
        _mark(con, p, "submitted" if filled > 0 else status, f"{head},未補單:{why}")
        st.lines.append(f"{sym} 開盤單未全數成交({filled:g}/{ordered:g}),未補單:{why}")
        con.commit()

    c = st.clock(et)
    if (c.hour, c.minute) >= ENTRY_CUTOFF:
        return stop_here("expired", f"超過補單時間窗(執行於 {c:%H:%M} ET,超過 10:30),剩餘 {rest} 股作廢")
    if bad:
        return stop_here("blocked", "對帳不一致,暫停新進場")
    if sym in flags.blocked:
        return stop_here("blocked", flags.blocked[sym])
    if sym in flags.no_add:
        return stop_here("blocked", flags.no_add[sym])
    if (why := _retry_conflict(con, broker, sym, rcid)):
        return stop_here("blocked", why)
    px = _live_price(broker, sym)
    allowed = p["qty"]
    if px is not None and px > p["ref_price"]:
        allowed = floor(allowed * p["ref_price"] / px + 1e-9)  # the premarket scale-down, on the whole plan
    # the opening order was already scaled at premarket: cap what is held plus
    # the retry at today's allowed size, never rescale the remainder again
    qty = min(rest, floor(allowed - filled + 1e-9))
    if qty <= 0:
        return stop_here("blocked", f"以即時價 {px:g} 重算後股數為 0")
    intent = OrderIntent(rcid, sym, AssetClass(p["asset_class"]), Side.BUY, qty=qty,
                         order_type=OrderType.MARKET, tif=TIF.DAY, strategy=p["strategy"], reason=p["reason"])
    d = risk.check(intent, snap, ref_price=p["ref_price"], orders_this_run=st.n, market_price=px)
    if not d.allow:
        return stop_here("blocked", f"風控擋下:{d.reason}")
    note = d.reason if px is not None else f"{d.reason}(無即時報價,只以前收盤價檢查)"
    _submit(con, broker, d.intent, "entry", p["stop"], plan=p,
            note=f"{head},盤中市價補單 {rcid} {d.intent.qty:g} 股:{note}")
    risk.reserve(d.intent, px if px is not None else p["ref_price"])
    st.n += 1
    st.sent.append(rcid)
    st.lines.append(f"補買 {sym} {d.intent.qty:g} 股(盤中市價,開盤單成交 {filled:g}/{ordered:g})")


def _retry_conflict(con, broker: Broker, sym: str, rcid: str) -> str | None:
    """_order_conflict for a retry, except that the symbol's own protective
    stop (placed for the shares the opening order did buy) is not a
    competing entry."""
    r = con.execute("SELECT client_order_id FROM submitted WHERE symbol=? AND recorded=0 AND client_order_id<>? "
                    "AND kind<>'stop'", (sym, rcid)).fetchone()
    if r:
        return f"已有未入帳的 {sym} 委託 {r[0]}"
    for o in broker.open_orders():
        if norm(o.symbol) != norm(sym) or o.client_order_id == rcid:
            continue
        if o.side == Side.SELL and o.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            continue
        return f"券商已有 {sym} 的未完成委託 {o.client_order_id}"
    return None


def postopen(con, broker: Broker, now: datetime, limits: Limits | None = None, mailbox=None) -> str:
    """Sync fills, send any exit that premarket had to leave planned (an
    unconfirmed stop cancel, a mismatch since cleared, a missed premarket) as
    a DAY market sell while the market is open, give an opening entry that
    ended short one DAY market order for the missing shares (see
    _retry_opg_entries), then the stop pass and the orphan sweep."""
    ensure_schema(con)
    broker = stock_only(broker)
    st = _Run(job="postopen", late=True)
    today = _today(now)
    risk = RiskEngine(limits or _PROFILE.limits(), con)
    ledger = risk.ledger
    waiting = {r[0] for r in con.execute("SELECT client_order_id FROM planned_orders WHERE status='planned' "
                                         "AND side='sell'")}
    events = sync_fills(con, broker, ledger, today)
    snap = broker.account()
    bad = _check_reconcile(con, ledger, snap, today)
    sells = con.execute("SELECT * FROM planned_orders WHERE status='planned' AND side='sell' AND plan_date < ? "
                        "ORDER BY client_order_id", (today,)).fetchall()
    # an exit the sync just re-planned (its opening sell ended unfilled) gets its stop back
    # before anything else, and is sent by the next run instead of right away
    for r in sells:
        if r["client_order_id"] not in waiting and norm(r["symbol"]) not in bad:
            events.append(f"{r['symbol']} 出場單未成交,補回停損單:"
                          + _restore_stop(con, broker, risk, r["symbol"], r["client_order_id"], today))
    sells = [r for r in sells if r["client_order_id"] in waiting]
    if sells and broker.is_equity_market_open():
        kw = dict(bad=bad, flags=None, through="", last_session="", et=now.astimezone(ET), today=today)
        _send_exits(con, broker, risk, ledger, snap, sells, st, kw)
        events += st.lines
    elif sells:
        events.append("市場未開,出場計畫留待下一次執行:" + "、".join(r["symbol"] for r in sells))
    events += _expire_unretried_opg(con, broker, now)
    retries = _opg_retry_candidates(con, today)
    if retries and broker.is_equity_market_open():
        flags = _bridge_flags(con, ledger, now, mailbox)  # Muse flags as of now, not as of premarket
        n_lines = len(st.lines)
        _retry_opg_entries(con, broker, risk, snap, retries, st, bad=bad, flags=flags,
                           et=now.astimezone(ET), today=today)
        events += st.lines[n_lines:]
    events += _premarket_stops(con, broker, risk, today, bad, st)
    if st.sent:
        events += _protect_late_fills(con, broker, ledger, risk, st.sent, today)
    events += sweep_orphan_stops(con, broker, ledger, today)
    if bad:
        return "對帳不一致:\n" + "\n".join(bad.values()) + ("\n" + "\n".join(events) if events else "")
    if events:
        notify.send("Desk 開盤後", "\n".join(events), notify.INFO)
    return "\n".join(events) or "無變化"


def recover(con, broker: Broker, now: datetime, limits: Limits | None = None) -> str:
    """Run at boot (launchd RunAtLoad): launchd replays a missed calendar job
    after sleep but not after a power-off, so a crash window could otherwise
    leave a position without a stop until postclose. Books fills (and
    resolves pending_submit rows), reconciles, restores protective stops and
    sweeps orphan stops. It never plans, buys or sends exits."""
    ensure_schema(con)
    broker = stock_only(broker)
    st = _Run(job="recover")
    today = _today(now)
    risk = RiskEngine(limits or _PROFILE.limits(), con)
    ledger = risk.ledger
    events = sync_fills(con, broker, ledger, today)
    snap = broker.account()
    bad = _check_reconcile(con, ledger, snap, today)
    events += _premarket_stops(con, broker, risk, today, bad, st)
    events += sweep_orphan_stops(con, broker, ledger, today)
    head = ("對帳不一致:\n" + "\n".join(bad.values()) + "\n") if bad else ""
    if events:
        notify.send("Desk 開機復原", (head + "\n".join(events))[:3500], notify.INFO)
    return head + ("\n".join(events) or "無變化")


def crypto(con, broker: Broker, now: datetime) -> str:
    ensure_schema(con)
    broker = stock_only(broker)
    events = sync_fills(con, broker, Ledger(con), _today(now))
    return "加密貨幣策略於第 1 階段上線。" + ("\n" + "\n".join(events) if events else "")


def bridge(con, now: datetime, mailbox=None) -> str:
    from stockmoney.desk import muse_ops
    from stockmoney.desk.bridge import inbox, mailbox as mb
    from stockmoney.desk.bridge.policy import active_flags
    ensure_schema(con)
    muse_ops.ensure_schema(con)
    # Mail is stamped with the job clock. policy.active_flags ignores anything
    # received after the time it evaluates, so a veto read during this job
    # must never carry a timestamp later than ``now``.
    if mailbox is None:
        if not bridge_configured():
            return "Muse 橋樑未設定,跳過。"
        with mb.ImapSmtpMailbox(mb.BridgeConfig.from_env()) as box:
            n = inbox.poll(con, box, now=now)
            ops = muse_ops.run_checks(con, now, box)
    else:
        n = inbox.poll(con, mailbox, now=now)
        ops = muse_ops.run_checks(con, now, mailbox)
    held = set(Ledger(con).positions())
    for note in active_flags(con, now, held).notify:
        _notify_once(con, f"muse:{note[:100]}", "Muse 警報", note, notify.WARN, _today(now))
    return f"Muse 新訊息 {n} 則" + (f"\n{ops}" if ops else "")


# ---------------------------------------------------------------- bridge glue

def _bridge_flags(con, ledger: Ledger, now: datetime, mailbox=None):
    """Poll Muse, then read the flags in force. Only Exception is caught: a
    JobTimeout (BaseException) always propagates."""
    from stockmoney.desk.bridge.policy import BridgeFlags, active_flags
    try:
        bridge(con, now, mailbox)
    except Exception as e:  # an unreachable mailbox must not stop trading; it only loses a veto
        log.warning("bridge poll failed: %s", e)
        _notify_once(con, "bridgefail", "Muse 橋樑連線失敗", str(e)[:300], notify.WARN, _today(now))
    try:
        return active_flags(con, now, set(ledger.positions()))
    except Exception as e:
        log.warning("bridge flags failed: %s", e)
        return BridgeFlags(blocked={}, no_add={}, notify=[])


def _bridge_outbound(con, ledger: Ledger, planned: list[dict], report: str, now: datetime) -> None:
    from stockmoney.desk.bridge import mailbox as mb, outbox
    from stockmoney.desk.bridge.schema import Report, ResearchRequest, WatchItem, Watchlist
    try:
        if not bridge_configured():
            return
        items = [WatchItem(ticker=s, status="held") for s in ledger.positions()]
        items += [WatchItem(ticker=p["symbol"], status="candidate", note=p["strategy"]) for p in planned]
        deadline = (now.astimezone(ET) + timedelta(days=1)).replace(hour=8, minute=30, second=0, microsecond=0)
        from stockmoney.desk import muse_ops
        muse_ops.ensure_schema(con)
        msgs = [Watchlist(msg_id=outbox.new_msg_id("wl"), sent_at=now, items=items)]
        msgs += [ResearchRequest(
            msg_id=outbox.new_msg_id("rq"), sent_at=now, ticker=p["symbol"], deadline=deadline,
            questions=["明天開盤前有沒有會推翻做多的消息(財報、監管、訴訟、併購、高層異動)?",
                       "社群情緒近 3 天是否急遽轉向?"]) for p in planned]
        msgs.append(Report(msg_id=outbox.new_msg_id("rp"), sent_at=now,
                           title=f"Desk 盤後報告 {_today(now)}", body_markdown=report))
        with mb.ImapSmtpMailbox(mb.BridgeConfig.from_env()) as box:
            for m in msgs:
                outbox.send(box, m)
                muse_ops.log_outbound(con, m)
    except Exception as e:
        log.warning("bridge outbound failed: %s", e)


# ---------------------------------------------------------------- misc

def _cancel_and_wait(broker: Broker, client_order_id: str, timeout_s: float | None = None) -> bool:
    """Cancel is asynchronous at real brokers; the shares a stop holds are only
    free to sell once the cancel is confirmed. True once the order is terminal
    (cancelled, or filled: the caller must check which)."""
    timeout_s = CANCEL_WAIT_S if timeout_s is None else timeout_s
    broker.cancel(client_order_id)
    deadline = time.monotonic() + timeout_s
    while True:
        o = broker.get_order(client_order_id)
        if o is None or o.status in _TERMINAL:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        _sleep(min(1.0, remaining))


def _mark(con, p, status: str, note: str) -> None:
    con.execute("UPDATE planned_orders SET status=?, note=? WHERE client_order_id=?",
                (status, note[:500], p["client_order_id"]))


def _opens_today(broker: Broker, now: datetime) -> bool:
    client = getattr(broker, "client", None)
    if client is None:
        return is_trading_day(now.astimezone(ET).date())
    clock = _broker_call(broker, client.get_clock, "get_clock")
    return clock.next_open.astimezone(ET).date() == now.astimezone(ET).date()


def _report(day, ctx, sys_eq, budget, mode, why, ledger: Ledger, events, planned, exits) -> str:
    lines = [f"## {day} 盤後報告", "",
             f"**系統淨值** {sys_eq:,.0f}({sys_eq / budget - 1:+.2%}),狀態 {mode}:{why}", "",
             f"**大盤**:{REGIME_LABEL[ctx.market.regime]};{ctx.market.note}",
             "**領漲板塊**:" + "、".join(f"{sector_name(r.etf)} {r.rs63:+.1%}" for r in ctx.sectors.head(4).itertuples()),
             ""]
    if events:
        lines += ["**今日成交/變動**", *[f"- {e}" for e in events], ""]
    pos = ledger.positions()
    if pos:
        lines += ["**持倉**", *[f"- {s} {p.qty:g} 股,成本 {p.avg_price:.2f},停損 {p.stop or 0:.2f}({p.strategy})"
                              for s, p in pos.items()], ""]
    if exits:
        lines.append("**明天開盤出場**:" + "、".join(exits))
    lines.append("**明天開盤進場計畫**:" + ("、".join(f"{p['symbol']} {p['qty']} 股({p['strategy']})" for p in planned) or "無"))
    return "\n".join(lines)


JOBS = {"postclose", "premarket", "postopen", "recover", "crypto", "bridge"}


def _acquire_lock(path: Path, wait_s: float):
    """Exclusive flock so two jobs never run at once (e.g. premarket and
    postopen coalescing after the Mac wakes). Returns the open file holding
    the lock, or None if it could not be taken within ``wait_s``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            if time.monotonic() >= deadline:
                fh.close()
                return None
            _sleep(min(1.0, max(deadline - time.monotonic(), 0.01)))


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if len(argv) != 2 or argv[1] not in JOBS:
        print(f"usage: python -m stockmoney.desk.run {{{'|'.join(sorted(JOBS))}}}")
        return 2
    job = argv[1]

    def _timeout(*_):
        raise JobTimeout(f"job {job} exceeded {JOB_TIMEOUT_S}s")
    signal.signal(signal.SIGALRM, _timeout)
    # the outer bound covers the work done before the lock and the lock wait;
    # the job itself gets JOB_TIMEOUT_S from the moment it holds the lock
    signal.alarm(int(JOB_TIMEOUT_S + LOCK_WAIT_S))
    lock = None
    ok = False
    try:
        bars = None
        if job == "postclose":
            # the slow network download runs before the lock, so it never holds up another job
            try:
                bars = data.to_bars(data.refresh())
            except Exception as e:
                log.warning("data refresh before the lock failed (postclose retries it): %s", e)
        lock = _acquire_lock(LOCK_PATH, LOCK_WAIT_S)
        if lock is None:
            msg = f"另一個 desk 任務仍在執行,等待 {LOCK_WAIT_S:.0f} 秒後放棄本次 {job}"
            log.error(msg)
            notify.send(f"Desk 任務略過:{job}", msg, notify.CRITICAL)
            return 1
        signal.alarm(JOB_TIMEOUT_S)
        now = datetime.now(timezone.utc)  # after the lock: a job that waited must not act on a stale clock
        with store.connect() as con:
            if job == "bridge":
                out = bridge(con, now)
            elif job == "postclose":
                out = postclose(con, make_broker(), now, bars=bars)
            else:
                broker = make_broker()
                out = {"premarket": premarket, "postopen": postopen, "recover": recover,
                       "crypto": crypto}[job](con, broker, now)
        print(out)
        ok = True
    except JobTimeout as e:
        signal.alarm(0)
        log.error("job %s timed out: %s", job, e)
        notify.send(f"Desk 任務逾時:{job}", str(e), notify.CRITICAL)
    except Exception as e:
        log.exception("job %s failed", job)
        notify.send(f"Desk 任務失敗:{job}", f"{type(e).__name__}: {e}", notify.CRITICAL)
    finally:
        signal.alarm(0)
        if lock is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
