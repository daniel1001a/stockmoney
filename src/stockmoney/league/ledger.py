"""Turns a trader's directional call into a booked entry in its $100,000 paper
options account (trader_portfolios/trader_trades, migration 035) -- the piece
IMPROVEMENT_PLAN.md §S3 / NEXT_AGENT_PLAN.md Wave D calls "make the Arena
real". Everything else that view needs (schema, Black-Scholes premium math in
models/options_pnl.py, the API's leaderboard/trader-profile queries, the
frontend) already existed; this module is the missing bridge that actually
writes rows, so it is deliberately thin -- reuse, not reinvent, the pricing.

Sizing is mechanical (CLAUDE.md section 7: a discretion input never touches
the model, only how big a already-decided call is sized) -- the SAME rule
applied to every trader, same spirit as league/option_bridge.py's "instrument
selection is mechanical" split. What makes it feel like "each trader plans its
own position size" (owner request, 2026-08-07) is that the rule is driven by
that trader's OWN conviction for that specific call (see `position_size_scale`)
-- a confident call gets a bigger ticket, a hesitant one gets a smaller one,
for every trader identically, rather than a hand-picked per-persona number
nobody has backtested. Nothing here is ever read by a training path.

Capital (owner request, 2026-08-07): $100,000 starting capital, shared
uniformly by every trader -- raised from an initial $25,000 which, combined
with the "single position <= 20% of capital" rule and a 31-symbol watchlist
issuing several same-day directional calls, meant an account could run out of
deployable capital after ~5 concurrent positions and simply miss most of its
own signals. $100,000 was picked as a round number comfortably above the
observed worst-case same-day concurrency, not backtested -- a v1 starting
point like the rest of this file's constants, revisit if a trader still runs
dry.

Sizing convention: each position is sized at up to `MAX_POSITION_PCT` of the
account's total committed capital -- cash plus the cost basis of currently
open positions, see `_sizing_budget` -- scaled by `position_size_scale`,
floored to whole contracts, and the final debit is always hard-capped at
available cash so a trader can never overspend money it doesn't have. Too
little cash for even one contract -> the call is recorded (trader_predictions
always has it) but no trade is booked, same "no trade, not an error"
convention option_bridge/grading_options already use.

Fill convention: reuses models/options_pnl.py's entry_premium/exit_premium
(the SAME Black-Scholes mid + bid-ask-spread convention grading_options.py
already uses to grade the fractional option_pnl on trader_predictions), so a
long pays the ask on entry and sells at the bid on exit -- the dollar ledger
and the fractional-return grade are two views of the identical fill, never
two different assumptions.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timezone

import duckdb

from stockmoney.data.integrity_fixes import nearest_friday
from stockmoney.data.trader_predictions import TraderPrediction
from stockmoney.models.options_pnl import (
    DEFAULT_RATE,
    DEFAULT_SPREAD_PCT,
    OptionEntry,
    OptionExit,
    entry_premium as _mid_entry_premium,
    exit_premium as _mid_exit_premium,
)

STARTING_CAPITAL = 100_000.0
MAX_POSITION_PCT = 0.20
INSTRUMENT_SCOPE = "short_dated_calls_puts"
_MARKET_OPEN = time(14, 30)  # ~9:30am US/Eastern, wall-clock stand-in (no intraday data)

# Quote lag (報價延遲, issue #7 P1): every fill in this ledger is priced off
# entry_price/option_structure, which trace back to the same yfinance
# free-tier feed data/ingestion/live_quotes.py documents as ~15min delayed.
# Stamped on every trade so a future backtest/perf accounting can treat this
# lag as an honest cost rather than silently assuming instant fills.
QUOTE_LAG_MINUTES = 15

# conviction 0.0 -> 40% of the position cap, conviction 1.0 -> the full cap.
# Floored at 0.4 rather than 0.0 so even a low-conviction call still puts on a
# meaningful ticket (a real trader who takes a trade at all rarely sizes it at
# literally nothing) -- linear in between, no claim this shape is optimal, just
# a defensible v1 the same way the rest of this module's constants are.
_SCALE_FLOOR = 0.4
_SCALE_RANGE = 0.6


def position_size_scale(conviction: float) -> float:
    """Fraction of `MAX_POSITION_PCT` a call of this conviction earns. Each
    trader's engine produces its own conviction for its own call, so this is
    genuinely that trader's own read on the trade -- not a rule imposed from
    outside -- even though the scale function itself is shared."""
    c = max(0.0, min(1.0, conviction))
    return _SCALE_FLOOR + _SCALE_RANGE * c


def _as_of(d: date) -> datetime:
    return datetime.combine(d, _MARKET_OPEN, tzinfo=timezone.utc)


def ensure_portfolio(conn: duckdb.DuckDBPyConnection, trader_id: str, inception_date: date) -> None:
    """Create the trader's $100,000 account on first use. No-op if it already
    exists (idempotent, safe to call from every open_trade)."""
    exists = conn.execute(
        "SELECT 1 FROM trader_portfolios WHERE trader_id = ?", [trader_id]
    ).fetchone()
    if exists is not None:
        return
    conn.execute(
        """
        INSERT INTO trader_portfolios
            (trader_id, starting_capital, cash, max_position_pct, instrument_scope, inception_date, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [trader_id, STARTING_CAPITAL, STARTING_CAPITAL, MAX_POSITION_PCT, INSTRUMENT_SCOPE,
         inception_date, datetime.now(timezone.utc)],
    )


