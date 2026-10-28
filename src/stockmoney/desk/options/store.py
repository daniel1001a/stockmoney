"""SQLite tables of the intraday options sleeve. Same database file and the
same ``store.connect`` helper as the rest of the desk; every table has the
``options_`` prefix and nothing here reads or writes the stock ledger.

Idempotent schema (CREATE ... IF NOT EXISTS). Rows are keyed by deterministic
ids (setup_id, position_id, client_order_id) so re-running a loop tick, or
restarting the session, finds the existing row instead of creating a second.

Long-running callers (``session``) must ``con.commit()`` after each tick:
``store.connect`` only commits on exit, and an open write transaction would
block the stock jobs that share the file.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from stockmoney.desk import store

SCHEMA = """
CREATE TABLE IF NOT EXISTS options_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT, for_date TEXT, received_at TEXT,
  status TEXT, shadow_only INTEGER, error TEXT, sha TEXT, raw TEXT);
CREATE INDEX IF NOT EXISTS options_plans_date ON options_plans(for_date, status);
CREATE TABLE IF NOT EXISTS options_setups (
  setup_id TEXT PRIMARY KEY, day TEXT, source TEXT, plan_id TEXT, plan_index INTEGER,
  underlying TEXT, uclass TEXT, state TEXT, direction TEXT, spec_json TEXT, signal_json TEXT,
  live_eligible INTEGER, live_note TEXT, created_at TEXT, updated_at TEXT, rvol REAL, regime_json TEXT);
CREATE TABLE IF NOT EXISTS options_positions (
  position_id TEXT PRIMARY KEY, setup_id TEXT, day TEXT, mode TEXT, source TEXT, underlying TEXT,
  uclass TEXT, dte_label TEXT, band_label TEXT, occ TEXT, right TEXT, strike REAL, expiry TEXT,
  qty_initial REAL, qty_open REAL, state TEXT, entry_time TEXT, entry_price REAL, entry_bid REAL,
  entry_ask REAL, entry_quote_json TEXT, invalidation REAL, direction TEXT, max_hold_min INTEGER,
  tp_done INTEGER DEFAULT 0, stop_price REAL, exit_time TEXT, exit_reason TEXT,
  realized_usd REAL DEFAULT 0, fees_usd REAL DEFAULT 0, spread_cost_usd REAL DEFAULT 0,
  max_bid REAL, min_bid REAL, last_bid REAL, last_ask REAL, flags_json TEXT, note TEXT, updated_at TEXT,
  exit_variant TEXT, inv_close REAL, inv_vwap INTEGER DEFAULT 0, trail_ref REAL);
CREATE INDEX IF NOT EXISTS options_positions_day ON options_positions(day, mode, state);
CREATE TABLE IF NOT EXISTS options_fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT, position_id TEXT, at TEXT, side TEXT, qty REAL,
  price REAL, bid REAL, ask REAL, reason TEXT, client_order_id TEXT,
  UNIQUE(position_id, client_order_id, side, reason));
