"""Bridge upgrade of 2026-10-27: Grok's extra sender types, per-channel
FEEDBACK, GROK_DAILY.in_play, lenient info types, PING/PONG, the DESK_BRIEF /
RESEARCH_REQUEST outbound to Grok, the 60-second poll cadence and Grok ALERTs
in veto / policy. Offline: FakeMailbox and in-memory sqlite."""
import copy
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from grok_helpers import SENDER, TOKEN, grok_daily, subject
from pydantic import TypeAdapter, ValidationError

from stockmoney.desk import bridge_job, grok_outbound, muse_ops, store, veto
from stockmoney.desk.bridge import inbox
from stockmoney.desk.bridge.mailbox import BridgeConfig, FakeMailbox
from stockmoney.desk.bridge.policy import active_flags
from stockmoney.desk.bridge.schema import Inbound, Outbound
from stockmoney.desk.execution import ledger
from stockmoney.desk.learn import earnings as learn_earnings
from stockmoney.desk.learn import tables as learn_tables
from stockmoney.desk.options import store as opt_store

GROK = "pika@mail.grokbot.com"
GTOKEN = "g" * 40
CONFIG = BridgeConfig(desk_address="desk@example.com", app_password="x", muse_address=SENDER, shared_token=TOKEN,
                      grok_address=GROK, grok_token=GTOKEN)
MUSE_ONLY = BridgeConfig(desk_address="desk@example.com", app_password="x", muse_address=SENDER, shared_token=TOKEN)
GOOD_AR = ("mx.google.com; dkim=pass header.i=@mail.grokbot.com header.s=k7abc header.b=AbC; "
           "spf=pass smtp.mailfrom=x@amazonses.com; dmarc=pass header.from=mail.grokbot.com")
UTC = timezone.utc
# Tuesday 2026-10-27 (EDT, UTC-4); the previous trading day is Monday 2026-10-26
DAY = date(2026, 10, 27)
PREV = date(2026, 10, 26)
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)  # 08:00 ET
INBOUND = TypeAdapter(Inbound)
OUTBOUND = TypeAdapter(Outbound)


def et(h, m, d=DAY):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=UTC) + timedelta(hours=4)  # EDT is UTC-4


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(store.SCHEMA)
    muse_ops.ensure_schema(c)
    inbox.ensure_schema(c)
    yield c
    c.close()


@pytest.fixture
def pages(monkeypatch):
    sent = []
    monkeypatch.setattr(muse_ops.notify, "send", lambda *a, **k: sent.append(a) or True)
    return sent


def send(box, payload, *, sender=GROK, token=GTOKEN, auth=GOOD_AR):
    return box.inject(sender, subject(payload, token), json.dumps(payload), auth_results=auth)


def stored(con):
    return {r["msg_id"]: dict(r) for r in con.execute(
        "SELECT msg_id, type, source, dropped_json, parsed_json FROM bridge_messages")}


def rejects(con):
    return [r["reason"] for r in con.execute("SELECT reason FROM bridge_rejects ORDER BY id")]


def alert(msg_id="alert-grok-0001", **kw):
    d = {"protocol": "1", "msg_id": msg_id, "sent_at": "2026-10-27T07:30:00-04:00", "type": "ALERT",
         "ticker": "NVDA", "severity": "critical", "headline": "Export curbs widened",
         "source_urls": ["https://www.reuters.com/a"], "observed_at": "2026-10-27T07:00:00-04:00"}
    d.update(kw)
    return d


def reply(msg_id="rr-grok-0001", **kw):
    d = {"protocol": "1", "msg_id": msg_id, "sent_at": "2026-10-27T19:30:00-04:00", "type": "RESEARCH_REPLY",
         "request_id": "rq-grok-abc", "ticker": "AMD", "risk_flags": [], "sentiment_shift": "unchanged",
         "summary": "Guidance cut.", "source_urls": ["https://www.reuters.com/b"]}
    d.update(kw)
    return d


CAPS = {"can_read_email": True, "can_send_email": True, "can_http_get": True, "can_http_post": False,
        "can_schedule": True, "schedule_times": ["07:30", "20:00"], "timezone": "America/New_York"}


def pong(nonce, msg_id="pong-grok-0001"):
    return {"protocol": "1", "msg_id": msg_id, "sent_at": "2026-10-27T08:05:00-04:00", "type": "PONG",
            "nonce": nonce, "capabilities": CAPS, "notes": "hello"}


def in_play(**kw):
    d = {"ticker": "AMD", "direction": "up", "catalyst": "Q3 beat and raise", "catalyst_kind": "earnings",
         "catalyst_time": "2026-10-26T16:05:00-04:00", "confirmed": True,
         "urls": ["https://www.reuters.com/amd"], "confidence": 0.6}
    d.update(kw)
    return d


# ---------------------------------------------------------------- Grok sender types

