"""Falsifiable event chains: structured invalidation conditions, their judge,
exclusion of unfalsifiable chains from evidence counts, and Muse/Grok source
independence. Offline, synthetic bars."""
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from stockmoney.desk import store
from stockmoney.desk.bridge.inbox import ensure_schema as bridge_schema
from stockmoney.desk.data import Bars
from stockmoney.desk.execution.ledger import ensure_schema as ledger_schema
from stockmoney.desk.learn import chains, corroboration, invalidation, tables
from stockmoney.desk.run import ensure_schema as run_schema
from stockmoney.desk.universe import ALL_SYMBOLS


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "d.sqlite") as c:
        run_schema(c)
        ledger_schema(c)
        tables.ensure_schema(c)
        yield c


def flat_bars(n=130) -> Bars:
    idx = pd.bdate_range("2025-01-01", periods=n)
    close = pd.DataFrame(100.0, index=idx, columns=list(ALL_SYMBOLS))
    vol = pd.DataFrame(1e6, index=idx, columns=close.columns)
    return Bars(close.copy(), close * 1.01, close * 0.99, close, vol)


def created_intraday(bars: Bars, i: int) -> datetime:
    d = bars.dates[i]
    return datetime(d.year, d.month, d.day, 15, 0, tzinfo=timezone.utc)  # 10-11am ET: base is session i-1


def price_cond(kind="close_below", symbol="AAPL", level=95.0, by="2025-02-03") -> dict:
    return {"kind": kind, "symbol": symbol, "level": level, "by_date": by}


def chain_with(invs, ticker="AAPL") -> chains.Chain:
    return chains.Chain(trigger="t", steps=["s"], confidence=0.6, source="owner", invalidations=invs,
                        impacts=[{"ticker": ticker, "direction": "up", "horizon_days": 5}])


def verdicts(con) -> dict[int, tuple]:
    return {r["idx"]: (r["verdict"], r["detail"]) for r in
            con.execute("SELECT idx, verdict, detail FROM chain_invalidations ORDER BY idx").fetchall()}


# ------------------------------------------------------------------ schema

def test_old_chain_without_invalidations_still_parses_and_is_unfalsifiable():
    c = chains.Chain.model_validate({"trigger": "t", "steps": ["s"], "confidence": 0.5,
                                     "impacts": [{"ticker": "AAPL", "direction": "up", "horizon_days": 5}],
                                     "checkpoints": [{"what": "x", "date": "2026-11-05"}]})
    assert c.invalidations == [] and c.falsifiable is False


def test_new_chain_parses_each_kind():
    c = chain_with([price_cond("close_below"), price_cond("close_above", level=130),
                    {"kind": "data_not_by", "description": "rule text", "by_date": "2025-02-03"}])
    assert [i.kind for i in c.invalidations] == ["close_below", "close_above", "data_not_by"]
    assert c.falsifiable is True


def test_only_a_data_condition_is_not_falsifiable():
    c = chain_with([{"kind": "data_not_by", "description": "rule text", "by_date": "2025-02-03"}])
    assert c.falsifiable is False


@pytest.mark.parametrize("bad", [
    {"kind": "close_between", "symbol": "AAPL", "level": 1, "by_date": "2025-02-03"},
    {"kind": "close_below", "symbol": "AAPL", "level": 1},
    {"kind": "close_below", "symbol": "AAPL", "level": -1, "by_date": "2025-02-03"},
    {"kind": "close_below", "symbol": "AAPL", "level": 1, "by_date": "2025-02-03", "x": 1},
])
def test_invalid_condition_rejected(bad):
    with pytest.raises(Exception):
        chain_with([bad])


def test_add_chain_stores_conditions_and_flag(con):
    t = datetime(2025, 3, 3, 15, 0, tzinfo=timezone.utc)
    cid = chains.add_chain(con, chain_with([price_cond()]), t)
    row = con.execute("SELECT falsifiable, invalidations_json FROM chains WHERE chain_id=?", (cid,)).fetchone()
    assert row["falsifiable"] == 1
    assert json.loads(row["invalidations_json"]) == [price_cond()]
    cid2 = chains.add_chain(con, chains.Chain(trigger="other", steps=["s"], confidence=0.5, source="owner",
                            impacts=[{"ticker": "AAPL", "direction": "up", "horizon_days": 5}]), t)
    row2 = con.execute("SELECT falsifiable, invalidations_json FROM chains WHERE chain_id=?", (cid2,)).fetchone()
    assert row2["falsifiable"] == 0 and row2["invalidations_json"] == "[]"  # accepted, marked


