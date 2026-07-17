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

Runs on the Claude Code subscription via the `claude` CLI (see
`claude_cli_synthesize_fn` in stockmoney.data.catalyst_synthesis) -- NOT the
paid Anthropic API. ANTHROPIC_API_KEY is intentionally never read here; no
API key is required or used.

Usage:
    uv run python scripts/synthesize_catalysts.py [--hours 48] [--model sonnet]
    uv run python scripts/synthesize_catalysts.py --symbols NVDA AMD  # bounded smoke test
"""
from __future__ import annotations

import argparse
import json

from stockmoney.data.catalyst_synthesis import DEFAULT_HOURS, run_catalyst_synthesis_pass
from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations


def main(
    *,
    hours: int = DEFAULT_HOURS,
    model: str = "sonnet",
    db_path: str = DEFAULT_DB_PATH,
    symbols: list[str] | None = None,
) -> None:
    conn = get_connection(db_path)
    run_migrations(conn)
    try:
        counts = run_catalyst_synthesis_pass(conn, hours=hours, model=model, symbols=symbols)
    finally:
        conn.close()
    print(json.dumps(counts))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Restrict to this subset of symbols (bounded smoke test); default is all symbols pass 1 found signal on.",
    )
    args = parser.parse_args()
    main(hours=args.hours, model=args.model, symbols=args.symbols)
