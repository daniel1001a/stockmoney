"""Idempotent, read-then-write migrations for existing bad rows found by the
2026-07-15 data-integrity audit (docs/data-integrity-audit-2026-07-15.md).

These are NOT schema migrations (see data/db.py's run_migrations / the
data/migrations/*.sql immutable-once-applied convention) -- they are one-off
data-repair passes over rows that predate strike_ladder.py existing, callable
from a one-line script or a REPL. Each function:

  - only UPDATEs the specific column(s) named in its docstring;
  - is idempotent (running it twice does nothing the second time);
  - never touches a column it can't prove is uncorrelated with the one it's
    fixing -- see each function's docstring for the specific argument.

**Why strike-only, not premium/PnL, for trader_trades/option_positions**: in
this codebase entry_premium/exit_premium/realized_pnl for these two tables are
never computed from `strike` (confirmed by reading scripts/seed_demo_data.py
and api/queries.py -- premium is `entry_underlying * a small random pct`, and
the P&L math in api/queries.py._rough_mark / _portfolio_stats keys off
entry_underlying/entry_premium/exit_premium only, never `strike`). So
correcting `strike` here does not create any new premium/strike inconsistency
to repair -- there wasn't a strike->premium dependency to begin with. Contrast
this with the REAL pipeline (league/option_bridge.py -> league/
grading_options.py), where `strike` genuinely drives the Black-Scholes
premium at grading time; that path is fixed at the source (option_bridge.py
now calls select_option(..., snap=True)) rather than patched after the fact,
and grading always reprices fresh from the stored strike, so there is nothing
to migrate there once a row is graded.

**Why option_pnl-graded trader_predictions rows are left alone**: if
option_pnl is already set, the strike was already priced into that stored
result. Retroactively changing the strike after grading would silently
change what the "profitable" or "losing" trade actually contained --
info-integrity-wise indistinguishable from rewriting history. These functions
skip such rows and report them separately so a human can decide.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import duckdb

from stockmoney.models.strike_ladder import snap_strike


def snap_trader_trades_strikes(conn: duckdb.DuckDBPyConnection) -> int:
    """UPDATE trader_trades SET strike = snap_strike(strike) wherever the
    stored strike isn't already on the ladder. Returns the number of rows
    changed. Touches ONLY the `strike` column."""
    rows = conn.execute("SELECT trade_id, strike FROM trader_trades").fetchall()
    updated = 0
    for trade_id, strike in rows:
        snapped = snap_strike(strike)
        if snapped != strike:
            conn.execute("UPDATE trader_trades SET strike = ? WHERE trade_id = ?", [snapped, trade_id])
            updated += 1
    return updated


def snap_option_positions_strikes(conn: duckdb.DuckDBPyConnection) -> int:
    """Same as snap_trader_trades_strikes but for option_positions (the
    'human/discretion-layer' position ledger, CLAUDE.md section 7). Touches
    ONLY the `strike` column."""
    rows = conn.execute("SELECT position_id, strike FROM option_positions").fetchall()
    updated = 0
    for position_id, strike in rows:
        snapped = snap_strike(strike)
        if snapped != strike:
            conn.execute(
                "UPDATE option_positions SET strike = ? WHERE position_id = ?", [snapped, position_id]
            )
            updated += 1
    return updated


def snap_trader_predictions_option_structures(
    conn: duckdb.DuckDBPyConnection,
) -> tuple[int, list[str]]:
    """Patch the `strike` key inside trader_predictions.option_structure JSON
    for any row where it's off-ladder. Returns (n_updated, skipped_prediction_ids)
    -- rows already graded (option_pnl IS NOT NULL) are skipped and returned
    for a human decision, never silently mutated, since the stored option_pnl
    was priced from the un-snapped strike."""
    rows = conn.execute(
        """
        SELECT prediction_id, option_structure, option_pnl
        FROM trader_predictions
        WHERE option_structure IS NOT NULL
        """
    ).fetchall()
    updated = 0
    skipped: list[str] = []
    for prediction_id, raw, option_pnl in rows:
        structure = json.loads(raw)
        strike = structure.get("strike")
        if strike is None:
            continue
        snapped = snap_strike(strike)
        if snapped == strike:
            continue
        if option_pnl is not None:
            skipped.append(prediction_id)
            continue
        structure["strike"] = snapped
        conn.execute(
            "UPDATE trader_predictions SET option_structure = ? WHERE prediction_id = ?",
            [json.dumps(structure), prediction_id],
        )
        updated += 1
    return updated, skipped


def nearest_friday(d: date, *, on_or_after: date | None = None) -> date:
    """The nearest Friday to `d` (real US equity options only expire on
    Fridays). Ties round forward. If `on_or_after` is given, the result is
    walked forward in 7-day steps until it's >= that floor -- used so
    realigning an expiry can never move it before a date it must not precede
    (e.g. a closed trade's own exit date, or "today" for an open position)."""
    weekday = d.weekday()  # Mon=0 .. Fri=4 .. Sun=6
    forward = (4 - weekday) % 7
    backward = (weekday - 4) % 7
    candidate = d + timedelta(days=forward) if forward <= backward else d - timedelta(days=backward)
    if on_or_after is not None:
        while candidate < on_or_after:
            candidate += timedelta(days=7)
    return candidate


def realign_trader_trades_expiries(
    conn: duckdb.DuckDBPyConnection, *, as_of: date | None = None
) -> int:
    """UPDATE trader_trades SET expiry_date = nearest_friday(expiry_date) for
    every row whose expiry_date isn't a Friday (real US equity/ETF options
    only expire on Fridays -- CLAUDE.md's option-risk discipline assumes
    realizable contracts throughout). Touches ONLY the `expiry_date` column.

    A closed trade's realigned expiry is floored at its own exit_at date (an
    option can't have expired before the trade that used it was closed); an
    open trade's is floored at `as_of` (default: today) so realigning never
    manufactures an "expired but still open" row.

    NB timezone trap: entry_at/exit_at are TIMESTAMPTZ. DuckDB returns them
    converted to the session's local timezone (whatever the OS locale is --
    America/New_York on this dev machine), so a naive `.date()` on a
    UTC-midnight timestamp silently rolls back to the previous calendar day
    (verified: inserting datetime(2026,6,20,tzinfo=utc) reads back as
    2026-06-19 20:00-04:00, `.date()` -> 2026-06-19). Always go through
    `.astimezone(timezone.utc).date()` here so the floor matches the calendar
    day the row was actually written for.
    """
    as_of = as_of or datetime.now(timezone.utc).date()
    rows = conn.execute(
        "SELECT trade_id, expiry_date, entry_at, exit_at, status FROM trader_trades"
    ).fetchall()
    updated = 0
    for trade_id, expiry, entry_at, exit_at, status in rows:
        if status == "closed" and exit_at is not None:
            floor = exit_at.astimezone(timezone.utc).date()
        else:
            floor = max(entry_at.astimezone(timezone.utc).date(), as_of)
        realigned = nearest_friday(expiry, on_or_after=floor)
        if realigned != expiry:
            conn.execute(
                "UPDATE trader_trades SET expiry_date = ? WHERE trade_id = ?", [realigned, trade_id]
            )
            updated += 1
    return updated


def run_all_strike_and_expiry_fixes(conn: duckdb.DuckDBPyConnection) -> dict:
    """Convenience entry point running every fix in this module. Safe to call
    repeatedly (idempotent). Returns a summary dict for logging.

    Run against a live DB with:
        .venv/bin/python -c "
        import duckdb
        from stockmoney.data.integrity_fixes import run_all_strike_and_expiry_fixes
        conn = duckdb.connect('data/stockmoney.duckdb')
        print(run_all_strike_and_expiry_fixes(conn))
        conn.close()
        "
    """
    trades_strikes = snap_trader_trades_strikes(conn)
    positions_strikes = snap_option_positions_strikes(conn)
    predictions_strikes, skipped_graded = snap_trader_predictions_option_structures(conn)
    expiries = realign_trader_trades_expiries(conn)
    return {
        "trader_trades_strikes_snapped": trades_strikes,
        "option_positions_strikes_snapped": positions_strikes,
        "trader_predictions_strikes_snapped": predictions_strikes,
        "trader_predictions_skipped_already_graded": skipped_graded,
        "trader_trades_expiries_realigned": expiries,
    }
