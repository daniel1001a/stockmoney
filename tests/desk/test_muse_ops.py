import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from stockmoney.desk import muse_ops, store
from stockmoney.desk.bridge.inbox import poll
from stockmoney.desk.bridge.mailbox import BridgeConfig, FakeMailbox
from stockmoney.desk.bridge.outbox import build_subject
from stockmoney.desk.bridge.schema import ResearchRequest

TOKEN = "s3cret-shared-token-abcdef"
MUSE = "owner@example.com"
CONFIG = BridgeConfig(desk_address="desk@example.com", app_password="x", muse_address=MUSE, shared_token=TOKEN)
ET = muse_ops.ET


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("DESK_MAIL_ADDRESS", "desk@example.com")
    monkeypatch.setenv("DESK_MAIL_APP_PASSWORD", "x")
    monkeypatch.setenv("MUSE_ADDRESS", MUSE)
    monkeypatch.setenv("MUSE_SHARED_TOKEN", TOKEN)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(store.SCHEMA)
    muse_ops.ensure_schema(c)
    yield c
    c.close()


def social(msg_id, when):
    return {"msg_id": msg_id, "sent_at": when.isoformat(), "type": "SOCIAL_DAILY",
            "collected_at": when.isoformat(), "items": []}


def deliver(box, payload, *, sender=MUSE, token=TOKEN, body=None):
    box.inject(sender, build_subject(payload["type"], payload["msg_id"], token), body or json.dumps(payload))


def test_feedback_only_for_authenticated_mail(con):
    box = FakeMailbox()
    now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    bad = social("sd-bad-0001", now)
    del bad["collected_at"]  # a missing required field: never repaired by leniency
    deliver(box, bad)
    deliver(box, social("sd-spoof-01", now), sender="attacker@example.com")
    deliver(box, social("sd-token-01", now), token="wrong-token-0123456789")
    poll(con, box, CONFIG, now=now)
    assert muse_ops.send_feedback(con, box, now) == 1
    to, subject, body = box.sent[0]
    assert to == MUSE and "[SMD/FEEDBACK/" in subject
    fb = json.loads(body)
    assert fb["about_msg_id"] == "sd-bad-0001" and "schema_invalid" in fb["problem"]
    # answered once only
    assert muse_ops.send_feedback(con, box, now) == 0


def test_missed_delivery_reported_once_and_only_when_enabled(con):
    day = datetime(2026, 10, 12, tzinfo=ET)
    late = day.replace(hour=9, minute=0)
    assert muse_ops.check_deliveries(con, late) == []  # nothing enabled yet
    store.set_setting(con, "muse_expect", json.dumps(["SOCIAL_AM", "SOCIAL_PM"]))
    misses = muse_ops.check_deliveries(con, late)
    assert len(misses) == 1 and "早盤" in misses[0]
    assert muse_ops.check_deliveries(con, late + timedelta(minutes=15)) == []


def test_on_time_delivery_not_reported(con):
    store.set_setting(con, "muse_expect", json.dumps(["SOCIAL_AM"]))
    box = FakeMailbox()
    morning = datetime(2026, 10, 12, 7, 30, tzinfo=ET)
    deliver(box, social("sd-20261012-am", morning))
    poll(con, box, CONFIG, now=morning)
    assert muse_ops.check_deliveries(con, morning.replace(hour=9)) == []
    card = muse_ops.scorecard(con, morning.replace(hour=9), days=1)
    assert card["deliveries"]["SOCIAL_AM"] == {"name": "早盤社群快照", "on_time": 1, "due": 1}


def test_overdue_research_reply_reported(con):
    now = datetime(2026, 10, 12, 21, 0, tzinfo=timezone.utc)
    req = ResearchRequest(msg_id="rq-000001", sent_at=now, ticker="XOM", questions=["q"],
                          deadline=now + timedelta(hours=11))
    muse_ops.log_outbound(con, req)
    assert muse_ops.check_deliveries(con, now + timedelta(hours=10)) == []
    misses = muse_ops.check_deliveries(con, now + timedelta(hours=12))
    assert misses and "XOM" in misses[0]


def test_reliability_feedback_names_correct_identity_and_ack_fields():
    correction = muse_ops.fix_for("msg_id_mismatch OPTIONS_PLAN")
    assert correction == muse_ops.PLAN_RULES
    hint = muse_ops.fields_hint("schema_invalid NOTICE_ACK.schedule_times.0:string_pattern_mismatch")
    assert "notice_id" in hint
    assert "schedule_times" in hint
