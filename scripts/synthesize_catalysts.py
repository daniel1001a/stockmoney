"""Pass 2 of catalyst reasoning (~/.claude/plans/frontend-catalyst-rebuild.md
Track A / A2): for every watchlist symbol pass 1
(scripts/classify_scan.py) found recent signal on, ask Sonnet to trace a
transmission chain from catalyst to symbol and estimate how much of it is
already priced in. Writes to `catalyst_signals` via
`stockmoney.data.catalyst_synthesis.run_catalyst_synthesis_pass`.

Deliberately NOT wired into nightly_refresh.py (same reasoning as
classify_scan.py: that script stays pure/deterministic, no LLM calls). Run
this after classify_scan.py has produced some scan_classifications rows to
act on -- with none yet, this is a no-op (nothing to synthesize).

Requires ANTHROPIC_API_KEY in the environment (see .env.example).

Usage:
    uv run python scripts/synthesize_catalysts.py [--hours 48] [--model claude-sonnet-5]
"""
from __future__ import annotations

import argparse
import json

from dotenv import load_dotenv

from stockmoney.data.catalyst_synthesis import DEFAULT_HOURS, DEFAULT_MODEL, run_catalyst_synthesis_pass
from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations


def main(
    *,
    hours: int = DEFAULT_HOURS,
    model: str = DEFAULT_MODEL,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    load_dotenv()
    conn = get_connection(db_path)
    run_migrations(conn)
    try:
        counts = run_catalyst_synthesis_pass(conn, hours=hours, model=model)
    finally:
        conn.close()
    print(json.dumps(counts))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    main(hours=args.hours, model=args.model)