def test_grok_alert_reply_and_pong_are_accepted_as_verified(con):
    box = FakeMailbox()
    send(box, alert())
    send(box, reply())
    send(box, pong("abcdef0123456789"))
    assert inbox.poll(con, box, CONFIG, now=NOW) == 3
    got = stored(con)
    assert {k: v["source"] for k, v in got.items()} == {
        "alert-grok-0001": "grok_verified", "rr-grok-0001": "grok_verified", "pong-grok-0001": "grok_verified"}


def test_grok_sender_still_may_not_send_muse_only_types(con):
    box = FakeMailbox()
    idea = {"protocol": "1", "msg_id": "idea-grok-0001", "sent_at": "2026-10-27T07:30:00-04:00", "type": "IDEA",
            "ticker": "AMD", "direction": "long", "thesis": "x", "source_urls": ["https://a.example/1"]}
    send(box, idea)
    assert inbox.poll(con, box, CONFIG, now=NOW) == 0
    assert rejects(con)


def notice_ack(**changes):
    body = {"protocol": "1", "msg_id": "ack-grok-20261027-0800", "sent_at": NOW.isoformat(),
            "type": "NOTICE_ACK", "notice_id": "notice-desk-20261027", "status": "done",
            "schedule_times": ["07:20", "08:15", "08:50", "20:00"], "timezone": "America/New_York",
            "questions": [], "notes": "Applied"}
    return {**body, **changes}


def test_notice_ack_only_from_grok_with_its_token(con):
    box = FakeMailbox()
    send(box, notice_ack())
    assert inbox.poll(con, box, CONFIG, now=NOW) == 1
    row = stored(con)["ack-grok-20261027-0800"]
    assert row["source"] == "grok_verified" and row["type"] == "NOTICE_ACK"
    for sender, token in ((SENDER, TOKEN), (GROK, TOKEN), ("unknown@example.com", GTOKEN)):
        box = FakeMailbox()
        send(box, notice_ack(msg_id="ack-invalid-20261027"), sender=sender, token=token)
        assert inbox.poll(con, box, CONFIG, now=NOW) == 0
    assert len(stored(con)) == 1


@pytest.mark.parametrize("changes", [
    {"status": "approved"}, {"status": "partial", "notes": ""},
    {"schedule_times": ["24:00"]}, {"schedule_times": ["8:50"]},
    {"notes": "x" * 2001}, {"sent_at": "2026-10-27T08:00:00"},
])
def test_notice_ack_strict_schema(changes):
    with pytest.raises(ValidationError):
        INBOUND.validate_json(json.dumps(notice_ack(**changes)), strict=True)


def desk_notice(**changes):
    return {"protocol": "1", "msg_id": "notice-desk-20261027", "sent_at": NOW.isoformat(),
            "type": "GROK_NOTICE", "notice_id": "notice-desk-20261027", "title": "Schedule update",
            "body": "Check the mailbox at 08:50 ET.", "actions": ["Confirm your schedule"], **changes}


def plan_reminder(**changes):
    return {"protocol": "1", "msg_id": "planrm-desk-20261027", "sent_at": NOW.isoformat(),
            "type": "PLAN_REMINDER", "for_date": "2026-10-27", "missing": ["OPTIONS_PLAN", "GROK_DAILY"],
            "live_cutoff": "09:20:00", "note": "Send an empty plan when there are no candidates.", **changes}


@pytest.mark.parametrize("payload", [desk_notice(), plan_reminder()])
def test_reliability_outbound_roundtrip_has_no_token(payload):
    from stockmoney.desk.bridge import outbox
    msg = OUTBOUND.validate_json(json.dumps(payload), strict=True)
    box = FakeMailbox()
    outbox.send(box, msg, CONFIG, channel="grok")
    recipient, subj, body = box.sent[0]
    assert recipient == GROK
    assert subj == outbox.build_subject(msg.type, msg.msg_id) == f"[SMD/{msg.type}/{msg.msg_id}]"
    assert GTOKEN not in subj and GTOKEN not in body
    assert OUTBOUND.validate_json(body, strict=True) == msg
    with pytest.raises(ValueError, match="grok channel"):
        outbox.send(FakeMailbox(), msg, CONFIG, channel="muse")


@pytest.mark.parametrize("payload", [
    desk_notice(notice_id="different-notice"), desk_notice(title="x" * 121),
    desk_notice(body="x" * 4001), desk_notice(actions=["a"] * 11), desk_notice(actions=["a" * 281]),
    plan_reminder(missing=["ALERT"]), plan_reminder(missing=["OPTIONS_PLAN", "OPTIONS_PLAN"]),
    plan_reminder(live_cutoff="09:21"), plan_reminder(note="x" * 501),
])
def test_reliability_outbound_bounds(payload):
    with pytest.raises(ValidationError):
        OUTBOUND.validate_json(json.dumps(payload), strict=True)


def test_outbound_only_types_are_not_accepted_inbound(con):
    box = FakeMailbox()
    ping = {"protocol": "1", "msg_id": "ping-grok-0001", "sent_at": "2026-10-27T07:30:00-04:00", "type": "PING",
            "nonce": "abcdef0123456789", "ask": "x"}
    send(box, ping)
    assert inbox.poll(con, box, CONFIG, now=NOW) == 0


