"""CLI for the daily prediction ledger (CLAUDE.md section 11's spirit: an
honest, gradeable track record -- never a silent retraining feedback loop).
This CLI itself stays a manual/by-hand tool (not wired into OpenClaw cron).
Its core per-symbol logic (`daily_predictions.record_live_prediction`) is
also called automatically by `scripts/build_dashboard_snapshot.py` (wired
into nightly_refresh.py) so the FastAPI backend has a fresh prediction to
read every morning without a human running this by hand first -- `grade` and
`list` remain manual-only.

`record-watchlist` loops over every active watchlist member (both the
semiconductor and big_tech sector groups now have the cross-sectional
dispersion feature production.py needs); any symbol still missing sector
features gets skipped with a clear reason rather than failing the whole
batch.

Usage:
    uv run python scripts/daily_prediction_cli.py record --symbol SOXL --sector semiconductor
    uv run python scripts/daily_prediction_cli.py record-watchlist
    uv run python scripts/daily_prediction_cli.py grade
    uv run python scripts/daily_prediction_cli.py list [--status pending|graded]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from stockmoney.data.daily_predictions import (
    get_prediction,
    grade_prediction,
    list_pending_predictions,
    record_live_prediction,
)
from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations
from stockmoney.data.positions import price_on_date
from stockmoney.data.watchlist import sector_for_symbol
from stockmoney.models import production

GRADE_LOOKUP_GRACE_DAYS = 5  # calendar days to search forward if label_end_date lands on a holiday


def _active_watchlist_symbols(conn) -> list[str]:
    rows = conn.execute(
        "SELECT symbol FROM watchlist_members WHERE removed_date IS NULL ORDER BY symbol"
    ).fetchall()
    return [r[0] for r in rows]


def _record_one(conn, *, symbol: str, sector: str, horizon: int) -> str | None:
    prediction_id, skip_reason = record_live_prediction(conn, symbol=symbol, sector=sector, horizon=horizon)
    if prediction_id is None:
        print(f"  {symbol}: skipped ({skip_reason})")
        return None

    p = get_prediction(conn, prediction_id)
    print(f"  {symbol}: recorded {prediction_id} (as_of={p.trade_date}, "
          f"proba down/range/up={[round(x, 3) for x in p.proba]}, matures {p.label_end_date})")
    return prediction_id


def _cmd_record(args: argparse.Namespace) -> int:
    conn = get_connection(args.db_path)
    run_migrations(conn)
    try:
        pid = _record_one(conn, symbol=args.symbol.upper(), sector=args.sector, horizon=args.horizon)
    finally:
        conn.close()
    return 0 if pid else 1


def _cmd_record_watchlist(args: argparse.Namespace) -> int:
    conn = get_connection(args.db_path)
    run_migrations(conn)
    try:
        print("Recording predictions for the active watchlist:")
        for symbol in _active_watchlist_symbols(conn):
            sector = sector_for_symbol(conn, symbol)
            if sector is None:
                print(f"  {symbol}: skipped (not resolvable to a feature-group sector)")
                continue
            _record_one(conn, symbol=symbol, sector=sector, horizon=args.horizon)
    finally:
        conn.close()
    return 0


def _cmd_grade(args: argparse.Namespace) -> int:
    conn = get_connection(args.db_path)
    run_migrations(conn)
    try:
        pending = list_pending_predictions(conn, as_of=date.today())
        if not pending:
            print("no matured predictions to grade")
            return 0

        for p in pending:
            actual_price = price_on_date(conn, p.symbol, p.label_end_date)
            checked_date = p.label_end_date
            if actual_price is None:
                for offset in range(1, GRADE_LOOKUP_GRACE_DAYS + 1):
                    checked_date = p.label_end_date + timedelta(days=offset)
                    actual_price = price_on_date(conn, p.symbol, checked_date)
                    if actual_price is not None:
                        break

            if actual_price is None:
                print(f"  {p.symbol} {p.prediction_id}: no price yet for {p.label_end_date} "
                      f"(or the {GRADE_LOOKUP_GRACE_DAYS}-day grace window) -- still pending")
                continue

            grade_prediction(conn, p.prediction_id, actual_price=actual_price)
            print(f"  {p.symbol} {p.prediction_id}: graded using {checked_date} close={actual_price}")
    finally:
        conn.close()
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    conn = get_connection(args.db_path)
    run_migrations(conn)
    try:
        if args.status == "pending":
            rows = list_pending_predictions(conn, as_of=date(9999, 12, 31))
            for p in rows:
                print(f"{p.prediction_id}  {p.symbol}  {p.trade_date} -> {p.label_end_date}  "
                      f"{p.predicted_direction}  status={p.status}")
            return 0

        where = "WHERE status = 'graded'" if args.status == "graded" else ""
        rows = conn.execute(
            f"SELECT prediction_id, trade_date, symbol, predicted_direction, status, outcome "
            f"FROM daily_predictions {where} ORDER BY trade_date DESC"
        ).fetchall()
        for r in rows:
            print(r)
    finally:
        conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    p_record = sub.add_parser("record", help="record today's prediction for one symbol")
    p_record.add_argument("--symbol", required=True)
    p_record.add_argument("--sector", required=True)
    p_record.add_argument("--horizon", type=int, default=production.DEFAULT_HORIZON)
    p_record.set_defaults(func=_cmd_record)

    p_watchlist = sub.add_parser("record-watchlist", help="record predictions for every active watchlist symbol")
    p_watchlist.add_argument("--horizon", type=int, default=production.DEFAULT_HORIZON)
    p_watchlist.set_defaults(func=_cmd_record_watchlist)

    p_grade = sub.add_parser("grade", help="grade matured pending predictions")
    p_grade.set_defaults(func=_cmd_grade)

    p_list = sub.add_parser("list", help="list predictions")
    p_list.add_argument("--status", choices=["pending", "graded", "all"], default="all")
    p_list.set_defaults(func=_cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
