"""GROK_DAILY: strict schema, receive/store, no policy effect, feeds the
learning layer. Offline (FakeMailbox, in-memory SQLite)."""
import base64
import copy
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from grok_helpers import TOKEN, SENDER, deliver, grok_daily, patched, worst_case
from pydantic import TypeAdapter, ValidationError

from stockmoney.desk.bridge import inbox, policy
from stockmoney.desk.bridge.mailbox import BridgeConfig, FakeMailbox
from stockmoney.desk.bridge.schema import Call, GrokChain, GrokDaily, Inbound

CONFIG = BridgeConfig(desk_address="desk@example.com", app_password="x", muse_address=SENDER, shared_token=TOKEN)
NOW = datetime(2026, 10, 12, 11, 40, tzinfo=timezone.utc)  # 07:40 ET
INBOUND = TypeAdapter(Inbound)


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def validate(d: dict):
    return INBOUND.validate_json(json.dumps(d, ensure_ascii=False), strict=True)


def rejected(d: dict) -> list[tuple[str, str]]:
    with pytest.raises(ValidationError) as e:
        validate(d)
    return [(".".join(str(p) for p in x["loc"]), x["type"]) for x in e.value.errors()]


def rejects(con):
    return [r["reason"] for r in con.execute("SELECT reason FROM bridge_rejects ORDER BY id")]


# ---------------------------------------------------------------- receive and store

def test_valid_grok_daily_is_stored_with_our_received_at(con):
    box = FakeMailbox()
    deliver(box, grok_daily())
    assert inbox.poll(con, box, CONFIG, now=NOW) == 1
    r = con.execute("SELECT * FROM bridge_messages").fetchone()
    assert r["type"] == "GROK_DAILY" and r["msg_id"] == "grok-20261012-0730"
    assert r["received_at"] == "2026-10-12T11:40:00+00:00"  # our clock, not Grok's
    parsed = json.loads(r["parsed_json"])
    assert parsed["collected_at"] == "2026-10-12T07:05:00-04:00"  # ET offset kept: first 10 chars = ET date
    assert con.execute("SELECT substr(json_extract(parsed_json,'$.collected_at'),1,10) FROM bridge_messages"
                       ).fetchone()[0] == "2026-10-12"


def test_reply_prefix_and_code_fence_are_tolerated(con):
    box = FakeMailbox()
    d = grok_daily()
    box.inject(SENDER, f"Re: [SMD/GROK_DAILY/{d['msg_id']}] {TOKEN}", "早安\n```json\n" + json.dumps(d) + "\n```\n")
    assert inbox.poll(con, box, CONFIG, now=NOW) == 1


def test_duplicate_and_mismatched_subject_are_rejected(con):
    box = FakeMailbox()
    d = grok_daily()
    deliver(box, d)
    deliver(box, d)
    box.inject(SENDER, f"[SMD/GROK_DAILY/grok-other-01] {TOKEN}", json.dumps(d))
    box.inject(SENDER, f"[SMD/CALLS/{d['msg_id']}] {TOKEN}", json.dumps(d))
    assert inbox.poll(con, box, CONFIG, now=NOW) == 1
    assert rejects(con) == ["duplicate_msg_id", "msg_id_mismatch", "type_mismatch"]


def test_schema_reject_names_the_type_for_feedback(con):
    box = FakeMailbox()
    d = grok_daily()
    del d["calls"]
    deliver(box, d)
    inbox.poll(con, box, CONFIG, now=NOW)
    assert rejects(con) == ["schema_invalid GROK_DAILY.calls:missing"]


def test_grok_daily_produces_no_policy_flags(con):
    box = FakeMailbox()
    d = grok_daily()  # carries a confirmed critical event on NVDA and a 'down' chain
    deliver(box, d)
    inbox.poll(con, box, CONFIG, now=NOW)
    for held in ((), ("NVDA",)):
        flags = policy.active_flags(con, NOW, held=held)
        assert flags.blocked == {} and flags.no_add == {} and flags.notify == []


