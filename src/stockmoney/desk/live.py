"""Live paper trading: the nightly job and the human actions the console calls.

Accounts:
- one ``machine`` account per strategy, plus ``combined`` (all strategies):
  fully automatic, the honest baseline.
- ``desk`` (kind ``human``): sees the same signals, but every entry waits for
  a human approve/reject/size decision. desk vs combined = the measured value
  of human judgement.

Catch-up: if the job misses days, it replays each missed session in order.
Orders awaiting approval on a missed night simply expire, as in real life.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from stockmoney.desk import data, engine, store
from stockmoney.desk.broker import Account, Order
from stockmoney.desk.indicators import REGIME_LABEL, compute
from stockmoney.desk.strategies import REGISTRY, Strategy
from stockmoney.desk.universe import sector_name

ET = ZoneInfo("America/New_York")
CAPITAL = 25_000.0
MACHINE_SETS = {**{n: [n] for n in REGISTRY}, "combined": list(REGISTRY)}
EARNINGS_WARN_DAYS = 7


def last_complete_session(bars: data.Bars, now: datetime | None = None) -> pd.Timestamp:
    """Drop today's bar until the session is over (data before ~16:30 ET is a
    partial intraday bar, using it would be look-ahead in the backtest sense)."""
    now = now or datetime.now(ET)
    last = bars.dates[-1]
    if last.date() == now.date() and (now.hour, now.minute) < (16, 30):
        return bars.dates[-2]
    return last


def build_strategies(con) -> dict[str, Strategy]:
    params = store.strategy_params(con)
    disabled = set(json.loads(store.get_setting(con, "disabled_strategies", "[]")))
    return {n: cls(**params.get(n, {})) for n, cls in REGISTRY.items() if n not in disabled}


def _init_accounts(con, start_after: pd.Timestamp, first_day: pd.Timestamp) -> None:
    last = str(start_after.date())
    for name in MACHINE_SETS:
        store.save_account(con, Account(name, CAPITAL, CAPITAL), "machine", last)
    store.save_account(con, Account("desk", CAPITAL, CAPITAL), "human", last)
    store.set_setting(con, "paper_start", str(first_day.date()))


def _earnings_flags(symbols: list[str]) -> dict[str, date | None]:
    if not symbols:
        return {}
    try:
        from stockmoney.data.earnings_calendar import get_next_earnings_dates

        return get_next_earnings_dates(symbols)
    except Exception:  # best effort, never block the run
        return {}


def run_daily(refresh: bool = True, now: datetime | None = None) -> str:
    long = data.refresh() if refresh else data.load()
    bars = data.to_bars(long)
    through = last_complete_session(bars, now)
    bars = bars.upto(through)
    ind = compute(bars)

    with store.connect() as con:
        if not con.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
            _init_accounts(con, bars.dates[-2], bars.dates[-1])
        accts = store.load_accounts(con)
        strats = build_strategies(con)
        halt = store.get_setting(con, "halt_new_entries", "0") == "1"
        first = min(pd.Timestamp(v[2]) for v in accts.values())
        todo = [d for d in bars.dates if d > first]

        latest_signals = []
        for t in todo:
            ctx = engine.day_context(ind, t)
            signals = engine.scan(strats, ctx)
            if t == todo[-1]:
                flags = _earnings_flags(sorted({s.symbol for s in signals}))
                for s in signals:
                    ed = flags.get(s.symbol)
                    if ed is not None and (ed - t.date()).days <= EARNINGS_WARN_DAYS:
                        s.layers.append({"layer": "事件風險", "ok": False, "note": f"財報日 {ed},{(ed - t.date()).days} 天內"})
                latest_signals = signals
            for s in signals:
                store.save_signal(con, engine.signal_id(s), s)
            for name, (acct, kind, last) in accts.items():
                if pd.Timestamp(last) >= t:
                    continue
                engine.morning(acct, bars, t)
                if kind == "human":
                    sub = strats
                    sigs = [] if halt else signals
                else:
                    sub = {n: strats[n] for n in MACHINE_SETS.get(name, []) if n in strats}
                    sigs = [s for s in signals if s.strategy in sub]
                engine.evening(acct, sub, ctx, sigs, require_approval=(kind == "human"))
                accts[name] = (acct, kind, str(t.date()))
        for name, (acct, kind, last) in accts.items():
            store.save_account(con, acct, kind, last)

        summary = daily_summary(ind, through, accts, latest_signals) if todo else "今日無新交易日資料。"
        if todo:
            con.execute(
                "INSERT INTO journal (date, auto_summary, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(date) DO UPDATE SET auto_summary=excluded.auto_summary, updated_at=excluded.updated_at",
                (str(through.date()), summary, store.now()),
            )
        con.execute(
            "INSERT INTO runs (run_at, through_date, status, message) VALUES (?,?,?,?)",
            (store.now(), str(through.date()), "ok", f"processed {len(todo)} session(s)"),
        )
    return summary


def daily_summary(ind, t: pd.Timestamp, accts: dict, signals: list) -> str:
    ctx = engine.day_context(ind, t)
    lines = [f"## {t.date()} 盤後摘要", "", f"**大盤**:{REGIME_LABEL[ctx.market.regime]} — {ctx.market.note}", ""]
    lead = ctx.sectors.head(4)
    lines.append("**資金流向(63日相對強度前4)**:" + "、".join(
        f"{sector_name(r.etf)} {r.rs63:+.1%}" for r in lead.itertuples()))
    lag = ctx.sectors.tail(3)
    lines.append("**最弱板塊**:" + "、".join(f"{sector_name(r.etf)} {r.rs63:+.1%}" for r in lag.itertuples()))
    lines += ["", f"**新訊號 {len(signals)} 個**:" + ("、".join(f"{s.symbol}({REGISTRY[s.strategy].label})" for s in signals) or "無")]
    day = str(t.date())
    lines += ["", "| 帳戶 | 淨值 | 累計 | 持倉 | 今日成交/出場 |", "|---|---|---|---|---|"]
    for name, (acct, kind, _) in accts.items():
        eq = acct.equity_curve[-1]["equity"] if acct.equity_curve else acct.starting_cash
        exits = [tr["symbol"] for tr in acct.trades if tr["exit_date"] == day]
        entries = [p.symbol for p in acct.positions.values() if p.entry_date == day]
        act = " ".join([f"+{s}" for s in entries] + [f"-{s}" for s in exits]) or "-"
        lines.append(f"| {name}{'(人工)' if kind == 'human' else ''} | {eq:,.0f} | {eq / acct.starting_cash - 1:+.2%} | {len(acct.positions)} | {act} |")
    return "\n".join(lines)


# ------------------------------------------------------------------ human actions
def _with_desk(fn):
    with store.connect() as con:
        accts = store.load_accounts(con)
        acct, kind, last = accts["desk"]
        result = fn(con, acct)
        store.save_account(con, acct, kind, last)
    return result


def decide(order_id: str, approve: bool, multiplier: float = 1.0, note: str = "") -> None:
    def _apply(con, acct: Account):
        for o in acct.orders:
            if o.order_id == order_id and o.status == "pending_approval":
                if approve:
                    o.shares = int(o.shares * multiplier)
                    o.status = "pending" if o.shares > 0 else "rejected"
                else:
                    o.status = "rejected"
                store.record_decision(con, o.signal_id, order_id, "approve" if approve else "reject", multiplier, note)
        acct.orders = [o for o in acct.orders if o.status in ("pending", "pending_approval")]

    _with_desk(_apply)


def manual_exit(symbol: str, note: str) -> None:
    """Human override: close a desk position at the next open."""
    def _apply(con, acct: Account):
        pos = acct.positions.get(symbol)
        if pos is None:
            return
        day = str(datetime.now(ET).date())
        acct.orders.append(Order(f"{day}-{symbol}-manual", symbol, "sell", pos.strategy, day, reason=f"人工出場:{note}"))
        store.record_decision(con, pos.signal_id, f"{day}-{symbol}-manual", "manual_exit", 0.0, note)

    _with_desk(_apply)