CREATE TABLE IF NOT EXISTS options_orders (
  client_order_id TEXT PRIMARY KEY, position_id TEXT, kind TEXT, side TEXT, qty REAL,
  order_type TEXT, limit_price REAL, status TEXT, reason TEXT, submitted_at TEXT,
  filled_qty REAL DEFAULT 0, avg_price REAL, booked_qty REAL DEFAULT 0, note TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS options_filtered (
  id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, at TEXT, stage TEXT, underlying TEXT,
  setup_id TEXT, variant TEXT, reason TEXT, detail_json TEXT,
  UNIQUE(day, stage, underlying, setup_id, variant, reason));
CREATE TABLE IF NOT EXISTS options_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, at TEXT, level TEXT, kind TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS options_notified (key TEXT PRIMARY KEY, at TEXT);
CREATE TABLE IF NOT EXISTS options_universe (
  day TEXT, symbol TEXT, uclass TEXT, origin TEXT, prev_close REAL, pm_dollar_vol REAL,
  PRIMARY KEY (day, symbol));
CREATE TABLE IF NOT EXISTS options_profiles (
  day TEXT, symbol TEXT, profile_json TEXT, note TEXT, PRIMARY KEY (day, symbol));
"""

# columns added after the first version of the tables: (table, column, type)
_ADDED = (("options_setups", "rvol", "REAL"), ("options_setups", "regime_json", "TEXT"),
          ("options_positions", "exit_variant", "TEXT"), ("options_positions", "inv_close", "REAL"),
          ("options_positions", "inv_vwap", "INTEGER DEFAULT 0"), ("options_positions", "trail_ref", "REAL"),
          # schema v1.1: Grok's directional prior on a rule setup, tagged at its trigger
          # (agree | disagree | none; NULL = not triggered), late = plan received after 09:20 ET
          ("options_setups", "grok_stance", "TEXT"), ("options_setups", "grok_stance_late", "INTEGER DEFAULT 0"),
          # the quote an order was sent against (arrival price), and the quote time on each fill
          ("options_orders", "ref_bid", "REAL"), ("options_orders", "ref_ask", "REAL"),
          ("options_orders", "ref_quote_at", "TEXT"), ("options_fills", "quote_at", "TEXT"),
          # a live exit that triggered and is not filled yet (JSON: reason, qty, open_at, at): the
          # engine keeps exiting it every poll until filled, even if the trigger flickers off
          ("options_positions", "pending_exit", "TEXT"))

PENDING_SUBMIT = "pending_submit"


def ensure_schema(con) -> None:
    con.executescript(SCHEMA)
    for table, col, typ in _ADDED:
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def cid(position_id: str, tag: str) -> str:
    """Deterministic client_order_id (<= 128 chars at Alpaca): the same
    position and tag always give the same id, so a re-run never double-submits."""
    day = position_id.split(":", 1)[0].replace("-", "")[2:]
    return f"opt-{day}-{hashlib.sha1(position_id.encode()).hexdigest()[:12]}-{tag}"


# ------------------------------------------------------------------ events
def event(con, day: str, at: datetime, level: str, kind: str, message: str) -> None:
    con.execute("INSERT INTO options_events (day, at, level, kind, message) VALUES (?,?,?,?,?)",
                (day, iso(at), level, kind, message[:1000]))


def filtered(con, day: str, at: datetime, stage: str, underlying: str, reason: str,
             setup_id: str = "", variant: str = "", detail: dict | None = None) -> None:
    """Every opportunity the filters drop is logged once (per stage / setup /
    variant / reason), so the filters themselves can be reviewed later."""
    con.execute(
        "INSERT OR IGNORE INTO options_filtered (day, at, stage, underlying, setup_id, variant, reason, detail_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (day, iso(at), stage, underlying, setup_id, variant, reason,
         json.dumps(detail or {}, ensure_ascii=False, default=str)))


def notify_once(con, key: str) -> bool:
    """True the first time ``key`` is seen (caller then sends the push)."""
    if con.execute("SELECT 1 FROM options_notified WHERE key=?", (key,)).fetchone():
        return False
    con.execute("INSERT INTO options_notified VALUES (?,?)", (key, store.now()))
    return True


# ------------------------------------------------------------------ setups
def upsert_setup(con, row: dict) -> None:
    cols = ("setup_id", "day", "source", "plan_id", "plan_index", "underlying", "uclass", "state", "direction",
            "spec_json", "signal_json", "live_eligible", "live_note", "created_at", "updated_at")
    con.execute(f"INSERT OR IGNORE INTO options_setups ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                tuple(row.get(c) for c in cols))


def update_setup(con, setup_id: str, **fields) -> None:
    fields["updated_at"] = store.now()
    sets = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE options_setups SET {sets} WHERE setup_id=?", (*fields.values(), setup_id))


def setups(con, day: str) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM options_setups WHERE day=? ORDER BY setup_id", (day,))]


# ------------------------------------------------------------------ positions
POSITION_COLS = (
    "position_id", "setup_id", "day", "mode", "source", "underlying", "uclass", "dte_label", "band_label", "occ",
    "right", "strike", "expiry", "qty_initial", "qty_open", "state", "entry_time", "entry_price", "entry_bid",
    "entry_ask", "entry_quote_json", "invalidation", "direction", "max_hold_min", "tp_done", "stop_price",
    "exit_time", "exit_reason", "realized_usd", "fees_usd", "spread_cost_usd", "max_bid", "min_bid", "last_bid",
    "last_ask", "flags_json", "note", "updated_at", "exit_variant", "inv_close", "inv_vwap", "trail_ref")


def insert_position(con, row: dict) -> bool:
    row = {**row, "updated_at": store.now()}
    cur = con.execute(
        f"INSERT OR IGNORE INTO options_positions ({','.join(POSITION_COLS)}) VALUES ({','.join('?' * len(POSITION_COLS))})",
        tuple(row.get(c) for c in POSITION_COLS))
    return cur.rowcount == 1


def update_position(con, position_id: str, **fields) -> None:
    fields["updated_at"] = store.now()
    sets = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE options_positions SET {sets} WHERE position_id=?", (*fields.values(), position_id))


def positions(con, day: str, mode: str | None = None, states: tuple[str, ...] | None = None) -> list[dict]:
    q, args = "SELECT * FROM options_positions WHERE day=?", [day]
    if mode:
        q += " AND mode=?"
        args.append(mode)
    if states:
        q += f" AND state IN ({','.join('?' * len(states))})"
        args.extend(states)
    return [dict(r) for r in con.execute(q + " ORDER BY position_id", args)]


def leftovers(con, before_day: str, mode: str, states: tuple[str, ...]) -> list[dict]:
    """Rows of an EARLIER day still in ``states`` (a session that died before
    its forced flat, or a half day nobody flattened)."""
    q = (f"SELECT * FROM options_positions WHERE day < ? AND mode=? AND state IN ({','.join('?' * len(states))}) "
         "ORDER BY day, position_id")
    return [dict(r) for r in con.execute(q, (before_day, mode, *states))]


def position(con, position_id: str) -> dict | None:
    r = con.execute("SELECT * FROM options_positions WHERE position_id=?", (position_id,)).fetchone()
    return dict(r) if r else None


def add_fill(con, position_id: str, at: datetime, side: str, qty: float, price: float,
             bid: float | None, ask: float | None, reason: str, client_order_id: str = "",
             quote_at: str | None = None) -> bool:
    cur = con.execute(
        "INSERT OR IGNORE INTO options_fills (position_id, at, side, qty, price, bid, ask, reason, client_order_id,"
        " quote_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (position_id, iso(at), side, qty, price, bid, ask, reason, client_order_id, quote_at))
    return cur.rowcount == 1


def fills(con, position_id: str) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM options_fills WHERE position_id=? ORDER BY id", (position_id,))]


# ------------------------------------------------------------------ orders
def order(con, client_order_id: str) -> dict | None:
    r = con.execute("SELECT * FROM options_orders WHERE client_order_id=?", (client_order_id,)).fetchone()
    return dict(r) if r else None


def record_order(con, client_order_id: str, position_id: str, kind: str, side: str, qty: float,
                 order_type: str, limit_price: float | None, reason: str, at: datetime,
                 ref_bid: float | None = None, ref_ask: float | None = None,
                 ref_quote_at: datetime | None = None) -> bool:
    """Written as pending_submit and COMMITTED before broker.submit, so a crash
    between the two never loses the record of an order the broker may hold.
    ref_* is the quote the order was sent against, so a fill's slippage is
    measured against the price at the decision, not a later poll."""
    cur = con.execute(
        "INSERT OR IGNORE INTO options_orders (client_order_id, position_id, kind, side, qty, order_type, limit_price,"
        " status, reason, submitted_at, updated_at, ref_bid, ref_ask, ref_quote_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (client_order_id, position_id, kind, side, qty, order_type, limit_price, PENDING_SUBMIT, reason,
         iso(at), store.now(), ref_bid, ref_ask, iso(ref_quote_at) if ref_quote_at else None))
    con.commit()
    return cur.rowcount == 1


def update_order(con, client_order_id: str, **fields) -> None:
    fields["updated_at"] = store.now()
    sets = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE options_orders SET {sets} WHERE client_order_id=?", (*fields.values(), client_order_id))


def orders_for(con, position_id: str) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM options_orders WHERE position_id=? ORDER BY submitted_at, client_order_id", (position_id,))]


# ------------------------------------------------------------------ universe
def save_universe(con, day: str, rows: list[tuple[str, str, str, float | None, float | None]]) -> None:
    """rows: (symbol, uclass, origin, prev_close, premarket dollar volume)."""
    con.executemany("INSERT OR IGNORE INTO options_universe VALUES (?,?,?,?,?,?)",
                    [(day, s, c, o, pc, dv) for s, c, o, pc, dv in rows])


def save_profile(con, day: str, symbol: str, profile: dict, note: str = "") -> None:
    con.execute("INSERT OR REPLACE INTO options_profiles VALUES (?,?,?,?)",
                (day, symbol, json.dumps(profile), note[:500]))


def profile(con, day: str, symbol: str) -> tuple[dict, str] | None:
    r = con.execute("SELECT profile_json, note FROM options_profiles WHERE day=? AND symbol=?", (day, symbol)).fetchone()
    return (json.loads(r[0]), r[1]) if r else None


def universe(con, day: str) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM options_universe WHERE day=? ORDER BY rowid", (day,))]


# ------------------------------------------------------------------ parameter changes
# The options config is code (config.py), so its changes are logged here, once,
# in the desk's param_changes (strategy 'options'): (param, old, new, reason).
AUDIT_20261027 = "2026-10-27 audit: stop-the-bleeding step 1"
PARAM_CHANGES = (
    ("live_sources", ["gap_go", "orb15", "grok_plan"], ["gap_go", "grok_plan"], AUDIT_20261027),
    ("rule_playbooks", ["gap_go", "gap_fade", "orb15", "vwap_reclaim"],
     ["gap_go", "gap_fade", "orb15", "orb15_5m", "vwap_reclaim"], AUDIT_20261027),
    ("close_inv_minutes", None, {"orb15_5m": 5}, AUDIT_20261027),
    ("exit_trigger_price", "mid", "bid (mid only when the bid is missing or zero)", AUDIT_20261027),
    ("exit_order_type", "limit at the bid, repriced, market after 3 unfilled",
     "stops: market; take_profit: one limit at bid - 1 tick, then market", AUDIT_20261027),
    ("exit_max_limit_attempts", 3, 1, AUDIT_20261027),
    ("exit_latch", False, True, AUDIT_20261027),
    ("poll_seconds_in_position", None, 5.0, AUDIT_20261027),
)


def record_param_changes(con) -> int:
    """Insert each PARAM_CHANGES row unless the same (param, new, reason) is
    already logged (the session calls this at every start); returns how many
    were inserted."""
    n = 0
    for param, old, new, reason in PARAM_CHANGES:
        if con.execute("SELECT 1 FROM param_changes WHERE strategy='options' AND param=? AND new=? AND reason=?",
                       (param, json.dumps(new), reason)).fetchone():
            continue
        con.execute("INSERT INTO param_changes (strategy, param, old, new, reason, changed_at) VALUES (?,?,?,?,?,?)",
                    ("options", param, json.dumps(old), json.dumps(new), reason, store.now()))
        n += 1
    return n
