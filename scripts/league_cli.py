"""CLI for the Trader League Arena (~/.claude/plans/trader-council-
rearchitecture.md, worker-1 arena). Mirrors daily_prediction_cli's style: a
by-hand tool. `predict` and `review` are also wired into nightly_refresh.py so
the league advances unattended; `grade` stays a deliberate manual step (same
discipline as the daily prediction ledger).

Usage:
    uv run python scripts/league_cli.py predict
    uv run python scripts/league_cli.py grade
    uv run python scripts/league_cli.py review
    uv run python scripts/league_cli.py table [--window 20] [--cost-bps 5]
    uv run python scripts/league_cli.py traders [--all]
    uv run python scripts/league_cli.py proposals [--status proposed]
"""
from __future__ import annotations

import argparse
import sys

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations
from stockmoney.data.trader_methods import list_proposals
from stockmoney.data.traders import list_active_traders, list_all_traders
from stockmoney.league import review as review_mod
from stockmoney.league.league_table import league_table
from stockmoney.league.orchestration import grade_matured, run_predictions


def _with_conn(args):
    conn = get_connection(args.db_path)
    run_migrations(conn)
    return conn


def _cmd_predict(args) -> int:
    conn = _with_conn(args)
    try:
        summary = run_predictions(conn, horizon=args.horizon)
        for trader in list_active_traders(conn):
            s = summary[trader.trader_id]
            print(f"  {trader.trader_id}: recorded {s['recorded']}, skipped {s['skipped']}")
        if args.verbose:
            for line in summary["skips"]:
                print(f"    skip: {line}")
    finally:
        conn.close()
    return 0


def _cmd_grade(args) -> int:
    conn = _with_conn(args)
    try:
        print(f"grade: {grade_matured(conn)}")
    finally:
        conn.close()
    return 0


def _cmd_review(args) -> int:
    conn = _with_conn(args)
    try:
        print(f"review: {review_mod.run_review(conn)}")
    finally:
        conn.close()
    return 0


def _fmt(x, nd=3):
    return "  -  " if x is None else f"{x:.{nd}f}"


def _cmd_table(args) -> int:
    conn = _with_conn(args)
    try:
        rows = league_table(conn, window=args.window, cost_bps=args.cost_bps)
        print(f"{'trader':<10} {'n':>4} {'hit':>6} {'brier':>6} {'avgPnL':>8} {'hiConvP':>8}  (rolling/{args.window})")
        for r in rows:
            roll = r["rolling"]
            print(f"{r['trader_id']:<10} {roll['n_graded']:>4} {_fmt(roll['hit_rate'],2):>6} "
                  f"{_fmt(roll['brier']):>6} {_fmt(roll['avg_pnl'],4):>8} "
                  f"{_fmt(roll['high_conviction_precision'],2):>8}")
            if args.regime:
                for regime, s in r["by_regime"].items():
                    print(f"    regime {regime}: n={s['n_graded']} hit={_fmt(s['hit_rate'],2)} "
                          f"brier={_fmt(s['brier'])}")
    finally:
        conn.close()
    return 0


def _cmd_traders(args) -> int:
    conn = _with_conn(args)
    try:
        traders = list_all_traders(conn) if args.all else list_active_traders(conn)
        for t in traders:
            flag = "" if t.active else " (retired)"
            print(f"  {t.trader_id}{flag}: {t.name} [engine={t.engine_key}]\n      {t.philosophy}")
    finally:
        conn.close()
    return 0


def _cmd_proposals(args) -> int:
    conn = _with_conn(args)
    try:
        for p in list_proposals(conn, status=args.status):
            print(f"  {p['proposal_id'][:8]} {p['trader_id']} {p['status']} from={p['from_version']}")
            print(f"      {p['rationale']}")
    finally:
        conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("predict", help="run every active trader over the watchlist")
    p.add_argument("--horizon", type=int, default=5)
    p.add_argument("--verbose", action="store_true", help="print skip reasons")
    p.set_defaults(func=_cmd_predict)

    p = sub.add_parser("grade", help="grade matured predictions (manual)")
    p.set_defaults(func=_cmd_grade)

    p = sub.add_parser("review", help="run per-trader review + divergence + method proposals")
    p.set_defaults(func=_cmd_review)

    p = sub.add_parser("table", help="print the league table")
    p.add_argument("--window", type=int, default=20)
    p.add_argument("--cost-bps", type=float, default=0.0)
    p.add_argument("--regime", action="store_true", help="also break down per regime")
    p.set_defaults(func=_cmd_table)

    p = sub.add_parser("traders", help="list traders")
    p.add_argument("--all", action="store_true", help="include retired traders")
    p.set_defaults(func=_cmd_traders)

    p = sub.add_parser("proposals", help="list method-update proposals")
    p.add_argument("--status", choices=["proposed", "accepted", "rejected"], default=None)
    p.set_defaults(func=_cmd_proposals)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
