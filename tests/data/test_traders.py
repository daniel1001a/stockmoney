from __future__ import annotations

import duckdb
import pytest

from stockmoney.data.db import run_migrations
from stockmoney.data.traders import (
    add_trader,
    deactivate_trader,
    get_trader,
    list_active_traders,
    list_all_traders,
)


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def test_seeded_traders_present_and_active():
    conn = _conn()
    active = {t.trader_id for t in list_active_traders(conn)}
    assert active == {"chartist", "analyst", "reversion", "flow", "sentiment"}
    assert get_trader(conn, "chartist").engine_key == "chartist"


def test_add_trader_is_idempotent():
    conn = _conn()
    first = add_trader(conn, trader_id="contrarian", name="Contrarian",
                       philosophy="fade the crowd", engine_key="contrarian")
    second = add_trader(conn, trader_id="contrarian", name="DIFFERENT",
                        philosophy="different", engine_key="different")
    assert first == second == "contrarian"
    # re-adding must not overwrite the original row
    assert get_trader(conn, "contrarian").name == "Contrarian"


def test_deactivate_retires_without_deleting():
    conn = _conn()
    deactivate_trader(conn, "analyst")
    active = {t.trader_id for t in list_active_traders(conn)}
    assert active == {"chartist", "reversion", "flow", "sentiment"}
    # still present in the full roster (history stays attributable)
    all_ids = {t.trader_id for t in list_all_traders(conn)}
    assert "analyst" in all_ids
    retired = get_trader(conn, "analyst")
    assert retired.active is False and retired.removed_date is not None


def test_deactivate_unknown_trader_raises():
    conn = _conn()
    with pytest.raises(ValueError):
        deactivate_trader(conn, "nope")
