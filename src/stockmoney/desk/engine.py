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


# Tie-break when two signals rank equally: earlier in this tuple wins.
STRATEGY_ORDER = ("breakout", "pullback", "rotation")


def rank_signals(signals: list[Signal]) -> list[Signal]:
    """Order signals for slot allocation, one signal per symbol.

    Raw scores are in different units per strategy (rotation's momentum score
    would always beat a breakout's), so each signal is ranked by the
    percentile of its score within its own strategy (best = 1.0) and the
    strategies interleave on that scale. Ties go to STRATEGY_ORDER, then the
    raw score, then the symbol, so the order is deterministic. When several
    strategies signal one symbol only the best-ranked signal is kept."""
    by_strat: dict[str, list[Signal]] = {}
    for s in signals:
        by_strat.setdefault(s.strategy, []).append(s)
    pct: dict[int, float] = {}
    for group in by_strat.values():
        ranks = pd.Series([s.score for s in group], dtype=float).rank(method="max", pct=True)
        for s, r in zip(group, ranks):
            pct[id(s)] = 0.0 if pd.isna(r) else float(r)

    def order(s: Signal):
        o = STRATEGY_ORDER.index(s.strategy) if s.strategy in STRATEGY_ORDER else len(STRATEGY_ORDER)
        score = s.score if pd.notna(s.score) else float("-inf")
        return (-pct[id(s)], o, s.strategy, -score, s.symbol)

    out, seen = [], set()
    for s in sorted(signals, key=order):
        if s.symbol in seen:
            continue
        seen.add(s.symbol)
        out.append(s)
    return out


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
    planned: set[str] = set()  # one symbol takes at most one slot, whichever strategy signals it
    for s in rank_signals([s for s in signals if s.strategy in strategies]):
        if slots <= 0:
            break
        if s.symbol in acct.positions or s.symbol in planned:
            continue
        sec = sector_of(s.symbol)
        if per_sector.get(sec, 0) >= acct.max_per_sector:
            continue
        shares = acct.size(eq, s.ref_price, s.stop, ctx.market.exposure_cap, invested, s.target_weight)
        if shares <= 0:
            continue
        invested += shares * s.ref_price
        planned.add(s.symbol)
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
