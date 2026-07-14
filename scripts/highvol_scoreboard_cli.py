"""Standalone scoreboard run for the high-vol/crypto-momentum universe
(COIN, MSTR, IREN) -- the "Lin-inverse" experiment (see
LINVERSE_EXPERIMENT_REPORT.md). Deliberately NOT mixed with
scripts.scoreboard_cli's default strategies: highvol_strategies.py registers
its own `long_stock`/`B2_random` baselines scoped to this different universe,
and `run_scoreboard` only dedupes by (strategy name, horizon) within a single
run's `mean_by_key` -- running both batteries in one call would let one
baseline's mean silently overwrite the other's under the same key.

No DB connection is actually used for reads (highvol_strategies.py loads
closes from data/highvol_ohlcv/*.parquet, not ohlcv_daily) -- an in-memory
duckdb connection is passed only because `run_scoreboard`'s signature
requires a `conn` argument that `ScoreboardContext` could, in principle, be
asked for by a future strategy.

Usage:
    .venv/bin/python -m scripts.highvol_scoreboard_cli
"""
from __future__ import annotations

import duckdb

from stockmoney.backtest.highvol_strategies import highvol_strategies
from stockmoney.backtest.scoreboard import DEFAULT_HORIZONS, format_table, run_scoreboard


def main() -> None:
    conn = duckdb.connect(":memory:")
    try:
        result = run_scoreboard(conn, horizons=DEFAULT_HORIZONS, strategies=highvol_strategies())
        print(format_table(result))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
