"""Read-side helper: the entry-day ATM implied vol for one symbol, used by
league/option_bridge.py to price the option each trader's directional call
would actually trade.

Prefers REAL captured IV (iv_surface_daily, Agent 3's daily capture cron --
see NEXT_AGENT_PLAN.md), falling back to the same realized-vol proxy
backtest_options_pnl.py already documents and uses when no real history
exists yet for a symbol/day: `entry_iv = realized_vol_20d * IV_RV_RATIO`. Both
paths are point-in-time safe by construction -- the real path only ever reads
the row stamped with `trade_date` itself (never a later snapshot), and the
proxy path takes realized_vol_20d as already computed for that same
trade_date by the caller.
"""
from __future__ import annotations

from datetime import date

import duckdb

# IV typically trades a little above realized vol -- same v1 placeholder
# ratio backtest_options_pnl.py uses, kept here so there is exactly one
# definition (that module re-exports/duplicates the constant for its own
# batch-backtest use, but the intent and value must stay identical).
IV_RV_RATIO = 1.1
TARGET_DTE_DAYS = 30  # matches option_selection.SelectionParams' v1 default


def real_entry_iv_for_symbol(
    conn: duckdb.DuckDBPyConnection, symbol: str, trade_date: date, *, target_dte_days: int = TARGET_DTE_DAYS
) -> float | None:
    """The ATM ('50'-delta-bucket) implied vol captured for `symbol` ON
    `trade_date` itself, from whichever available expiry is closest to
    `target_dte_days` out. None if no real snapshot exists for that exact day
    (the common case until Agent 3's capture cron has accumulated history) --
    callers should fall back to entry_iv_proxy in that case, never reach for a
    different day's snapshot (that would silently change which contract was
    "chosen" after the fact)."""
    rows = conn.execute(
        """
        SELECT expiry_date, implied_vol
        FROM iv_surface_daily
        WHERE symbol = ? AND trade_date = ? AND delta_bucket = '50' AND implied_vol IS NOT NULL
        """,
        [symbol.upper(), trade_date],
    ).fetchall()
    if not rows:
        return None

    def _dte_gap(expiry_date: date) -> int:
        return abs((expiry_date - trade_date).days - target_dte_days)

    _, iv = min(rows, key=lambda r: _dte_gap(r[0]))
    return float(iv)


def entry_iv_proxy(realized_vol_20d: float) -> float:
    return realized_vol_20d * IV_RV_RATIO


def entry_iv_for_symbol(
    conn: duckdb.DuckDBPyConnection, symbol: str, trade_date: date, *, realized_vol_20d: float,
    target_dte_days: int = TARGET_DTE_DAYS,
) -> tuple[float, str]:
    """Real snapshot if one exists for this exact trade_date, else the
    realized-vol proxy. Returns (entry_iv, source) where source is 'real' or
    'proxy' -- callers/tests can assert which path fired without re-deriving
    it, and it doubles as an honest label to surface in engine_payload/UI
    later (CLAUDE.md §2's "self-estimated v1, label it as such" discipline)."""
    real = real_entry_iv_for_symbol(conn, symbol, trade_date, target_dte_days=target_dte_days)
    if real is not None:
        return real, "real"
    return entry_iv_proxy(realized_vol_20d), "proxy"