def _cash(conn: duckdb.DuckDBPyConnection, trader_id: str) -> float:
    row = conn.execute("SELECT cash FROM trader_portfolios WHERE trader_id = ?", [trader_id]).fetchone()
    return row[0]


def _sizing_budget(conn: duckdb.DuckDBPyConnection, trader_id: str) -> float:
    """Position sizing is `MAX_POSITION_PCT` of total committed capital (cash +
    cost basis of currently open positions), not of cash alone. Cash-only
    sizing would shrink multiplicatively every time a trader opens several
    positions the same day (0.8 compounded per position) even though nothing
    has been lost -- the capital is still sitting in open positions, not gone.
    Cost basis rather than a live mark-to-market: no options quote store
    exists (same limitation _rough_mark's docstring notes), so this is the
    honest v1 approximation. The final debit is still hard-capped at available
    cash below, so this can never overspend real cash."""
    cash = _cash(conn, trader_id)
    open_cost = conn.execute(
        "SELECT COALESCE(SUM(entry_premium * 100 * contracts), 0) FROM trader_trades "
        "WHERE trader_id = ? AND status = 'open'",
        [trader_id],
    ).fetchone()[0]
    return cash + open_cost


def _entry_from_structure(s: dict) -> OptionEntry:
    return OptionEntry(spot=s["spot"], strike=s["strike"], is_call=s["is_call"], iv=s["iv"],
                        t_years=s["t_years"], side=s["side"])


