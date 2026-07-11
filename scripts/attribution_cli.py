"""CLI for the daily attribution / review engine (CLAUDE.md section 11).

A read-only analysis side-branch: it reads graded predictions + realized
prices and writes `attribution_log` (finished facts) plus, only for the
"wrong but a signal existed" case, `proposed` rows into `feature_candidates`
for human review. It never rewrites labels, never touches the model, and
never auto-promotes a candidate into the feature store.

`run` is idempotent and is also wired into scripts/nightly_refresh.py (after
grading) so attribution accrues automatically each night. `list` and
`candidates` are read-only inspection helpers.

Usage:
    uv run python scripts/attribution_cli.py run
    uv run python scripts/attribution_cli.py list [--verdict wrong_signal_existed]
    uv run python scripts/attribution_cli.py candidates [--status proposed]
"""
from __future__ import annotations

import argparse
import sys

from stockmoney.data.attribution import run_attribution
from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations


def _cmd_run(args: argparse.Namespace) -> int:
    conn = get_connection(args.db_path)
    run_migrations(conn)
    try:
        summary = run_attribution(conn)
        print("Attribution run complete:")
        for key in ("right_reason_right", "wrong_noise", "wrong_signal_existed", "skipped", "candidates_proposed"):
            print(f"  {key}: {summary.get(key, 0)}")
    finally:
        conn.close()
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    conn = get_connection(args.db_path)
    run_migrations(conn)
    try:
        where, params = "", []
        if args.verdict:
            where = "WHERE verdict_class = ?"
            params.append(args.verdict)
        rows = conn.execute(
            f"""
            SELECT attribution_date, symbol, predicted_direction, actual_return,
                   attr_macro, attr_sector, attr_idiosyncratic, event_tags, verdict_class
            FROM attribution_log {where}
            ORDER BY attribution_date DESC, symbol
            """,
            params,
        ).fetchall()
        if not rows:
            print("no attribution_log rows yet")
            return 0
        for d, sym, pdir, ret, macro, sector, idio, tags, verdict in rows:
            print(
                f"{d}  {sym:5}  pred={pdir:5}  ret={ret:+.2%}  "
                f"macro={macro:+.2%} sector={sector:+.2%} idio={idio:+.2%}  "
                f"tags=[{tags}]  -> {verdict}"
            )
    finally:
        conn.close()
    return 0


def _cmd_candidates(args: argparse.Namespace) -> int:
    conn = get_connection(args.db_path)
    run_migrations(conn)
    try:
        where, params = "", []
        if args.status:
            where = "WHERE status = ?"
            params.append(args.status)
        rows = conn.execute(
            f"""
            SELECT proposed_date, proposed_feature_name, status, source_attribution_date, hypothesis
            FROM feature_candidates {where}
            ORDER BY proposed_date DESC
            """,
            params,
        ).fetchall()
        if not rows:
            print("no feature_candidates yet")
            return 0
        for proposed_date, name, status, src, hypothesis in rows:
            print(f"[{status}] {name}  (from {src}, proposed {proposed_date})")
            print(f"    {hypothesis}")
    finally:
        conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="attribute all graded predictions (idempotent)")
    p_run.set_defaults(func=_cmd_run)

    p_list = sub.add_parser("list", help="list attribution_log rows")
    p_list.add_argument(
        "--verdict",
        choices=["right_reason_right", "wrong_noise", "wrong_signal_existed"],
        default=None,
    )
    p_list.set_defaults(func=_cmd_list)

    p_cand = sub.add_parser("candidates", help="list proposed feature candidates")
    p_cand.add_argument(
        "--status", choices=["proposed", "under_review", "accepted", "rejected"], default=None
    )
    p_cand.set_defaults(func=_cmd_candidates)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