def test_existing_database_gets_the_new_columns(tmp_path):
    with store.connect(tmp_path / "old.sqlite") as c:
        c.execute("CREATE TABLE chains (chain_id TEXT PRIMARY KEY, created_at TEXT, source TEXT, trigger_text TEXT, "
                  "urls_json TEXT, steps_json TEXT, impacts_json TEXT, checkpoints_json TEXT, confidence REAL)")
        c.execute("INSERT INTO chains VALUES ('old','2026-01-01','owner','t','[]','[]','[]','[]',0.5)")
        tables.ensure_schema(c)
        tables.ensure_schema(c)  # idempotent
        row = c.execute("SELECT falsifiable, invalidations_json FROM chains WHERE chain_id='old'").fetchone()
        assert row["falsifiable"] == 0 and row["invalidations_json"] == "[]"


def test_muse_idea_invalidations_optional_and_validated():
    from pydantic import TypeAdapter
    from stockmoney.desk.bridge.schema import Inbound
    base = {"type": "IDEA", "msg_id": "idea-20261012-1100", "sent_at": "2026-10-12T11:00:00-04:00",
            "ticker": "PLTR", "direction": "long", "horizon_days": 20, "thesis": "t", "source_urls": []}
    ta = TypeAdapter(Inbound)
    assert ta.validate_python(base).invalidations == []  # older IDEA messages
    ok = ta.validate_python({**base, "invalidations": [price_cond("close_below", "PLTR", 24, "2026-10-30")]})
    assert ok.invalidations[0].kind == "close_below"
    with pytest.raises(Exception):
        ta.validate_python({**base, "invalidations": [{"kind": "close_below", "symbol": "PLTR", "level": 24}]})


# ------------------------------------------------------------------ ingest policy

def _grok_msg(chain: dict) -> dict:
    return {"type": "GROK_DAILY", "events": [], "calls": [], "attention": [], "tracked_accounts": [],
            "chains": [chain]}


