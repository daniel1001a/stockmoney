"""Orchestrator: the scheduled jobs that run the desk against a real broker
(Alpaca paper today). Every job is idempotent and safe to re-run.

    python -m stockmoney.desk.run postclose   # 16:45 ET: sync fills, reconcile, trail stops, plan tomorrow
    python -m stockmoney.desk.run premarket   # 08:45 ET: Muse flags, risk checks, submit market-on-open
    python -m stockmoney.desk.run postopen    # 09:50 ET: sync fills, place protective stops
    python -m stockmoney.desk.run crypto      # 20:00 ET daily: crypto sleeves (Phase 1)
    python -m stockmoney.desk.run bridge      # every 15 min: poll Muse

Flow of one stock trade:
  postclose(T)  strategy signal -> planned buy (sized)          [planned_orders]
  premarket(T+1) Muse flags + risk check -> OPG market buy      [submitted]
  postopen(T+1)  fill -> ledger; GTC stop placed at broker      [ledger, position_meta]
  postclose(...) trail the stop up; rule exit -> planned sell
  premarket      cancel stop, OPG market sell
The broker holds the truth. A reconcile mismatch halts new entries and pages
the owner; exits and protective stops keep running for every symbol that still
reconciles (a mismatched symbol is left alone for a human).

Crash safety: every broker side effect (submit, cancel) is committed to the
database right after it happens, so a later exception cannot roll back the
record of an order the broker already holds. Jobs never run concurrently (an
exclusive lock on data/desk/run.lock), and a job that exceeds JOB_TIMEOUT_S is
aborted with JobTimeout, a BaseException that no retry layer can swallow.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import floor
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from stockmoney.desk import data, engine, notify, store
from stockmoney.desk.execution.broker import Broker
from stockmoney.desk.execution.ledger import Ledger, norm
from stockmoney.desk.execution.reconcile import mismatches
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
LOCK_WAIT_S = 300.0      # a job waits this long for the previous one, then gives up
PLAN_THROUGH_KEY = "last_postclose_through"  # session date the newest valid plans were made from

# Stock-book sizing (backtested values; see docs/desk/OPERATING_PLAN.md)
RISK_PER_TRADE = 0.0075
MAX_POSITIONS = 8
MAX_PER_SECTOR = 2
MAX_POSITION_PCT = 0.20  # same cap as the backtest Account

_TERMINAL = (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED)
_sleep = time.sleep


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


def _halt(con, reason: str, day: str) -> None:
    store.set_setting(con, "halt_new_entries", "1")
    _notify_once(con, f"halt:{reason[:80]}", "Desk 已暫停新進場", reason, notify.CRITICAL, day)


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


def _last_closed_session(broker: Broker, now: datetime) -> str:
    """ISO date of the most recent regular session that has closed by ``now``.
    Uses the broker's market calendar when it has one (Alpaca), else weekdays
    (FakeBroker, or a calendar outage)."""
    et = now.astimezone(ET)
    client = getattr(broker, "client", None)
    if client is not None:
        try:
            from alpaca.trading.requests import GetCalendarRequest
            cal = client.get_calendar(GetCalendarRequest(start=et.date() - timedelta(days=14), end=et.date()))
            local = et.replace(tzinfo=None)  # the calendar's open/close are naive ET wall-clock times
            closed = [c.date for c in cal if c.close <= local]
            if closed:
                return max(closed).isoformat()
        except Exception as e:
            log.warning("market calendar unavailable, falling back to weekdays: %s", e)
    d = et.date()
    if not (d.weekday() < 5 and (et.hour, et.minute) >= SESSION_CLOSE):
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    return d.isoformat()


def _check_reconcile(con, ledger: Ledger, snap: AccountSnapshot, day: str) -> dict[str, str]:
    """Mismatched symbols (normalized) -> problem. Any mismatch halts new
    entries and pages the owner; the caller keeps managing the clean symbols."""
    bad = mismatches(ledger, snap)
    if bad:
        _halt(con, "對帳不一致:" + ";".join(list(bad.values())[:5]), day)
        con.commit()
    return bad


# ---------------------------------------------------------------- fill sync

def sync_fills(con, broker: Broker, ledger: Ledger, day: str | None = None) -> list[str]:
    """Pull status of every unrecorded order; book completed fills once. Each
    row is its own transaction: a row that cannot be booked (for example a
    SleeveConflict) halts new entries and pages the owner, and the remaining
    rows are still booked."""
    day = day or _today(datetime.now(timezone.utc))
    events = []
    con.commit()
    rows = con.execute("SELECT * FROM submitted WHERE recorded=0 ORDER BY submitted_at, client_order_id").fetchall()
    for r in rows:
        try:
            ev = _sync_one(con, broker, ledger, r)
            con.commit()
            if ev:
                events.append(ev)
        except Exception as e:
            con.rollback()
            log.exception("could not book order %s", r["client_order_id"])
            why = f"成交入帳失敗 {r['symbol']}({r['client_order_id']}):{type(e).__name__}: {e}"
            _halt(con, why, day)
            con.commit()
            events.append(why)
    return events


def _sync_one(con, broker: Broker, ledger: Ledger, r) -> str | None:
    o = broker.get_order(r["client_order_id"])
    if o is None:
        return None
    con.execute("UPDATE submitted SET status=? WHERE client_order_id=?", (o.status.value, r["client_order_id"]))
    if o.status not in _TERMINAL:
        return None
    ev = None
    if o.filled_qty and o.filled_qty > 0:
        side = Side(r["side"])
        ledger.apply_fill(
            client_order_id=r["client_order_id"], symbol=r["symbol"], strategy=r["strategy"],
            side=side, qty=float(o.filled_qty), price=float(o.filled_avg_price),
            filled_at=o.filled_at.isoformat() if o.filled_at else None,
            stop=r["stop"] if side == Side.BUY else None,
        )
        verb = "買進" if side == Side.BUY else ("停損賣出" if r["kind"] == "stop" else "賣出")
        ev = f"{verb} {r['symbol']} {o.filled_qty:g} @ {o.filled_avg_price:.2f}({r['strategy']})"
        if side == Side.BUY:
            con.execute(
                "INSERT INTO position_meta (symbol, highest_close, initial_stop, entry_date) VALUES (?,?,?,?) "
                "ON CONFLICT(symbol) DO NOTHING",
                (r["symbol"], float(o.filled_avg_price), r["stop"], _today(o.filled_at or datetime.now(timezone.utc))))
        elif ledger.position(r["symbol"]) is None:
            con.execute("DELETE FROM position_meta WHERE symbol=?", (r["symbol"],))
    elif o.status != OrderStatus.CANCELED or r["kind"] != "stop":
        ev = f"未成交 {r['symbol']} {r['side']}:{o.status.value}"
    con.execute("UPDATE submitted SET recorded=1 WHERE client_order_id=?", (r["client_order_id"],))
    return ev


def _submit(con, broker: Broker, intent: OrderIntent, kind: str, stop: float | None = None, *,
            plan=None, note: str = "", stop_for: str | None = None, day: str = ""):
    """Send one order and commit its bookkeeping at once, so nothing that fails
    later in the job can roll back the record of an order the broker already
    holds. ``plan``: the planned_orders row to mark submitted. ``stop_for``:
    the symbol whose position_meta.stop_order_id this order becomes."""
    o = broker.submit(intent)
    con.execute(
        "INSERT OR IGNORE INTO submitted (client_order_id, kind, symbol, asset_class, strategy, side, qty, stop, submitted_at, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (intent.client_order_id, kind, intent.symbol, intent.asset_class.value, intent.strategy,
         intent.side.value, intent.qty, stop, store.now(), o.status.value))
    if plan is not None:
        _mark(con, plan, "submitted", note)
    if stop_for is not None:
        _set_stop_id(con, stop_for, intent.client_order_id, stop, day)
    con.commit()
    return o


def _set_stop_id(con, sym: str, cid: str | None, stop: float | None = None, day: str = "") -> None:
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
    re-created. ``skip``: normalized symbols that failed reconciliation and are
    left for a human. One symbol's failure is reported and the rest continue."""
    events = []
    open_orders = broker.open_orders()
    for sym, pos in ledger.positions().items():
        if "/" in sym or norm(sym) in skip:
            continue  # crypto stops are managed by the crypto job (Phase 1)
        try:
            ev = _ensure_stop(con, broker, risk, snap, day, sym, pos, open_orders)
        except Exception as e:
            con.commit()  # keep the record of whatever the broker already did
            log.exception("stop management failed for %s", sym)
            ev = f"停損單處理失敗 {sym}:{type(e).__name__}: {e}"
        if ev:
            events.append(ev)
    return events


