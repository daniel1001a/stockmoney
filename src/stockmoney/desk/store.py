"""Desk operational state (SQLite: small, transactional, safe for the console
and the daily job to touch concurrently).

Separation of concerns (CLAUDE.md section 7):
- ``accounts`` / ``signals``: machine output. Humans never edit these rows.
- ``decisions``: every human approve/reject/size change, as its own time
  series, so "did the human add value?" can be measured later.
- ``journal`` / ``param_changes``: the learning loop. A parameter change is
  logged with a reason; nothing is tuned silently.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from stockmoney.desk.broker import Account

DEFAULT_PATH = Path("data/desk/desk.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  name TEXT PRIMARY KEY, kind TEXT, state_json TEXT, last_date TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS signals (
  signal_id TEXT PRIMARY KEY, date TEXT, strategy TEXT, symbol TEXT, ref_price REAL,
  stop REAL, score REAL, thesis TEXT, invalidation TEXT, layers_json TEXT,
  ai_review TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT, order_id TEXT, decision TEXT,
  multiplier REAL, note TEXT, decided_at TEXT);
CREATE TABLE IF NOT EXISTS journal (
  date TEXT PRIMARY KEY, auto_summary TEXT, market_view TEXT, lessons TEXT,
  mistakes TEXT, tags TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS param_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT, param TEXT, old TEXT, new TEXT,
  reason TEXT, changed_at TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, through_date TEXT, status TEXT, message TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path = DEFAULT_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    try:
        yield con
        con.commit()
    finally:
        con.close()


# ------------------------------------------------------------------ accounts
def save_account(con, acct: Account, kind: str, last_date: str) -> None:
    con.execute(
        "INSERT INTO accounts VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
        "state_json=excluded.state_json, last_date=excluded.last_date, updated_at=excluded.updated_at, kind=excluded.kind",
        (acct.name, kind, json.dumps(acct.to_dict(), ensure_ascii=False), last_date, now()),
    )


def load_accounts(con) -> dict[str, tuple[Account, str, str]]:
    rows = con.execute("SELECT name, kind, state_json, last_date FROM accounts").fetchall()
    return {r["name"]: (Account.from_dict(json.loads(r["state_json"])), r["kind"], r["last_date"]) for r in rows}


# ------------------------------------------------------------------ signals
def save_signal(con, sid: str, s) -> None:
    con.execute(
        "INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, str(s.date.date()), s.strategy, s.symbol, s.ref_price, s.stop, s.score,
         s.thesis, s.invalidation, json.dumps(s.layers, ensure_ascii=False), None, now()),
    )


# ------------------------------------------------------------------ settings
def get_setting(con, key: str, default: str = "") -> str:
    r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(con, key: str, value: str) -> None:
    con.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def strategy_params(con, *, paper: bool = False) -> dict[str, dict]:
    params = json.loads(get_setting(con, "strategy_params", "{}"))
    if paper:
        for name, values in json.loads(get_setting(con, "paper_strategy_params", "{}")).items():
            params.setdefault(name, {}).update(values)
    return params


def change_param(con, strategy: str, param: str, new, reason: str, *, paper: bool = False) -> None:
    key = "paper_strategy_params" if paper else "strategy_params"
    params = json.loads(get_setting(con, key, "{}"))
    old = params.get(strategy, {}).get(param)
    params.setdefault(strategy, {})[param] = new
    set_setting(con, key, json.dumps(params))
    con.execute(
        "INSERT INTO param_changes (strategy, param, old, new, reason, changed_at) VALUES (?,?,?,?,?,?)",
        (strategy, param, json.dumps(old), json.dumps(new), reason, now()),
    )


# ------------------------------------------------------------------ decisions
def record_decision(con, signal_id: str, order_id: str, decision: str, multiplier: float, note: str) -> None:
    con.execute(
        "INSERT INTO decisions (signal_id, order_id, decision, multiplier, note, decided_at) VALUES (?,?,?,?,?,?)",
        (signal_id, order_id, decision, multiplier, note, now()),
    )
