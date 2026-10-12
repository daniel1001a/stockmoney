"""Link checker, weekly one-line scoreboard and the Friday push. Offline: the
network is a fake fetch function, the push is captured."""
import json
from datetime import datetime, timezone

import pytest

from stockmoney.desk import store
from stockmoney.desk.bridge.inbox import ensure_schema as bridge_schema
from stockmoney.desk.learn import job, links, shadow, tables
from stockmoney.desk.run import ensure_schema as run_schema
from stockmoney.desk.execution.ledger import ensure_schema as ledger_schema
from test_learn import make_bars


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "d.sqlite") as c:
        run_schema(c)
        ledger_schema(c)
        tables.ensure_schema(c)
        bridge_schema(c)
        yield c


def put(con, msg_id, type_, msg, received="2026-10-12T11:40:00+00:00"):
    con.execute("INSERT INTO bridge_messages VALUES (?,?,?,?,?,?,?)",
                (msg_id, type_, "s", received, received, "{}", json.dumps(msg)))


def seed_messages(con):
    put(con, "grok-20261012-0730", "GROK_DAILY", {
        "events": [{"urls": ["https://example.com/a", "https://x.com/u/status/1"]}],
        "chains": [{"urls": ["https://example.org/dead"]}],
        "calls": [{"post_url": "https://twitter.com/u/status/2"}]})
    put(con, "muse-alert-0001", "ALERT", {"source_urls": ["https://news.example.net/ok", "https://gone.invalid-host.test/x"]})
    put(con, "muse-social-01", "SOCIAL_DAILY", {"items": [{"sample_urls": ["https://www.threads.net/@a/post/1"]}]})
    put(con, "muse-calls-001", "CALLS", {"calls": [{"post_url": "https://instagram.com/p/9"}]})


class Fake:
    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, url, timeout):
        self.calls.append(url)
        v = self.table.get(url, 200)
        if isinstance(v, Exception):
            raise v
        return v


def test_collect_reads_urls_from_every_message_type_with_agent(con):
    seed_messages(con)
    found = links.collect(con)
    assert found["https://example.com/a"] == ("grok-20261012-0730", "Pika(Grok)")
    assert found["https://news.example.net/ok"][1] == "Muse"
    assert found["https://instagram.com/p/9"][1] == "Muse"
    assert len(found) == 8


def test_classification_rules_and_no_fetch_for_blocked_or_unsafe_hosts():
    f = Fake({"https://a.example/404": 404, "https://a.example/410": 410, "https://a.example/403": 403,
              "https://a.example/429": 429, "https://a.example/500": 500, "https://a.example/301": 301,
              "https://nx.example/": links.DnsFailure("nx"), "https://slow.example/": TimeoutError()})
    c = lambda u: links.classify(u, f)[0]  # noqa: E731
    assert c("https://a.example/ok") == "ok" and c("https://a.example/301") == "ok"
    assert c("https://a.example/404") == c("https://a.example/410") == "dead"
    assert c("https://nx.example/") == "dead"
    assert [c(f"https://a.example/{n}") for n in (403, 429, 500)] == ["unverifiable"] * 3
    assert c("https://slow.example/") == "unverifiable"
    n = len(f.calls)
    for u in ("https://x.com/a", "https://mobile.twitter.com/a", "https://www.threads.net/a", "https://instagram.com/a",
              "https://facebook.com/a", "http://127.0.0.1/a", "https://localhost/a", "http://10.0.0.5/a",
              "ftp://a.example/a", "https://intranet/a"):
        assert c(u) == "unverifiable", u
    assert len(f.calls) == n  # none of those touched the network
    assert links.domain_of("https://WWW.Example.com/p") == "example.com"


def test_step_records_once_and_rate_excludes_unverifiable(con):
    seed_messages(con)
    f = Fake({"https://example.org/dead": 404, "https://gone.invalid-host.test/x": links.DnsFailure("nx")})
    r = links.step(con, "2026-10-12", n=50, fetch=f)
    assert (r["checked"], r["ok"], r["dead"], r["unverifiable"]) == (8, 2, 2, 4)
    rate = links.real_link_rate(con)
    assert rate["rate"] == pytest.approx(0.5) and rate["meets_threshold"] is False
    row = con.execute("SELECT * FROM link_checks WHERE url='https://x.com/u/status/1'").fetchone()
    assert (row["status"], row["domain"], row["agent"]) == ("unverifiable", "x.com", "Pika(Grok)")
    assert links.step(con, "2026-10-13", n=50, fetch=f)["checked"] == 0  # each URL is checked once
    assert links.real_link_rate(con, "Muse")["ok"] == 1