def test_stored_message_feeds_the_learning_layer(con):
    from stockmoney.desk.learn import chains, tables
    tables.ensure_schema(con)
    box = FakeMailbox()
    deliver(box, grok_daily())
    inbox.poll(con, box, CONFIG, now=NOW)
    assert chains.ingest_grok(con) == {"added": 1, "skipped": 0}
    row = con.execute("SELECT * FROM chains").fetchone()
    assert row["source"] == "grok" and row["created_at"].startswith("2026-10-12T11:40")
    assert json.loads(row["urls_json"]) == ["https://x.com/a/status/1"]
    assert json.loads(row["checkpoints_json"]) == [{"what": "Final rule text", "date": "2026-10-20"}]
    assert {i["ticker"] for i in json.loads(row["impacts_json"])} == {"NVDA", "BTC"}


def test_chain_serialisation_matches_ingest_grok_fields():
    from stockmoney.desk.learn.chains import Chain
    dumped = json.loads(GrokChain.model_validate(grok_daily()["chains"][0]).model_dump_json())
    assert set(dumped) == {"trigger", "urls", "steps", "impacts", "checkpoints", "confidence"}
    assert isinstance(dumped["urls"][0], str) and dumped["checkpoints"][0]["date"] == "2026-10-20"
    assert set(dumped["impacts"][0]) == {"ticker", "direction", "horizon_days"}
    # the worst case still fits the learning layer's own limits
    Chain.model_validate({**json.loads(GrokChain.model_validate(worst_case()["chains"][0]).model_dump_json()),
                          "impacts": [{"ticker": "MU", "direction": "down", "horizon_days": 60}], "source": "grok"})


def test_grok_call_is_a_valid_calls_call():
    call = GrokDaily.model_validate_json(json.dumps(grok_daily()), strict=True).calls[0]
    assert isinstance(call, Call)
    Call.model_validate_json(call.model_dump_json(), strict=True)


# ---------------------------------------------------------------- schema rules

def test_full_example_and_worst_case_validate():
    assert validate(grok_daily()).type == "GROK_DAILY"
    assert validate(worst_case()).type == "GROK_DAILY"


def test_worst_case_fits_the_inbox_limit_after_base64():
    body = json.dumps(worst_case(), ensure_ascii=False, separators=(",", ":")).encode()
    assert len(body) < 115_000
    assert len(base64.encodebytes(body)) + 5_000 < inbox.MAX_RAW_BYTES  # headers budget 5 KB
    assert len(json.dumps(worst_case()).encode()) < inbox.MAX_RAW_BYTES  # all-\\u escaped ASCII, sent 7bit


@pytest.mark.parametrize("value", ["2026-10-12T07:30:00", "2026-10-12T11:30:00Z", "2026-10-12T06:30:00-05:00",
                                   "2026-10-12T07:30:00+08:00"])
def test_timestamps_need_the_eastern_offset(value):
    assert rejected(patched("sent_at", value, grok_daily(collected_at="2026-10-12T06:00:00-04:00")))


def test_eastern_standard_time_accepted_after_dst_ends():
    d = grok_daily(msg_id="grok-20261102-0730", sent_at="2026-11-02T07:30:00-05:00",
                   collected_at="2026-11-02T07:00:00-05:00")
    assert validate(d).sent_at.utcoffset().total_seconds() == -5 * 3600


@pytest.mark.parametrize("ticker", ["BTC", "ETH", "BRK.B", "NVDA/USD", "btc/usd", "ABCDEF", "nvda"])
def test_bad_tickers_rejected(ticker):
    assert rejected(patched("attention.0.ticker", ticker))


@pytest.mark.parametrize("ticker", ["BTC/USD", "BRK-B", "QQQ", "TQQQ", "V"])
def test_good_tickers_accepted(ticker):
    validate(patched("attention.0.ticker", ticker))


@pytest.mark.parametrize("path,value", [
    ("events.0.urls.0", "http://example.com/a"),
    ("events.0.urls.0", "https://x.com/" + "a" * 237),  # 251 chars
    ("events.0.urls", []),
    ("events.0.severity", "high"),
    ("events.0.confirmed", "true"),
    ("chains.0.confidence", 1.01),
    ("chains.0.confidence", -0.01),
    ("chains.0.confidence", "0.5"),
    ("chains.0.impacts.0.horizon_days", 10),
    ("chains.0.impacts.0.direction", "flat"),
    ("chains.0.checkpoints.0.date", "2026/10/30"),
    ("chains.0.checkpoints.0.date", "2026-10-30T00:00:00-04:00"),
    ("chains.0.steps", []),
    ("chains.0.impacts", []),
    ("calls.0.platform", "threads"),
    ("calls.0.author", "trader_abc"),
    ("calls.0.direction", "buy"),
    ("calls.0.target_price", 0),
    ("calls.0.target_price", "300"),
    ("attention.0.trend", "flat"),
    ("tracked_accounts.0", "trader_abc"),
    ("msg_id", "gd-20261012-0730"),
    ("collected_at", "2026-10-12T07:40:00-04:00"),  # after sent_at
    ("collected_at", "2026-10-12T01:00:00-04:00"),  # more than 6h before sent_at
    ("tracked_accounts", ["@a", "@a"]),
])
def test_field_rules(path, value):
    assert rejected(patched(path, value))


