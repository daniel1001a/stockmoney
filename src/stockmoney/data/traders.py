"""DB access layer for the `traders` registry -- the Trader League roster
(~/.claude/plans/trader-council-rearchitecture.md section 2).

A trader is {trader_id, name, philosophy, engine_key}. `engine_key` binds to
`stockmoney.league.engines.ENGINE_REGISTRY`, which turns a trader into a daily
prediction. Traders come and go (人數流動): `deactivate_trader` retires one
without deleting it, so its historical predictions/review stay attributable.

This is a discretion/meta-layer table (CLAUDE.md section 7): nothing here (or
anything keyed off trader_id) is ever read by a training path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import duckdb


@dataclass
class Trader:
    trader_id: str
    name: str
    philosophy: str
    engine_key: str
    active: bool
    added_date: date
    removed_date: date | None = None


_SELECT_COLUMNS = "trader_id, name, philosophy, engine_key, active, added_date, removed_date"


def _row_to_trader(row: tuple) -> Trader:
    trader_id, name, philosophy, engine_key, active, added_date, removed_date = row
    return Trader(
        trader_id=trader_id, name=name, philosophy=philosophy, engine_key=engine_key,
        active=active, added_date=added_date, removed_date=removed_date,
    )


def list_active_traders(conn: duckdb.DuckDBPyConnection) -> list[Trader]:
    rows = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM traders WHERE active = true ORDER BY trader_id"
    ).fetchall()
    return [_row_to_trader(r) for r in rows]


def list_all_traders(conn: duckdb.DuckDBPyConnection) -> list[Trader]:
    rows = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM traders ORDER BY active DESC, trader_id"
    ).fetchall()
    return [_row_to_trader(r) for r in rows]


def get_trader(conn: duckdb.DuckDBPyConnection, trader_id: str) -> Trader | None:
    row = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM traders WHERE trader_id = ?", [trader_id]
    ).fetchone()
    return _row_to_trader(row) if row else None


def add_trader(
    conn: duckdb.DuckDBPyConnection,
    *,
    trader_id: str,
    name: str,
    philosophy: str,
    engine_key: str,
    added_date: date | None = None,
) -> str:
    """Register a new trader. Idempotent per trader_id: re-adding an existing
    id returns it unchanged (so a bootstrap can run twice safely)."""
    existing = conn.execute("SELECT trader_id FROM traders WHERE trader_id = ?", [trader_id]).fetchone()
    if existing is not None:
        return existing[0]
    conn.execute(
        """
        INSERT INTO traders (trader_id, name, philosophy, engine_key, active, added_date, created_at)
        VALUES (?, ?, ?, ?, true, ?, ?)
        """,
        [trader_id, name, philosophy, engine_key, added_date or date.today(), datetime.now(timezone.utc)],
    )
    return trader_id


def deactivate_trader(
    conn: duckdb.DuckDBPyConnection, trader_id: str, *, removed_date: date | None = None
) -> None:
    """Retire a trader (active=false + removed_date). Never hard-deletes, so
    its predictions/review history stay attributable."""
    row = conn.execute("SELECT active FROM traders WHERE trader_id = ?", [trader_id]).fetchone()
    if row is None:
        raise ValueError(f"unknown trader_id: {trader_id!r}")
    conn.execute(
        "UPDATE traders SET active = false, removed_date = ? WHERE trader_id = ?",
        [removed_date or date.today(), trader_id],
    )