def _ensure_stop(con, broker: Broker, risk: RiskEngine, snap: AccountSnapshot, day: str, sym: str, pos,
                 open_orders: list[BrokerOrder]) -> str | None:
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
    if cur and cur.status != OrderStatus.PENDING and matches(cur):
        return None
    strays = [o for o in open_orders
              if norm(o.symbol) == norm(sym) and o.side == Side.SELL and o.client_order_id != cur_id
              and o.order_type in (OrderType.STOP, OrderType.STOP_LIMIT)]
    if cur is None:
        for o in strays:
            if o.status != OrderStatus.PENDING and o.order_type == OrderType.STOP and matches(o):
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
            return f"{sym} 停損單已成交,待下次同步入帳"
    if cur_id:
        _set_stop_id(con, sym, None)
        con.commit()
    n = con.execute("SELECT COUNT(*) FROM submitted WHERE symbol=? AND kind='stop'", (sym,)).fetchone()[0]
    intent = OrderIntent(
        _cid(day, f"s{n}", sym), sym, AssetClass.EQUITY, Side.SELL, qty=qty,
        order_type=OrderType.STOP, tif=TIF.GTC, stop_price=want,
        strategy=pos.strategy, reason="protective stop")
    d = risk.check(intent, snap, ref_price=want, orders_this_run=0)
    if not d.allow:
        return f"停損單被風控擋下 {sym}:{d.reason}"
    _submit(con, broker, d.intent, "stop", want, stop_for=sym, day=day)
    return f"停損單 {sym} @ {want:.2f}"


