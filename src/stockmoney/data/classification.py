"""The single write boundary between LLM classification judgment and the
database (CLAUDE.md section 13's prompt-injection concern, and the OpenClaw
skill docs' own "exec must not allow arbitrary injection from untrusted
input" warning). An agent that reads scraped Reddit/RSS content never emits
SQL or arbitrary shell commands -- it can only produce one of these two
narrow, validated record shapes, which get routed through the exact same
`append_rows` path every other connector in this project uses.

- `record_sentiment`: aggregated sentiment for an ALREADY-TRACKED watchlist
  symbol -> `alt_social_hourly`. Deliberately rejects unknown symbols so a
  new ticker can't sneak into the feature pipeline without human review.
- `record_candidate`: a proposed new ticker/theme -> `watchlist_candidates`
  (CLAUDE.md section 3's scanner-proposes/human-vetoes mechanism). This is
  the ONLY path for anything not already on the watchlist, and it never
  writes to `watchlist_members` itself.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import duckdb
import polars as pl

from stockmoney.data.db import append_rows

REQUIRED_SENTIMENT_FIELDS = {"symbol", "platform", "hour", "sentiment_score", "post_count"}


def record_sentiment(conn: duckdb.DuckDBPyConnection, payload: dict) -> None:
    missing = REQUIRED_SENTIMENT_FIELDS - payload.keys()
    if missing:
        raise ValueError(f"sentiment record missing fields: {sorted(missing)}")

    symbol = str(payload["symbol"]).upper()
    known = conn.execute(
        "SELECT 1 FROM watchlist_members WHERE symbol = ? AND removed_date IS NULL",
        [symbol],
    ).fetchone()
    if not known:
        raise ValueError(
            f"symbol {symbol!r} is not an active watchlist member; "
            "use kind='candidate' to propose new tickers instead"
        )

    hour_bucket = _parse_datetime(payload["hour"])
    df = pl.DataFrame(
        {
            "symbol": [symbol],
            "platform": [str(payload["platform"])],
            "hour_bucket": [hour_bucket],
            "post_count": [int(payload["post_count"])],
            "sentiment_score": [float(payload["sentiment_score"])],
            "sentiment_accel": [payload.get("sentiment_accel")],
            "source": [str(payload["platform"])],
        }
    )
    append_rows(conn, "alt_social_hourly", df)

    item_id = payload.get("item_id")
    if item_id:
        _index_symbol_evidence(
            conn, item_id=str(item_id), platform=str(payload["platform"]), symbol=symbol
        )


_PLATFORM_TO_EVIDENCE_ITEM_TYPE = {"reddit": "reddit", "rss": "news"}


def _index_symbol_evidence(
    conn: duckdb.DuckDBPyConnection, *, item_id: str, platform: str, symbol: str
) -> None:
    """Index which raw item a symbol's sentiment record came from, so the
    catalyst synthesis pass (stockmoney.data.catalyst_synthesis) can later
    look up the source post/article text for that symbol. Reuses
    `scan_classifications` -- originally the paid-API scan_classify.py
    idempotency ledger, now doubling as a shared item->symbol index.

    One item can be classified against multiple symbols (SKILL.md: "once per
    (ticker, item)"), so this merges into any existing `symbols` list for
    (item_id, item_type) rather than overwriting it -- an INSERT OR REPLACE
    with a single-symbol list would silently drop earlier symbols recorded
    for the same item."""
    item_type = _PLATFORM_TO_EVIDENCE_ITEM_TYPE.get(platform)
    if item_type is None:
        return
    existing = conn.execute(
        "SELECT symbols FROM scan_classifications WHERE item_id = ? AND item_type = ?",
        [item_id, item_type],
    ).fetchone()
    symbols = set(json.loads(existing[0])) if existing and existing[0] else set()
    symbols.add(symbol)
    conn.execute(
        """
        INSERT OR REPLACE INTO scan_classifications
            (item_id, item_type, processed_at, model_version, result_kind, symbols)
        VALUES (?, ?, ?, 'record-classification-v1', 'sentiment', ?)
        """,
        [item_id, item_type, datetime.now(timezone.utc), json.dumps(sorted(symbols))],
    )


def record_candidate(conn: duckdb.DuckDBPyConnection, payload: dict) -> None:
    if "rationale" not in payload:
        raise ValueError("candidate record missing required field: 'rationale'")
    symbol = payload.get("symbol")
    theme = payload.get("theme")
    if not symbol and not theme:
        raise ValueError("candidate record needs at least one of 'symbol' or 'theme'")

    now = datetime.now(timezone.utc)
    df = pl.DataFrame(
        {
            "candidate_id": [str(uuid.uuid4())],
            "proposed_date": [now.date()],
            "symbol": [str(symbol).upper() if symbol else None],
            "theme": [theme],
            "rationale": [str(payload["rationale"])],
            "evidence_count": [payload.get("evidence_count")],
            "source_refs": [json.dumps(payload.get("source_refs", []))],
            "status": ["proposed"],
            "created_at": [now],
        }
    )
    append_rows(conn, "watchlist_candidates", df)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