# ---------------------------------------------------------------- in_play

def test_in_play_is_optional_and_capped_at_15():
    INBOUND.validate_json(json.dumps(grok_daily()), strict=True)
    d = grok_daily()
    d["in_play"] = [in_play()] * 15
    assert len(INBOUND.validate_json(json.dumps(d), strict=True).in_play) == 15
    d["in_play"] = [in_play()] * 16
    with pytest.raises(ValidationError):
        INBOUND.validate_json(json.dumps(d), strict=True)


@pytest.mark.parametrize("bad", [{"direction": "long"}, {"catalyst_kind": "rumor"}, {"urls": []},
                                 {"urls": ["http://plain.example/x"]}, {"confidence": 1.2},
                                 {"catalyst_time": "2026-10-26T16:05:00"}])
def test_in_play_item_rules(bad):
    d = grok_daily()
    d["in_play"] = [in_play(**bad)]
    with pytest.raises(ValidationError):
        INBOUND.validate_json(json.dumps(d), strict=True)


def test_grok_in_play_reads_verified_mail_before_0920_only(con):
    box = FakeMailbox()
    early = grok_daily("grok-20261027-0730", sent_at="2026-10-27T07:30:00-04:00",
                       collected_at="2026-10-27T07:05:00-04:00")
    early["in_play"] = [in_play()]
    send(box, early)
    inbox.poll(con, box, CONFIG, now=et(7, 31))
    late = grok_daily("grok-20261027-0930", sent_at="2026-10-27T09:30:00-04:00",
                      collected_at="2026-10-27T09:25:00-04:00")
    late["in_play"] = [in_play(ticker="TSLA")]
    send(box, late)
    inbox.poll(con, box, CONFIG, now=et(9, 31))
    legacy = grok_daily("grok-20261027-0731", sent_at="2026-10-27T07:31:00-04:00",
                        collected_at="2026-10-27T07:05:00-04:00")
    legacy["in_play"] = [in_play(ticker="MU")]
    send(box, legacy, sender=SENDER, token=TOKEN, auth="")
    inbox.poll(con, box, CONFIG, now=et(7, 32))
    got = muse_ops.grok_in_play(con, DAY)
    assert [g["ticker"] for g in got] == ["AMD"]
    assert got[0]["msg_id"] == "grok-20261027-0730"
    assert muse_ops.grok_in_play(con, PREV) == []


# ---------------------------------------------------------------- leniency

def test_info_type_drops_bad_urls_and_unknown_fields_and_records_them(con):
    box = FakeMailbox()
    send(box, reply(source_urls=["https://ok.example/a", "not a url"], mood="calm"))
    assert inbox.poll(con, box, CONFIG, now=NOW) == 1
    row = stored(con)["rr-grok-0001"]
    dropped = json.loads(row["dropped_json"])
    assert {d["path"] for d in dropped["dropped"]} == {"source_urls.1", "mood"}
    assert dropped["feedback"] is True
    assert json.loads(row["parsed_json"])["source_urls"] == ["https://ok.example/a"]


def test_missing_required_field_still_rejects(con):
    box = FakeMailbox()
    bad = alert()
    del bad["headline"]
    send(box, bad)
    assert inbox.poll(con, box, CONFIG, now=NOW) == 0
    assert "headline" in rejects(con)[0]


def test_dropping_the_only_url_of_an_alert_rejects(con):
    box = FakeMailbox()
    send(box, alert(source_urls=["nope"]))
    assert inbox.poll(con, box, CONFIG, now=NOW) == 0


def test_pruning_never_empties_a_list_that_had_items():
    """GROK_DAILY whose only event is bad must reject, not pass as an empty
    digest; with a second good event the bad one is dropped."""
    d = grok_daily()
    d["events"][0]["severity"] = "apocalyptic"
    assert inbox.lenient_validate(json.dumps(d), "GROK_DAILY") == (None, [])
    good = copy.deepcopy(grok_daily()["events"][0])
    d["events"].append(good)
    msg, dropped = inbox.lenient_validate(json.dumps(d), "GROK_DAILY")
    assert msg is not None and len(msg.events) == 1 and dropped
    r = reply(risk_flags=[{"bad": 1}])
    assert inbox.lenient_validate(json.dumps(r), "RESEARCH_REPLY") == (None, [])


def test_bad_impact_order_drops_the_impact_not_the_field():
    """An order outside 1|2 must not fall back to the default and count as a
    first-order impact; the impact itself is pruned."""
    d = grok_daily()
    d["chains"][0]["impacts"][1]["order"] = 3
    msg, dropped = inbox.lenient_validate(json.dumps(d), "GROK_DAILY")
    assert msg is not None and dropped
    assert [i.ticker for i in msg.chains[0].impacts] == ["NVDA"]
    d["chains"][0]["impacts"][1]["order"] = 2
    msg, _ = inbox.lenient_validate(json.dumps(d), "GROK_DAILY")
    assert msg is None or msg.chains[0].impacts[1].order == 2


