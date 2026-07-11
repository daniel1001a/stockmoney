"""The ONLY database write path exposed to the overnight scanning skill.
Reads one JSON object from stdin and routes it to `record_sentiment` or
`record_candidate` (see stockmoney.data.classification for the safety
rationale). Never accepts or executes SQL/shell content -- the agent's job
is to emit one of these two narrow shapes, nothing else.

Usage:
    echo '{"kind": "sentiment", "symbol": "NVDA", "platform": "reddit",
           "hour": "2026-07-10T00:00:00Z", "sentiment_score": 0.6,
           "post_count": 3}' | uv run python scripts/record_classification.py

    echo '{"kind": "candidate", "theme": "thermal_management",
           "rationale": "...", "evidence_count": 5,
           "source_refs": ["post_1", "article_2"]}' \\
        | uv run python scripts/record_classification.py
"""
from __future__ import annotations

import json
import sys

from stockmoney.data.classification import record_candidate, record_sentiment
from stockmoney.data.db import DEFAULT_DB_PATH, get_connection, run_migrations

VALID_KINDS = {"sentiment", "candidate"}


def main(db_path: str = DEFAULT_DB_PATH) -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"invalid JSON: {exc}"}))
        return 1

    kind = payload.get("kind")
    if kind not in VALID_KINDS:
        print(json.dumps({"ok": False, "error": f"kind must be one of {sorted(VALID_KINDS)}"}))
        return 1

    conn = get_connection(db_path)
    run_migrations(conn)
    try:
        if kind == "sentiment":
            record_sentiment(conn, payload)
        else:
            record_candidate(conn, payload)
    except (ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    finally:
        conn.close()

    print(json.dumps({"ok": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
