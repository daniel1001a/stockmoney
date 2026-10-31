"""GROK_NOTICE send script and NOTICE_ACK tracking (GROK_RELIABILITY_SPEC B.3).
Offline only: FakeMailbox, an in-memory or tmp sqlite, notify patched."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from grok_helpers import SENDER, TOKEN

from stockmoney.desk import grok_notice, muse_ops, notify, store
from stockmoney.desk.bridge import inbox
from stockmoney.desk.bridge.mailbox import BridgeConfig, FakeMailbox

ROOT = Path(__file__).resolve().parents[2]
NOTICE = ROOT / "docs/desk/notices/notice-desk-20261031-reliability.json"
GROK = "pika@mail.grokbot.com"
GTOKEN = "g" * 40
CONFIG = BridgeConfig(desk_address="desk@example.com", app_password="x", muse_address=SENDER, shared_token=TOKEN,
                      grok_address=GROK, grok_token=GTOKEN)
GOOD_AR = ("mx.google.com; dkim=pass header.i=@mail.grokbot.com header.s=k7abc header.b=AbC; "
           "spf=pass smtp.mailfrom=x@mail.grokbot.com; dmarc=pass header.from=mail.grokbot.com")
NOW = datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)
NID = "notice-desk-20261031-reliability"


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(store.SCHEMA)
    muse_ops.ensure_schema(c)
    yield c
    c.close()


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda title, body, *a, **k: sent.append((title, body)))
    return sent


def _script():
    from scripts.desk_grok_notice import main
    return main


def _ack(status="done", msg_id="ack-grok-20261031-1700", notice_id=NID):
    return {"protocol": "1", "msg_id": msg_id, "sent_at": (NOW + timedelta(hours=1)).isoformat(),
            "type": "NOTICE_ACK", "notice_id": notice_id, "status": status,
            "schedule_times": ["07:20", "08:15", "08:50", "20:00"], "timezone": "America/New_York",
            "questions": [], "notes": "" if status == "done" else "One cron could not be changed"}


def _deliver(con, payload, auth=GOOD_AR):
    box = FakeMailbox()
    box.inject(GROK, f"[SMD/NOTICE_ACK/{payload['msg_id']}] {GTOKEN}", json.dumps(payload), auth_results=auth)
    return inbox.poll(con, box, CONFIG, now=NOW + timedelta(hours=1))


def test_notice_file_validates_and_keeps_the_spec_limits():
    notice = grok_notice.load(NOTICE, NOW)
    assert notice.notice_id == notice.msg_id == NID and notice.ack_required
    assert notice.sent_at == NOW and len(notice.actions) == 5
    assert all(len(a) <= 280 for a in notice.actions)


@pytest.mark.parametrize("field", ["msg_id", "sent_at", "type", "protocol"])
def test_notice_file_may_not_set_send_time_fields(tmp_path, field):
    data = json.loads(NOTICE.read_text())
    data[field] = "x"
    path = tmp_path / "n.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match=field):
        grok_notice.load(path, NOW)


def test_dry_run_prints_exactly_what_a_send_mails(tmp_path, capsys):
    main = _script()
    assert main(["--file", str(NOTICE), "--dry-run"], now=NOW, db=tmp_path / "desk.sqlite") == 0
    printed = capsys.readouterr().out
    assert not (tmp_path / "desk.sqlite").exists()  # a dry run records nothing
    box = FakeMailbox()
    assert main(["--file", str(NOTICE)], mailbox=box, config=CONFIG, now=NOW, db=tmp_path / "desk.sqlite") == 0
    [(to, subject, body)] = box.sent
    assert to == GROK and printed == f"Subject: {subject}\n\n{body}\n"
    assert GTOKEN not in subject and TOKEN not in subject and GTOKEN not in body


def test_a_notice_id_is_never_sent_twice(tmp_path, capsys):
    main, box, db = _script(), FakeMailbox(), tmp_path / "desk.sqlite"
    assert main(["--file", str(NOTICE)], mailbox=box, config=CONFIG, now=NOW, db=db) == 0
    assert main(["--file", str(NOTICE)], mailbox=box, config=CONFIG, now=NOW, db=db) == 1
    assert len(box.sent) == 1
    with store.connect(db) as c:
        with pytest.raises(ValueError, match="already sent"):
            grok_notice.send(c, box, grok_notice.load(NOTICE, NOW), CONFIG)


def test_send_refuses_without_the_grok_channel(tmp_path):
    muse_only = BridgeConfig(desk_address="desk@example.com", app_password="x", muse_address=SENDER,
                             shared_token=TOKEN)
    box = FakeMailbox()
    assert _script()(["--file", str(NOTICE)], mailbox=box, config=muse_only, now=NOW,
                     db=tmp_path / "desk.sqlite") == 2
    assert box.sent == []


def test_verified_ack_marks_the_notice_and_stops_the_push(con, pushes):
    grok_notice.send(con, FakeMailbox(), grok_notice.load(NOTICE, NOW), CONFIG)
    assert _deliver(con, _ack()) == 1
    assert grok_notice.ack_status(con, NID) == "done"
    assert grok_notice.check_acks(con, NOW + timedelta(hours=30)) == [] and pushes == []


def test_unverified_ack_does_not_replace_a_verified_one_or_stop_the_push(con, pushes):
    grok_notice.send(con, FakeMailbox(), grok_notice.load(NOTICE, NOW), CONFIG)
    assert _deliver(con, _ack("partial"), auth="mx.google.com; dkim=fail") == 1
    assert grok_notice.ack_status(con, NID) == "partial unverified"
    assert len(grok_notice.check_acks(con, NOW + timedelta(hours=25))) == 1
    assert _deliver(con, _ack(msg_id="ack-grok-20261031-1800")) == 1
    assert grok_notice.ack_status(con, NID) == "done"
    assert _deliver(con, _ack("cannot", msg_id="ack-grok-20261031-1900"), auth="mx.google.com; dkim=fail") == 1
    assert grok_notice.ack_status(con, NID) == "done"


def test_ack_for_an_unsent_notice_marks_nothing(con):
    assert _deliver(con, _ack(notice_id="notice-never-sent")) == 1  # stored as data
    assert grok_notice.ack_status(con, "notice-never-sent") is None


def test_missing_ack_pushes_the_owner_once_after_24h(con, pushes):
    grok_notice.send(con, FakeMailbox(), grok_notice.load(NOTICE, NOW), CONFIG)
    assert grok_notice.check_acks(con, NOW + timedelta(hours=23)) == [] and pushes == []
    assert muse_ops.run_checks(con, NOW + timedelta(hours=24, minutes=5))  # wired into the bridge checks
    assert [t for t, _ in pushes] == ["Grok notice 未確認"] and NID in pushes[0][1]
    assert grok_notice.check_acks(con, NOW + timedelta(hours=48)) == []
    assert len(pushes) == 1


def test_no_ack_required_never_pushes(con, pushes, tmp_path):
    data = json.loads(NOTICE.read_text())
    data.update(notice_id="notice-desk-20261031-info", ack_required=False)
    path = tmp_path / "n.json"
    path.write_text(json.dumps(data))
    grok_notice.send(con, FakeMailbox(), grok_notice.load(path, NOW), CONFIG)
    assert grok_notice.ack_status(con, "notice-desk-20261031-info") == "not_required"
    assert grok_notice.check_acks(con, NOW + timedelta(days=3)) == [] and pushes == []
