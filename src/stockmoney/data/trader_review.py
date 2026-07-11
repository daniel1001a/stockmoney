"""DB access for the per-trader review outputs: `trader_review_log` (the
multi-trader generalization of attribution_log) and `trader_divergence_log`
(CLAUDE.md section 8 cross-trader disagreement). Both are read-only analysis
side-branches; nothing here is read by a training path.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import duckdb


def has_review(
    conn: duckdb.DuckDBPyConnection, *, review_date: date, symbol: str, trader_id: str, method_version: str
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM trader_review_log WHERE review_date = ? AND symbol = ? "
        "AND trader_id = ? AND method_version = ?",
        [review_date, symbol, trader_id, method_version],
    ).fetchone()
    return row is not None


def record_review(
    conn: duckdb.DuckDBPyConnection,
    *,
    review_date: date,
    symbol: str,
    trader_id: str,
    method_version: str,
    predicted_direction: str,
    predicted_confidence: float,
    actual_return: float,
    attr_macro: float,
    attr_sector: float,
    attr_idiosyncratic: float,
    event_tags: list[str],
    verdict_class: str,
) -> None:
    conn.execute(
        """
        INSERT INTO trader_review_log (
            review_date, symbol, trader_id, method_version, predicted_direction,
            predicted_confidence, actual_return, attr_macro, attr_sector,
            attr_idiosyncratic, event_tags, verdict_class, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            review_date, symbol, trader_id, method_version, predicted_direction,
            predicted_confidence, actual_return, attr_macro, attr_sector,
            attr_idiosyncratic, ",".join(event_tags), verdict_class,
            datetime.now(timezone.utc),
        ],
    )


def upsert_divergence(
    conn: duckdb.DuckDBPyConnection,
    *,
    trade_date: date,
    symbol: str,
    trader_id: str,
    direction: str,
    conviction: float,
    disagreed: bool,
    was_right: bool | None,
) -> None:
    """Idempotent per (trade_date, symbol, trader_id): a re-run after grading
    updates `was_right` (and `disagreed`) in place rather than duplicating."""
    conn.execute("DELETE FROM trader_divergence_log WHERE trade_date = ? AND symbol = ? AND trader_id = ?",
                 [trade_date, symbol, trader_id])
    conn.execute(
        """
        INSERT INTO trader_divergence_log (
            trade_date, symbol, trader_id, direction, conviction, disagreed, was_right, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [trade_date, symbol, trader_id, direction, conviction, disagreed, was_right,
         datetime.now(timezone.utc)],
    )


def recent_divergence_rows(conn: duckdb.DuckDBPyConnection, *, hours: int = 168) -> list[dict]:
    """Divergence rows from the last `hours`, symbol/day grouped, disagreements
    first -- the cross-trader radar's data source."""
    cols = ["trade_date", "symbol", "trader_id", "direction", "conviction", "disagreed", "was_right"]
    rows = conn.execute(
        f"""
        SELECT {", ".join(cols)} FROM trader_divergence_log
        WHERE created_at >= now() - (? * INTERVAL 1 HOUR)
        ORDER BY trade_date DESC, symbol, disagreed DESC, trader_id
        """,
        [hours],
    ).fetchall()
    return [dict(zip(cols, r)) for r in rows]
