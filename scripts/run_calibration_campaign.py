"""CLI for the weekend calibration campaign
(~/.claude/plans/frolicking-wishing-cook.md, CLAUDE.md section 16's
"not yet optimized" parameters).

Deliberately four separate steps, not one big "just do everything" command --
`confirm` consumes the untouched holdout tail and is designed to never
silently re-run (see calibration_campaign.run_confirmation), so a human
should be able to look at `shortlist` before committing to `confirm`.

Usage:
    uv run python scripts/run_calibration_campaign.py search [--campaign-id ID] [--symbols SOXL,NVDA]
    uv run python scripts/run_calibration_campaign.py shortlist --campaign-id ID [--symbol SOXL]
    uv run python scripts/run_calibration_campaign.py confirm --campaign-id ID [--symbol SOXL]
    uv run python scripts/run_calibration_campaign.py report --campaign-id ID
"""
from __future__ import annotations

import argparse
import sys

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations
from stockmoney.data.watchlist import sector_for_symbol
from stockmoney.models.calibration_campaign import (
    active_watchlist_symbols,
    new_campaign_id,
    run_confirmation,
    run_search_grid,
    shortlist_top_k,
)


def _resolve_symbols(conn, raw: str | None) -> list[tuple[str, str]]:
    if raw is None or raw == "all":
        return active_watchlist_symbols(conn)
    out = []
    for sym in raw.split(","):
        sym = sym.strip().upper()
        sector = sector_for_symbol(conn, sym)
        if sector is None:
            print(f"  {sym}: skipped (not resolvable to a feature-group sector)")
            continue
        out.append((sym, sector))
    return out


def _cmd_search(args: argparse.Namespace) -> int:
    conn = get_connection(args.db_path)
    run_migrations(conn)
    try:
        campaign_id = args.campaign_id or new_campaign_id()
        symbols = _resolve_symbols(conn, args.symbols)
        print(f"campaign_id: {campaign_id}")
        print(f"symbols: {[s for s, _ in symbols]}")
        counts = run_search_grid(conn, campaign_id=campaign_id, symbols=symbols)
        print(f"search done: {counts}")
        print(f"next: uv run python scripts/run_calibration_campaign.py shortlist --campaign-id {campaign_id}")
    finally:
        conn.close()
    return 0


def _cmd_shortlist(args: argparse.Namespace) -> int:
    conn = get_connection(args.db_path)
    run_migrations(conn)
    try:
        symbols = [args.symbol.upper()] if args.symbol else [s for s, _ in active_watchlist_symbols(conn)]
        for symbol in symbols:
            top = shortlist_top_k(conn, campaign_id=args.campaign_id, symbol=symbol)
            if not top:
                print(f"{symbol}: no configs passed the search criteria")
                continue
            print(f"{symbol}:")
            for params in top:
                print(f"  {params}")
    finally:
        conn.close()
    return 0


def _cmd_confirm(args: argparse.Namespace) -> int:
    conn = get_connection(args.db_path)
    run_migrations(conn)
    try:
        symbols = [args.symbol.upper()] if args.symbol else [s for s, sec in active_watchlist_symbols(conn)]
        for symbol in symbols:
            sector = sector_for_symbol(conn, symbol)
            top = shortlist_top_k(conn, campaign_id=args.campaign_id, symbol=symbol)
            if not top:
                continue
            for params in top:
                verdict = run_confirmation(
                    conn, campaign_id=args.campaign_id, symbol=symbol, sector=sector, params=params
                )
                if verdict is None:
                    print(f"{symbol} {params}: already confirmed this campaign, skipping")
                else:
                    print(f"{symbol} {params}: confirm -> {verdict}")
    finally:
        conn.close()
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    conn = get_connection(args.db_path)
    run_migrations(conn)
    try:
        search_n, search_passed = conn.execute(
            "SELECT count(*), sum(CASE WHEN criteria_passes THEN 1 ELSE 0 END) "
            "FROM calibration_runs WHERE campaign_id = ? AND phase = 'search'",
            [args.campaign_id],
        ).fetchone()
        confirm_n, confirm_passed = conn.execute(
            "SELECT count(*), sum(CASE WHEN criteria_passes THEN 1 ELSE 0 END) "
            "FROM calibration_runs WHERE campaign_id = ? AND phase = 'confirm'",
            [args.campaign_id],
        ).fetchone()
        proposed = conn.execute(
            "SELECT symbol, params FROM calibration_candidates WHERE campaign_id = ? AND status = 'proposed'",
            [args.campaign_id],
        ).fetchall()

        print(f"campaign_id: {args.campaign_id}")
        print(f"search: {search_n or 0} runs, {search_passed or 0} passed search criteria")
        print(f"confirm: {confirm_n or 0} runs, {confirm_passed or 0} passed confirmation")
        print(f"proposed candidates awaiting human review: {len(proposed)}")
        for symbol, params in proposed:
            print(f"  {symbol}: {params}")
    finally:
        conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="Tier 1 grid sweep, writes phase='search' rows")
    p_search.add_argument("--campaign-id", default=None, help="omit to start a new campaign")
    p_search.add_argument("--symbols", default="all", help="comma-separated symbols, or 'all'")
    p_search.set_defaults(func=_cmd_search)

    p_shortlist = sub.add_parser("shortlist", help="show top-K search-passing configs per symbol")
    p_shortlist.add_argument("--campaign-id", required=True)
    p_shortlist.add_argument("--symbol", default=None)
    p_shortlist.set_defaults(func=_cmd_shortlist)

    p_confirm = sub.add_parser("confirm", help="run the one-shot holdout confirmation for the shortlist")
    p_confirm.add_argument("--campaign-id", required=True)
    p_confirm.add_argument("--symbol", default=None)
    p_confirm.set_defaults(func=_cmd_confirm)

    p_report = sub.add_parser("report", help="summarize a campaign's search/confirm/candidate counts")
    p_report.add_argument("--campaign-id", required=True)
    p_report.set_defaults(func=_cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
