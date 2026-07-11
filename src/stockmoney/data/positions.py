"""DB access layer for `option_positions` -- the human's own holdings ledger
that the risk-control track (`stockmoney.models.options_risk`) evaluates
against. Unlike the append-only raw ingestion tables, positions are ordinary
mutable rows: opened once, closed in place on exit.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import duckdb

from stockmoney.models.options_risk import OptionPosition


def open_position(
    conn: duckdb.DuckDBPyConnection,
    *,
    symbol: str,
    option_right: str,
    side: str,
    strike: float,
    expiry_date: date,
    entry_date: date,
    entry_underlying_price: float,
    entry_premium: float,
    entry_iv: float | None = None,
    regime_at_entry: int | None = None,
    thesis_note: str | None = None,
) -> str:
    if option_right not in ("call", "put"):
        raise ValueError(f"option_right must be 'call' or 'put', got {option_right!r}")
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long' or 'short', got {side!r}")

    position_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO option_positions (
            position_id, symbol, option_right, side, strike, expiry_date,
            entry_date, entry_underlying_price, entry_premium, entry_iv,
            regime_at_entry, thesis_note, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """,
        [
            position_id, symbol.upper(), option_right, side, strike, expiry_date,
            entry_date, entry_underlying_price, entry_premium, entry_iv,
            regime_at_entry, thesis_note, datetime.now(timezone.utc),
        ],
    )
    return position_id


def close_position(
    conn: duckdb.DuckDBPyConnection, position_id: str, *, reason: str | None = None
) -> None:
    row = conn.execute(
        "SELECT status FROM option_positions WHERE position_id = ?", [position_id]
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown position_id: {position_id!r}")
    if row[0] == "closed":
        raise ValueError(f"position {position_id!r} is already closed")

    conn.execute(
        """
        UPDATE option_positions
        SET status = 'closed', closed_at = ?, closed_reason = ?
        WHERE position_id = ?
        """,
        [datetime.now(timezone.utc), reason, position_id],
    )


def _row_to_position(row: tuple) -> OptionPosition:
    (
        position_id, symbol, option_right, side, _strike, _expiry_date,
        entry_date, entry_underlying_price, entry_premium, entry_iv, regime_at_entry,
    ) = row
    return OptionPosition(
        position_id=position_id,
        symbol=symbol,
        option_right=option_right,
        side=side,
        entry_date=entry_date,
        entry_underlying_price=entry_underlying_price,
        entry_premium=entry_premium,
        entry_iv=entry_iv,
        regime_at_entry=regime_at_entry,
    )


_SELECT_COLUMNS = """
    position_id, symbol, option_right, side, strike, expiry_date,
    entry_date, entry_underlying_price, entry_premium, entry_iv, regime_at_entry
"""


def get_position(conn: duckdb.DuckDBPyConnection, position_id: str) -> OptionPosition | None:
    row = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM option_positions WHERE position_id = ?",
        [position_id],
    ).fetchone()
    return _row_to_position(row) if row else None


def list_open_positions(conn: duckdb.DuckDBPyConnection) -> list[OptionPosition]:
    rows = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM option_positions WHERE status = 'open' ORDER BY entry_date"
    ).fetchall()
    return [_row_to_position(r) for r in rows]


def latest_underlying_price(conn: duckdb.DuckDBPyConnection, symbol: str) -> tuple[date, float] | None:
    """Most recent close for `symbol`, deduped the same way feature engineering
    is (highest `ingested_at` wins for a given `trade_date`)."""
    row = conn.execute(
        """
        WITH latest AS (
            SELECT trade_date, close,
                   row_number() OVER (
                       PARTITION BY trade_date ORDER BY ingested_at DESC
                   ) AS rn
            FROM ohlcv_daily
            WHERE symbol = ? AND close IS NOT NULL
        )
        SELECT trade_date, close FROM latest WHERE rn = 1
        ORDER BY trade_date DESC LIMIT 1
        """,
        [symbol.upper()],
    ).fetchone()
    return (row[0], row[1]) if row else None


def price_on_date(conn: duckdb.DuckDBPyConnection, symbol: str, trade_date: date) -> float | None:
    """Close on an exact `trade_date`, deduped the same way as
    `latest_underlying_price` (highest `ingested_at` wins). Used by the
    daily-prediction grading job to look up the price on a prediction's
    label_end_date."""
    row = conn.execute(
        """
        WITH latest AS (
            SELECT trade_date, close,
                   row_number() OVER (
                       PARTITION BY trade_date ORDER BY ingested_at DESC
                   ) AS rn
            FROM ohlcv_daily
            WHERE symbol = ? AND trade_date = ? AND close IS NOT NULL
        )
        SELECT close FROM latest WHERE rn = 1
        """,
        [symbol.upper(), trade_date],
    ).fetchone()
    return row[0] if row else None
