"""Monthly exit check (learn/exit_check.py, EDGE_PLAN 7.4 item 6) and the
weekly process vs outcome lines (learn/process_review.py, item 7). Offline."""
import pytest

from stockmoney.desk import store
from stockmoney.desk.learn import exit_check as ec
from stockmoney.desk.learn import postmortem, tables
from stockmoney.desk.learn import process_review as pr
from stockmoney.desk.options import store as ostore

TODAY = "2026-11-05"


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        ostore.ensure_schema(c)
        yield c


def _pos(con, pid, *, source="vwap_reclaim", ev="A", reason="time_stop", realized=100.0, fees=0.0,
         day="2026-11-03", mode="shadow", setup=None, dte="1-3dte", state="EXITED"):
    con.execute("INSERT INTO options_positions (position_id, setup_id, day, mode, source, dte_label, state, "
                "realized_usd, fees_usd, exit_variant, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (pid, setup, day, mode, source, dte, state, realized, fees, ev, reason))


def test_groups_exclude_unknown_old_and_open(con):
    _pos(con, "a", reason="take_profit", realized=100.0, fees=1.0)
    _pos(con, "b", reason="take_profit", realized=50.0, fees=1.0)
    _pos(con, "c", reason="broker_flat_unreconciled")
    _pos(con, "d", reason="manual_resolved")
    _pos(con, "e", day="2026-09-01")             # older than 30 days
    _pos(con, "f", state="ENTERED")
    r = ec.rows(con, 30, TODAY)
    assert [(g["exit_reason"], g["n"]) for g in r["groups"]] == [("take_profit", 2)]
    g = r["groups"][0]
    assert g["mean"] == pytest.approx(74.0) and g["total"] == pytest.approx(148.0)


def test_flag_when_runners_beat_take_profit_winners(con):
    for i in range(10):
        _pos(con, f"tp{i}", reason="take_profit", realized=100.0)
        _pos(con, f"run{i}", reason="atr_trail", realized=200.0)
        _pos(con, f"loss{i}", reason="time_stop", realized=-50.0)  # losers do not enter the mean winner
    c = ec.rows(con, 30, TODAY)["compare"][0]
    assert c["tp_n"] == 10 and c["run_n"] == 10 and c["run_mean_win"] == pytest.approx(200.0) and c["flag"]
    assert ec.CAP_FLAG in ec.report(con, 30, TODAY)


def test_no_flag_below_ratio_or_sample(con):
    for i in range(10):
        _pos(con, f"tp{i}", reason="breakeven_stop", realized=100.0)
        _pos(con, f"run{i}", reason="time_stop", realized=140.0)
    assert not ec.rows(con, 30, TODAY)["compare"][0]["flag"]           # 1.4x < 1.5x
    _pos(con, "big", source="orb15", reason="atr_trail", realized=900.0)
    _pos(con, "tp", source="orb15", reason="take_profit", realized=10.0)
    o = [c for c in ec.rows(con, 30, TODAY)["compare"] if c["source"] == "orb15"][0]
    assert not o["flag"]                                                 # n < 10
    assert "樣本不足" in ec.report(con, 30, TODAY)


def test_exit_check_cli_and_empty_db(con, tmp_path, capsys):
    _pos(con, "a")
    con.commit()
    before = (tmp_path / "desk.sqlite").read_bytes()
    assert ec.main(["exit_check", "--db", str(tmp_path / "desk.sqlite"), "--days", "36500"]) == 0
    assert "出場檢查" in capsys.readouterr().out
    assert (tmp_path / "desk.sqlite").read_bytes() == before
    with store.connect(tmp_path / "bare.sqlite") as c:
        assert "沒有已出場部位" in ec.report(c, 30, TODAY)


def test_process_review_lines(con):
    _pos(con, "live1", mode="live", setup="s1", realized=-100.0)
    _pos(con, "twin1", setup="s1", realized=-90.0)                 # live twin: dropped
    _pos(con, "bshadow", ev="B", realized=500.0)                   # not the live exit variant
    _pos(con, "g1", source="gap_go", reason="carry_over", realized=20.0)
    _pos(con, "g2", source="gap_go", reason="broker_flat_unreconciled")
    _pos(con, "old", source="orb15", day="2026-10-20")
    ostore.event(con, "2026-11-03", __import__("datetime").datetime(2026, 11, 3, 15), "critical", "exit_refused",
                 "live1 SPY: refused")
    rs = {r["source"]: r for r in pr.week_rows(con, TODAY)}
    assert set(rs) == {"vwap_reclaim", "gap_go"}
    v, g = rs["vwap_reclaim"], rs["gap_go"]
    assert (v["n"], v["errors"], v["unknown"], v["non_rule"]) == (1, 1, 0, 0) and v["pnl"] == pytest.approx(-100.0)
    assert (g["n"], g["unknown"], g["non_rule"]) == (1, 1, 1)
    text = "\n".join(pr.lines(con, TODAY))
    assert "流程:未照規則" in text and "結果:n=1" in text and "一件要改的事:(週五覆盤填寫)" in text
    _pos(con, "clean", source="orb15", realized=30.0)
    assert any("流程:照規則" in x for x in pr.lines(con, TODAY))


def test_symptom_report_appends_process_lines(con):
    tables.ensure_schema(con)
    con.execute("INSERT INTO trade_postmortem (trade_id, symbol, strategy, entry_day, exit_day, pnl, symptoms) "
                "VALUES ('t1','SPY','x','2026-11-02','2026-11-03',-10,'S0')")
    _pos(con, "a")
    text, _ = postmortem.symptom_report(con, TODAY)
    assert text.startswith("## 驗屍卡週報") and "期權劇本流程與結果" in text