def _store_msg(con, msg_id, msg, received="2026-10-12T11:40:00+00:00"):
    bridge_schema(con)
    con.execute("INSERT INTO bridge_messages (msg_id, type, sender, sent_at, received_at, raw_body, parsed_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (msg_id, msg.get("type", "GROK_DAILY"), "x", received, received, "{}", json.dumps(msg)))


BASE_CHAIN = {"trigger": "supplier warns", "urls": ["https://x.com/a/status/1"], "steps": ["s1"],
              "impacts": [{"ticker": "MU", "direction": "down", "horizon_days": 20}],
              "checkpoints": [], "confidence": 0.4}


def test_ingest_accepts_old_message_and_marks_it_unfalsifiable(con):
    _store_msg(con, "grok-20261012-0730", _grok_msg(dict(BASE_CHAIN)))
    assert chains.ingest_grok(con) == {"added": 1, "skipped": 0}
    row = con.execute("SELECT falsifiable, invalidations_json FROM chains").fetchone()
    assert row["falsifiable"] == 0 and row["invalidations_json"] == "[]"


def test_ingest_keeps_valid_conditions_and_drops_bad_ones_without_dropping_the_chain(con):
    invs = [price_cond("close_below", "MU", 80.0, "2026-10-30"),
            price_cond("close_above", "BTC/USD", 90000.0, "2026-10-30"),  # no series in the stock cache
            {"kind": "close_below", "symbol": "MU", "level": "high", "by_date": "2026-10-30"}]  # junk
    _store_msg(con, "grok-20261012-0730", _grok_msg({**BASE_CHAIN, "invalidations": invs}))
    assert chains.ingest_grok(con) == {"added": 1, "skipped": 0}
    row = con.execute("SELECT falsifiable, invalidations_json FROM chains").fetchone()
    assert row["falsifiable"] == 1
    assert json.loads(row["invalidations_json"]) == [price_cond("close_below", "MU", 80.0, "2026-10-30")]


def test_unfalsifiable_chain_is_excluded_from_evidence_counts(con):
    con.execute("INSERT INTO chains (chain_id, falsifiable) VALUES ('f', 1), ('u', 0)")
    for cid in ("f", "u"):
        for i in range(3):
            con.execute("INSERT INTO chain_scores (chain_id, ticker, horizon, declared, direction, base_date, scored_date, excess_ret, hit, confidence, scored_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (cid, f"T{i}", 5, 1, "up", "2026-01-01", "2026-01-08", 0.01, 1, 0.7, "x"))
    s = chains.summary(con)
    assert s["scored"] == 3 and s["hits"] == 3 and s["scored_unfalsifiable"] == 3 and s["unfalsifiable"] == 1
    assert "不列入" in chains.report(con)


def test_authority_ignores_unfalsifiable_scores(con):
    con.execute("INSERT INTO chains (chain_id, falsifiable) VALUES ('u', 0)")
    for i in range(60):
        con.execute("INSERT INTO chain_scores (chain_id, ticker, horizon, declared, direction, base_date, scored_date, excess_ret, hit, confidence, scored_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("u", f"T{i}", 20, 1, "up", "2026-01-01", "2026-02-01", 0.01, 1, 0.7, "x"))
    assert chains.authority(con)[0] == "log_only"
    assert chains.summary(con)["scored"] == 0


# ------------------------------------------------------------------ judge

def test_close_below_invalidated_when_any_close_in_window_crosses(con):
    bars = flat_bars()
    d = bars.dates
    bars.close.loc[d[103], "AAPL"] = 94.0
    cid = chains.add_chain(con, chain_with([price_cond(by=str(d[105].date()))]), created_intraday(bars, 100))
    assert invalidation.judge_due(con, bars, d[110]) == 1
    v = verdicts(con)[0]
    assert v[0] == "invalidated" and str(d[103].date()) in v[1]
    assert cid  # chain row exists


def test_close_below_held_when_level_never_crossed(con):
    bars = flat_bars()
    d = bars.dates
    chains.add_chain(con, chain_with([price_cond(level=95.0, by=str(d[105].date()))]), created_intraday(bars, 100))
    invalidation.judge_due(con, bars, d[110])
    assert verdicts(con)[0][0] == "held"


def test_close_above_both_outcomes(con):
    bars = flat_bars()
    d = bars.dates
    bars.close.loc[d[102], "MSFT"] = 131.0
    c = chain_with([price_cond("close_above", "AAPL", 130.0, str(d[105].date())),
                    price_cond("close_above", "MSFT", 130.0, str(d[105].date()))])
    chains.add_chain(con, c, created_intraday(bars, 100))
    invalidation.judge_due(con, bars, d[110])
    assert verdicts(con) == {0: ("held", verdicts(con)[0][1]), 1: ("invalidated", verdicts(con)[1][1])}


def test_level_equal_to_close_is_not_a_cross(con):
    bars = flat_bars()
    d = bars.dates
    chains.add_chain(con, chain_with([price_cond("close_below", level=100.0, by=str(d[105].date()))]),
                     created_intraday(bars, 100))
    invalidation.judge_due(con, bars, d[110])
    assert verdicts(con)[0][0] == "held"


def test_close_known_before_the_chain_was_written_does_not_count(con):
    bars = flat_bars()
    d = bars.dates
    bars.close.loc[d[99], "AAPL"] = 50.0  # the base close, public when the chain was written
    chains.add_chain(con, chain_with([price_cond(by=str(d[105].date()))]), created_intraday(bars, 100))
    invalidation.judge_due(con, bars, d[110])
    assert verdicts(con)[0][0] == "held"


def test_not_due_is_not_judged_and_complete_bar_convention_holds(con):
    bars = flat_bars()
    d = bars.dates
    chains.add_chain(con, chain_with([price_cond(by=str(d[105].date()))]), created_intraday(bars, 100))
    assert invalidation.judge_due(con, bars.upto(d[104]), d[104]) == 0
    assert verdicts(con) == {}
    assert invalidation.judge_due(con, bars, d[105]) == 1  # due on the day itself


def test_no_data_cases(con):
    bars = flat_bars()
    d = bars.dates
    bars.close.loc[d[103], "MSFT"] = np.nan
    by = str(d[105].date())
    c = chain_with([
        {"kind": "data_not_by", "description": "rule text", "by_date": by},
        price_cond("close_below", "ZZZZ", 90.0, by),            # no series
        price_cond("close_below", "MSFT", 90.0, by),            # missing close, no crossing seen
        price_cond("close_below", "AAPL", 90.0, str(d[100].date())),  # window ends before any session of ours
    ])
    chains.add_chain(con, c, datetime(d[100].year, d[100].month, d[100].day, 22, 0, tzinfo=timezone.utc))
    # created after 16:05 ET on d[100]: base is d[100], so the last condition has an empty window
    invalidation.judge_due(con, bars, d[110])
    v = verdicts(con)
    assert [v[i][0] for i in range(4)] == ["no_data"] * 4
    assert "not mechanically checkable" in v[0][1]


def test_missing_close_does_not_hide_a_visible_crossing(con):
    bars = flat_bars()
    d = bars.dates
    bars.close.loc[d[102], "AAPL"] = np.nan
    bars.close.loc[d[103], "AAPL"] = 80.0
    chains.add_chain(con, chain_with([price_cond(by=str(d[105].date()))]), created_intraday(bars, 100))
    invalidation.judge_due(con, bars, d[110])
    assert verdicts(con)[0][0] == "invalidated"


def test_data_not_by_is_never_judged_true_or_false(con):
    bars = flat_bars()
    d = bars.dates
    c = chain_with([{"kind": "data_not_by", "description": "rule text", "by_date": str(d[105].date())},
                    price_cond(by=str(d[105].date()))])
    chains.add_chain(con, c, created_intraday(bars, 100))
    invalidation.judge_due(con, bars, d[110])
    assert verdicts(con)[0][0] == "no_data"
    assert verdicts(con)[1][0] == "held"
    assert invalidation.judge_due(con, bars, d[111]) == 0  # final, idempotent


def test_no_data_is_rejudged_when_the_closes_arrive(con):
    bars = flat_bars()
    d = bars.dates
    full = bars.close.copy()
    bars.close.loc[d[103], "AAPL"] = np.nan
    chains.add_chain(con, chain_with([price_cond(by=str(d[105].date()))]), created_intraday(bars, 100))
    invalidation.judge_due(con, bars, d[110])
    assert verdicts(con)[0][0] == "no_data"
    bars.close.loc[:, :] = full
    assert invalidation.judge_due(con, bars, d[111]) == 1
    assert verdicts(con)[0][0] == "held"


def test_weekend_by_date_is_judged_after_the_next_session(con):
    bars = flat_bars()
    d = bars.dates
    i = next(k for k in range(100, 120) if d[k].weekday() == 4)  # a Friday
    sat = d[i] + pd.Timedelta(days=1)
    chains.add_chain(con, chain_with([price_cond(by=str(sat.date()))]), created_intraday(bars, 100))
    assert invalidation.judge_due(con, bars, d[i]) == 0  # Saturday not reached yet
    assert invalidation.judge_due(con, bars, d[i + 1]) == 1
    assert verdicts(con)[0][0] == "held"


# ------------------------------------------------------------------ independence

def test_normalize_url_collapses_cosmetic_differences():
    n = corroboration.normalize_url
    assert n("https://www.Reuters.com/tech/nvidia-curbs/?utm_source=x#top") == "reuters.com/tech/nvidia-curbs"
    assert n("http://reuters.com/tech/nvidia-curbs/") == "reuters.com/tech/nvidia-curbs"
    assert n("https://twitter.com/Alice/status/12") == n("https://mobile.x.com/alice/status/12") == "x.com/alice/status/12"
    assert n("https://reuters.com/a") != n("https://reuters.com/b")  # same domain, different story


def test_compare_shared_url_is_not_independent():
    r = corroboration.compare(["https://www.reuters.com/a/b/?ref=1"], ["https://reuters.com/a/b"])
    assert r["independent"] is False and r["shared_urls"] == ["reuters.com/a/b"]


def test_compare_shared_author_is_not_independent():
    r = corroboration.compare(["https://x.com/Alice/status/1"], ["https://twitter.com/alice/status/2"])
    assert r["independent"] is False and r["shared_urls"] == [] and r["shared_authors"] == ["alice"]
    r2 = corroboration.compare(["https://example.com/a"], ["https://example.com/b"], muse_authors=["@Bob"],
                               grok_authors=["bob"])
    assert r2["independent"] is False and r2["shared_authors"] == ["bob"]


def test_compare_no_overlap_is_independent():
    r = corroboration.compare(["https://reuters.com/a"], ["https://x.com/alice/status/2"])
    assert r == {"independent": True, "shared_urls": [], "shared_authors": []}


def _muse_alert(con, msg_id, ticker, urls, received="2026-10-12T13:00:00+00:00"):
    _store_msg(con, msg_id, {"type": "ALERT", "ticker": ticker, "severity": "elevated", "headline": "h",
                             "source_urls": urls}, received)


def test_refresh_labels_pairs_and_counts_non_independent_once(con):
    # Grok chain on MU citing a Reuters article, ingested at 11:40 UTC on 2026-10-12
    _store_msg(con, "grok-20261012-0730", _grok_msg({**BASE_CHAIN, "urls": ["https://www.reuters.com/a/mu-warn"]}))
    chains.ingest_grok(con)
    _muse_alert(con, "mu-alert-1", "MU", ["https://reuters.com/a/mu-warn?x=1"])  # same article
    _muse_alert(con, "nv-alert-1", "NVDA", ["https://reuters.com/a/mu-warn"])    # other ticker: not a pair
    _muse_alert(con, "mu-alert-old", "MU", ["https://example.com/z"], received="2026-09-01T13:00:00+00:00")
    assert corroboration.refresh(con) == {"added": 1}
    row = con.execute("SELECT * FROM corroboration").fetchone()
    assert row["independent"] == 0 and row["muse_msg_id"] == "mu-alert-1" and row["ticker"] == "MU"
    assert json.loads(row["shared_urls_json"]) == ["reuters.com/a/mu-warn"]
    c = corroboration.counts(con)
    assert c["items"] == 1 and c["confirmations"] == 1 and c["confirmed_independent"] == 0
    assert c["not_independent_pairs"] == 1
    assert corroboration.refresh(con) == {"added": 0}  # idempotent


def test_independent_pair_counts_as_two_confirmations(con):
    _store_msg(con, "grok-20261012-0730", _grok_msg({**BASE_CHAIN, "urls": ["https://x.com/alice/status/1"]}))
    chains.ingest_grok(con)
    _muse_alert(con, "mu-alert-1", "MU", ["https://sec.gov/filing/1"])
    _muse_alert(con, "mu-alert-2", "MU", ["https://x.com/alice/status/9"])  # same author as Grok's source
    corroboration.refresh(con)
    c = corroboration.counts(con)
    assert c["items"] == 1 and c["confirmed_independent"] == 1 and c["confirmations"] == 2
    assert c["not_independent_pairs"] == 1
    assert "not independent" in corroboration.report(con)


def test_grok_events_are_compared_too(con):
    msg = {"type": "GROK_DAILY", "chains": [], "calls": [], "attention": [], "tracked_accounts": [],
           "events": [{"ticker": "NVDA", "headline": "h", "severity": "critical", "confirmed": True,
                       "first_seen": "2026-10-12T06:10:00-04:00", "urls": ["https://reuters.com/n/1"]}]}
    _store_msg(con, "grok-20261012-0730", msg)
    _muse_alert(con, "nv-alert-1", "NVDA", ["https://reuters.com/n/1"])
    assert corroboration.refresh(con) == {"added": 1}
    assert con.execute("SELECT ref_kind, independent FROM corroboration").fetchone()[:] == ("event", 0)
