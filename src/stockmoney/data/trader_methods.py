"""DB access for trader method evolution: `trader_method_versions` (the
immutable, forward-only catalogue each prediction pins) and
`trader_method_proposals` (human-reviewed method-update proposals).

v1 discipline (rearchitecture doc section 2 "交易員進化紀律", CLAUDE.md
sections 7/11): no auto-mutation. The review layer emits a `proposed` update;
a human `accept_proposal`s it, which mints a new version. New versions only
apply forward -- past predictions are never re-graded under a new method.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import duckdb

PROPOSED = "proposed"
ACCEPTED = "accepted"
REJECTED = "rejected"


def current_method_version(conn: duckdb.DuckDBPyConnection, trader_id: str) -> str | None:
    """The trader's currently-active method version (what new predictions pin).
    None if the trader has no version catalogued yet."""
    row = conn.execute(
        """
        SELECT method_version FROM trader_method_versions
        WHERE trader_id = ? AND status = 'active'
        ORDER BY effective_date DESC LIMIT 1
        """,
        [trader_id],
    ).fetchone()
    return row[0] if row else None


def propose_method_update(
    conn: duckdb.DuckDBPyConnection,
    *,
    trader_id: str,
    from_version: str,
    rationale: str,
    source_review_date: date | None = None,
) -> str | None:
    """Emit a `proposed` method update for human review. Idempotent per
    (trader_id, from_version, source_review_date): the review layer re-running
    over the same day won't stack duplicate proposals."""
    existing = conn.execute(
        """
        SELECT proposal_id FROM trader_method_proposals
        WHERE trader_id = ? AND from_version = ? AND status = 'proposed'
          AND source_review_date IS NOT DISTINCT FROM ?
        """,
        [trader_id, from_version, source_review_date],
    ).fetchone()
    if existing is not None:
        return existing[0]

    proposal_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO trader_method_proposals (
            proposal_id, trader_id, from_version, to_version, rationale,
            source_review_date, status, created_at
        ) VALUES (?, ?, ?, NULL, ?, ?, 'proposed', ?)
        """,
        [proposal_id, trader_id, from_version, rationale, source_review_date, datetime.now(timezone.utc)],
    )
    return proposal_id


def list_proposals(conn: duckdb.DuckDBPyConnection, *, status: str | None = None) -> list[dict]:
    query = (
        "SELECT proposal_id, trader_id, from_version, to_version, rationale, "
        "source_review_date, status, reviewed_by, reviewed_date FROM trader_method_proposals"
    )
    params: list = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC"
    cols = ["proposal_id", "trader_id", "from_version", "to_version", "rationale",
            "source_review_date", "status", "reviewed_by", "reviewed_date"]
    return [dict(zip(cols, r)) for r in conn.execute(query, params).fetchall()]


def accept_proposal(
    conn: duckdb.DuckDBPyConnection,
    proposal_id: str,
    *,
    to_version: str,
    spec: str,
    reviewed_by: str,
    effective_date: date | None = None,
) -> None:
    """Human accepts a proposal: mint the new active version (forward-only,
    effective from `effective_date`), supersede the old one, and mark the
    proposal accepted. This is the ONLY path that changes a trader's live
    method (no auto-mutation in v1)."""
    row = conn.execute(
        "SELECT trader_id, from_version, status FROM trader_method_proposals WHERE proposal_id = ?",
        [proposal_id],
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown proposal_id: {proposal_id!r}")
    trader_id, from_version, status = row
    if status != PROPOSED:
        raise ValueError(f"proposal {proposal_id!r} is already {status}, not proposed")

    eff = effective_date or date.today()
    conn.execute(
        "UPDATE trader_method_versions SET status = 'superseded' WHERE trader_id = ? AND status = 'active'",
        [trader_id],
    )
    conn.execute(
        """
        INSERT INTO trader_method_versions (trader_id, method_version, effective_date, spec, status, created_at)
        VALUES (?, ?, ?, ?, 'active', ?)
        """,
        [trader_id, to_version, eff, spec, datetime.now(timezone.utc)],
    )
    conn.execute(
        """
        UPDATE trader_method_proposals
        SET status = 'accepted', to_version = ?, reviewed_by = ?, reviewed_date = ?
        WHERE proposal_id = ?
        """,
        [to_version, reviewed_by, eff, proposal_id],
    )


def reject_proposal(conn: duckdb.DuckDBPyConnection, proposal_id: str, *, reviewed_by: str) -> None:
    conn.execute(
        "UPDATE trader_method_proposals SET status = 'rejected', reviewed_by = ?, reviewed_date = ? "
        "WHERE proposal_id = ? AND status = 'proposed'",
        [reviewed_by, date.today(), proposal_id],
    )