def test_pruning_a_side_section_to_empty_keeps_the_digest():
    """One bad item that is the only one in an optional section (in_play,
    tracked_accounts) is dropped; the events in the same digest are kept."""
    d = grok_daily()
    assert d["events"]
    d["tracked_accounts"] = ["not a handle!"]
    d["in_play"] = [{"bad": 1}]
    msg, dropped = inbox.lenient_validate(json.dumps(d), "GROK_DAILY")
    assert msg is not None and dropped
    assert msg.tracked_accounts == [] and msg.in_play == [] and len(msg.events) == len(d["events"])


def test_options_plan_stays_strict(con):
    box = FakeMailbox()
    plan = {"protocol": "1", "msg_id": "grok-plan-20261027", "sent_at": "2026-10-27T08:00:00-04:00",
            "type": "OPTIONS_PLAN", "unknown_field": 1}
    send(box, plan)
    assert inbox.poll(con, box, CONFIG, now=NOW) == 0
    assert stored(con) == {}


def test_clean_message_has_no_dropped_json(con):
    box = FakeMailbox()
    send(box, alert())
    inbox.poll(con, box, CONFIG, now=NOW)
    assert stored(con)["alert-grok-0001"]["dropped_json"] is None


# ---------------------------------------------------------------- FEEDBACK routing

def _subjects(box):
    return [(to, s) for to, s, _ in box.sent]


def test_feedback_goes_back_on_the_channel_it_came_from(con, pages):
    box = FakeMailbox()
    send(box, alert(mood="x"))
    send(box, alert("alert-muse-0001", mood="x"), sender=SENDER, token=TOKEN, auth="")
    inbox.poll(con, box, CONFIG, now=NOW)
    assert muse_ops.send_feedback(con, box, NOW, CONFIG) == 2
    sent = _subjects(box)
    assert {to for to, _ in sent} == {GROK, SENDER}
    for to, s in sent:
        assert s.startswith("[SMD/FEEDBACK/")
        assert s.endswith(GTOKEN if to == GROK else TOKEN)
    chans = {r[0] for r in con.execute("SELECT channel FROM bridge_outbox WHERE type='FEEDBACK'")}
    assert chans == {"grok", "muse"}
    body = json.loads([b for to, _, b in box.sent if to == GROK][0])
    assert "【給 Grok】" not in body["problem"] and "mood" in body["problem"]
    assert muse_ops.send_feedback(con, box, NOW, CONFIG) == 0  # once per message


def test_feedback_cap_is_per_channel(con, pages):
    for i in range(muse_ops.FEEDBACK_CAP):
        con.execute("INSERT INTO bridge_outbox (msg_id, type, sent_at, channel) VALUES (?,?,?,?)",
                    (f"fb-old{i}", "FEEDBACK", (NOW - timedelta(hours=1)).isoformat(), "grok"))
    box = FakeMailbox()
    send(box, alert(mood="x"))
    send(box, alert("alert-muse-0001", mood="x"), sender=SENDER, token=TOKEN, auth="")
    inbox.poll(con, box, CONFIG, now=NOW)
    assert muse_ops.send_feedback(con, box, NOW, CONFIG) == 1
    assert [to for to, _ in _subjects(box)] == [SENDER]
    assert len(pages) == 1 and "Grok" in pages[0][0]


def test_send_feedback_with_nothing_to_send_needs_no_config(con, monkeypatch):
    monkeypatch.delenv("DESK_MAIL_ADDRESS", raising=False)
    assert muse_ops.send_feedback(con, FakeMailbox(), NOW) == 0


# ---------------------------------------------------------------- PING / PONG

def test_ping_pong_round_trip(con):
    box = FakeMailbox()
    msg = muse_ops.send_ping(con, box, "grok", NOW, CONFIG)
    to, subj, body = box.sent[-1]
    assert to == GROK and re.match(r"^\[SMD/PING/ping-grok-[0-9a-f]{16}\] " + GTOKEN + "$", subj)
    OUTBOUND.validate_json(body, strict=True)
    assert muse_ops.ping_results(con)[0]["answered"] is False
    send(box, pong(msg.nonce))
    inbox.poll(con, box, CONFIG, now=NOW + timedelta(minutes=3))
    r = muse_ops.ping_results(con)[0]
    assert r["answered"] and r["rtt_s"] == 180 and r["capabilities"]["schedule_times"] == ["07:30", "20:00"]


def test_pong_from_the_other_channel_does_not_answer(con):
    box = FakeMailbox()
    msg = muse_ops.send_ping(con, box, "grok", NOW, CONFIG)
    send(box, pong(msg.nonce, "pong-muse-0001"), sender=SENDER, token=TOKEN, auth="")
    inbox.poll(con, box, CONFIG, now=NOW + timedelta(minutes=1))
    assert muse_ops.ping_results(con)[0]["answered"] is False


