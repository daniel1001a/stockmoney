"""Contract tests: does queries.py's dict output actually match the shape
frontend/src/lib/api.ts's TypeScript interfaces claim it has?

api.ts's own header admits the two are kept in sync "by hand (no shared
schema generation yet, v1)" -- nothing before this file caught drift between
them; a field renamed on one side just showed up as `undefined` in the UI.
This doesn't replace real schema generation (a bigger, separate investment),
but it turns silent drift into a failing test for the two shapes touched on
every dashboard feature. Extending to more interfaces is a matter of adding
another `_ts_interface_fields(...)` assertion below.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import duckdb

from stockmoney.api import queries
from stockmoney.data import trader_predictions as tp
from stockmoney.data.daily_predictions import record_prediction
from stockmoney.data.db import run_migrations

API_TS = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "api.ts").read_text()

_FIELD_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\??\s*:")


def _ts_interface_fields(name: str) -> set[str]:
    """Top-level field names of `export interface {name} { ... }` in api.ts.
    Depth-aware over nested `{ ... }` object-literal field types (e.g.
    Opportunity.top_news) so they aren't misread as sibling fields. Does NOT
    resolve `extends` -- only flat interfaces are usable here, which is the
    scope this contract test needs today."""
    m = re.search(rf"export interface {name}\b\s*{{", API_TS)
    if not m:
        raise AssertionError(f"interface {name} not found in api.ts")
    fields: set[str] = set()
    depth = 0
    for line in API_TS[m.end():].splitlines():
        if depth == 0:
            field_match = _FIELD_RE.match(line)
            if field_match:
                fields.add(field_match.group(1))
        depth += line.count("{") - line.count("}")
        if depth < 0:
            break
    return fields


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def test_opportunity_dict_matches_ts_interface():
    conn = _conn()
    record_prediction(
        conn, trade_date=date(2026, 7, 9), symbol="NVDA", sector="semiconductor", horizon=5,
        label_end_date=date(2026, 7, 16), regime=1, proba=(0.2, 0.3, 0.5),
        entry_price=180.0, feature_values={"realized_vol_20d": 0.3, "adx_14": 20.0, "xsec_dispersion": 0.01,
                                            "yield_curve_10y2y": 0.5, "dxy_chg_1d": 0.0, "oil_chg_1d": 0.0},
        model_version="test-v1",
    )
    [item] = queries.opportunities(conn)
    assert set(item.keys()) == _ts_interface_fields("Opportunity")


def test_leaderboard_entry_dict_matches_ts_interface():
    conn = _conn()
    pid = tp.record_trader_prediction(
        conn, trader_id="chartist", method_version="chartist:gmm-logistic-v2",
        trade_date=date(2026, 6, 1), symbol="SOXL", sector="semiconductor", horizon=5,
        label_end_date=date(2026, 6, 8), direction="up", conviction=0.7, rationale="r",
        invalidation="i", entry_price=100.0, grade_vol=0.4, engine_payload={}, regime=0,
    )
    tp.grade_trader_prediction(conn, pid, actual_price=130.0)

    [row] = [r for r in queries.leaderboard(conn) if r["trader_id"] == "chartist"]
    assert set(row.keys()) == _ts_interface_fields("LeaderboardEntry")
