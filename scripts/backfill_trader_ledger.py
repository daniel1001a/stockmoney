"""One-time replay of trader_predictions history into the paper-trading ledger
(trader_portfolios/trader_trades). Going forward, league/orchestration.py books
each trade live as predict/grade run; this script exists only to catch up
history recorded before league/ledger.py existed (2026-07-09 onward on the
live DB) so the Arena's leaderboard equity, per-trader trade history, and Live
Board aren't empty for weeks of real predictions that already happened.

Safe to re-run: ledger.open_trade/close_trade are both idempotent (they check
for an existing linked trade before booking), so replaying an already-booked
prediction is a no-op.

Replays each trader's own history independently, in true chronological order
(a trade's open event on trade_date, its close event on label_end_date if
already graded) rather than open-everything-then-close-everything, so cash
recycles the way it actually would have: closing a matured trade frees cash
before a same-day or later open competes for it. Getting this ordering wrong
would understate how many trades a trader could actually afford.

Usage:
    uv run python scripts/backfill_trader_ledger.py [--db-path PATH]
"""
from __future__ import annotations

import argparse

from stockmoney.data import trader_predictions as tp
from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations
from stockmoney.data.traders import list_all_traders
from stockmoney.league import ledger


def _events_for_trader(conn, trader_id: str) -> list[tuple]:
    """(event_date, priority, kind, prediction) sorted so same-day closes run
    before opens (priority 0 vs 1) and ties otherwise keep trade_date order."""
    events = []
    for p in tp.list_predictions_for_trader(conn, trader_id):
        if p.option_structure is None:
            continue  # a 'range' call or no usable entry IV -- never booked
        events.append((p.trade_date, 1, "open", p))
        if p.status == "graded" and p.actual_price is not None:
            events.append((p.label_end_date, 0, "close", p))
    events.sort(key=lambda e: (e[0], e[1]))
    return events


def backfill(conn, *, verbose: bool = False) -> dict:
    summary: dict = {}
    for trader in list_all_traders(conn):
        opened = closed = 0
        for _date, _prio, kind, prediction in _events_for_trader(conn, trader.trader_id):
            if kind == "open":
                if ledger.open_trade(conn, prediction) is not None:
                    opened += 1
            else:
                if ledger.close_trade(conn, prediction) is not None:
                    closed += 1
        row = conn.execute(
            "SELECT cash FROM trader_portfolios WHERE trader_id = ?", [trader.trader_id]
        ).fetchone()
        cash = row[0] if row else None
        n_open = conn.execute(
            "SELECT count(*) FROM trader_trades WHERE trader_id = ? AND status = 'open'", [trader.trader_id]
        ).fetchone()[0]
        summary[trader.trader_id] = {"opened": opened, "closed": closed, "cash": cash, "n_open": n_open}
        if verbose:
            print(f"  {trader.trader_id}: opened {opened}, closed {closed}, "
                  f"cash={cash}, {n_open} still open")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    conn = get_connection(args.db_path)
    run_migrations(conn)
    try:
        summary = backfill(conn, verbose=True)
    finally:
        conn.close()
    total_opened = sum(s["opened"] for s in summary.values())
    total_closed = sum(s["closed"] for s in summary.values())
    print(f"total: opened {total_opened}, closed {total_closed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