def test_ping_on_grok_channel_needs_grok_config(con):
    with pytest.raises(Exception):
        muse_ops.send_ping(con, FakeMailbox(), "grok", NOW, MUSE_ONLY)


# ---------------------------------------------------------------- outbound to Grok

@pytest.fixture
def desk(con):
    ledger.ensure_schema(con)
    opt_store.ensure_schema(con)
    learn_earnings.ensure_schema(con)
    learn_tables.ensure_schema(con)
    f = "INSERT INTO fills (client_order_id, symbol, strategy, side, qty, price, filled_at, realized_pnl) " \
        "VALUES (?,?,?,?,?,?,?,?)"
    con.execute(f, ("c1", "AMD", "momentum", "sell", 10, 150.0, "2026-10-26T19:30:00.123456+00:00", -42.5))
    con.execute(f, ("c2", "NVDA", "trend", "buy", 5, 180.0, "2026-10-26T14:00:00+00:00", 0))
    con.execute(f, ("c3", "MSFT", "trend", "sell", 3, 500.0, "2026-10-27T18:00:00+00:00", -12.0))
    con.execute(f, ("c4", "AAPL", "trend", "sell", 3, 200.0, "2026-10-25T18:00:00+00:00", 99.0))  # Sunday: not prev
    con.execute("INSERT INTO sleeve_positions VALUES ('NVDA','trend',5,180.0,'2026-10-26',170.0)")
    con.execute("INSERT INTO options_positions (position_id, setup_id, day, mode, underlying, occ, qty_initial, "
                "qty_open, state, entry_price, exit_time, exit_reason, realized_usd, fees_usd) VALUES "
                "('p1','2026-10-26:orb15:QQQ','2026-10-26','live','QQQ','QQQ261026C00600000',2,0,'EXITED',1.5,"
                "'2026-10-26T15:30:00+00:00','stop',-60.0,1.3)")
    con.execute("INSERT INTO options_positions (position_id, setup_id, day, mode, underlying, occ, qty_initial, "
                "qty_open, state, entry_price, exit_time, exit_reason, realized_usd, fees_usd) VALUES "
                "('p2','2026-10-26:orb15:SPY','2026-10-26','shadow','SPY','SPY261026C00600000',2,0,'EXITED',1.5,"
                "'2026-10-26T15:30:00+00:00','stop',-99.0,0)")
    con.execute("INSERT INTO earnings (symbol, date, time_text) VALUES ('AMD','2026-10-27','time-after-hours')")
    con.execute("INSERT INTO earnings (symbol, date, time_text, removed_at) VALUES ('X','2026-10-27','', 'y')")
    return con


def booked(con, day="2026-10-27"):
    """What postclose leaves behind: the session it planned through."""
    con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT OR REPLACE INTO settings VALUES ('last_postclose_through', ?)", (day,))


def returns(day):
    return {PREV: {"AMD": -0.051, "NVDA": 0.012, "QQQ": 0.004, "MSFT": 0.0},
            DAY: {"NVDA": 0.045, "MSFT": -0.01, "AMD": 0.0}}.get(day, {})


def _bodies(box, typ):
    return [json.loads(b) for _, s, b in box.sent if s.startswith(f"[SMD/{typ}/")]


def test_desk_brief_contents(desk):
    box = FakeMailbox()
    lines = grok_outbound.run(desk, box, et(6, 30), CONFIG, returns)
    assert len(lines) == 1 and box.sent[0][0] == GROK and box.sent[0][1].endswith(GTOKEN)
    OUTBOUND.validate_json(box.sent[0][2], strict=True)
    b = _bodies(box, "DESK_BRIEF")[0]
    assert b["day"] == "2026-10-27" and b["prev_day"] == "2026-10-26"
    assert {p["sleeve"]: p["realized"] for p in b["realized_pnl"]} == {"momentum": -42.5, "options:orb15": -61.3}
    trades = {t["symbol"]: t for t in b["closed_trades"]}
    assert set(trades) == {"AMD", "QQQ"}
    assert trades["AMD"]["pnl"] == -42.5 and trades["QQQ"]["strategy"] == "orb15"
    assert [p["symbol"] for p in b["open_positions"]] == ["NVDA"]
    assert b["calendar"] == [{"what": "AMD earnings", "when": "time-after-hours"}]
    assert len(b["questions"]) == 1 and "AMD" in b["questions"][0] and "-5.1%" in b["questions"][0]
    row = desk.execute("SELECT type, channel FROM bridge_outbox").fetchone()
    assert tuple(row) == ("DESK_BRIEF", "grok")


def test_desk_brief_gating(desk):
    box = FakeMailbox()
    assert grok_outbound.run(desk, box, et(6, 29), CONFIG, returns) == []
    assert grok_outbound.run(desk, box, et(7, 0), MUSE_ONLY, returns) == []
    assert grok_outbound.run(desk, box, et(7, 0, date(2026, 10, 31)), CONFIG, returns) == []  # Saturday
    box = FakeMailbox()
    assert len(grok_outbound.run(desk, box, et(7, 0), CONFIG, returns)) == 1
    assert grok_outbound.run(desk, box, et(8, 0), CONFIG, returns) == []  # once per day
    assert len(box.sent) == 1