def test_rate_is_none_when_nothing_is_verifiable_and_threshold_is_90(con):
    assert links.real_link_rate(con) == {"ok": 0, "dead": 0, "unverifiable": 0, "rate": None, "meets_threshold": None}
    for i in range(9):
        con.execute("INSERT INTO link_checks VALUES (?,?,?,?,?,?,?,?,?)", (f"u{i}", "m", "Muse", "d", "ok", 200, "", "d", "t"))
    con.execute("INSERT INTO link_checks VALUES ('d','m','Muse','d','dead',404,'','d','t')")
    con.execute("INSERT INTO link_checks VALUES ('x','m','Muse','x.com','unverifiable',NULL,'','d','t')")
    r = links.real_link_rate(con)
    assert r["rate"] == pytest.approx(0.9) and r["meets_threshold"] is True
    assert "90%" in links.report(con)


def test_sample_is_bounded_and_deterministic_per_day(con):
    for i in range(30):
        put(con, f"muse-alert-{i:04d}", "ALERT", {"source_urls": [f"https://s{i}.example/a"]})
    a = links.step(con, "2026-10-12", n=5, fetch=Fake({}))
    assert a["checked"] == 5 and a["pool"] == 30
    assert links._pick([f"u{i}" for i in range(20)], "d", 4) == links._pick([f"u{i}" for i in range(20)], "d", 4)


def test_default_fetch_failure_is_stored_not_raised(con, monkeypatch):
    seed_messages(con)

    def boom(url, timeout=0):
        raise OSError("network down")
    monkeypatch.setattr(links, "default_fetch", boom)
    r = links.step(con, "2026-10-12", n=50)
    assert r["checked"] == 8 and r["ok"] == 0 and r["dead"] == 0


def test_period_returns_use_common_start_and_say_no_data(con):
    con.execute("INSERT INTO equity_history VALUES ('2026-10-02',10000,10000,'paper',0,'t')")
    con.execute("INSERT INTO equity_history VALUES ('2026-10-09',10200,10200,'paper',1,'t')")
    for name, a, b in (("bench:spy", 100, 101), ("shadow:random_trend_0", 100, 98),
                       ("shadow:random_trend_1", 100, 104), ("shadow:random_trend_2", 100, 103)):
        con.execute("INSERT INTO shadow_equity VALUES (?,?,?)", (name, "2026-10-02", a))
        con.execute("INSERT INTO shadow_equity VALUES (?,?,?)", (name, "2026-10-09", b))
    p = shadow.period_returns(con, "2026-10-09")
    assert p["start"] == "2026-10-02"
    assert p["live"] == pytest.approx(0.02) and p["spy"] == pytest.approx(0.01)
    assert p["random"] == pytest.approx(0.03)  # median of -2%, +4%, +3%
    line = shadow.weekly_line(con, "2026-10-09")
    assert "+2.00%" in line and "+1.00%" in line and "+3.00%" in line
    # a series that began after the period start moves the common start; no pair of dates -> no data
    assert shadow.weekly_line(con, "2026-10-02").count("no data") == 3


def test_weekly_line_with_empty_tables_is_all_no_data(con):
    assert shadow.weekly_line(con, "2026-10-09").count("no data") == 3


def test_friday_push_always_has_the_line_once_per_day_and_survives_network_failure(con, monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(job.notify, "send", lambda title, msg, priority=3, tags=None: sent.append((title, msg)) or True)
    monkeypatch.setattr(job, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(links, "default_fetch", lambda url, timeout=0: (_ for _ in ()).throw(OSError("offline")))
    seed_messages(con)
    bars = make_bars(423)  # last bar is a Friday
    assert bars.dates[-1].weekday() == 4
    now = datetime(bars.dates[-1].year, bars.dates[-1].month, bars.dates[-1].day, 21, 15, tzinfo=timezone.utc)
    body = job.run(con, now, bars=bars, shadow_start=str(bars.dates[-200].date()), with_allocator=False)
    assert "來源連結抽查" in body
    assert len(sent) == 1 and sent[0][0].startswith("Desk 學習週報")
    assert "SPY" in sent[0][1] and "隨機對照中位數" in sent[0][1]
    job.run(con, now, bars=bars, shadow_start=str(bars.dates[-200].date()), with_allocator=False)
    assert len(sent) == 1  # de-duplicated for the day
