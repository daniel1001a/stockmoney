"""EDGAR / earnings flag collector: classification, Form 4 parsing, insider cluster,
skip and failure behaviour. Offline: fetch is injected, no network."""
import json
from datetime import date

import pytest

from stockmoney.desk import store, veto
from stockmoney.desk.learn import earnings, filings, tables

TODAY = date(2026, 10, 19)


@pytest.fixture
def db(tmp_path):
    with store.connect(tmp_path / "d.sqlite") as c:
        tables.ensure_schema(c)
        yield c


def _form4(owner_cik, code, shares=1000, price=50.0, planned=False, day="2026-10-15"):
    return f"""<ownershipDocument><aff10b5One>{1 if planned else 0}</aff10b5One>
<reportingOwner><reportingOwnerId><rptOwnerCik>{owner_cik}</rptOwnerCik></reportingOwnerId></reportingOwner>
<nonDerivativeTable><nonDerivativeTransaction>
<transactionDate><value>{day}</value></transactionDate>
<transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>{shares}</value></transactionShares>
<transactionPricePerShare><value>{price}</value></transactionPricePerShare></transactionAmounts>
</nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>"""


def _subs(rows):
    keys = ("accessionNumber", "filingDate", "form", "items", "primaryDocument")
    return json.dumps({"filings": {"recent": {k: [r[i] for r in rows] for i, k in enumerate(keys)}}})


def _fetcher(pages):
    def fetch(url):
        return pages.get(url)
    return fetch


TICKERS = json.dumps({"0": {"cik_str": 111, "ticker": "AAPL", "title": "Apple"}})
SUB_URL = filings.SUBMISSIONS_URL.format(cik=111)


def _doc_url(acc, doc="x.xml"):
    return filings.ARCHIVE_URL.format(cik=111, acc=acc.replace("-", ""), doc=doc)


def test_classify_8k_levels():
    assert filings.classify_8k("2.02,9.01") == ("info", "8-K items 2.02,9.01")
    assert filings.classify_8k("5.02") == ("elevated", "8-K 5.02 officer/director change")
    assert filings.classify_8k("9.01, 4.02") == ("critical", "8-K 4.02 financials non-reliance")
    assert filings.classify_8k("")[0] == "info"


def test_parse_form4_fields_and_bad_xml():
    tx = filings.parse_form4(_form4(7, "S", 200, 10.5))
    assert tx == [{"owner": "7", "code": "S", "shares": 200.0, "price": 10.5, "date": "2026-10-15", "planned": False}]
    assert filings.parse_form4("not xml") == []
    assert filings.parse_form4(_form4(7, "S", planned=True))[0]["planned"] is True


def test_cluster_counts_distinct_discretionary_sellers_only():
    txs = [{"owner": o, "code": "S", "shares": 100.0, "price": 10.0, "planned": False} for o in ("a", "b", "c")]
    assert filings.insider_cluster(txs)["sell"] is True
    txs[2]["planned"] = True
    c = filings.insider_cluster(txs)
    assert c["sell"] is False and c["sell_owners"] == 2 and c["sell_value"] == 2000.0
    same = [{"owner": "a", "code": "S", "shares": 1.0, "price": 1.0, "planned": False}] * 5
    assert filings.insider_cluster(same)["sell_owners"] == 1


def _pages_with_sells(n_owners, planned=False, day="2026-10-17"):
    rows, pages = [], {filings.TICKERS_URL: TICKERS}
    for i in range(n_owners):
        acc = f"0000000-26-00000{i}"
        rows.append((acc, day, "4", "", "x.xml"))
        pages[_doc_url(acc)] = _form4(100 + i, "S", planned=planned)
    pages[SUB_URL] = _subs(rows)
    return pages


def test_refresh_stores_sell_cluster_as_elevated_insider_flag(db):
    out = filings.refresh(db, TODAY, fetch=_fetcher(_pages_with_sells(3)), symbols=["AAPL"], sleep=lambda s: None)
    assert out["scanned"] == 1 and out["filings"] == 1
    r = db.execute("SELECT source, kind, severity FROM research_flags").fetchall()
    assert [(x["source"], x["kind"], x["severity"]) for x in r] == [("insider", "sell_cluster", "elevated")]


