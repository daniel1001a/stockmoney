"""Research veto: 2+ independent sources flagging a symbol block a NEW BUY only.
Offline; fabricated bridge rows and research_flags rows."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from desk_helpers import buy, sell

from stockmoney.desk import veto
from stockmoney.desk.bridge.inbox import BRIDGE_SCHEMA


@pytest.fixture
def db(con):
    con.executescript(BRIDGE_SCHEMA)
    veto.ensure_schema(con)
    return con


def _iso(days_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _flag(con, sym, source, sev="elevated", days_ago=0.0, kind="k", url=""):
    con.execute("INSERT INTO research_flags (symbol, source, kind, severity, headline, url, event_date, collected_at) "
                "VALUES (?,?,?,?,?,?,?,?)", (sym, source, kind, sev, "h", url, "2026-10-19", _iso(days_ago)))


def _msg(con, msg_id, typ, body, days_ago=0.0, source=None):
    con.execute("INSERT INTO bridge_messages (msg_id, type, sender, sent_at, received_at, raw_body, parsed_json, source) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (msg_id, typ, "x", _iso(days_ago), _iso(days_ago), "raw", json.dumps(body), source))


def _muse_alert(con, sym="AAPL", sev="elevated", urls=("https://a.example/1",), days_ago=0.0, msg_id="m1"):
    _msg(con, msg_id, "ALERT", {"ticker": sym, "severity": sev, "headline": "x", "source_urls": list(urls),
                                "observed_at": _iso(days_ago)}, days_ago)


def _grok(con, sym="AAPL", sev="elevated", urls=("https://b.example/2",), days_ago=0.0, msg_id="g1",
          source="grok_verified"):
    _msg(con, msg_id, "GROK_DAILY", {"events": [{"ticker": sym, "headline": "x", "severity": sev, "confirmed": True,
                                                  "first_seen": _iso(days_ago), "urls": list(urls)}]}, days_ago, source)


def test_two_independent_sources_block_buy(db, make_engine, broker):
    _flag(db, "AAPL", "filings")
    _flag(db, "AAPL", "insider")
    d = make_engine().check(buy(qty=10), broker.account(), 100.0, 0)
    assert not d.allow and d.intent is None
    assert "research veto: 2 independent sources flagged AAPL" in d.reason


def test_one_source_does_not_block(db, make_engine, broker):
    _flag(db, "AAPL", "filings")
    _flag(db, "AAPL", "filings", kind="other")
    assert make_engine().check(buy(qty=10), broker.account(), 100.0, 0).allow


def test_other_symbol_flags_do_not_block(db, make_engine, broker):
    _flag(db, "MSFT", "filings")
    _flag(db, "MSFT", "insider")
    assert make_engine().check(buy(qty=10), broker.account(), 100.0, 0).allow


def test_info_severity_never_counts(db, make_engine, broker):
    _flag(db, "AAPL", "filings", sev="info")
    _flag(db, "AAPL", "insider", sev="info")
    _muse_alert(db, sev="info")
    assert make_engine().check(buy(qty=10), broker.account(), 100.0, 0).allow


def test_flags_outside_window_do_not_block(db, make_engine, broker):
    _flag(db, "AAPL", "filings", days_ago=20)
    _flag(db, "AAPL", "insider", days_ago=20)
    assert make_engine().check(buy(qty=10), broker.account(), 100.0, 0).allow


def test_muse_and_grok_with_distinct_sources_count_twice(db):
    _muse_alert(db, urls=("https://a.example/1",))
    _grok(db, urls=("https://b.example/2",))
    r = veto.check(db, "AAPL")
    assert r.blocked and set(r.sources) == {"muse", "grok"}


def test_muse_and_grok_sharing_a_url_count_once(db):
    _muse_alert(db, urls=("https://same.example/story",))
    _grok(db, urls=("https://same.example/story",))
    r = veto.check(db, "AAPL")
    assert not r.blocked and veto.vote_count(r.sources) == 1


def test_unsourced_muse_and_grok_count_once(db):
    _muse_alert(db, urls=())
    _grok(db, urls=())
    r = veto.check(db, "AAPL")
    assert not r.blocked and veto.vote_count(r.sources) == 1


def test_unsourced_grok_event_does_not_split_a_shared_story(db):
    _muse_alert(db, urls=("https://same.example/story",))
    _grok(db, urls=("https://same.example/story",))
    _grok(db, urls=(), msg_id="g2")
    r = veto.check(db, "AAPL")
    assert not r.blocked and veto.vote_count(r.sources) == 1


def test_shared_source_plus_filing_still_two_votes(db):
    _muse_alert(db, urls=("https://same.example/story",))
    _grok(db, urls=("https://same.example/story",))
    _flag(db, "AAPL", "filings")
    r = veto.check(db, "AAPL")
    assert r.blocked and "count once" in r.reason


def test_research_reply_risk_flags_count_for_muse(db):
    _msg(db, "r1", "RESEARCH_REPLY", {"ticker": "AAPL", "risk_flags": ["lawsuit"], "source_urls": []})
    _flag(db, "AAPL", "earnings")
    assert veto.check(db, "AAPL").blocked


def test_sell_is_never_blocked(db, make_engine, broker, hold):
    hold("AAPL", "breakout", 10)
    _flag(db, "AAPL", "filings", sev="critical")
    _flag(db, "AAPL", "insider", sev="critical")
    _flag(db, "AAPL", "earnings")
    d = make_engine().check(sell(qty=10), broker.account(), 100.0, 0)
    assert d.allow


def test_sell_with_kill_file_still_passes_and_veto_not_consulted(db, make_engine, broker, hold, halt_file):
    hold("AAPL", "breakout", 10)
    _flag(db, "AAPL", "filings")
    _flag(db, "AAPL", "insider")
    halt_file.touch()
    assert make_engine().check(sell(qty=10), broker.account(), 100.0, 0).allow
    assert db.execute("SELECT COUNT(*) FROM veto_log").fetchone()[0] == 0


def test_block_is_logged_with_reason(db, make_engine, broker):
    _flag(db, "AAPL", "filings")
    _flag(db, "AAPL", "insider")
    d = make_engine().check(buy(qty=10), broker.account(), 100.0, 0)
    rows = db.execute("SELECT symbol, reason, sources_json FROM veto_log").fetchall()
    assert len(rows) == 1 and rows[0]["symbol"] == "AAPL" and rows[0]["reason"] in d.reason
    assert set(json.loads(rows[0]["sources_json"])) == {"filings", "insider"}
    lo, hi = _iso(1), _iso(-1)
    assert veto.blocks_between(db, "aapl", lo, hi) == [rows[0]["reason"]]
    assert "AAPL" in veto.report(db, lo)


def test_missing_tables_mean_no_veto(con, make_engine, broker):
    assert not veto.check(con, "AAPL").blocked
    assert make_engine().check(buy(qty=10), broker.account(), 100.0, 0).allow


def test_limits_can_raise_the_bar(db, make_engine, broker):
    _flag(db, "AAPL", "filings")
    _flag(db, "AAPL", "insider")
    assert make_engine(veto_min_sources=3).check(buy(qty=10), broker.account(), 100.0, 0).allow


def test_ticker_normalisation(db):
    _flag(db, "BRK-B", "filings")
    _flag(db, "BRK-B", "insider")
    assert veto.check(db, "brk.b").blocked


def test_postmortem_card_records_flags_and_vetoes_seen_during_the_hold(con):
    import pandas as pd

    from stockmoney.desk.indicators import compute
    from stockmoney.desk.learn import postmortem, tables
    from stockmoney.desk.execution.ledger import ensure_schema as ledger_schema
    from stockmoney.desk.run import ensure_schema as run_schema
    from tests.desk.test_learn import fill, make_bars

    run_schema(con)
    ledger_schema(con)
    tables.ensure_schema(con)
    bars = make_bars()
    ind = compute(bars)
    e, x = bars.dates[300], bars.dates[306]
    px = float(bars.open.at[e, "AAPL"])
    fill(con, "b1", "AAPL", "buy", 10, px, f"{e.date()}T14:35:00+00:00")
    fill(con, "s1", "AAPL", "sell", 10, px * 0.97, f"{x.date()}T14:35:00+00:00", pnl=-1)
    mid = f"{(e + pd.Timedelta(days=2)).date()}T12:00:00+00:00"
    con.execute("INSERT INTO research_flags (symbol, source, kind, severity, headline, url, event_date, collected_at) "
                "VALUES ('AAPL','insider','sell_cluster','elevated','3 insiders sold','','2026-01-01', ?)", (mid,))
    con.execute("INSERT INTO research_flags (symbol, source, kind, severity, headline, url, event_date, collected_at) "
                "VALUES ('AAPL','filings','10-Q','info','10-Q filed','','2026-01-01', ?)", (mid,))
    con.execute("INSERT INTO veto_log (symbol, blocked_at, reason, sources_json) VALUES ('AAPL', ?, 'research veto: test', '{}')", (mid,))
    postmortem.update_cards(con, bars, ind)
    facts = json.loads(con.execute("SELECT facts_json FROM trade_postmortem").fetchone()["facts_json"])
    assert facts["research_flags_in_hold"] == ["insider/elevated: 3 insiders sold"]
    assert facts["veto_blocks_in_hold"] == ["research veto: test"]