def test_thanksgiving_is_not_a_trading_day(desk):
    assert grok_outbound.run(desk, FakeMailbox(), et(7, 0, date(2026, 11, 26)), CONFIG, returns) == []


def test_failed_send_is_retried(desk):
    class Broken(FakeMailbox):
        def send(self, *a, **k):
            raise OSError("smtp down")
    with pytest.raises(OSError):
        grok_outbound.run(desk, Broken(), et(7, 0), CONFIG, returns)
    box = FakeMailbox()
    assert len(grok_outbound.run(desk, box, et(7, 5), CONFIG, returns)) == 1


def test_research_requests_after_1745(desk):
    desk.execute("INSERT INTO bridge_ops VALUES ('brief:2026-10-27', 'x')")
    booked(desk)
    box = FakeMailbox()
    assert grok_outbound.run(desk, box, et(17, 44), CONFIG, returns) == []
    lines = grok_outbound.run(desk, box, et(17, 45), CONFIG, returns)
    reqs = _bodies(box, "RESEARCH_REQUEST")
    assert {r["ticker"] for r in reqs} == {"MSFT", "NVDA"} and len(lines) == 1
    for r in reqs:
        assert r["msg_id"].startswith("rq-grok-")
        assert datetime.fromisoformat(r["deadline"]) == datetime(2026, 10, 28, 1, 0, tzinfo=UTC)  # 21:00 ET
    assert all(to == GROK for to, _, _ in box.sent)
    assert {r[0] for r in desk.execute("SELECT channel FROM bridge_outbox WHERE type='RESEARCH_REQUEST'")} == {"grok"}
    assert grok_outbound.run(desk, box, et(18, 0), CONFIG, returns) == []


def test_research_requests_wait_for_postclose(desk):
    """A bridge poll after 17:45 that runs before postclose has booked the day
    sends nothing and leaves the day open; by 21:00 without it the day closes
    unsent (a request past its own deadline is no use)."""
    desk.execute("INSERT INTO bridge_ops VALUES ('brief:2026-10-27', 'x')")
    box = FakeMailbox()
    assert grok_outbound.run(desk, box, et(17, 50), CONFIG, returns) == []
    booked(desk, "2026-10-26")  # yesterday's marker does not count
    assert grok_outbound.run(desk, box, et(18, 0), CONFIG, returns) == []
    assert box.sent == [] and not grok_outbound._done(desk, "grokrq:2026-10-27")
    booked(desk)
    assert len(grok_outbound.run(desk, box, et(18, 5), CONFIG, returns)) == 1
    assert {r["ticker"] for r in _bodies(box, "RESEARCH_REQUEST")} == {"MSFT", "NVDA"}

    desk.execute("DELETE FROM bridge_ops WHERE key='grokrq:2026-10-27'")
    booked(desk, "2026-10-26")
    box = FakeMailbox()
    assert grok_outbound.run(desk, box, et(21, 0), CONFIG, returns) == []
    assert box.sent == [] and grok_outbound._done(desk, "grokrq:2026-10-27")


def test_late_grok_reply_is_reported_on_the_grok_wording(desk, pages):
    desk.execute("INSERT INTO bridge_ops VALUES ('brief:2026-10-27', 'x')")
    booked(desk)
    box = FakeMailbox()
    grok_outbound.run(desk, box, et(17, 45), CONFIG, returns)
    out = muse_ops.check_deliveries(desk, et(21, 30))
    assert any("Grok 沒有在期限前回覆" in line for line in out)


def test_no_returns_still_sends_the_brief(desk):
    def boom(day):
        raise FileNotFoundError("no cache")
    box = FakeMailbox()
    assert len(grok_outbound.run(desk, box, et(7, 0), CONFIG, boom)) == 1
    assert _bodies(box, "DESK_BRIEF")[0]["questions"] == []


def test_bridge_runs_outbound_only_with_an_explicit_config_in_tests(desk, monkeypatch):
    from stockmoney.desk import run
    monkeypatch.setattr(grok_outbound, "ohlcv_returns", returns)
    box = FakeMailbox()
    monkeypatch.setattr(muse_ops, "run_checks", lambda *a, **k: "")
    monkeypatch.setattr(inbox, "poll", lambda *a, **k: 0)
    run.bridge(desk, et(7, 0), box)
    assert box.sent == []
    out = run.bridge(desk, et(7, 0), box, grok_config=CONFIG)
    assert "DESK_BRIEF" in out and len(box.sent) == 1


# ---------------------------------------------------------------- cadence

@pytest.mark.parametrize("when, fast", [(et(6, 0), True), (et(16, 29), True), (et(16, 30), False),
                                        (et(5, 59), False), (et(10, 0, date(2026, 10, 31)), False)])
def test_fast_window(when, fast):
    assert bridge_job.fast_window(when) is fast


