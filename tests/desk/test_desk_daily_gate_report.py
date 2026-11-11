"""Daily gate report (scripts/desk_daily_gate_report.py, EVOLUTION_PLAN
section 6 item 2f): day/source grouping, cumulative P&L, best-day exclusion,
10-minute invalidation count, and read-only CLI behaviour. Offline."""
import json
import sys
from pathlib import Path

import pytest

from stockmoney.desk import store
from stockmoney.desk.options import store as ostore

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from desk_daily_gate_report import daily_rows, main, report  # noqa: E402


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        ostore.ensure_schema(c)
        yield c


def _pos(con, pid, *, source="vwap_reclaim", mode="shadow", day="2026-11-02", state="EXITED", realized=100.0,
         fees=0.6, ev="A", reason="take_profit", qty=10, entry_time=None, exit_time=None, setup=None, flags=None):
    con.execute("INSERT INTO options_positions (position_id, setup_id, day, mode, source, dte_label, state, "
                "realized_usd, fees_usd, exit_variant, exit_reason, entry_price, entry_bid, entry_ask, qty_initial, "
                "entry_time, exit_time, flags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, setup, day, mode, source, "1-3dte", state, realized, fees, ev, reason, 1.0, 0.95, 1.05, qty,
                 entry_time, exit_time, json.dumps(flags or {})))


def test_daily_rows_groups_by_day_and_source(con):
    _pos(con, "a", day="2026-11-02", realized=100.0, fees=0.6)   # net 99.4
    _pos(con, "b", day="2026-11-02", realized=-20.0, fees=0.6)   # net -20.6, same day: aggregates with a
    _pos(con, "c", day="2026-11-03", realized=50.0, fees=0.6)    # next day, running sum continues
    _pos(con, "d", day="2026-11-02", source="orb15", realized=10.0, fees=0.6)  # different source: separate series
    rows = {(r["day"], r["source"]): r for r in daily_rows(con)}

    d1 = rows[("2026-11-02", "vwap_reclaim")]
    assert d1["n"] == 2 and d1["daily_net_pnl"] == pytest.approx(99.4 - 20.6)
    assert d1["cumulative_pnl"] == pytest.approx(78.8) and d1["total_excluding_best_day"] == pytest.approx(0.0)

    d2 = rows[("2026-11-03", "vwap_reclaim")]
    assert d2["daily_net_pnl"] == pytest.approx(49.4)
    assert d2["cumulative_pnl"] == pytest.approx(78.8 + 49.4)
    # best day so far is day 1 (78.8); day 2 alone (49.4) is smaller, so excluding the best day leaves day2 - day1
    assert d2["total_excluding_best_day"] == pytest.approx(49.4)

    other = rows[("2026-11-02", "orb15")]
    assert other["n"] == 1 and other["daily_net_pnl"] == pytest.approx(9.4)


def test_daily_rows_excludes_out_of_scope_rows(con):
    _pos(con, "live", mode="live", setup="s1", day="2026-11-02", realized=-50.0)
    _pos(con, "twin", setup="s1", day="2026-11-02", realized=-40.0)   # same setup/dte as live: dedup drops it
    _pos(con, "open", state="ENTERED", day="2026-11-02")               # still open
    _pos(con, "unknown", reason="manual_resolved", day="2026-11-02")   # UNKNOWN_PNL
    _pos(con, "b_variant", ev="B", day="2026-11-02")                   # not the live exit variant
    rows = daily_rows(con)
    assert len(rows) == 1 and rows[0]["n"] == 1 and rows[0]["daily_net_pnl"] == pytest.approx(-50.6)


def test_first_10m_invalidation_count(con):
    _pos(con, "quick", day="2026-11-02", reason="invalidation",
         entry_time="2026-11-02T09:40:00+00:00", exit_time="2026-11-02T09:45:00+00:00")   # 5 min: counts
    _pos(con, "slow", day="2026-11-02", reason="invalidation",
         entry_time="2026-11-02T09:40:00+00:00", exit_time="2026-11-02T10:00:00+00:00")   # 20 min: does not count
    _pos(con, "other_reason", day="2026-11-02", reason="take_profit",
         entry_time="2026-11-02T09:40:00+00:00", exit_time="2026-11-02T09:41:00+00:00")   # not invalidation
    _pos(con, "no_times", day="2026-11-02", reason="invalidation")                        # missing timestamps
    rows = daily_rows(con)
    assert len(rows) == 1 and rows[0]["first_10m_invalidation_count"] == 1


def test_report_contains_both_tables(con):
    _pos(con, "a", day="2026-11-02", realized=100.0)
    text = report(con)
    assert "每日逐策略關卡報告" in text and "| 2026-11-02 | vwap_reclaim | 1 |" in text
    assert "來源加總" in text and "| vwap_reclaim |" in text


def test_report_on_a_database_without_options_tables(tmp_path):
    with store.connect(tmp_path / "bare.sqlite") as c:
        assert daily_rows(c) == [] and "每日逐策略關卡報告" in report(c)


def test_cli_reads_without_writing(con, tmp_path, capsys):
    _pos(con, "a", day="2026-11-02", realized=100.0)
    con.commit()
    before = (tmp_path / "desk.sqlite").read_bytes()
    assert main(["desk_daily_gate_report", "--db", str(tmp_path / "desk.sqlite")]) == 0
    out = capsys.readouterr().out
    assert "| 2026-11-02 | vwap_reclaim | 1 |" in out
    assert (tmp_path / "desk.sqlite").read_bytes() == before


def test_cli_db_connection_is_read_only(con, tmp_path):
    con.commit()
    from desk_daily_gate_report import sqlite3
    ro = sqlite3.connect(f"file:{tmp_path / 'desk.sqlite'}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO options_positions (position_id) VALUES ('x')")
        ro.commit()
