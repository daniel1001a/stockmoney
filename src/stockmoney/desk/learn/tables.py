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
  steps_json TEXT, impacts_json TEXT, checkpoints_json TEXT, confidence REAL,
  invalidations_json TEXT DEFAULT '[]', falsifiable INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS chain_scores (
  chain_id TEXT, ticker TEXT, horizon INTEGER, declared INTEGER, direction TEXT, base_date TEXT,
  scored_date TEXT, excess_ret REAL, hit INTEGER, confidence REAL, scored_at TEXT,
  PRIMARY KEY (chain_id, ticker, horizon));
CREATE TABLE IF NOT EXISTS chain_invalidations (
  chain_id TEXT, idx INTEGER, kind TEXT, subject TEXT, level REAL, by_date TEXT,
  verdict TEXT, detail TEXT, judged_on TEXT, judged_at TEXT,
  PRIMARY KEY (chain_id, idx));
CREATE TABLE IF NOT EXISTS corroboration (
  ref TEXT, ref_kind TEXT, ticker TEXT, muse_msg_id TEXT, muse_type TEXT,
  shared_urls_json TEXT, shared_authors_json TEXT, independent INTEGER, created_at TEXT,
  PRIMARY KEY (ref, ticker, muse_msg_id));
CREATE TABLE IF NOT EXISTS alloc_log (
  date TEXT PRIMARY KEY, cell TEXT, weights_json TEXT, detail_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS learn_reports (
  date TEXT PRIMARY KEY, body TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS link_checks (
  url TEXT PRIMARY KEY, msg_id TEXT, agent TEXT, domain TEXT, status TEXT, http_code INTEGER,
  detail TEXT, checked_day TEXT, checked_at TEXT);
CREATE TABLE IF NOT EXISTS blocked_counterfactual (
  client_order_id TEXT PRIMARY KEY, plan_date TEXT, symbol TEXT, strategy TEXT, block_class TEXT,
  severity TEXT, note TEXT, ref_price REAL, stop REAL, entry_day TEXT, entry_px REAL,
  ret_5 REAL, ret_20 REAL, excess_5 REAL, excess_20 REAL, stop_ret_5 REAL, stop_ret_20 REAL,
  stop_hit INTEGER, first_block INTEGER, status TEXT, updated_at TEXT);
"""

# Columns added after a table first shipped; CREATE TABLE IF NOT EXISTS will not add them.
ADDED_COLUMNS = {
    "chains": [("invalidations_json", "TEXT DEFAULT '[]'"), ("falsifiable", "INTEGER DEFAULT 0")],
    # 1 / 0 / NULL (inputs unavailable): Grok and Muse independently named the symbol in the 3 days before entry
    "trade_postmortem": [("grok_muse_independent_same_symbol", "INTEGER")],
    # 1: the stock buy was not blocked; only the pre-2026-10-21 Muse rule would have blocked it
    "blocked_counterfactual": [("legacy_only", "INTEGER DEFAULT 0")],
}


def _add_missing_columns(con) -> None:
    for table, cols in ADDED_COLUMNS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in cols:
            if name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def ensure_schema(con) -> None:
    from stockmoney.desk import veto
    from stockmoney.desk.learn import calls, earnings, hypotheses, market_risk, prescreen
    con.executescript(SCHEMA)
    _add_missing_columns(con)
    earnings.ensure_schema(con)
    calls.ensure_schema(con)
    prescreen.ensure_schema(con)
    veto.ensure_schema(con)
    hypotheses.ensure_schema(con)
    market_risk.ensure_schema(con)
