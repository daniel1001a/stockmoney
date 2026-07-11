"""DB access layer for `catalyst_signals` -- the write/read boundary for the
Sonnet deep-reasoning pass (`stockmoney.data.catalyst_synthesis`). Mirrors
`stockmoney.data.daily_predictions`'s idempotent-per-day pattern rather than
`classification.py`'s narrow validated-shape pattern, since the caller here
is trusted orchestration code (not an LLM agent emitting raw records) -- the
untrusted-content boundary for this pipeline is upstream, in
`scan_classify.apply_result`.

`record_catalyst_signal` still independently re-validates that `symbol` is
an active watchlist member (CLAUDE.md section 3's discipline: nothing here
can create a new tracked symbol), and is idempotent per (symbol, as_of_date)
so re-running the synthesis pass twice in one day doesn't duplicate rows.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

import duckdb


@dataclass
class CatalystSignal:
    signal_id: str
    symbol: str
    as_of_date: date
    catalyst_summary: str
    transmission_chain: str
    novelty_score: float | None
    sentiment_score: float | None
    priced_in_estimate: float | None
    source_refs: list[str]
    model_version: str
    available_at: datetime


def record_catalyst_signal(
    conn: duckdb.DuckDBPyConnection,
    *,
    symbol: str,
    as_of_date: date,
    catalyst_summary: str,
    transmission_chain: str,
    novelty_score: float | None = None,
    sentiment_score: float | None = None,
    priced_in_estimate: float | None = None,
    source_refs: list[str] | None = None,
    model_version: str,
) -> str:
    symbol = symbol.upper()
    known = conn.execute(
        "SELECT 1 FROM watchlist_members WHERE symbol = ? AND removed_date IS NULL", [symbol]
    ).fetchone()
    if not known:
        raise ValueError(f"symbol {symbol!r} is not an active watchlist member")

    existing = conn.execute(
        "SELECT signal_id FROM catalyst_signals WHERE symbol = ? AND as_of_date = ?",
        [symbol, as_of_date],
    ).fetchone()
    if existing is not None:
        return existing[0]

    signal_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO catalyst_signals (
            signal_id, symbol, as_of_date, catalyst_summary, transmission_chain,
            novelty_score, sentiment_score, priced_in_estimate, source_refs,
            model_version, available_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            signal_id, symbol, as_of_date, catalyst_summary, transmission_chain,
            novelty_score, sentiment_score, priced_in_estimate,
            json.dumps(source_refs or []), model_version,
            datetime.now(timezone.utc), datetime.now(timezone.utc),
        ],
    )
    return signal_id


_SELECT_COLUMNS = """
    signal_id, symbol, as_of_date, catalyst_summary, transmission_chain,
    novelty_score, sentiment_score, priced_in_estimate, source_refs,
    model_version, available_at
"""


def _row_to_signal(row: tuple) -> CatalystSignal:
    (
        signal_id, symbol, as_of_date, catalyst_summary, transmission_chain,
        novelty_score, sentiment_score, priced_in_estimate, source_refs_json,
        model_version, available_at,
    ) = row
    return CatalystSignal(
        signal_id=signal_id, symbol=symbol, as_of_date=as_of_date,
        catalyst_summary=catalyst_summary, transmission_chain=transmission_chain,
        novelty_score=novelty_score, sentiment_score=sentiment_score,
        priced_in_estimate=priced_in_estimate,
        source_refs=json.loads(source_refs_json) if source_refs_json else [],
        model_version=model_version, available_at=available_at,
    )


def list_recent_catalysts(
    conn: duckdb.DuckDBPyConnection, *, hours: int = 48
) -> list[CatalystSignal]:
    """Recent catalyst signals ranked by novelty x |sentiment| (a rough
    "freshest, most directionally-opinionated first" ordering) -- the "消息
    雷達" (catalyst radar) view's data source. Ties/NULLs sort last, not
    excluded, so the frontend can still show them with a visibly lower
    ranking rather than silently dropping incomplete rows."""
    rows = conn.execute(
        f"""
        SELECT {_SELECT_COLUMNS} FROM catalyst_signals
        WHERE available_at >= now() - (? * INTERVAL 1 HOUR)
        ORDER BY coalesce(novelty_score, 0) * abs(coalesce(sentiment_score, 0)) DESC
        """,
        [hours],
    ).fetchall()
    return [_row_to_signal(r) for r in rows]


def get_latest_catalyst_for_symbol(
    conn: duckdb.DuckDBPyConnection, symbol: str
) -> CatalystSignal | None:
    row = conn.execute(
        f"""
        SELECT {_SELECT_COLUMNS} FROM catalyst_signals
        WHERE symbol = ? ORDER BY as_of_date DESC LIMIT 1
        """,
        [symbol.upper()],
    ).fetchone()
    return _row_to_signal(row) if row else None
