"""Research veto Jev catalyst filter (2026-10-27): a free-text item Jev reads as a
positive catalyst stops counting as a risk vote; Jev failure keeps the old count.
Offline: a fake TypeSafe client stands in for the network."""
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from desk_helpers import buy

from stockmoney.desk import veto
from stockmoney.desk.bridge.inbox import BRIDGE_SCHEMA

PG_BEAT = "P&G公布2027財年Q1財報超預期,有機銷售增長"


class FakeJev:
    """Answers by keyword in the item text; records every call."""

    def __init__(self, delay=0.0, fail=None):
        self.delay, self.fail, self.calls = delay, fail, []

    def system_one(self, state, questions, model=None, timeout=None):
        self.calls.append({"state": state, "questions": questions, "model": model, "timeout": timeout})
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        good = "超預期" in state["news"]["text"] or "beat" in state["news"]["text"]
        p = {"negative": 0.1 if good else 0.9, "positive": 0.9 if good else 0.1}
        return SimpleNamespace(model=model, answers={k: SimpleNamespace(noul=p[k]) for k in questions})


@pytest.fixture
def db(con):
    con.executescript(BRIDGE_SCHEMA)
    veto.ensure_schema(con)
    return con


@pytest.fixture
def jev(monkeypatch):
    def _use(client):
        monkeypatch.setattr(veto, "jev_client", lambda: client)
        return client
    return _use


def _now():
    return datetime.now(timezone.utc).isoformat()


def _alert(con, sym, headline, msg_id="m1", urls=("https://a.example/1",)):
    con.execute("INSERT INTO bridge_messages (msg_id, type, sender, sent_at, received_at, raw_body, parsed_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (msg_id, "ALERT", "x", _now(), _now(), "raw",
                 json.dumps({"ticker": sym, "severity": "elevated", "headline": headline,
                             "source_urls": list(urls), "observed_at": _now()})))


def _earnings(con, sym):
    con.execute("INSERT INTO research_flags (symbol, source, kind, severity, headline, url, event_date, collected_at) "
                "VALUES (?,?,?,?,?,?,?,?)", (sym, "earnings", "earnings_soon", "elevated", "earnings in 1d", "",
                                             "2026-10-23", _now()))


def _shadow(con):
    return con.execute("SELECT * FROM veto_jev_shadow ORDER BY id").fetchall()


def test_positive_catalyst_does_not_count(db, jev, make_engine, broker):
    """2026-10-23 PG: an earnings beat ALERT plus earnings_soon blocked the buy."""
    fake = jev(FakeJev())
    _alert(db, "AAPL", PG_BEAT)
    _earnings(db, "AAPL")
    d = make_engine().check(buy(qty=10), broker.account(), 100.0, 0)
    assert d.allow, d.reason
    assert db.execute("SELECT COUNT(*) FROM veto_log").fetchone()[0] == 0
    call = fake.calls[0]
    assert call["model"] == veto.JEV_MODEL == "jev-1.13.0" and call["timeout"] == veto.JEV_TIMEOUT_S
    assert (call["questions"]["negative"].instructions
            == "Is this news negative for the stock price of AAPL over the next few days?")
    (row,) = _shadow(db)
    assert row["symbol"] == "AAPL" and row["item_ref"] == "m1" and row["source"] == "muse"
    assert row["client_order_id"] == buy(qty=10).client_order_id
    assert row["jev_p"] == pytest.approx(0.1) and row["jev_p_positive"] == pytest.approx(0.9)
    assert row["excluded"] == 1 and row["error"] == "" and row["latency_ms"] >= 0
    assert (row["votes_before"], row["votes_after"]) == (2, 1)
    assert (row["decision_before"], row["decision"]) == ("block", "pass")


def test_negative_news_still_blocks(db, jev):
    jev(FakeJev())
    _alert(db, "AAPL", "SEC opens probe into accounting")
    _earnings(db, "AAPL")
    r = veto.check(db, "AAPL", client_order_id="c1")
    assert r.blocked
    (row,) = _shadow(db)
    assert row["excluded"] == 0 and row["decision_before"] == row["decision"] == "block"
    assert row["jev_p"] == pytest.approx(0.9)


def test_timeout_falls_back_to_counting(db, jev, monkeypatch):
    monkeypatch.setattr(veto, "JEV_TIMEOUT_S", 0.05)
    jev(FakeJev(delay=0.5))
    _alert(db, "AAPL", PG_BEAT)
    _earnings(db, "AAPL")
    t0 = time.monotonic()
    r = veto.check(db, "AAPL")
    assert time.monotonic() - t0 < 0.4  # does not wait for the slow answer
    assert r.blocked
    (row,) = _shadow(db)
    assert row["error"] == "timeout" and row["jev_p"] is None and row["excluded"] == 0
    assert row["decision_before"] == row["decision"] == "block"


def test_error_falls_back_to_counting(db, jev):
    jev(FakeJev(fail=RuntimeError("boom")))
    _alert(db, "AAPL", PG_BEAT)
    _earnings(db, "AAPL")
    assert veto.check(db, "AAPL").blocked
    (row,) = _shadow(db)
    assert row["error"] == "RuntimeError" and row["excluded"] == 0


def test_no_client_counts_as_before(db):
    """conftest gives no client: the PG case still blocks, the row says why."""
    _alert(db, "AAPL", PG_BEAT)
    _earnings(db, "AAPL")
    assert veto.check(db, "AAPL").blocked
    (row,) = _shadow(db)
    assert row["error"] == "unavailable" and row["jev_p"] is None


def test_single_vote_pass_is_shadowed_without_changing_decision(db, jev):
    jev(FakeJev())
    _alert(db, "AAPL", "SEC opens probe into accounting")
    r = veto.check(db, "AAPL")
    assert not r.blocked
    (row,) = _shadow(db)
    assert row["votes_before"] == row["votes_after"] == 1 and row["decision"] == "pass"


def test_structured_items_are_never_asked(db, jev):
    fake = jev(FakeJev())
    _earnings(db, "AAPL")
    db.execute("INSERT INTO research_flags (symbol, source, kind, severity, headline, url, event_date, collected_at) "
               "VALUES ('AAPL','filings','8-K','elevated','beat','', '2026-10-23', ?)", (_now(),))
    assert veto.check(db, "AAPL").blocked
    assert fake.calls == [] and _shadow(db) == []


def test_no_votes_no_jev(db, jev):
    fake = jev(FakeJev())
    assert not veto.check(db, "AAPL").blocked
    assert fake.calls == [] and _shadow(db) == []


def test_jev_client_needs_key(monkeypatch):
    monkeypatch.undo()  # drop the conftest stub to read the real factory
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert veto.jev_client() is None
    monkeypatch.setenv("TYPESAFE_API_KEY", "x")
    monkeypatch.setenv(veto.JEV_OFF_ENV, "0")
    assert veto.jev_client() is None