def test_planned_sales_do_not_make_a_cluster(db):
    out = filings.refresh(db, TODAY, fetch=_fetcher(_pages_with_sells(3, planned=True)), symbols=["AAPL"], sleep=lambda s: None)
    assert out["filings"] == 0


def test_stale_insider_activity_is_ignored(db):
    out = filings.refresh(db, TODAY, fetch=_fetcher(_pages_with_sells(3, day="2026-10-01")), symbols=["AAPL"],
                          sleep=lambda s: None)
    assert out["filings"] == 0


def test_8k_and_late_notice_flags(db):
    rows = [("a-1", "2026-10-16", "8-K", "4.02,9.01", "d.htm"), ("a-2", "2026-10-17", "NT 10-Q", "", "n.htm"),
            ("a-3", "2026-10-18", "10-Q", "", "q.htm"), ("a-4", "2026-09-01", "8-K", "1.03", "old.htm")]
    pages = {filings.TICKERS_URL: TICKERS, SUB_URL: _subs(rows)}
    filings.refresh(db, TODAY, fetch=_fetcher(pages), symbols=["AAPL"], sleep=lambda s: None)
    got = {(r["kind"], r["severity"]) for r in db.execute("SELECT kind, severity FROM research_flags")}
    assert got == {("8-K", "critical"), ("NT 10-Q", "elevated"), ("10-Q", "info")}


def test_refresh_is_idempotent(db):
    f = _fetcher(_pages_with_sells(3))
    filings.refresh(db, TODAY, fetch=f, symbols=["AAPL"], sleep=lambda s: None)
    again = filings.refresh(db, TODAY, fetch=f, symbols=["AAPL"], sleep=lambda s: None)
    assert again["filings"] == 0
    assert db.execute("SELECT COUNT(*) FROM research_flags").fetchone()[0] == 1


def test_fetch_failure_writes_nothing_and_is_counted(db):
    out = filings.refresh(db, TODAY, fetch=_fetcher({filings.TICKERS_URL: TICKERS}), symbols=["AAPL"], sleep=lambda s: None)
    assert out["failed"] == 1 and out["scanned"] == 0
    assert db.execute("SELECT COUNT(*) FROM research_flags").fetchone()[0] == 0


def test_missing_ticker_map_skips_edgar(db):
    out = filings.refresh(db, TODAY, fetch=_fetcher({}), symbols=["AAPL"], sleep=lambda s: None)
    assert out["skipped_edgar"] == "ticker to CIK map unavailable"


def test_no_user_agent_skips_edgar_without_network(db, monkeypatch):
    monkeypatch.delenv(filings.UA_ENV, raising=False)
    monkeypatch.setattr(filings, "_http_get", lambda *a: pytest.fail("network must not be touched"))
    out = filings.refresh(db, TODAY, symbols=["AAPL"])
    assert out["skipped_edgar"] == "SEC_USER_AGENT not set"


def test_earnings_soon_becomes_flag_and_votes_with_another_source(db):
    db.execute("INSERT INTO earnings (symbol, date, first_seen, last_seen) VALUES ('AAPL', '2026-10-21', 'x', 'x')")
    out = filings.refresh(db, TODAY, fetch=_fetcher({}), symbols=["AAPL"], sleep=lambda s: None)
    assert out["earnings"] == 1
    assert veto.check(db, "AAPL").blocked is False
    db.execute("INSERT INTO research_flags (symbol, source, kind, severity, headline, url, event_date, collected_at) "
               "VALUES ('AAPL','filings','8-K','elevated','h','','2026-10-19', ?)", (store.now(),))
    assert veto.check(db, "AAPL").blocked is True


def test_report_lists_only_elevated_and_critical(db):
    assert "沒有升高" in filings.report(db)
    filings.refresh(db, TODAY, fetch=_fetcher(_pages_with_sells(3)), symbols=["AAPL"], sleep=lambda s: None)
    assert "AAPL [insider/elevated]" in filings.report(db)
