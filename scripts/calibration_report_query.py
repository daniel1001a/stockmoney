"""Read-only, no-argument aggregation script for the OpenClaw calibration
narrator pass (see skills/stockmoney-scanner/SKILL.md's 4th pass). Reads
calibration_runs/calibration_candidates and prints compact JSON to stdout for
the most recent campaign -- deliberately never the raw per-row data (a
campaign can be thousands of rows), just enough for an LLM to write a short
human-readable progress note from.

Fixed, no-argument entry point so it can be exactly allowlisted in
~/.openclaw/exec-approvals.json (same reasoning as
scripts/scanner_check_watchlist.py -- inline `python -c` can't be precisely
allowlisted).

Usage:
    uv run python scripts/calibration_report_query.py
"""
from __future__ import annotations

import json

import duckdb

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations


def build_report(conn: duckdb.DuckDBPyConnection) -> dict:
    latest = conn.execute(
        "SELECT campaign_id, min(started_at), max(finished_at) FROM calibration_runs "
        "GROUP BY campaign_id ORDER BY max(finished_at) DESC LIMIT 1"
    ).fetchone()
    if latest is None:
        return {"campaign_id": None, "message": "no calibration campaign has run yet"}

    campaign_id, started_at, finished_at = latest

    search_n, search_passed = conn.execute(
        "SELECT count(*), sum(CASE WHEN criteria_passes THEN 1 ELSE 0 END) "
        "FROM calibration_runs WHERE campaign_id = ? AND phase = 'search'",
        [campaign_id],
    ).fetchone()
    confirm_n, confirm_passed = conn.execute(
        "SELECT count(*), sum(CASE WHEN criteria_passes THEN 1 ELSE 0 END) "
        "FROM calibration_runs WHERE campaign_id = ? AND phase = 'confirm'",
        [campaign_id],
    ).fetchone()
    rejected_n = conn.execute(
        "SELECT count(*) FROM calibration_candidates WHERE campaign_id = ? AND status = 'rejected'",
        [campaign_id],
    ).fetchone()[0]

    proposed = []
    for symbol, params_json, ev_kelly_json in conn.execute(
        "SELECT symbol, params, ev_kelly_json FROM calibration_candidates "
        "WHERE campaign_id = ? AND status = 'proposed'",
        [campaign_id],
    ).fetchall():
        entry = {"symbol": symbol, "params": json.loads(params_json)}
        if ev_kelly_json:
            ev_grid = json.loads(ev_kelly_json)["ev_grid"]
            best = max(ev_grid, key=lambda g: (g["passed_win_rate"] or 0))
            entry["best_ev_gate"] = {
                "window": best["window"], "quantile": best["quantile"],
                "passed_win_rate": best["passed_win_rate"], "passed_n": best["passed_n"],
            }
        proposed.append(entry)

    return {
        "campaign_id": campaign_id,
        "started_at": str(started_at),
        "last_activity_at": str(finished_at),
        "search": {"runs": search_n or 0, "passed_search_criteria": search_passed or 0},
        "confirm": {
            "runs": confirm_n or 0, "passed_holdout": confirm_passed or 0, "failed_holdout": rejected_n,
        },
        "proposed_candidates_awaiting_review": proposed,
    }


def main(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    run_migrations(conn)
    try:
        print(json.dumps(build_report(conn), default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