def test_slow_cadence_is_15_minutes_and_persisted(con):
    t = et(20, 0)
    assert bridge_job.due(con, t)  # never polled
    bridge_job.mark_polled(con, t)
    assert not bridge_job.due(con, t + timedelta(minutes=14))
    assert bridge_job.due(con, t + timedelta(minutes=15))
    assert bridge_job.due(con, t - timedelta(hours=1))  # clock moved back
    assert bridge_job.due(con, et(10, 0))  # fast window ignores the last poll


def test_bridge_job_skips_when_not_due_or_locked(con, tmp_path, monkeypatch, capsys):
    from contextlib import contextmanager

    from stockmoney.desk import run

    @contextmanager
    def fake_connect():
        yield con
    monkeypatch.setattr(store, "connect", fake_connect)
    calls = []
    monkeypatch.setattr(run, "bridge", lambda c, now, *a, **k: calls.append(now) or "Muse 新訊息 0 則")
    monkeypatch.setattr(run, "LOCK_PATH", tmp_path / "run.lock")
    monkeypatch.setattr(bridge_job, "due", lambda c, now: False)
    assert bridge_job.main() == 0 and calls == []
    monkeypatch.setattr(bridge_job, "due", lambda c, now: True)
    held = run._acquire_lock(tmp_path / "run.lock", 0.0)
    assert bridge_job.main() == 0 and calls == []
    held.close()
    assert bridge_job.main() == 0 and len(calls) == 1
    assert capsys.readouterr().out == ""  # an idle poll prints nothing
    assert bridge_job.last_poll(con) is not None


# ---------------------------------------------------------------- veto / policy

@pytest.mark.parametrize("confirmed, bucket", [(True, "grok"), (None, "grok_unconfirmed"),
                                               (False, "grok_unconfirmed")])
def test_veto_counts_a_verified_grok_alert(con, confirmed, bucket):
    veto.ensure_schema(con)
    box = FakeMailbox()
    a = alert()
    if confirmed is not None:
        a["confirmed"] = confirmed
    send(box, a)
    inbox.poll(con, box, CONFIG, now=NOW)
    items = veto._collect_all(con, "NVDA", NOW, veto.DAYS)  # collect() hides grok_unconfirmed
    assert len(items.get(bucket, [])) == 1 and "muse" not in items
    assert ("grok" in veto.collect(con, "NVDA", now=NOW)) is (bucket == "grok")


def test_veto_ignores_an_unverified_grok_alert(con):
    veto.ensure_schema(con)
    box = FakeMailbox()
    send(box, alert(confirmed=True), auth="")
    inbox.poll(con, box, CONFIG, now=NOW)
    items = veto.collect(con, "NVDA", now=NOW)
    assert items.get("grok", []) == [] and items.get("grok_unconfirmed", []) == []


def test_policy_names_grok_for_a_grok_alert(con):
    box = FakeMailbox()
    send(box, alert(severity="elevated"))
    send(box, alert("alert-muse-0001", ticker="AMD", severity="elevated"), sender=SENDER, token=TOKEN, auth="")
    inbox.poll(con, box, CONFIG, now=NOW)
    notes = active_flags(con, NOW + timedelta(minutes=1), held={"NVDA", "AMD"}).notify
    assert any(n.startswith("Grok ALERT") and "NVDA" in n for n in notes)
    assert any(n.startswith("Muse ALERT") and "AMD" in n for n in notes)


# ---------------------------------------------------------------- 08:45 plan reminder (spec B.2)

def _briefed(con, d="2026-10-27"):
    con.execute("INSERT INTO bridge_ops VALUES (?, 'x')", (f"brief:{d}",))


def _plan_row(con, status="accepted", source="grok_verified", for_date="2026-10-27"):
    con.execute("INSERT INTO options_plans (plan_id, for_date, received_at, status, shadow_only, error, sha, raw, "
                "source) VALUES (?,?,?,?,?,?,?,?,?)", ("p-20261027", for_date, et(8, 10).isoformat(), status, 0, "",
                                                     "s", "{}", source))


def _daily_row(con, at, source="grok_verified", msg_id="grok-20261027-0730"):
    con.execute("INSERT INTO bridge_messages (msg_id, type, sender, received_at, raw_body, parsed_json, source) "
                "VALUES (?,?,?,?,?,?,?)", (msg_id, "GROK_DAILY", GROK, muse_ops._iso(at), "{}", "{}", source))


