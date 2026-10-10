"""Sleeve attribution ledger (SQLite).

The BROKER is the source of truth for positions and cash. This ledger only adds
attribution: which strategy sleeve owns which position, at what average price,
with which stop, and the realized P&L of every closing fill. The risk layer
derives system equity from it (capital_budget + realized + unrealized), and
``reconcile`` compares it to the broker every run.

Rules:
- One symbol belongs to at most one strategy at a time. Opening a symbol that is
  held by another strategy raises ``SleeveConflict``.
- Long-only. Selling more than is held raises.
- ``apply_fill`` is idempotent on client_order_id, so re-running a job cannot
  double-count a fill.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from stockmoney.desk.execution.symbols import from_broker
from stockmoney.desk.execution.types import Side

QTY_EPS = 1e-9  # below this a position counts as closed

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS sleeve_positions (
  symbol TEXT PRIMARY KEY, strategy TEXT NOT NULL, qty REAL NOT NULL,
  avg_price REAL NOT NULL, opened_at TEXT, stop REAL);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT, client_order_id TEXT UNIQUE, symbol TEXT NOT NULL,
  strategy TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, price REAL NOT NULL,
  filled_at TEXT, realized_pnl REAL NOT NULL DEFAULT 0, recorded_at TEXT);
CREATE INDEX IF NOT EXISTS fills_strategy ON fills(strategy);
CREATE TABLE IF NOT EXISTS risk_state (key TEXT PRIMARY KEY, value TEXT);
"""


class LedgerError(ValueError):
    pass


class SleeveConflict(LedgerError):
    """A symbol is already held by a different strategy."""


def norm(symbol: str) -> str:
    """Comparison key: 'BTC/USD' and 'BTCUSD' are the same instrument, and so
    are the universe spelling 'BRK-B' and the broker spelling 'BRK.B'."""
    return from_broker(symbol).replace("/", "").upper()


