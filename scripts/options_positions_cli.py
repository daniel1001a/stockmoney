"""CLI for logging option positions and checking their risk-control lights
(CLAUDE.md section 10.1). No dashboard exists yet, so this is today's
interface: enter a position when you open it, run `check` daily against the
current premium you see on your broker screen.

Usage:
    uv run python scripts/options_positions_cli.py open \\
        --symbol SOXL --right call --side long --strike 180 \\
        --expiry 2026-09-18 --entry-date 2026-07-10 \\
        --entry-underlying-price 174.82 --entry-premium 12.50 \\
        --entry-iv 0.55 --thesis "semis breaking out of range regime"

    uv run python scripts/options_positions_cli.py list

    uv run python scripts/options_positions_cli.py check --position-id <id> \\
        --current-premium 9.80

    uv run python scripts/options_positions_cli.py close --position-id <id> \\
        --reason "hit premium stop"
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations
from stockmoney.data.positions import (
    close_position,
    get_position,
    latest_underlying_price,
    list_open_positions,
    open_position,
)
from stockmoney.data.watchlist import sector_for_symbol
from stockmoney.models import production
from stockmoney.models.options_risk import MarketSnapshot, assess_position

LIGHT_EMOJI = {"green": "\U0001F7E2", "yellow": "\U0001F7E1", "red": "\U0001F534"}


def _cmd_open(args: argparse.Namespace) -> int:
    conn = get_connection(args.db_path)
    run_migrations(conn)
    try:
        regime_at_entry = args.regime_at_entry
        if regime_at_entry is None:
            sector = sector_for_symbol(conn, args.symbol)
            if sector is not None:
                pred = production.predict_latest(conn, target_symbol=args.symbol, sector=sector)
                regime_at_entry = pred.regime if pred is not None else None

        position_id = open_position(
            conn,
            symbol=args.symbol,
            option_right=args.right,
            side=args.side,
            strike=args.strike,
            expiry_date=date.fromisoformat(args.expiry),
            entry_date=date.fromisoformat(args.entry_date),
            entry_underlying_price=args.entry_underlying_price,
            entry_premium=args.entry_premium,
            entry_iv=args.entry_iv,
            regime_at_entry=regime_at_entry,
            thesis_note=args.thesis,
        )
    finally:
        conn.close()
    print(f"opened position {position_id}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    conn = get_connection(args.db_path)
    run_migrations(conn)
    try:
        positions = list_open_positions(conn)
    finally:
        conn.close()
    if not positions:
        print("no open positions")
        return 0
    for p in positions:
        print(
            f"{p.position_id}  {p.symbol} {p.option_right} {p.side}  "
            f"entry_date={p.entry_date} entry_premium={p.entry_premium}"
        )
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    conn = get_connection(args.db_path)
    run_migrations(conn)
    try:
        position = get_position(conn, args.position_id)
        if position is None:
            print(f"unknown position_id: {args.position_id}", file=sys.stderr)
            return 1

        as_of_date = date.today()
        underlying_price = args.underlying_price
        if underlying_price is None:
            latest = latest_underlying_price(conn, position.symbol)
            if latest is None:
                print(f"no ohlcv_daily rows for {position.symbol}; pass --underlying-price", file=sys.stderr)
                return 1
            as_of_date, underlying_price = latest

        current_regime = args.current_regime
        ev_of_continuing = args.ev_of_continuing
        if current_regime is None or ev_of_continuing is None:
            sector = sector_for_symbol(conn, position.symbol)
            if sector is None:
                print(f"  (auto-fill skipped: {position.symbol} is not an active watchlist member)")
            else:
                if current_regime is None:
                    pred = production.predict_latest(conn, target_symbol=position.symbol, sector=sector)
                    current_regime = pred.regime if pred is not None else None
                if args.ev_of_continuing is None:
                    ev_of_continuing = production.current_ev_of_continuing(
                        conn, target_symbol=position.symbol, sector=sector, asof_date=as_of_date
                    )
                    if ev_of_continuing is not None:
                        print("  (note: ev_of_continuing reflects module A/B's own historical "
                              "directional calls, not this specific option contract)")
    finally:
        conn.close()

    snapshot = MarketSnapshot(
        as_of_date=as_of_date,
        underlying_price=underlying_price,
        current_premium=args.current_premium,
        current_regime=current_regime,
        ev_of_continuing=ev_of_continuing,
    )
    result = assess_position(position, snapshot)

    print(f"{LIGHT_EMOJI[result.light]} {position.symbol} {position.option_right} {position.side} -- {result.light}")
    for t in result.triggers:
        print(f"  [{t.kind}] {t.light}: {t.detail}")
    for n in result.notes:
        print(f"  (skipped) {n}")
    return 0


def _cmd_close(args: argparse.Namespace) -> int:
    conn = get_connection(args.db_path)
    run_migrations(conn)
    try:
        close_position(conn, args.position_id, reason=args.reason)
    finally:
        conn.close()
    print(f"closed position {args.position_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    p_open = sub.add_parser("open", help="log a new position")
    p_open.add_argument("--symbol", required=True)
    p_open.add_argument("--right", required=True, choices=["call", "put"])
    p_open.add_argument("--side", required=True, choices=["long", "short"])
    p_open.add_argument("--strike", required=True, type=float)
    p_open.add_argument("--expiry", required=True, help="YYYY-MM-DD")
    p_open.add_argument("--entry-date", required=True, help="YYYY-MM-DD")
    p_open.add_argument("--entry-underlying-price", required=True, type=float)
    p_open.add_argument("--entry-premium", required=True, type=float)
    p_open.add_argument("--entry-iv", type=float, default=None)
    p_open.add_argument("--regime-at-entry", type=int, default=None,
                         help="defaults to production.predict_latest's current regime, if the symbol is on the watchlist")
    p_open.add_argument("--thesis", default=None)
    p_open.set_defaults(func=_cmd_open)

    p_list = sub.add_parser("list", help="list open positions")
    p_list.set_defaults(func=_cmd_list)

    p_check = sub.add_parser("check", help="evaluate risk-control lights for a position")
    p_check.add_argument("--position-id", required=True)
    p_check.add_argument("--underlying-price", type=float, default=None, help="defaults to latest ohlcv_daily close")
    p_check.add_argument("--current-premium", type=float, default=None)
    p_check.add_argument("--current-regime", type=int, default=None)
    p_check.add_argument("--ev-of-continuing", type=float, default=None)
    p_check.set_defaults(func=_cmd_check)

    p_close = sub.add_parser("close", help="close a position")
    p_close.add_argument("--position-id", required=True)
    p_close.add_argument("--reason", default=None)
    p_close.set_defaults(func=_cmd_close)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
