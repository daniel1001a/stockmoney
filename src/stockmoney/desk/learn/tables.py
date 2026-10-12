"""Tables owned by the learning layer (SQLite, same file as the desk state)."""
from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_postmortem (
  trade_id TEXT PRIMARY KEY, symbol TEXT, strategy TEXT, entry_day TEXT, exit_day TEXT,
  entry_px REAL, exit_px REAL, qty REAL, pnl REAL, ret REAL, r_multiple REAL,
  exit_kind TEXT, exit_reason TEXT, symptoms TEXT, facts_json TEXT, status TEXT,
  lesson TEXT DEFAULT '', created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS shadow_equity (
  name TEXT, date TEXT, equity REAL, PRIMARY KEY (name, date));
CREATE TABLE IF NOT EXISTS chains (
  chain_id TEXT PRIMARY KEY, created_at TEXT, source TEXT, trigger_text TEXT, urls_json TEXT,
  steps_json TEXT, impacts_json TEXT, checkpoints_json TEXT, confidence REAL);
CREATE TABLE IF NOT EXISTS chain_scores (
  chain_id TEXT, ticker TEXT, horizon INTEGER, declared INTEGER, direction TEXT, base_date TEXT,
  scored_date TEXT, excess_ret REAL, hit INTEGER, confidence REAL, scored_at TEXT,
  PRIMARY KEY (chain_id, ticker, horizon));
CREATE TABLE IF NOT EXISTS alloc_log (
  date TEXT PRIMARY KEY, cell TEXT, weights_json TEXT, detail_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS learn_reports (
  date TEXT PRIMARY KEY, body TEXT, created_at TEXT);
"""


def ensure_schema(con) -> None:
    from stockmoney.desk.learn import calls, earnings
    con.executescript(SCHEMA)
    earnings.ensure_schema(con)
    calls.ensure_schema(con)
