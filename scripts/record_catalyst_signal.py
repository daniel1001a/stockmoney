"""The ONLY database write path exposed to the catalyst synthesis pass of the
overnight scanning skill. Reads one JSON object from stdin and routes it to
`record_catalyst_signal` (see stockmoney.data.catalyst_signals for the
validation/idempotency rationale). Never accepts or executes SQL/shell
content -- the agent's job is to emit this one narrow shape, nothing else.

Usage:
    echo '{"symbol": "NVDA", "catalyst_summary": "...",
           "transmission_chain": "...", "novelty_score": 0.7,
           "sentiment_score": 0.4, "priced_in_estimate": 0.3,
           "source_refs": ["post_1", "article_2"]}' \\
        | uv run python scripts/record_catalyst_signal.py
"""
from __future__ import annotations

import json
import sys
from datetime import date

from stockmoney.data.catalyst_signals import record_catalyst_signal
from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations

# Distinct from catalyst_synthesis.MODEL_VERSION ("catalyst-synthesis-sonnet-v1"),
# which tags rows written by the paid-API reference script
# (scripts/synthesize_catalysts.py). This path runs via OpenClaw + claude-cli
# (subscription), not a direct Anthropic API call.
MODEL_VERSION = "catalyst-synthesis-claude-cli-v1"

REQUIRED_FIELDS = {"symbol", "catalyst_summary", "transmission_chain"}


def main(db_path: str = DEFAULT_DB_PATH) -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"invalid JSON: {exc}"}))
        return 1

    missing = REQUIRED_FIELDS - payload.keys()
    if missing:
        print(json.dumps({"ok": False, "error": f"missing required field(s): {sorted(missing)}"}))
        return 1

    conn = get_connection(db_path)
    run_migrations(conn)
    try:
        signal_id = record_catalyst_signal(
            conn,
            symbol=payload["symbol"],
            as_of_date=date.today(),
            catalyst_summary=payload["catalyst_summary"],
            transmission_chain=payload["transmission_chain"],
            novelty_score=payload.get("novelty_score"),
            sentiment_score=payload.get("sentiment_score"),
            priced_in_estimate=payload.get("priced_in_estimate"),
            source_refs=payload.get("source_refs"),
            model_version=MODEL_VERSION,
        )
    except (ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    finally:
        conn.close()

    print(json.dumps({"ok": True, "signal_id": signal_id}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