# ---------------------------------------------------------------- jobs

def postclose(con, broker: Broker, now: datetime, bars: data.Bars | None = None, limits: Limits | None = None) -> str:
    ensure_schema(con)
    today = _today(now)
    limits = limits or Limits()
    risk = RiskEngine(limits, con)
    ledger = risk.ledger
    events = sync_fills(con, broker, ledger, today)
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
        if "/" in sym or norm(sym) in bad or sym not in close or pd.isna(close[sym]):
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
                _plan(con, day, sym, Side.SELL, pos.qty, pos.strategy, None, float(close[sym]), reason, "")
        except Exception as e:
            log.exception("position management failed for %s", sym)
            events.append(f"持倉管理失敗 {sym}:{type(e).__name__}: {e}")
        con.commit()
    events += ensure_stops(con, broker, ledger, risk, snap, day, skip=set(bad))
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
    con.execute(
        "INSERT INTO journal (date, auto_summary, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET auto_summary=excluded.auto_summary, updated_at=excluded.updated_at",
        (day, report, store.now()))
    con.commit()
    _bridge_outbound(con, ledger, planned, report, now)
    notify.send(f"Desk 盤後 {day}", report[:3500], notify.INFO)
    return report


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
    whichever strategies signal it."""
    positions = ledger.positions()
    held = {s for s in positions if "/" not in s} - exiting
    if ctx.market.exposure_cap <= 0 or not signals:
        return []
    equity = risk.system_equity(snap)
    if not (_finite(equity) and equity > 0):
        log.warning("system equity %s is not usable; no entries planned", equity)
        return []
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
    out = []
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
    """Mutable state of one premarket run."""
    n: int = 0
    lines: list[str] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)


def premarket(con, broker: Broker, now: datetime, limits: Limits | None = None, mailbox=None) -> str:
    ensure_schema(con)
    today = _today(now)
    if not broker.is_equity_market_open() and not _opens_today(broker, now):
        return f"{today} 不開盤,跳過。"
    risk = RiskEngine(limits or Limits(), con)
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
    # Market-on-open is only accepted until ~09:28 ET. A late run (Mac was asleep)
    # falls back to a plain market order, and gives up on new entries after 10:30
    # so we never chase a stale plan deep into the session.
    late = (et.hour, et.minute) >= OPG_CUTOFF
    entries_closed = (et.hour, et.minute) >= ENTRY_CUTOFF
    st = _Run(lines=["對帳不一致,只允許出場:" + ";".join(bad.values())] if bad else [])
    for p in plans:
        try:
            _premarket_one(con, broker, risk, ledger, snap, p, st, bad=bad, flags=flags, through=through,
                           last_session=last_session, late=late, entries_closed=entries_closed, et=et, today=today)
        except Exception as e:
            log.exception("premarket plan %s failed", p["client_order_id"])
            why = f"{type(e).__name__}: {e}"
            _mark(con, p, "error", why)
            con.commit()
            st.lines.append(f"送單失敗 {p['symbol']}:{why}")
            notify.send(f"Desk 送單失敗 {p['symbol']}", why[:1000], notify.CRITICAL)
    con.commit()
    if late and st.sent:
        st.lines += _protect_late_fills(con, broker, ledger, risk, st.sent, today)
    msg = f"{today} 盤前送單 {st.n} 筆\n" + "\n".join(st.lines)
    for note in flags.notify:
        _notify_once(con, f"muse:{note[:100]}", "Muse 警報", note, notify.WARN, today)
    if st.n:
        notify.send(f"Desk 盤前 {today}", msg, notify.INFO)
    return msg


def _premarket_one(con, broker: Broker, risk: RiskEngine, ledger: Ledger, snap: AccountSnapshot, p, st: _Run,
                   *, bad, flags, through, last_session, late, entries_closed, et, today) -> None:
    sym, side = p["symbol"], Side(p["side"])
    if p["plan_date"] != through or through != last_session:
        # only plans from the postclose of the last completed session are valid
        _mark(con, p, "expired", f"過期計畫:計畫日 {p['plan_date']},最近一次盤後規劃 {through or '無'},"
                                 f"上一個交易日 {last_session}")
        return
    tif = TIF.DAY if late else TIF.OPG
    if side == Side.BUY:
        if entries_closed:
            _mark(con, p, "expired", f"錯過進場時間窗(執行於 {et:%H:%M} ET,超過 10:30)")
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
        if (why := _order_conflict(con, broker, sym, p["client_order_id"])):
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
        intent = OrderIntent(p["client_order_id"], sym, AssetClass(p["asset_class"]), side, qty=qty,
                             order_type=OrderType.MARKET, tif=tif, strategy=p["strategy"], reason=p["reason"])
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
            _mark(con, p, "blocked", "對帳不一致,此檔出場待人工確認")
            st.lines.append(f"{sym} 對帳不一致,出場待人工確認")
            return
        if ledger.position(sym) is None:
            _mark(con, p, "cancelled", "已無持倉")
            return
        meta = con.execute("SELECT stop_order_id FROM position_meta WHERE symbol=?", (sym,)).fetchone()
        if meta and meta["stop_order_id"]:
            stop_id = meta["stop_order_id"]
            if not _cancel_and_wait(broker, stop_id):
                _mark(con, p, "planned", "停損單取消未確認,下次再試")
                st.lines.append(f"{sym} 停損單取消未確認,出場延後")
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
        live = broker.account()  # size the exit from what the broker holds now, not the job-start snapshot
        held = next((bp.qty for bp in live.positions if norm(bp.symbol) == norm(sym)), 0.0)
        qty = floor(held + 1e-9)
        if qty <= 0:
            _mark(con, p, "cancelled", "券商已無持倉")
            return
        intent = OrderIntent(p["client_order_id"], sym, AssetClass(p["asset_class"]), side, qty=qty,
                             order_type=OrderType.MARKET, tif=tif, strategy=p["strategy"], reason=p["reason"])
        d = risk.check(intent, live, ref_price=p["ref_price"], orders_this_run=st.n)
        if not d.allow:
            _mark(con, p, "blocked", d.reason)
            st.lines.append(f"風控擋下 {sym}:{d.reason}")
            return
        _submit(con, broker, d.intent, "exit", None, plan=p, note=d.reason)
    st.n += 1
    st.sent.append(d.intent.client_order_id)
    st.lines.append(f"{'買' if side == Side.BUY else '賣'} {sym} {d.intent.qty:g} 股({'盤中市價' if late else '開盤市價'})")


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
    filling); postopen and postclose reconcile them properly."""
    try:
        deadline = time.monotonic() + CANCEL_WAIT_S
        while True:
            pending = [c for c in cids if (o := broker.get_order(c)) is not None and o.status not in _TERMINAL]
            if not pending or time.monotonic() >= deadline:
                break
            _sleep(1.0)
        events = sync_fills(con, broker, ledger, today)
        snap = broker.account()
        events += ensure_stops(con, broker, ledger, risk, snap, today, skip=set(mismatches(ledger, snap)))
        return events
    except Exception as e:
        log.exception("late stop placement failed")
        notify.send("Desk 盤中進場停損單失敗", f"{type(e).__name__}: {e}", notify.CRITICAL)
        return [f"盤中進場停損單失敗:{type(e).__name__}: {e}"]


