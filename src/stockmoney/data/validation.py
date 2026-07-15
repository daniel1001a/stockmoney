"""Data-integrity validation layer (2026-07-15 audit,
docs/data-integrity-audit-2026-07-15.md).

Pure functions that check one fact about one row and return a `Violation` (or
None if the row is clean) -- unit-testable with synthetic inputs and no
DuckDB connection at all. The `validate_*` functions below them are thin
DB-reading wrappers that fetch rows and run the pure checks over them, for use
against the live database (read-only) in tests and in an ad-hoc audit run.

Design intent: these functions **fail loudly** -- `assert_clean` raises with
every violation's detail rather than silently logging, so a broken build
can't ship a Dashboard full of impossible numbers unnoticed. Call
`assert_clean(validate_all(conn))` in a pre-deploy check or a pytest test
that must pass against the live DB.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import duckdb

from stockmoney.models.strike_ladder import is_on_ladder


@dataclass(frozen=True)
class Violation:
    table: str
    field: str
    row_id: str
    detail: str
    severity: str = "error"  # 'error' | 'warning'

    def __str__(self) -> str:
        return f"[{self.severity}] {self.table}.{self.field} row={self.row_id}: {self.detail}"


def assert_clean(violations: list[Violation]) -> None:
    """Raise loudly if `violations` is non-empty. Warnings alone do not
    raise -- only 'error' severity does; call sites that want zero-tolerance
    can filter first."""
    errors = [v for v in violations if v.severity == "error"]
    if errors:
        detail = "\n".join(str(v) for v in errors)
        raise AssertionError(f"{len(errors)} data-integrity violation(s):\n{detail}")


# --- pure per-row checks -----------------------------------------------------

def strike_violation(table: str, row_id: str, strike: float) -> Violation | None:
    """Strike must sit on the realistic listed-option ladder (strike_ladder.py)
    -- catches the flagship "impossible strike" bug (e.g. "NFLX 996.7 Call")."""
    if strike is None or strike <= 0:
        return Violation(table, "strike", row_id, f"strike must be positive, got {strike!r}")
    if not is_on_ladder(strike):
        return Violation(table, "strike", row_id, f"strike {strike} is not on a valid listed increment")
    return None


def expiry_violation(
    table: str, row_id: str, expiry_date: date, status: str, *, as_of: date | None = None
) -> Violation | None:
    """Real US equity/ETF options only expire on Fridays; an OPEN position's
    expiry must not already be in the past (that's a position that should
    have closed itself out, not a live open trade)."""
    if expiry_date is None:
        return Violation(table, "expiry_date", row_id, "expiry_date is NULL")
    if expiry_date.weekday() != 4:
        return Violation(
            table, "expiry_date", row_id,
            f"expiry_date {expiry_date} is a {expiry_date.strftime('%A')}, not a Friday",
        )
    as_of = as_of or datetime.now(timezone.utc).date()
    if status == "open" and expiry_date < as_of:
        return Violation(
            table, "expiry_date", row_id,
            f"status='open' but expiry_date {expiry_date} is before as_of {as_of}",
        )
    return None


def premium_violation(
    table: str, row_id: str, premium: float, *, spot: float, label: str = "entry_premium"
) -> Violation | None:
    """A long option's premium must be strictly between 0 and the underlying
    spot (a premium >= spot would mean paying more for optionality than the
    stock itself costs -- never happens for the strikes/tenors this system
    trades)."""
    if premium is None:
        return Violation(table, label, row_id, f"{label} is NULL")
    if not (0 < premium < spot):
        return Violation(
            table, label, row_id, f"{label}={premium} not in (0, spot={spot})",
        )
    return None


def future_timestamp_violation(
    table: str, row_id: str, field: str, ts: datetime, *, as_of: datetime | None = None
) -> Violation | None:
    """No timestamp may be dated after `as_of` (default: now) -- a future
    entry_at/created_at/published_at is a look-ahead-bias smoking gun."""
    if ts is None:
        return None
    as_of = as_of or datetime.now(timezone.utc)
    ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    as_of_utc = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
    if ts_utc > as_of_utc:
        return Violation(table, field, row_id, f"{field}={ts} is in the future (as_of={as_of})")
    return None


def realized_pnl_violation(
    table: str, row_id: str, *, side: str, contracts: int,
    entry_premium: float, exit_premium: float, realized_pnl: float, tol: float = 1.0,
) -> Violation | None:
    """realized_pnl (dollars) must equal (exit-entry) premium x 100 x
    contracts, sign-flipped for a short. `tol` allows for cent-level
    rounding in the stored premiums."""
    if side not in ("long", "short"):
        return Violation(table, "side", row_id, f"unknown side {side!r}")
    if side == "long":
        expected = (exit_premium - entry_premium) * 100 * contracts
    else:
        expected = (entry_premium - exit_premium) * 100 * contracts
    if abs(expected - realized_pnl) > tol:
        return Violation(
            table, "realized_pnl", row_id,
            f"realized_pnl={realized_pnl} but (side={side}, entry={entry_premium}, "
            f"exit={exit_premium}, contracts={contracts}) implies {round(expected, 2)}",
        )
    return None


def portfolio_stats_violation(
    table: str, row_id: str, *, n_closed: int, wins: int, losses: int, cum_pnl: float, trade_pnls: list[float],
) -> Violation | None:
    """win-rate bookkeeping sanity: wins+losses must reconcile with n_closed
    (allowing pushes -- exactly-zero-P&L trades that count as neither), and
    the sum of individual trade P&Ls must match the reported cumulative P&L."""
    if wins + losses > n_closed:
        return Violation(
            table, "n_closed", row_id, f"wins({wins}) + losses({losses}) > n_closed({n_closed})",
        )
    total = round(sum(trade_pnls), 2)
    if abs(total - round(cum_pnl, 2)) > 0.01 * max(1.0, abs(cum_pnl)):
        return Violation(
            table, "cum_pnl", row_id, f"sum(trade P&Ls)={total} != reported cum_pnl={cum_pnl}",
        )
    return None


# --- DB-reading wrappers (read-only; use against the live DB) ---------------

def validate_trader_trades(conn: duckdb.DuckDBPyConnection, *, as_of: date | None = None) -> list[Violation]:
    rows = conn.execute(
        """
        SELECT trade_id, strike, expiry_date, status, side, contracts,
               entry_underlying, entry_premium, entry_at, exit_underlying,
               exit_premium, exit_at, realized_pnl
        FROM trader_trades
        """
    ).fetchall()
    cols = [
        "trade_id", "strike", "expiry_date", "status", "side", "contracts",
        "entry_underlying", "entry_premium", "entry_at", "exit_underlying",
        "exit_premium", "exit_at", "realized_pnl",
    ]
    violations: list[Violation] = []
    for values in rows:
        r = dict(zip(cols, values))
        rid = r["trade_id"]
        v = strike_violation("trader_trades", rid, r["strike"])
        if v:
            violations.append(v)
        v = expiry_violation("trader_trades", rid, r["expiry_date"], r["status"], as_of=as_of)
        if v:
            violations.append(v)
        v = premium_violation("trader_trades", rid, r["entry_premium"], spot=r["entry_underlying"])
        if v:
            violations.append(v)
        if r["status"] == "closed" and r["exit_premium"] is not None and r["exit_underlying"] is not None:
            v = premium_violation(
                "trader_trades", rid, r["exit_premium"], spot=r["exit_underlying"], label="exit_premium",
            )
            if v:
                violations.append(v)
        v = future_timestamp_violation("trader_trades", rid, "entry_at", r["entry_at"])
        if v:
            violations.append(v)
        if r["exit_at"] is not None:
            v = future_timestamp_violation("trader_trades", rid, "exit_at", r["exit_at"])
            if v:
                violations.append(v)
        if r["status"] == "closed" and r["realized_pnl"] is not None and r["exit_premium"] is not None:
            v = realized_pnl_violation(
                "trader_trades", rid, side=r["side"], contracts=r["contracts"],
                entry_premium=r["entry_premium"], exit_premium=r["exit_premium"],
                realized_pnl=r["realized_pnl"],
            )
            if v:
                violations.append(v)
    return violations


def validate_option_positions(conn: duckdb.DuckDBPyConnection, *, as_of: date | None = None) -> list[Violation]:
    rows = conn.execute(
        "SELECT position_id, strike, expiry_date, status, entry_underlying_price, entry_premium "
        "FROM option_positions"
    ).fetchall()
    violations: list[Violation] = []
    for position_id, strike, expiry_date, status, spot, premium in rows:
        v = strike_violation("option_positions", position_id, strike)
        if v:
            violations.append(v)
        v = expiry_violation("option_positions", position_id, expiry_date, status, as_of=as_of)
        if v:
            violations.append(v)
        v = premium_violation("option_positions", position_id, premium, spot=spot)
        if v:
            violations.append(v)
    return violations


def validate_all(conn: duckdb.DuckDBPyConnection, *, as_of: date | None = None) -> list[Violation]:
    """Run every table-level check available. Extend as more tables are
    covered; keep each table's checks in its own validate_* function above so
    a single table can be tested/run in isolation too."""
    return [
        *validate_trader_trades(conn, as_of=as_of),
        *validate_option_positions(conn, as_of=as_of),
    ]