def open_trade(
    conn: duckdb.DuckDBPyConnection, prediction: TraderPrediction,
    *, r: float = DEFAULT_RATE, spread_pct: float = DEFAULT_SPREAD_PCT,
) -> str | None:
    """Book the entry fill for one prediction's chosen instrument. Returns the
    new trade_id, or None if there was nothing to book (no option_structure --
    a 'range' call or no usable entry IV that day -- or not enough cash left
    for even a single contract)."""
    if prediction.option_structure is None:
        return None
    already = conn.execute(
        "SELECT 1 FROM trader_trades WHERE linked_prediction_id = ?", [prediction.prediction_id]
    ).fetchone()
    if already is not None:
        return None  # idempotent: this prediction already has a booked trade

    ensure_portfolio(conn, prediction.trader_id, prediction.trade_date)
    s = prediction.option_structure
    entry = _entry_from_structure(s)
    # option_bridge.py's traders are directional-long only (see its module
    # docstring) -- no live caller ever produces a 'short' structure, so this
    # stays long-only rather than carrying an unverifiable short-side branch.
    assert entry.side == "long", f"ledger only supports long entries, got {entry.side!r}"
    mid = _mid_entry_premium(entry, r=r)
    if mid <= 0:
        return None
    half = spread_pct / 2.0
    fill = mid * (1.0 + half)  # buyer pays the ask
    if fill <= 0:
        return None

    cash = _cash(conn, prediction.trader_id)
    scale = position_size_scale(prediction.conviction)
    budget = _sizing_budget(conn, prediction.trader_id) * MAX_POSITION_PCT * scale
    contracts = int(budget // (fill * 100))
    if contracts < 1:
        return None
    cost = contracts * fill * 100
    if cost > cash:  # guard against float edge cases at the boundary
        contracts = int(cash // (fill * 100))
        if contracts < 1:
            return None
        cost = contracts * fill * 100

    trade_id = str(uuid.uuid4())
    # Real US equity/ETF options only expire on Fridays (see
    # data/integrity_fixes.py's nearest_friday/realign_trader_trades_expiries,
    # written for exactly this class of bug) -- trade_date + dte_days in raw
    # calendar days lands on an arbitrary weekday, which stockmoney-refresh-live's
    # source-DB validation (data/validation.py) rejects wholesale, wedging the
    # live DB (2026-08-14 incident: this bug alone stalled the entire published
    # dashboard for a week even though upstream ingestion kept running fine).
    raw_expiry = date.fromordinal(prediction.trade_date.toordinal() + s["dte_days"])
    expiry_date = nearest_friday(raw_expiry, on_or_after=prediction.trade_date)
    conn.execute(
        """
        INSERT INTO trader_trades (
            trade_id, trader_id, symbol, option_right, side, strike, expiry_date, contracts,
            entry_at, entry_underlying, entry_premium, status, thesis, linked_prediction_id,
            quote_lag_minutes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
        """,
        [trade_id, prediction.trader_id, prediction.symbol, "call" if s["is_call"] else "put",
         entry.side, s["strike"], expiry_date, contracts,
         _as_of(prediction.trade_date), s["spot"], round(fill, 4),
         prediction.rationale, prediction.prediction_id,
         QUOTE_LAG_MINUTES, datetime.now(timezone.utc)],
    )
    conn.execute(
        "UPDATE trader_portfolios SET cash = cash - ?, updated_at = ? WHERE trader_id = ?",
        [cost, datetime.now(timezone.utc), prediction.trader_id],
    )
    return trade_id


def close_trade(
    conn: duckdb.DuckDBPyConnection, prediction: TraderPrediction,
    *, r: float = DEFAULT_RATE, spread_pct: float = DEFAULT_SPREAD_PCT,
) -> str | None:
    """Close the trade linked to a now-graded prediction, repricing the SAME
    stored contract at the actual exit spot (never a hindsight re-selection --
    same discipline as grading_options.grade_option_pnl). Returns the trade_id
    closed, or None if there was nothing open for this prediction (it was
    never opened -- no instrument or insufficient cash at the time -- or it is
    already closed)."""
    row = conn.execute(
        """
        SELECT trade_id, entry_premium, contracts, side
        FROM trader_trades WHERE linked_prediction_id = ? AND status = 'open'
        """,
        [prediction.prediction_id],
    ).fetchone()
    if row is None or prediction.option_structure is None or prediction.actual_price is None:
        return None
    trade_id, entry_fill, contracts, side = row
    assert side == "long", f"ledger only supports long entries, got {side!r}"

    s = prediction.option_structure
    entry = _entry_from_structure(s)
    days_held = max((prediction.label_end_date - prediction.trade_date).days, 1)
    exit_ = OptionExit(spot=prediction.actual_price, iv=None, days_held=days_held)
    mid = _mid_exit_premium(entry, exit_, r=r)
    half = spread_pct / 2.0
    fill = max(mid * (1.0 - half), 0.0)  # seller receives the bid

    per_share = fill - entry_fill
    realized_pnl = round(per_share * 100 * contracts, 2)
    proceeds = fill * 100 * contracts

    conn.execute(
        """
        UPDATE trader_trades
        SET exit_at = ?, exit_underlying = ?, exit_premium = ?, realized_pnl = ?,
            status = 'closed', exit_reason = 'horizon_reached'
        WHERE trade_id = ?
        """,
        [_as_of(prediction.label_end_date), prediction.actual_price, round(fill, 4), realized_pnl, trade_id],
    )
    conn.execute(
        "UPDATE trader_portfolios SET cash = cash + ?, updated_at = ? WHERE trader_id = ?",
        [proceeds, datetime.now(timezone.utc), prediction.trader_id],
    )
    return trade_id
