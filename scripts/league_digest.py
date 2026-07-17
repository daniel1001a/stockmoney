"""One-shot read-only JSON dump of the trader-league state for the morning
digest (the OpenClaw `stockmoney-scan-digest` pass reads this and writes a
short Traditional-Chinese summary the user gets over WhatsApp).

Deliberately read-only and self-contained -- it never writes, and it reuses the
same graded-only, no-look-ahead metrics the Arena page shows
(`stockmoney.league.league_table`), so the message can't disagree with the app.

Usage:
    uv run python scripts/league_digest.py

Prints ONE JSON object:
    {
      "standings":     [ {trader_id, name, n_graded, hit_rate, avg_pnl,
                          cum_option_pnl} ... ],   # ranked, best cum_option_pnl first
      "latest_calls":  [ {trader_id, trade_date, symbol, direction, conviction,
                          rationale} ... ],        # each trader's highest-conviction call on its latest day
      "new_proposal":  {trader_id, rationale} | null   # newest un-reviewed method-update proposal
    }
"""
from __future__ import annotations

import json

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations
from stockmoney.data.trader_methods import list_proposals
from stockmoney.league.league_table import league_equity_curves, league_table


def build_digest(conn) -> dict:
    # Standings: rolling hit/avg from league_table, cumulative option P&L from
    # the equity curves (last point). Keyed by trader_id, then ranked.
    table = {r["trader_id"]: r for r in league_table(conn, window=60)}
    curves = {c["trader_id"]: c for c in league_equity_curves(conn)}

    standings = []
    for trader_id, curve in curves.items():
        rolling = (table.get(trader_id) or {}).get("rolling", {})
        points = curve["points"]
        standings.append({
            "trader_id": trader_id,
            "name": curve["name"],
            "n_graded": rolling.get("n_graded", len(points)),
            "hit_rate": rolling.get("hit_rate"),
            "avg_pnl": rolling.get("avg_pnl"),
            "cum_option_pnl": points[-1]["cum_option_pnl"] if points else 0.0,
        })
    standings.sort(key=lambda s: s["cum_option_pnl"], reverse=True)

    # Each trader's single highest-conviction call on its most recent trade_date.
    latest_calls = []
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT trader_id, MAX(trade_date) AS d
            FROM trader_predictions GROUP BY trader_id
        )
        SELECT p.trader_id, CAST(p.trade_date AS VARCHAR), p.symbol, p.direction,
               p.conviction, p.rationale
        FROM trader_predictions p
        JOIN latest l ON p.trader_id = l.trader_id AND p.trade_date = l.d
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY p.trader_id ORDER BY p.conviction DESC NULLS LAST
        ) = 1
        """
    ).fetchall()
    for r in rows:
        latest_calls.append({
            "trader_id": r[0], "trade_date": r[1], "symbol": r[2],
            "direction": r[3], "conviction": r[4], "rationale": r[5],
        })

    proposals = list_proposals(conn, status="proposed")
    new_proposal = None
    if proposals:
        p = proposals[0]
        new_proposal = {"trader_id": p["trader_id"], "rationale": p["rationale"]}

    return {"standings": standings, "latest_calls": latest_calls, "new_proposal": new_proposal}


def main(*, db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    run_migrations(conn)
    try:
        print(json.dumps(build_digest(conn), default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