def postopen(con, broker: Broker, now: datetime, limits: Limits | None = None) -> str:
    ensure_schema(con)
    today = _today(now)
    risk = RiskEngine(limits or Limits(), con)
    ledger = risk.ledger
    events = sync_fills(con, broker, ledger, today)
    snap = broker.account()
    bad = _check_reconcile(con, ledger, snap, today)
    events += ensure_stops(con, broker, ledger, risk, snap, today, skip=set(bad))
    if bad:
        return "對帳不一致:\n" + "\n".join(bad.values()) + ("\n" + "\n".join(events) if events else "")
    if events:
        notify.send("Desk 開盤後", "\n".join(events), notify.INFO)
    return "\n".join(events) or "無變化"


def crypto(con, broker: Broker, now: datetime) -> str:
    ensure_schema(con)
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
        return now.astimezone(ET).weekday() < 5
    clock = client.get_clock()
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


JOBS = {"postclose", "premarket", "postopen", "crypto", "bridge"}


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
    signal.alarm(JOB_TIMEOUT_S)
    now = datetime.now(timezone.utc)
    lock = None
    ok = False
    try:
        lock = _acquire_lock(LOCK_PATH, LOCK_WAIT_S)
        if lock is None:
            msg = f"另一個 desk 任務仍在執行,等待 {LOCK_WAIT_S:.0f} 秒後放棄本次 {job}"
            log.error(msg)
            notify.send(f"Desk 任務略過:{job}", msg, notify.CRITICAL)
            return 1
        with store.connect() as con:
            if job == "bridge":
                out = bridge(con, now)
            else:
                broker = make_broker()
                out = {"postclose": postclose, "premarket": premarket, "postopen": postopen,
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