def test_plan_reminder_sent_at_0845(desk, pages):
    _briefed(desk)
    _daily_row(desk, et(7, 30))
    box = FakeMailbox()
    assert grok_outbound.run(desk, box, et(8, 44), CONFIG, returns) == []
    lines = grok_outbound.run(desk, box, et(8, 45), CONFIG, returns)
    assert len(lines) == 1 and "PLAN_REMINDER" in lines[0]
    (body,) = _bodies(box, "PLAN_REMINDER")
    assert body["for_date"] == "2026-10-27" and body["missing"] == ["OPTIONS_PLAN"]
    assert body["live_cutoff"] == "09:20:00" and body["msg_id"].startswith("planrm-")
    assert OUTBOUND.validate_python(body).type == "PLAN_REMINDER"
    assert all(to == GROK for to, _, _ in box.sent)
    assert desk.execute("SELECT channel FROM bridge_outbox WHERE type='PLAN_REMINDER'").fetchone()[0] == "grok"
    assert grok_outbound._done(desk, "planrm:2026-10-27")
    assert [p[1] for p in pages] == ["Grok plan missing at 08:45, reminder sent"]
    # never twice
    assert grok_outbound.run(desk, box, et(9, 0), CONFIG, returns) == []
    assert len(_bodies(box, "PLAN_REMINDER")) == 1 and len(pages) == 1


@pytest.mark.parametrize("status", ["accepted", "rejected"])
def test_no_reminder_when_a_verified_plan_exists(desk, pages, status):
    _briefed(desk)
    _plan_row(desk, status=status)
    box = FakeMailbox()
    assert grok_outbound.run(desk, box, et(8, 50), CONFIG, returns) == []
    assert box.sent == [] and pages == []
    assert not grok_outbound._done(desk, "planrm:2026-10-27")


def test_unverified_or_other_day_plan_does_not_stop_the_reminder(desk, pages):
    _briefed(desk)
    _plan_row(desk, source="grok_legacy")
    desk.execute("UPDATE options_plans SET plan_id='p-old'")
    _plan_row(desk, for_date="2026-10-26")
    box = FakeMailbox()
    assert len(grok_outbound.run(desk, box, et(8, 50), CONFIG, returns)) == 1
    assert len(_bodies(box, "PLAN_REMINDER")) == 1


def test_grok_daily_listed_only_when_absent_at_0830(desk, pages):
    _briefed(desk)
    _daily_row(desk, et(8, 31))  # late daily
    _daily_row(desk, et(7, 0), source="grok_legacy", msg_id="grok-20261027-0700")  # unverified
    _daily_row(desk, et(7, 0, PREV), msg_id="grok-20261026-0700")  # yesterday's
    box = FakeMailbox()
    grok_outbound.run(desk, box, et(8, 45), CONFIG, returns)
    assert _bodies(box, "PLAN_REMINDER")[0]["missing"] == ["OPTIONS_PLAN", "GROK_DAILY"]

    desk.execute("DELETE FROM bridge_ops WHERE key='planrm:2026-10-27'")
    _daily_row(desk, et(8, 30), msg_id="grok-20261027-0830")  # exactly 08:30 is on time
    box = FakeMailbox()
    grok_outbound.run(desk, box, et(8, 46), CONFIG, returns)
    assert _bodies(box, "PLAN_REMINDER")[0]["missing"] == ["OPTIONS_PLAN"]


def test_first_poll_after_0920_skips_late(desk, pages):
    _briefed(desk)
    box = FakeMailbox()
    assert grok_outbound.run(desk, box, et(9, 20), CONFIG, returns) == []
    assert box.sent == [] and pages == []
    assert desk.execute("SELECT at FROM bridge_ops WHERE key='planrm:2026-10-27'").fetchone()[0] == "skipped_late"
    assert grok_outbound.run(desk, box, et(9, 25), CONFIG, returns) == [] and box.sent == []


def test_no_reminder_on_holiday_or_without_grok(desk, pages):
    box = FakeMailbox()
    assert grok_outbound.run(desk, box, et(8, 50, date(2026, 11, 26)), CONFIG, returns) == []
    assert grok_outbound.run(desk, box, et(8, 50, date(2026, 10, 31)), CONFIG, returns) == []  # Saturday
    assert grok_outbound.run(desk, box, et(8, 50), MUSE_ONLY, returns) == []
    assert box.sent == [] and pages == []
    assert desk.execute("SELECT COUNT(*) FROM bridge_ops WHERE key LIKE 'planrm:%'").fetchone()[0] == 0


def test_failed_reminder_send_is_retried(desk, pages):
    _briefed(desk)

    class Broken(FakeMailbox):
        def send(self, *a, **k):
            raise OSError("smtp down")
    with pytest.raises(OSError):
        grok_outbound.run(desk, Broken(), et(8, 45), CONFIG, returns)
    assert not grok_outbound._done(desk, "planrm:2026-10-27") and pages == []
    box = FakeMailbox()
    assert len(grok_outbound.run(desk, box, et(8, 50), CONFIG, returns)) == 1


def test_failed_owner_push_does_not_resend(desk, monkeypatch):
    _briefed(desk)

    def boom(*a, **k):
        raise RuntimeError("ntfy down")
    monkeypatch.setattr(muse_ops.notify, "send", boom)
    box = FakeMailbox()
    assert len(grok_outbound.run(desk, box, et(8, 45), CONFIG, returns)) == 1
    assert grok_outbound.run(desk, box, et(8, 50), CONFIG, returns) == []
    assert len(_bodies(box, "PLAN_REMINDER")) == 1