def test_url_of_exactly_250_chars_accepted():
    validate(patched("events.0.urls.0", "https://x.com/" + "a" * 236))


def test_confidence_bounds_and_integer_accepted():
    for v in (0, 1, 0.5):
        validate(patched("chains.0.confidence", v))


def test_optional_fields():
    d = grok_daily()
    del d["protocol"], d["notes"], d["calls"][0]["followers"], d["calls"][0]["target_price"]
    validate(d)
    d = grok_daily()
    d["events"] = d["chains"] = d["calls"] = d["attention"] = d["tracked_accounts"] = []
    validate(d)


@pytest.mark.parametrize("field", ["msg_id", "sent_at", "collected_at", "events", "chains", "calls", "attention",
                                   "tracked_accounts"])
def test_required_top_level_fields(field):
    d = grok_daily()
    del d[field]
    assert rejected(d)


@pytest.mark.parametrize("where", ["", "events.0", "chains.0", "chains.0.impacts.0", "chains.0.checkpoints.0",
                                   "calls.0", "attention.0"])
def test_extra_fields_rejected_at_every_level(where):
    d = grok_daily()
    node = d
    for p in where.split(".") if where else []:
        node = node[int(p)] if p.isdigit() else node[p]
    node["run_at"] = "2026-10-12T07:05:00-04:00"  # the old field name is extra now
    assert rejected(d)


@pytest.mark.parametrize("path,limit", [("events", 25), ("chains", 6), ("calls", 50), ("attention", 20),
                                        ("chains.0.urls", 3), ("chains.0.steps", 6), ("chains.0.impacts", 6),
                                        ("chains.0.checkpoints", 4), ("events.0.urls", 3)])
def test_list_limits(path, limit):
    base = worst_case()
    node = base
    *head, last = path.split(".")
    for p in head:
        node = node[int(p)] if p.isdigit() else node[p]
    node[last] = list(node[last])[:1] * limit
    validate(base)
    over = copy.deepcopy(base)
    node = over
    for p in head:
        node = node[int(p)] if p.isdigit() else node[p]
    node[last] = node[last] + node[last][:1]
    assert rejected(over)


def test_tracked_accounts_limit():
    validate(patched("tracked_accounts", [f"@a{i}" for i in range(60)]))
    assert rejected(patched("tracked_accounts", [f"@a{i}" for i in range(61)]))


@pytest.mark.parametrize("path,limit", [("events.0.headline", 160), ("chains.0.trigger", 200),
                                        ("chains.0.steps.0", 120), ("chains.0.checkpoints.0.what", 100),
                                        ("calls.0.horizon_text", 60), ("attention.0.why", 120), ("notes", 1000)])
def test_string_limits(path, limit):
    validate(patched(path, "中" * limit))
    assert rejected(patched(path, "中" * (limit + 1)))


# ---------------------------------------------------------------- D3: grok- prefix is reserved

def test_other_inbound_types_cannot_use_the_grok_prefix(con):
    alert = {"type": "ALERT", "msg_id": "grok-20261012-0731", "sent_at": "2026-10-12T07:31:00-04:00",
             "ticker": "NVDA", "severity": "critical", "headline": "x", "source_urls": ["https://example.com/a"],
             "observed_at": "2026-10-12T07:30:00-04:00"}
    assert rejected(alert)
    assert rejected({**alert, "msg_id": "GROK-20261012-0731"})
    validate({**alert, "msg_id": "alert-grok-0001"})  # only the prefix is reserved
    box = FakeMailbox()
    deliver(box, alert)
    assert inbox.poll(con, box, CONFIG, now=NOW) == 0
    assert rejects(con)[0].startswith("schema_invalid ALERT")
