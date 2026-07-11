from __future__ import annotations

from datetime import date

import duckdb
import pytest

from stockmoney.data.db import run_migrations
from stockmoney.data import trader_methods as tm


def _conn():
    conn = duckdb.connect(":memory:")
    run_migrations(conn)
    return conn


def test_seeded_current_versions():
    conn = _conn()
    assert tm.current_method_version(conn, "chartist") == "chartist:gmm-logistic-v2"
    assert tm.current_method_version(conn, "analyst") == "analyst:catalyst-v1"


def test_propose_is_idempotent_per_day():
    conn = _conn()
    a = tm.propose_method_update(conn, trader_id="analyst", from_version="analyst:catalyst-v1",
                                 rationale="x", source_review_date=date(2026, 6, 8))
    b = tm.propose_method_update(conn, trader_id="analyst", from_version="analyst:catalyst-v1",
                                 rationale="y", source_review_date=date(2026, 6, 8))
    assert a == b
    assert len(tm.list_proposals(conn, status="proposed")) == 1


def test_accept_is_forward_only_and_supersedes():
    conn = _conn()
    pid = tm.propose_method_update(conn, trader_id="analyst", from_version="analyst:catalyst-v1",
                                   rationale="add options-flow signal")
    tm.accept_proposal(conn, pid, to_version="analyst:catalyst-v2",
                       spec="adds options-flow", reviewed_by="human",
                       effective_date=date(2026, 7, 1))
    # new version is now current; old one superseded (not deleted -> old
    # predictions still pin their original version)
    assert tm.current_method_version(conn, "analyst") == "analyst:catalyst-v2"
    statuses = dict(conn.execute(
        "SELECT method_version, status FROM trader_method_versions WHERE trader_id='analyst'"
    ).fetchall())
    assert statuses == {"analyst:catalyst-v1": "superseded", "analyst:catalyst-v2": "active"}
    assert tm.list_proposals(conn, status="accepted")[0]["to_version"] == "analyst:catalyst-v2"


def test_accept_twice_rejected():
    conn = _conn()
    pid = tm.propose_method_update(conn, trader_id="analyst", from_version="analyst:catalyst-v1",
                                   rationale="x")
    tm.accept_proposal(conn, pid, to_version="analyst:catalyst-v2", spec="s", reviewed_by="h")
    with pytest.raises(ValueError):
        tm.accept_proposal(conn, pid, to_version="analyst:catalyst-v3", spec="s", reviewed_by="h")


def test_reject_proposal():
    conn = _conn()
    pid = tm.propose_method_update(conn, trader_id="analyst", from_version="analyst:catalyst-v1",
                                   rationale="x")
    tm.reject_proposal(conn, pid, reviewed_by="human")
    assert tm.list_proposals(conn, status="proposed") == []
    assert tm.list_proposals(conn, status="rejected")[0]["proposal_id"] == pid
    # rejecting does not change the live version
    assert tm.current_method_version(conn, "analyst") == "analyst:catalyst-v1"
