"""Options post-mortem cards (closed live option positions) and the Grok/Muse
same-symbol independence column on trade_postmortem. Offline, synthetic rows."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from stockmoney.desk import store
from stockmoney.desk.bridge import inbox
from stockmoney.desk.execution.ledger import ensure_schema as ledger_schema
from stockmoney.desk.indicators import compute
from stockmoney.desk.learn import earnings, options_postmortem as opm, postmortem, tables
from stockmoney.desk.options import store as ostore
from stockmoney.desk.options.config import FEED_CAVEAT
from stockmoney.desk.run import ensure_schema as run_schema

from test_learn import fill, make_bars

ENTRY = datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc)
DAY = "2026-10-14"


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "d.sqlite") as c:
        run_schema(c)
        ledger_schema(c)
        tables.ensure_schema(c)
        ostore.ensure_schema(c)
        inbox.ensure_schema(c)
        yield c


def pos(con, setup="s1", mode="live", variant="A", state="EXITED", entry_px=2.0, realized=-100.0, fees=1.0,
        exit_reason="premium_stop", hold_min=60, max_bid=2.1, flags=None, und="NVDA", day=DAY, entry=ENTRY):
    pid = f"{setup}:{mode}:1-3dte:{variant}"
    exit_t = entry + timedelta(minutes=hold_min) if state == "EXITED" else None
    ostore.insert_position(con, {
        "position_id": pid, "setup_id": setup, "day": day, "mode": mode, "source": "grok", "underlying": und,
        "uclass": "stock", "dte_label": "1-3dte", "occ": f"{und}261016C00100000", "right": "call", "strike": 100.0,
        "expiry": "2026-10-16", "qty_initial": 1, "qty_open": 0 if state == "EXITED" else 1, "state": state,
        "entry_time": ostore.iso(entry), "entry_price": entry_px, "entry_bid": entry_px and entry_px - 0.05,
        "entry_ask": entry_px and entry_px + 0.05, "direction": "long", "exit_time": ostore.iso(exit_t) if exit_t else None,
        "exit_reason": exit_reason if state == "EXITED" else None, "realized_usd": realized, "fees_usd": fees,
        "max_bid": max_bid, "min_bid": 0.5, "flags_json": json.dumps(flags or {"muse_flag": ""}),
        "exit_variant": variant})
    return pid, exit_t


def fills(con, pid, exit_t, entry_px=2.0, exit_px=1.0, buy_q=(1.95, 2.05), sell_q=None, entry=ENTRY):
    ostore.add_fill(con, pid, entry, "buy", 1, entry_px, *buy_q, "entry", client_order_id="b")
    if exit_t is not None:
        sq = sell_q or (exit_px - 0.05, exit_px + 0.05)
        ostore.add_fill(con, pid, exit_t, "sell", 1, exit_px, *sq, "exit", client_order_id="s")


def loser(con, **kw):
    pid, x = pos(con, **kw)
    fills(con, pid, x)
    return pid


def card(con, pid):
    return con.execute("SELECT * FROM trade_postmortem WHERE trade_id=?", ("opt:" + pid,)).fetchone()


def codes(con, pid):
    return card(con, pid)["symptoms"].split(",")


def test_plain_loss_is_o0_and_card_fields(con):
    pid = loser(con)
    assert opm.update_option_cards(con) == {"new": 1, "updated": 0}
    c = card(con, pid)
    assert codes(con, pid) == ["O0"]
    assert (c["symbol"], c["strategy"], c["entry_day"], c["exit_px"], c["exit_kind"]) == (
        "NVDA", "options:grok", DAY, 1.0, "stop")
    assert c["pnl"] == pytest.approx(-101.0) and c["ret"] == pytest.approx(-0.5)
    assert c["status"] == "final"


def test_winner_without_symptoms_has_no_code(con):
    pid, x = pos(con, realized=100.0, exit_reason="time_stop")
    fills(con, pid, x, exit_px=3.0)
    opm.update_option_cards(con)
    assert card(con, pid)["symptoms"] == ""


def test_no_cards_for_shadow_open_or_abandoned(con):
    pos(con, setup="a", mode="shadow")
    pos(con, setup="b", state="ENTERED")
    pos(con, setup="c", state="ABANDONED", entry_px=None)
    assert opm.update_option_cards(con) == {"new": 0, "updated": 0}
    assert con.execute("SELECT COUNT(*) FROM trade_postmortem").fetchone()[0] == 0


def test_missing_options_tables_is_a_noop(tmp_path):
    with store.connect(tmp_path / "e.sqlite") as c:
        tables.ensure_schema(c)
        assert opm.update_option_cards(c) == {"new": 0, "updated": 0}


def test_o2_stop_while_shadow_twin_won(con):
    pid = loser(con)
    pos(con, mode="shadow", variant="B", realized=150.0, exit_reason="atr_trail")
    opm.update_option_cards(con)
    assert "O2" in codes(con, pid) and "O0" not in codes(con, pid)


def test_o2_negative_twin_lost_or_exit_not_a_stop(con):
    pid = loser(con)
    pos(con, mode="shadow", variant="B", realized=-50.0)
    pid2 = loser(con, setup="s2", exit_reason="time_stop")
    pos(con, setup="s2", mode="shadow", variant="B", realized=150.0)
    opm.update_option_cards(con)
    assert codes(con, pid) == ["O0"] and codes(con, pid2) == ["O0"]


def test_provisional_while_twin_open_then_final_and_idempotent(con):
    pid = loser(con)
    twin, _ = pos(con, mode="shadow", variant="B", state="ENTERED")
    assert opm.update_option_cards(con) == {"new": 1, "updated": 0}
    assert card(con, pid)["status"] == "provisional"
    assert opm.update_option_cards(con) == {"new": 0, "updated": 1}
    con.execute("UPDATE options_positions SET state='EXITED', realized_usd=200, exit_reason='atr_trail' "
                "WHERE position_id=?", (twin,))
    assert opm.update_option_cards(con) == {"new": 0, "updated": 1}
    c = card(con, pid)
    assert c["status"] == "final" and "O2" in codes(con, pid)
    con.execute("UPDATE trade_postmortem SET lesson='keep'")
    assert opm.update_option_cards(con) == {"new": 0, "updated": 0}
    assert card(con, pid)["lesson"] == "keep"
    assert con.execute("SELECT COUNT(*) FROM trade_postmortem").fetchone()[0] == 1


def _scan(con, start, end):
    earnings.ensure_schema(con)
    d = start
    while d <= end:
        con.execute("INSERT OR REPLACE INTO earnings_scan VALUES (?,?,?,?)", (d.isoformat(), 1, 0, "x"))
        d += timedelta(days=1)


def test_o6_earnings_inside_hold(con):
    _scan(con, date(2026, 10, 14), date(2026, 10, 16))
    con.execute("INSERT INTO earnings VALUES ('NVDA', '2026-10-15', '', 'x', 'x', NULL)")
    pid = loser(con, hold_min=60 * 24)
    opm.update_option_cards(con)
    assert "O6" in codes(con, pid)
    assert json.loads(card(con, pid)["facts_json"])["earnings_in_hold"] == ["2026-10-15"]


def test_o6_negative_outside_span_or_not_scanned(con):
    _scan(con, date(2026, 10, 14), date(2026, 10, 14))
    con.execute("INSERT INTO earnings VALUES ('NVDA', '2026-10-20', '', 'x', 'x', NULL)")
    pid = loser(con)
    pid2 = loser(con, setup="s2", day="2026-10-13", entry=ENTRY - timedelta(days=1))  # 10-13 never scanned
    opm.update_option_cards(con)
    assert codes(con, pid) == ["O0"] and codes(con, pid2) == ["O0"]
    assert json.loads(card(con, pid)["facts_json"])["earnings_in_hold"] == []
    assert json.loads(card(con, pid2)["facts_json"])["earnings_in_hold"] is None


def test_o7_gave_back_a_big_move(con):
    pid = loser(con, max_bid=3.0)  # 1.5x the 2.0 entry
    opm.update_option_cards(con)
    assert "O7" in codes(con, pid) and "O0" not in codes(con, pid)


def test_o7_negative_small_move_or_kept_profit(con):
    pid = loser(con, max_bid=2.9)
    pid2, x = pos(con, setup="s2", max_bid=4.0, realized=50.0, exit_reason="atr_trail")
    fills(con, pid2, x, exit_px=2.5)
    opm.update_option_cards(con)
    assert codes(con, pid) == ["O0"]
    assert "O7" not in card(con, pid2)["symptoms"].split(",")


def test_o8_fill_outside_quote_or_wide_spread(con):
    a, xa = pos(con, setup="a")
    fills(con, a, xa, buy_q=(1.9, 1.99))  # bought 2.0 above a 1.99 ask
    b, xb = pos(con, setup="b")
    fills(con, b, xb, sell_q=(1.02, 1.1))  # sold 1.0 below a 1.02 bid
    c, xc = pos(con, setup="c")
    fills(con, c, xc, buy_q=(1.7, 2.3))  # 30% of mid
    opm.update_option_cards(con)
    for pid in (a, b, c):
        assert "O8" in codes(con, pid)
    assert json.loads(card(con, a)["facts_json"])["fill_problems"][0]["why"] == ["buy_above_ask"]


def test_o8_negative_inside_quote_and_unquoted_fill_skipped(con):
    pid = loser(con)
    p2, x2 = pos(con, setup="s2")
    ostore.add_fill(con, p2, ENTRY, "buy", 1, 2.0, None, None, "entry", client_order_id="b")
    ostore.add_fill(con, p2, x2, "sell", 1, 1.0, 0.95, 1.05, "exit", client_order_id="s")
    opm.update_option_cards(con)
    assert codes(con, pid) == ["O0"] and codes(con, p2) == ["O0"]
    assert json.loads(card(con, p2)["facts_json"])["fills_without_quote"] == 1


def _bridge(con, msg_id, typ, at, body):
    con.execute("INSERT INTO bridge_messages (msg_id, type, sender, sent_at, received_at, raw_body, parsed_json) "
                "VALUES (?,?,?,?,?,?,?)", (msg_id, typ, "x", ostore.iso(at), ostore.iso(at), "", json.dumps(body)))


def _alert(ticker, url="https://news.example.com/a"):
    return {"type": "ALERT", "ticker": ticker, "severity": "high", "headline": "guidance cut", "source_urls": [url]}


def test_o9_muse_flag_or_alert_before_entry(con):
    pid = loser(con, flags={"muse_flag": "Muse ALERT high: x"})
    _bridge(con, "m1", "ALERT", ENTRY - timedelta(hours=2), _alert("NVDA"))
    pid2 = loser(con, setup="s2")
    opm.update_option_cards(con)
    assert "O9" in codes(con, pid) and "O9" in codes(con, pid2)


def test_o9_negative_alert_after_entry_too_old_or_other_symbol(con):
    _bridge(con, "m1", "ALERT", ENTRY + timedelta(minutes=5), _alert("NVDA"))
    _bridge(con, "m2", "ALERT", ENTRY - timedelta(hours=30), _alert("NVDA"))
    _bridge(con, "m3", "ALERT", ENTRY - timedelta(hours=1), _alert("AMD"))
    pid = loser(con)
    opm.update_option_cards(con)
    assert codes(con, pid) == ["O0"]


def test_oq_quick_exit(con):
    pid = loser(con, hold_min=10)
    pid2 = loser(con, setup="s2", hold_min=11)
    opm.update_option_cards(con)
    assert "OQ" in codes(con, pid) and "O0" not in codes(con, pid)
    assert codes(con, pid2) == ["O0"]


def test_o0_only_for_unexplained_losses(con):
    flat, x = pos(con, realized=0.0, fees=0.0, exit_reason="time_stop")
    fills(con, flat, x, exit_px=2.0)
    win, xw = pos(con, setup="w", realized=80.0, exit_reason="time_stop")
    fills(con, win, xw, exit_px=2.8)
    explained = loser(con, setup="e", hold_min=5)
    opm.update_option_cards(con)
    assert codes(con, flat) == ["O0"]  # zero gross minus zero fees is not a win
    assert "O0" not in card(con, win)["symptoms"].split(",")
    assert codes(con, explained) == ["OQ"]


# grok_muse_independent_same_symbol ------------------------------------------------------------

def _grok_chain(con, cid, ticker, at, url):
    con.execute("INSERT INTO chains (chain_id, created_at, source, urls_json, impacts_json) VALUES (?,?,?,?,?)",
                (cid, ostore.iso(at), "grok", json.dumps([url]), json.dumps([{"ticker": ticker}])))


def _idea(con, msg_id, ticker, at, url):
    _bridge(con, msg_id, "IDEA", at, {"type": "IDEA", "ticker": ticker, "source_urls": [url]})


def _gm(con, pid):
    return card(con, pid)["grok_muse_independent_same_symbol"]


def test_gm_one_when_independent_sources_before_entry(con):
    _grok_chain(con, "c1", "NVDA", ENTRY - timedelta(days=1), "https://a.example.com/x")
    _idea(con, "m1", "NVDA", ENTRY - timedelta(days=2), "https://b.example.com/y")
    pid = loser(con)
    opm.update_option_cards(con)
    assert _gm(con, pid) == 1


def test_gm_zero_when_shared_source_or_one_side_missing(con):
    _grok_chain(con, "c1", "NVDA", ENTRY - timedelta(days=1), "https://a.example.com/x")
    _idea(con, "m1", "NVDA", ENTRY - timedelta(days=1), "https://a.example.com/x")  # same URL: not independent
    _grok_chain(con, "c2", "AMD", ENTRY - timedelta(days=1), "https://a.example.com/z")  # Grok only
    pid = loser(con)
    pid2 = loser(con, setup="s2", und="AMD")
    opm.update_option_cards(con)
    assert _gm(con, pid) == 0 and _gm(con, pid2) == 0


def test_gm_ignores_messages_after_entry_or_older_than_three_days(con):
    _grok_chain(con, "c1", "NVDA", ENTRY - timedelta(days=1), "https://a.example.com/x")
    _idea(con, "m1", "NVDA", ENTRY + timedelta(minutes=1), "https://b.example.com/y")  # after entry
    _idea(con, "m2", "NVDA", ENTRY - timedelta(days=3, minutes=1), "https://c.example.com/y")  # too old
    pid = loser(con)
    opm.update_option_cards(con)
    assert _gm(con, pid) == 0


def test_gm_null_when_inputs_unavailable(con):
    pid = loser(con)
    con.execute("DROP TABLE bridge_messages")
    opm.update_option_cards(con)
    assert _gm(con, pid) is None
    assert postmortem.grok_muse_independent(([], []), "NVDA", None) is None


def test_gm_filled_on_stock_cards(con):
    bars = make_bars()
    ind = compute(bars)
    d = bars.dates
    e, x = d[-30], d[-20]
    entry = datetime.fromisoformat(f"{e.date()}T14:35:00+00:00")
    fill(con, "b1", "AAPL", "buy", 10, 100, ostore.iso(entry))
    fill(con, "s1", "AAPL", "sell", 10, 95, f"{x.date()}T14:35:00+00:00", pnl=-50)
    fill(con, "b2", "MSFT", "buy", 10, 100, ostore.iso(entry))
    fill(con, "s2", "MSFT", "sell", 10, 95, f"{x.date()}T14:35:00+00:00", pnl=-50)
    _grok_chain(con, "c1", "AAPL", entry - timedelta(days=1), "https://a.example.com/x")
    _idea(con, "m1", "AAPL", entry - timedelta(hours=5), "https://b.example.com/y")
    postmortem.update_cards(con, bars, ind)
    got = {r["symbol"]: r["grok_muse_independent_same_symbol"]
           for r in con.execute("SELECT symbol, grok_muse_independent_same_symbol FROM trade_postmortem")}
    assert got == {"AAPL": 1, "MSFT": 0}


# report ---------------------------------------------------------------------------------------

def test_symptom_report_keeps_option_codes_separate(con):
    for i, day in enumerate(("2026-10-16", "2026-10-09")):
        con.execute("INSERT INTO trade_postmortem (trade_id, symbol, strategy, exit_day, pnl, symptoms) "
                    "VALUES (?,?,?,?,?,?)", (f"opt:p{i}", "NVDA", "options:grok", day, -100.0, "O8"))
    con.execute("INSERT INTO trade_postmortem (trade_id, symbol, strategy, exit_day, pnl, symptoms) "
                "VALUES ('t1','AAPL','breakout','2026-10-16',-10,'S2')")
    text, hyp = postmortem.symptom_report(con, "2026-10-16")
    assert "期權驗屍卡" in text
    assert any("O8" in h for h in hyp)
    assert not any("S2" in h for h in hyp)  # one stock week only
    stock = postmortem.week_stats(con, "2026-10-16", 1)[0]
    options = postmortem.week_stats(con, "2026-10-16", 1, book="options")[0]
    assert (stock["n"], dict(stock["cnt"])) == (1, {"S2": 1})
    assert (options["n"], dict(options["cnt"])) == (1, {"O8": 1})


def test_symptom_report_option_section_carries_feed_caveat(con):
    con.execute("INSERT INTO trade_postmortem (trade_id, symbol, strategy, exit_day, pnl, symptoms) "
                "VALUES ('opt:p0','NVDA','options:grok','2026-10-16',-100,'O0')")
    text, _ = postmortem.symptom_report(con, "2026-10-16")
    assert FEED_CAVEAT in text.split("### 期權驗屍卡(實盤紙上)")[1]


def test_learn_job_writes_cards_for_a_closed_stock_trade_and_a_closed_option(con, monkeypatch, tmp_path):
    """Trigger paths: job.run -> guarded("postmortem") -> postmortem.update_cards (flat-to-flat
    round trips in fills) and guarded("options_postmortem") -> update_option_cards (live EXITED)."""
    from stockmoney.desk.learn import job, links
    monkeypatch.setattr(job.notify, "send", lambda *a, **k: True)
    monkeypatch.setattr(job, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(job.earnings, "refresh", lambda *a, **k: None)
    monkeypatch.setattr(job.filings, "refresh", lambda *a, **k: None)
    monkeypatch.setattr(links, "default_fetch", lambda url, timeout=0: (_ for _ in ()).throw(OSError("offline")))
    bars = make_bars(423)
    d = bars.dates
    fill(con, "b1", "AAPL", "buy", 10, 100, f"{d[-30].date()}T14:35:00+00:00")
    fill(con, "s1", "AAPL", "sell", 10, 95, f"{d[-20].date()}T14:35:00+00:00", pnl=-50)
    oday = d[-2]
    oentry = datetime(oday.year, oday.month, oday.day, 14, 0, tzinfo=timezone.utc)
    opid, ox = pos(con, day=str(oday.date()), entry=oentry)
    fills(con, opid, ox, entry=oentry)
    pos(con, setup="s2", mode="shadow", day=str(oday.date()), entry=oentry)  # shadow: never carded
    now = datetime(d[-1].year, d[-1].month, d[-1].day, 21, 15, tzinfo=timezone.utc)
    body = job.run(con, now, bars=bars, shadow_start=str(d[-200].date()), with_allocator=False)
    ids = {r["trade_id"] for r in con.execute("SELECT trade_id FROM trade_postmortem")}
    assert "opt:" + opid in ids and not any(i.startswith("opt:s2:") for i in ids)
    assert len([i for i in ids if not i.startswith("opt:")]) == 1  # the AAPL round trip
    assert "驗屍卡:新增 1" in body and "期權驗屍卡:新增 1" in body
    assert FEED_CAVEAT in body
