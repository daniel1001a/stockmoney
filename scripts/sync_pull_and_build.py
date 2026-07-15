"""Bring THIS machine's dashboard up to date from git-synced data.

Two-machine setup (see docs/two-machine-sync-zh.md)
---------------------------------------------------
Only the OpenClaw machine ingests raw data (news / OHLCV / options / macro) and
runs the LLM catalyst chain. It ships those raw tables as watermarked Parquet
via ``scripts/export_for_sync.py`` -> git. The OTHER machine (no OpenClaw, view
only) runs THIS script after a ``git pull`` to rebuild a complete, servable
dashboard from that synced raw data.

Why a rebuild is needed and not just an import: the sync contract ships only
*raw* source tables (``import_from_sync.py``'s ``TABLE_PRIMARY_KEYS``). The
derived tables the API serves -- ``feature_store``, ``daily_predictions``,
``symbol_backtest_snapshot`` -- are NOT synced, because they are cheap to
recompute locally from the raw data with the in-repo LightGBM/logistic pipeline
(no OpenClaw, no paid API needed for the core dashboard). So the flow is:

    git pull                          # you run this (or pass --git-pull)
    import_from_sync   -> working DB  # raw tables land in data/stockmoney.duckdb
    compute_features   -> working DB  # realized_vol / adx / dispersion / ...
    build_dashboard_snapshot          # per-symbol prediction + backtest cache
    refresh_live_db    -> live DB     # atomic publish to data/stockmoney_live.duckdb

Bootstrapping note: the sync only ships recent watermarked *deltas*, not the
full multi-year history. The very first time you set up the second machine, copy
``data/stockmoney.duckdb`` over once (AirDrop/scp) so it has the historical
OHLCV/feature backfill; after that this script keeps it fresh incrementally.

Usage
-----
    .venv/bin/python scripts/sync_pull_and_build.py            # assumes you already git pulled
    .venv/bin/python scripts/sync_pull_and_build.py --git-pull # also run `git pull` first
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

WORKING_DB = "data/stockmoney.duckdb"
PY = sys.executable  # the same interpreter running this script (.venv/bin/python)


def _run(argv: list[str], *, label: str, env: dict | None = None) -> None:
    print(f"\n>>> {label}: {' '.join(argv)}", flush=True)
    proc = subprocess.run(argv, capture_output=True, text=True, env=env)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"step failed ({label}): exit {proc.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--git-pull", action="store_true", help="run `git pull` before importing"
    )
    parser.add_argument("--working-db", default=WORKING_DB)
    args = parser.parse_args()

    if args.git_pull:
        _run(["git", "pull", "--ff-only"], label="git pull")

    # 1. Raw synced tables -> working DB (NOT the live DB: we rebuild derived
    #    tables into the working DB, then publish the whole thing to live).
    _run(
        [PY, "scripts/import_from_sync.py", "--db", args.working_db],
        label="import_from_sync",
    )
    # compute_features / build_dashboard_snapshot only honour DEFAULT_DB_PATH
    # (i.e. the STOCKMONEY_DB env var), not a positional arg -- so point them at
    # the working DB that way.
    child_env = {**os.environ, "STOCKMONEY_DB": args.working_db}
    # 2. Recompute derived features from the freshly-imported raw data.
    _run([PY, "scripts/compute_features.py"], label="compute_features", env=child_env)
    # 3. Rebuild per-symbol prediction + backtest snapshot cache the API reads.
    _run(
        [PY, "scripts/build_dashboard_snapshot.py"],
        label="build_dashboard_snapshot",
        env=child_env,
    )
    # 4. Atomically publish the complete working DB to the app-served live DB.
    _run(
        [PY, "scripts/refresh_live_db.py", "--source", args.working_db],
        label="refresh_live_db",
    )

    print(json.dumps({"status": "ok", "working_db": args.working_db}, indent=2))


if __name__ == "__main__":
    main()