def is_crypto_symbol(symbol: str) -> bool:
    return "/" in symbol


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(LEDGER_SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SleevePosition:
    symbol: str
    strategy: str
    qty: float
    avg_price: float
    opened_at: str | None
    stop: float | None


class Ledger:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        ensure_schema(con)

    # ------------------------------------------------------------ positions
    def positions(self) -> dict[str, SleevePosition]:
        rows = self.con.execute(
            "SELECT symbol, strategy, qty, avg_price, opened_at, stop FROM sleeve_positions ORDER BY symbol"
        ).fetchall()
        return {r[0]: SleevePosition(*tuple(r)) for r in rows}

    def position(self, symbol: str) -> SleevePosition | None:
        key = norm(symbol)
        for sym, pos in self.positions().items():
            if norm(sym) == key:
                return pos
        return None

    def owner(self, symbol: str) -> str | None:
        pos = self.position(symbol)
        return pos.strategy if pos else None

    def set_stop(self, symbol: str, stop: float | None) -> None:
        pos = self.position(symbol)
        if pos is None:
            raise LedgerError(f"no ledger position for {symbol}")
        with self.con:
            self.con.execute("UPDATE sleeve_positions SET stop=? WHERE symbol=?", (stop, pos.symbol))

    # ---------------------------------------------------------------- fills
    def apply_fill(self, *, client_order_id: str, symbol: str, strategy: str, side: Side,
                   qty: float, price: float, filled_at: str | None = None,
                   stop: float | None = None, cumulative: bool = False) -> bool:
        """Record one completed fill. Returns False (no-op) if this
        client_order_id was already recorded. Raises on rule violations
        (strategy conflict, oversell) without writing anything.

        ``cumulative=True``: ``qty``/``price`` are the order's cumulative
        filled qty and average price so far (a partial fill, as the broker
        reports it). Only the part not yet booked for this order is added;
        the idempotency key is client_order_id plus that cumulative qty (the
        first booking of an order keeps the plain client_order_id)."""
        if qty <= 0 or price <= 0:
            raise LedgerError(f"fill needs qty>0 and price>0, got qty={qty} price={price}")
        if cumulative:
            done_qty, done_cost = self.booked(client_order_id)
            delta = qty - done_qty
            if delta <= QTY_EPS:
                return False
            if done_qty > QTY_EPS:
                px = (qty * price - done_cost) / delta
                price = px if math.isfinite(px) and px > 0 else price
                client_order_id = f"{client_order_id}#{qty:g}"
            qty = delta
        seen = self.con.execute("SELECT 1 FROM fills WHERE client_order_id=?", (client_order_id,)).fetchone()
        if seen:
            return False
        pos = self.position(symbol)
        filled_at = filled_at or _now()
        with self.con:
            if side == Side.BUY:
                realized = 0.0
                if pos is None:
                    if not strategy:
                        raise LedgerError(f"opening {symbol} needs a strategy")
                    self.con.execute(
                        "INSERT INTO sleeve_positions (symbol, strategy, qty, avg_price, opened_at, stop) "
                        "VALUES (?,?,?,?,?,?)", (symbol, strategy, qty, price, filled_at, stop))
                else:
                    if strategy and pos.strategy != strategy:
                        raise SleeveConflict(
                            f"{symbol} is held by strategy '{pos.strategy}'; cannot open it under '{strategy}'")
                    new_qty = pos.qty + qty
                    new_avg = (pos.avg_price * pos.qty + price * qty) / new_qty
                    self.con.execute(
                        "UPDATE sleeve_positions SET qty=?, avg_price=?, stop=COALESCE(?, stop) WHERE symbol=?",
                        (new_qty, new_avg, stop, pos.symbol))
                owner = pos.strategy if pos else strategy
            else:
                if pos is None:
                    raise LedgerError(f"sell of {symbol} but the ledger holds none (no shorting)")
                if qty > pos.qty + QTY_EPS:
                    raise LedgerError(f"sell {qty} of {symbol} exceeds held {pos.qty} (no shorting)")
                realized = (price - pos.avg_price) * qty
                left = pos.qty - qty
                if left <= QTY_EPS:
                    self.con.execute("DELETE FROM sleeve_positions WHERE symbol=?", (pos.symbol,))
                else:
                    self.con.execute("UPDATE sleeve_positions SET qty=? WHERE symbol=?", (left, pos.symbol))
                owner = pos.strategy
            self.con.execute(
                "INSERT INTO fills (client_order_id, symbol, strategy, side, qty, price, filled_at, realized_pnl, recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (client_order_id, symbol, owner, side.value, qty, price, filled_at, realized, _now()))
        return True

    def booked(self, client_order_id: str) -> tuple[float, float]:
        """(qty, qty * price) already booked for one broker order, over its
        plain row and every ``<id>#<cumulative qty>`` partial-fill row."""
        r = self.con.execute(
            "SELECT COALESCE(SUM(qty),0), COALESCE(SUM(qty*price),0) FROM fills "
            "WHERE client_order_id=? OR substr(client_order_id, 1, ?)=?",
            (client_order_id, len(client_order_id) + 1, client_order_id + "#")).fetchone()
        return float(r[0]), float(r[1])

    # ------------------------------------------------------------------ P&L
    def realized_pnl(self, strategy: str | None = None) -> float:
        if strategy is None:
            r = self.con.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM fills").fetchone()
        else:
            r = self.con.execute(
                "SELECT COALESCE(SUM(realized_pnl),0) FROM fills WHERE strategy=?", (strategy,)).fetchone()
        return float(r[0])

    def unrealized_pnl(self, prices: dict[str, float], strategy: str | None = None) -> float:
        """``prices`` maps symbol -> last price (any spelling of the symbol).
        A position with no price contributes 0 (valued at cost)."""
        by_key = {norm(s): p for s, p in prices.items()}
        total = 0.0
        for pos in self.positions().values():
            if strategy is not None and pos.strategy != strategy:
                continue
            px = by_key.get(norm(pos.symbol))
            if px is not None:
                total += (px - pos.avg_price) * pos.qty
        return total

    def system_equity(self, capital_budget: float, prices: dict[str, float]) -> float:
        return capital_budget + self.realized_pnl() + self.unrealized_pnl(prices)

    # ---------------------------------------------------------- risk state
    def get_state(self, key: str) -> str | None:
        r = self.con.execute("SELECT value FROM risk_state WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_state(self, key: str, value: str) -> None:
        with self.con:
            self.con.execute(
                "INSERT INTO risk_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
