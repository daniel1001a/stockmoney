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
The broker holds the truth; reconcile() halts everything on any mismatch.
"""
from __future__ import annotations

import json
import logging
import signal
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import floor
from zoneinfo import ZoneInfo

import pandas as pd

from stockmoney.desk import data, engine, notify, store
from stockmoney.desk.execution.broker import Broker
from stockmoney.desk.execution.ledger import Ledger, norm
from stockmoney.desk.execution.reconcile import reconcile
from stockmoney.desk.execution.risk import Limits, RiskEngine
from stockmoney.desk.execution.types import (
    AccountSnapshot, AssetClass, OrderIntent, OrderStatus, OrderType, Side, TIF,
)
from stockmoney.desk.indicators import REGIME_LABEL, compute
from stockmoney.desk.strategies import REGISTRY, Strategy
from stockmoney.desk.universe import sector_name, sector_of

log = logging.getLogger("desk.run")
ET = ZoneInfo("America/New_York")
JOB_TIMEOUT_S = 900  # the Alpaca SDK sets no HTTP timeout; never let a job hang forever

# Stock-book sizing (backtested values; see docs/desk/OPERATING_PLAN.md)
RISK_PER_TRADE = 0.0075
MAX_POSITIONS = 8
MAX_PER_SECTOR = 2

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


def _today(now: datetime) -> str:
    return now.astimezone(ET).date().isoformat()


def _cid(day: str, tag: str, symbol: str, strategy: str = "") -> str:
    # Alpaca client_order_id is short; keep it compact and deterministic
    return f"{day.replace('-', '')[2:]}-{tag}-{symbol.replace('/', '')}-{strategy[:4]}".rstrip("-")


def _notify_once(con, key: str, title: str, msg: str, priority: int) -> None:
    if con.execute("SELECT 1 FROM notified WHERE key=?", (key,)).fetchone():
        return
    notify.send(title, msg, priority)
    con.execute("INSERT INTO notified VALUES (?,?)", (key, store.now()))


def _halt(con, reason: str) -> None:
    store.set_setting(con, "halt_new_entries", "1")
    _notify_once(con, f"halt:{reason[:80]}", "Desk 已暫停新進場", reason, notify.CRITICAL)


def _strategies(con) -> dict[str, Strategy]:
    params = store.strategy_params(con)
    disabled = set(json.loads(store.get_setting(con, "disabled_strategies", "[]")))
    return {n: cls(**params.get(n, {})) for n, cls in REGISTRY.items() if n not in disabled}


def bridge_configured() -> bool:
    import os
    return all(os.environ.get(k) for k in ("DESK_MAIL_ADDRESS", "DESK_MAIL_APP_PASSWORD", "MUSE_ADDRESS", "MUSE_SHARED_TOKEN"))


def make_broker() -> Broker:
    from stockmoney.desk.execution.alpaca import AlpacaBroker
    return AlpacaBroker()


def _last_complete(bars: data.Bars, now: datetime) -> pd.Timestamp:
    from stockmoney.desk.live import last_complete_session
    return last_complete_session(bars, now.astimezone(ET))


# ---------------------------------------------------------------- fill sync

def sync_fills(con, broker: Broker, ledger: Ledger) -> list[str]:
    """Pull status of every unrecorded order; book completed fills once."""
    events = []
    rows = con.execute("SELECT * FROM submitted WHERE recorded=0").fetchall()
    for r in rows:
        o = broker.get_order(r["client_order_id"])
        if o is None:
            continue
        terminal = o.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED)
        con.execute("UPDATE submitted SET status=? WHERE client_order_id=?", (o.status.value, r["client_order_id"]))
        if not terminal:
            continue
        if o.filled_qty and o.filled_qty > 0:
            side = Side(r["side"])
            ledger.apply_fill(
                client_order_id=r["client_order_id"], symbol=r["symbol"], strategy=r["strategy"],
                side=side, qty=float(o.filled_qty), price=float(o.filled_avg_price),
                filled_at=o.filled_at.isoformat() if o.filled_at else None,
                stop=r["stop"] if side == Side.BUY else None,
            )
            verb = "買進" if side == Side.BUY else ("停損賣出" if r["kind"] == "stop" else "賣出")
            events.append(f"{verb} {r['symbol']} {o.filled_qty:g} @ {o.filled_avg_price:.2f}({r['strategy']})")
            if side == Side.BUY:
                con.execute(
                    "INSERT INTO position_meta (symbol, highest_close, initial_stop, entry_date) VALUES (?,?,?,?) "
                    "ON CONFLICT(symbol) DO NOTHING",
                    (r["symbol"], float(o.filled_avg_price), r["stop"], _today(o.filled_at or datetime.now(timezone.utc))))
            elif ledger.position(r["symbol"]) is None:
                con.execute("DELETE FROM position_meta WHERE symbol=?", (r["symbol"],))
        elif o.status != OrderStatus.CANCELED or r["kind"] != "stop":
            events.append(f"未成交 {r['symbol']} {r['side']}:{o.status.value}")
        con.execute("UPDATE submitted SET recorded=1 WHERE client_order_id=?", (r["client_order_id"],))
    return events


def _submit(con, broker: Broker, intent: OrderIntent, kind: str, stop: float | None = None):
    o = broker.submit(intent)
    con.execute(
        "INSERT OR IGNORE INTO submitted (client_order_id, kind, symbol, asset_class, strategy, side, qty, stop, submitted_at, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (intent.client_order_id, kind, intent.symbol, intent.asset_class.value, intent.strategy,
         intent.side.value, intent.qty, stop, store.now(), o.status.value))
    return o


def ensure_stops(con, broker: Broker, ledger: Ledger, risk: RiskEngine, snap: AccountSnapshot, day: str) -> list[str]:
    """Every equity position must have a live GTC stop at the ledger stop price.
    Replaces a stop whose price changed; re-creates a missing one."""
    events = []
    open_by_id = {o.client_order_id: o for o in broker.open_orders()}
    for sym, pos in ledger.positions().items():
        if "/" in sym or not pos.stop:
            continue  # crypto stops are managed by the crypto job (Phase 1)
        meta = con.execute("SELECT stop_order_id FROM position_meta WHERE symbol=?", (sym,)).fetchone()
        cur_id = meta["stop_order_id"] if meta else None
        cur = open_by_id.get(cur_id) if cur_id else None
        if cur and abs((cur.stop_price or 0) - pos.stop) < 0.005 and abs(cur.qty - pos.qty) < 1e-9:
            continue
        if cur:
            broker.cancel(cur_id)
        n = con.execute("SELECT COUNT(*) FROM submitted WHERE symbol=? AND kind='stop'", (sym,)).fetchone()[0]
        intent = OrderIntent(
            _cid(day, f"s{n}", sym), sym, AssetClass.EQUITY, Side.SELL, qty=floor(pos.qty),
            order_type=OrderType.STOP, tif=TIF.GTC, stop_price=round(pos.stop, 2),
            strategy=pos.strategy, reason="protective stop")
        d = risk.check(intent, snap, ref_price=pos.stop, orders_this_run=0)
        if not d.allow:
            events.append(f"停損單被風控擋下 {sym}:{d.reason}")
            continue
        _submit(con, broker, d.intent, "stop")
        con.execute("UPDATE position_meta SET stop_order_id=? WHERE symbol=?", (intent.client_order_id, sym))
        events.append(f"停損單 {sym} @ {pos.stop:.2f}")
    return events


# ---------------------------------------------------------------- jobs

def postclose(con, broker: Broker, now: datetime, bars: data.Bars | None = None, limits: Limits | None = None) -> str:
    ensure_schema(con)
    day = _today(now)
    limits = limits or Limits()
    risk = RiskEngine(limits, con)
    ledger = risk.ledger
    events = sync_fills(con, broker, ledger)
    snap = broker.account()

    problems = reconcile(ledger, snap)
    if problems:
        _halt(con, "對帳不一致:" + ";".join(problems[:5]))
        return "對帳不一致,已暫停:\n" + "\n".join(problems)

    if bars is None:
        bars = data.to_bars(data.refresh())
    through = _last_complete(bars, now)
    if str(through.date()) != day:
        return f"{day} 非交易日或資料未更新(最新 {through.date()}),跳過規劃。"
    bars = bars.upto(through)
    ind = compute(bars)
    ctx = engine.day_context(ind, through)
    strats = _strategies(con)
    close = ind.close.loc[through]

    # manage open stock positions: trail stops, rule exits
    exits = []
    for sym, pos in ledger.positions().items():
        if "/" in sym or sym not in close or pd.isna(close[sym]):
            continue
        meta = con.execute("SELECT * FROM position_meta WHERE symbol=?", (sym,)).fetchone()
        hc = max(float(meta["highest_close"]) if meta else pos.avg_price, float(close[sym]))
        con.execute("UPDATE position_meta SET highest_close=? WHERE symbol=?", (hc, sym))
        strat = strats.get(pos.strategy) or REGISTRY[pos.strategy]()
        lp = LivePos(pos.stop or 0.0, hc, meta["entry_date"] if meta else day)
        new_stop = strat.trail_stop(ctx, sym, lp)
        if new_stop > (pos.stop or 0) + 0.005:
            ledger.set_stop(sym, round(new_stop, 2))
        reason = strat.exit_reason(ctx, sym, lp)
        if reason:
            exits.append(sym)
            _plan(con, day, sym, Side.SELL, pos.qty, pos.strategy, None, float(close[sym]), reason, "")
    events += ensure_stops(con, broker, ledger, risk, snap, day)

    # new entries for tomorrow's open
    signals = engine.scan(strats, ctx)
    for s in signals:
        store.save_signal(con, engine.signal_id(s), s)
    planned = _plan_entries(con, day, ledger, risk, snap, ctx, signals, set(exits))

    mode, why = risk.mode(snap)
    sys_eq = risk.system_equity(snap)
    con.execute(
        "INSERT INTO equity_history VALUES (?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
        "system_equity=excluded.system_equity, broker_equity=excluded.broker_equity, mode=excluded.mode, "
        "n_positions=excluded.n_positions, recorded_at=excluded.recorded_at",
        (day, sys_eq, snap.equity, mode, len(ledger.positions()), store.now()))

    report = _report(day, ctx, sys_eq, limits.capital_budget, mode, why, ledger, events, planned, exits)
    con.execute(
        "INSERT INTO journal (date, auto_summary, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET auto_summary=excluded.auto_summary, updated_at=excluded.updated_at",
        (day, report, store.now()))
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
    held = {s for s in ledger.positions() if "/" not in s} - exiting
    if ctx.market.exposure_cap <= 0 or not signals:
        return []
    equity = risk.system_equity(snap)
    prices = {p.symbol: p.current_price for p in snap.positions}
    invested = sum(ledger.positions()[s].qty * prices.get(s, ledger.positions()[s].avg_price) for s in held)
    slots = MAX_POSITIONS - len(held)
    per_sector: dict[str | None, int] = {}
    for s in held:
        per_sector[sector_of(s)] = per_sector.get(sector_of(s), 0) + 1
    out = []
    for s in sorted(signals, key=lambda s: -s.score):
        if slots <= 0:
            break
        if s.symbol in held or ledger.position(s.symbol) is not None:
            continue
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
        dollars = min(dollars, equity * ctx.market.exposure_cap - invested)
        qty = floor(dollars / s.ref_price)
        if qty <= 0:
            continue
        invested += qty * s.ref_price
        slots -= 1
        per_sector[sec] = per_sector.get(sec, 0) + 1
        _plan(con, day, s.symbol, Side.BUY, qty, s.strategy, round(s.stop, 2), s.ref_price, s.thesis, engine.signal_id(s))
        out.append({"symbol": s.symbol, "strategy": s.strategy, "qty": qty, "ref": s.ref_price, "stop": s.stop})
    return out


def premarket(con, broker: Broker, now: datetime, limits: Limits | None = None, mailbox=None) -> str:
    ensure_schema(con)
    today = _today(now)
    if not broker.is_equity_market_open() and not _opens_today(broker, now):
        return f"{today} 不開盤,跳過。"
    risk = RiskEngine(limits or Limits(), con)
    ledger = risk.ledger
    sync_fills(con, broker, ledger)
    snap = broker.account()
    if (problems := reconcile(ledger, snap)):
        _halt(con, "對帳不一致:" + ";".join(problems[:5]))
        return "對帳不一致,只允許出場。"

    flags = _bridge_flags(con, ledger, now, mailbox)
    plans = con.execute(
        "SELECT * FROM planned_orders WHERE status='planned' AND plan_date < ? ORDER BY side DESC", (today,)).fetchall()
    lines, n = [], 0
    latest = max((p["plan_date"] for p in plans), default=None)
    for p in plans:
        sym, side = p["symbol"], Side(p["side"])
        if p["plan_date"] != latest:  # a plan older than the last session is stale
            _mark(con, p, "expired", "過期計畫")
            continue
        if side == Side.BUY and sym in flags.blocked:
            _mark(con, p, "blocked", flags.blocked[sym])
            lines.append(f"擋下 {sym}:{flags.blocked[sym]}")
            continue
        qty = p["qty"]
        if side == Side.SELL:
            pos = ledger.position(sym)
            if pos is None:
                _mark(con, p, "cancelled", "已無持倉")
                continue
            qty = floor(pos.qty)
            meta = con.execute("SELECT stop_order_id FROM position_meta WHERE symbol=?", (sym,)).fetchone()
            if meta and meta["stop_order_id"]:
                if not _cancel_and_wait(broker, meta["stop_order_id"]):
                    _mark(con, p, "planned", "停損單取消未確認,下次再試")
                    lines.append(f"{sym} 停損單取消未確認,出場延後")
                    continue
                con.execute("UPDATE position_meta SET stop_order_id=NULL WHERE symbol=?", (sym,))
        intent = OrderIntent(p["client_order_id"], sym, AssetClass(p["asset_class"]), side, qty=qty,
                             order_type=OrderType.MARKET, tif=TIF.OPG, strategy=p["strategy"], reason=p["reason"])
        d = risk.check(intent, snap, ref_price=p["ref_price"], orders_this_run=n)
        if not d.allow:
            _mark(con, p, "blocked", d.reason)
            lines.append(f"風控擋下 {sym}:{d.reason}")
            continue
        _submit(con, broker, d.intent, "entry" if side == Side.BUY else "exit", p["stop"])
        _mark(con, p, "submitted", d.reason)
        n += 1
        lines.append(f"{'買' if side == Side.BUY else '賣'} {sym} {d.intent.qty:g} 股(開盤市價)")
    msg = f"{today} 盤前送單 {n} 筆\n" + "\n".join(lines)
    for note in flags.notify:
        _notify_once(con, f"muse:{note[:100]}", "Muse 警報", note, notify.WARN)
    if n:
        notify.send(f"Desk 盤前 {today}", msg, notify.INFO)
    return msg


def postopen(con, broker: Broker, now: datetime, limits: Limits | None = None) -> str:
    ensure_schema(con)
    risk = RiskEngine(limits or Limits(), con)
    ledger = risk.ledger
    events = sync_fills(con, broker, ledger)
    snap = broker.account()
    if (problems := reconcile(ledger, snap)):
        _halt(con, "對帳不一致:" + ";".join(problems[:5]))
        return "對帳不一致:\n" + "\n".join(problems)
    events += ensure_stops(con, broker, ledger, risk, snap, _today(now))
    if events:
        notify.send("Desk 開盤後", "\n".join(events), notify.INFO)
    return "\n".join(events) or "無變化"


def crypto(con, broker: Broker, now: datetime) -> str:
    ensure_schema(con)
    events = sync_fills(con, broker, Ledger(con))
    return "加密貨幣策略於第 1 階段上線。" + ("\n" + "\n".join(events) if events else "")


def bridge(con, now: datetime, mailbox=None) -> str:
    from stockmoney.desk.bridge import inbox, mailbox as mb
    from stockmoney.desk.bridge.policy import active_flags
    ensure_schema(con)
    if mailbox is None:
        if not bridge_configured():
            return "Muse 橋樑未設定,跳過。"
        with mb.ImapSmtpMailbox(mb.BridgeConfig.from_env()) as box:
            n = inbox.poll(con, box)
    else:
        n = inbox.poll(con, mailbox)
    held = set(Ledger(con).positions())
    for note in active_flags(con, now, held).notify:
        _notify_once(con, f"muse:{note[:100]}", "Muse 警報", note, notify.WARN)
    return f"Muse 新訊息 {n} 則"


# ---------------------------------------------------------------- bridge glue

def _bridge_flags(con, ledger: Ledger, now: datetime, mailbox=None):
    from stockmoney.desk.bridge.policy import BridgeFlags, active_flags
    try:
        bridge(con, now, mailbox)
    except Exception as e:  # an unreachable mailbox must not stop trading; it only loses a veto
        log.warning("bridge poll failed: %s", e)
        _notify_once(con, f"bridgefail:{_today(now)}", "Muse 橋樑連線失敗", str(e)[:300], notify.WARN)
    try:
        return active_flags(con, now, set(ledger.positions()))
    except Exception as e:
        log.warning("bridge flags failed: %s", e)
        return BridgeFlags(blocked={}, no_add={}, notify=[])


def _bridge_outbound(con, ledger: Ledger, planned: list[dict], report: str, now: datetime) -> None:
    from datetime import timedelta
    from stockmoney.desk.bridge import mailbox as mb, outbox
    from stockmoney.desk.bridge.schema import Report, ResearchRequest, WatchItem, Watchlist
    try:
        if not bridge_configured():
            return
        items = [WatchItem(ticker=s, status="held") for s in ledger.positions()]
        items += [WatchItem(ticker=p["symbol"], status="candidate", note=p["strategy"]) for p in planned]
        deadline = (now.astimezone(ET) + timedelta(days=1)).replace(hour=8, minute=30, second=0, microsecond=0)
        with mb.ImapSmtpMailbox(mb.BridgeConfig.from_env()) as box:
            outbox.send(box, Watchlist(msg_id=outbox.new_msg_id(), sent_at=now, items=items))
            for p in planned:
                outbox.send(box, ResearchRequest(
                    msg_id=outbox.new_msg_id(), sent_at=now, ticker=p["symbol"], deadline=deadline,
                    questions=["明天開盤前有沒有會推翻做多的消息(財報、監管、訴訟、併購、高層異動)?",
                               "社群情緒近 3 天是否急遽轉向?"]))
            outbox.send(box, Report(msg_id=outbox.new_msg_id(), sent_at=now,
                                    title=f"Desk 盤後報告 {_today(now)}", body_markdown=report))
    except Exception as e:
        log.warning("bridge outbound failed: %s", e)


# ---------------------------------------------------------------- misc

def _cancel_and_wait(broker: Broker, client_order_id: str, timeout_s: float = 20.0) -> bool:
    """Cancel is asynchronous at real brokers; the shares a stop holds are only
    free to sell once the cancel is confirmed."""
    import time
    broker.cancel(client_order_id)
    deadline = time.monotonic() + timeout_s
    while True:
        o = broker.get_order(client_order_id)
        if o is None or o.status in (OrderStatus.CANCELED, OrderStatus.FILLED, OrderStatus.EXPIRED, OrderStatus.REJECTED):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1.0)


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


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if len(argv) != 2 or argv[1] not in JOBS:
        print(f"usage: python -m stockmoney.desk.run {{{'|'.join(sorted(JOBS))}}}")
        return 2
    job = argv[1]

    def _timeout(*_):
        raise TimeoutError(f"job {job} exceeded {JOB_TIMEOUT_S}s")
    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(JOB_TIMEOUT_S)
    now = datetime.now(timezone.utc)
    try:
        with store.connect() as con:
            if job == "bridge":
                out = bridge(con, now)
            else:
                broker = make_broker()
                out = {"postclose": postclose, "premarket": premarket, "postopen": postopen,
                       "crypto": crypto}[job](con, broker, now)
        print(out)
        con_ok = True
    except Exception as e:
        log.exception("job %s failed", job)
        notify.send(f"Desk 任務失敗:{job}", f"{type(e).__name__}: {e}", notify.CRITICAL)
        con_ok = False
    finally:
        signal.alarm(0)
    return 0 if con_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
