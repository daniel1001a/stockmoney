"""Print today's new watchlist_candidates plus recent alt_social_hourly
sentiment for the digest pass. Read-only. Fixed, argument-free script so it
can be allowlisted with an exact argv match (no inline eval).

Usage:
    uv run python scripts/scanner_todays_candidates.py
"""
from __future__ import annotations

import json
from datetime import timedelta

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection


def main(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)

    candidates = conn.execute(
        "SELECT symbol, theme, rationale, evidence_count, source_refs "
        "FROM watchlist_candidates WHERE proposed_date = CURRENT_DATE "
        "ORDER BY evidence_count DESC NULLS LAST"
    ).fetchall()

    sentiment = conn.execute(
        """
        SELECT symbol, platform, avg(sentiment_score) AS avg_sentiment, sum(post_count) AS n
        FROM alt_social_hourly
        WHERE hour_bucket >= now() - INTERVAL 24 HOUR
        GROUP BY symbol, platform
        ORDER BY n DESC
        """
    ).fetchall()
    conn.close()

    out = {
        "new_candidates": [
            {
                "symbol": c[0], "theme": c[1], "rationale": c[2],
                "evidence_count": c[3], "source_refs": json.loads(c[4]) if c[4] else [],
            }
            for c in candidates
        ],
        "sentiment_last_24h": [
            {"symbol": s[0], "platform": s[1], "avg_sentiment": s[2], "post_count": s[3]}
            for s in sentiment
        ],
    }
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    main()
