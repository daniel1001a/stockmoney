"""Print the active watchlist as JSON. Read-only. Fixed, argument-free script
so it can be allowlisted with an exact argv match (no inline eval).

Usage:
    uv run python scripts/scanner_check_watchlist.py
"""
from __future__ import annotations

import json

from stockmoney.data.db import DEFAULT_DB_PATH, get_connection


def main(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    symbols = [
        row[0]
        for row in conn.execute(
            "SELECT symbol FROM watchlist_members WHERE removed_date IS NULL ORDER BY symbol"
        ).fetchall()
    ]
    conn.close()
    print(json.dumps({"active_symbols": symbols}))


if __name__ == "__main__":
    main()
