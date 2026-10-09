"""One trading day for one account. Shared by the backtest and the live paper
run, so the paper month exercises exactly the code that was backtested."""
from __future__ import annotations

import hashlib

import pandas as pd

from stockmoney.desk.broker import Account, Order
from stockmoney.desk.data import Bars
from stockmoney.desk.indicators import Ind, market_state
from stockmoney.desk.strategies import Context, Signal, Strategy, build_context
from stockmoney.desk.universe import sector_of


def signal_id(s: Signal) -> str:
    return hashlib.sha1(f"{s.strategy}|{s.symbol}|{s.date.date()}".encode()).hexdigest()[:12]


def day_context(ind: Ind, t: pd.Timestamp) -> Context:
    return build_context(ind, t, market_state(ind, t))


def morning(acct: Account, bars: Bars, t: pd.Timestamp) -> None:
    acct.process_open(t, bars.open.loc[t], bars.low.loc[t], bars.dates)


def evening(
    acct: Account,
    strategies: dict[str, Strategy],
    ctx: Context,
    signals: list[Signal],
    *,
    require_approval: bool = False,
) -> list[Order]:
    """Mark to market, manage open positions, queue tomorrow's orders."""
    t, close = ctx.t, ctx.ind.close.loc[ctx.t]
    eq = acct.mark(t, close)
    new: list[Order] = []
    day = str(t.date())

    selling = set()
    for sym, pos in acct.positions.items():
        strat = strategies.get(pos.strategy)
        if strat is None or pd.isna(close.get(sym)):
            continue
        pos.stop = strat.trail_stop(ctx, sym, pos)
        reason = strat.exit_reason(ctx, sym, pos)
        if reason:
            selling.add(sym)
            new.append(Order(f"{day}-{sym}-sell", sym, "sell", pos.strategy, day, reason=reason))

    held = set(acct.positions) - selling
    invested = sum(acct.positions[s].shares * float(close[s]) for s in held)
    slots = acct.max_positions - len(held)
    per_sector: dict[str | None, int] = {}
    for sym in held:
        per_sector[sector_of(sym)] = per_sector.get(sector_of(sym), 0) + 1
    for s in sorted(signals, key=lambda s: -s.score):
        if slots <= 0:
            break
        if s.symbol in acct.positions or s.strategy not in strategies:
            continue
        sec = sector_of(s.symbol)
        if per_sector.get(sec, 0) >= acct.max_per_sector:
            continue
        shares = acct.size(eq, s.ref_price, s.stop, ctx.market.exposure_cap, invested, s.target_weight)
        if shares <= 0:
            continue
        invested += shares * s.ref_price
        slots -= 1
        per_sector[sec] = per_sector.get(sec, 0) + 1
        new.append(
            Order(
                f"{day}-{s.symbol}-buy", s.symbol, "buy", s.strategy, day, shares, s.stop,
                reason=s.thesis, signal_id=signal_id(s),
                status="pending_approval" if require_approval else "pending",
            )
        )
    acct.orders.extend(new)
    return new


def scan(strategies: dict[str, Strategy], ctx: Context) -> list[Signal]:
    out: list[Signal] = []
    for strat in strategies.values():
        out.extend(strat.candidates(ctx))
    return out
