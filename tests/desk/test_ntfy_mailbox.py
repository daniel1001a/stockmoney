import json
import sqlite3

import pytest

from stockmoney.desk.bridge import inbox, outbox
from stockmoney.desk.bridge.mailbox import ConfigError
from stockmoney.desk.bridge.ntfy_mailbox import CHANNEL_TAG, NtfyConfig, NtfyMailbox, configured
from stockmoney.desk.bridge.schema import Report

TOKEN = "t" * 40
TO_DESK = "desk-in-aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TO_MUSE = "desk-out-bbbbbbbbbbbbbbbbbbbbbbbbbbbb"
CFG = NtfyConfig(TO_DESK, TO_MUSE, TOKEN)


class Resp:
    def __init__(self, text="", content=None, status=200):
        self.text, self.status_code = text, status
        self.content = content if content is not None else text.encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FakeHttp:
    def __init__(self):
        self.pages: dict[str, Resp] = {}
        self.gets: list[tuple[str, dict]] = []
        self.posts: list[dict] = []

    def get(self, url, params=None, timeout=None, stream=False):
        self.gets.append((url, params or {}))
        return self.pages[url]

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append({"url": url, "data": data, "headers": headers})
        return Resp()


def ev(id_, title, message="", **extra):
    return json.dumps({"id": id_, "time": 1, "event": "message", "topic": TO_DESK, "title": title,
                       "message": message, **extra})


def alert_body(msg_id="alert-0001"):
    return json.dumps({"msg_id": msg_id, "sent_at": "2026-10-12T20:00:00+00:00", "type": "ALERT",
                       "ticker": "NVDA", "severity": "elevated", "headline": "probe",
                       "source_urls": ["https://example.com/a"], "observed_at": "2026-10-12T19:50:00+00:00"})


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def make(tmp_path, http):
    return NtfyMailbox(CFG, cursor_path=tmp_path / "cursor.json", http=http)


def test_config_validation():
    with pytest.raises(ConfigError):
        NtfyConfig("short", TO_MUSE, TOKEN)
    with pytest.raises(ConfigError):
        NtfyConfig(TO_DESK, TO_DESK, TOKEN)
    with pytest.raises(ConfigError):
        NtfyConfig(TO_DESK, TO_MUSE, "tooshort")
    assert "desk-in" not in repr(CFG)


def test_configured_flag():
    assert not configured({})
    assert configured({"NTFY_BRIDGE_TO_DESK": "a", "NTFY_BRIDGE_TO_MUSE": "b", "MUSE_SHARED_TOKEN": "c"})


def test_small_message_roundtrips_through_inbox(tmp_path, con):
    http = FakeHttp()
    subj = f"[SMD/ALERT/alert-0001] {TOKEN}"
    http.pages[f"{CFG.server}/{TO_DESK}/json"] = Resp(
        json.dumps({"id": "o1", "event": "open", "topic": TO_DESK}) + "\n" + ev("m1", subj, alert_body()) + "\n"
        + json.dumps({"id": "k1", "event": "keepalive"}) + "\n")
    box = make(tmp_path, http)
    n = inbox.poll(con, box, CFG.bridge_config())
    assert n == 1
    assert con.execute("SELECT type, sender FROM bridge_messages").fetchone()["sender"] == CHANNEL_TAG
    # the cursor advanced, so the next poll asks only for newer traffic
    http.gets.clear()
    inbox.poll(con, box, CFG.bridge_config())
    assert http.gets[0][1]["since"] == "m1"


def test_wrong_token_and_bad_body_rejected(tmp_path, con):
    http = FakeHttp()
    http.pages[f"{CFG.server}/{TO_DESK}/json"] = Resp(
        ev("m1", "[SMD/ALERT/alert-0001] " + "x" * 40, alert_body()) + "\n"
        + ev("m2", f"[SMD/ALERT/alert-0002] {TOKEN}", "not json at all"))
    assert inbox.poll(con, make(tmp_path, http), CFG.bridge_config()) == 0
    reasons = [r["reason"] for r in con.execute("SELECT reason FROM bridge_rejects ORDER BY id")]
    assert reasons[0] == "bad_token" and len(reasons) == 2


def test_large_message_arrives_as_attachment(tmp_path, con):
    http = FakeHttp()
    body = alert_body("alert-0009").encode()
    url = "https://ntfy.sh/file/abc.txt"
    http.pages[f"{CFG.server}/{TO_DESK}/json"] = Resp(
        ev("m1", f"[SMD/ALERT/alert-0009] {TOKEN}", "You received a file: abc.txt",
           attachment={"name": "abc.txt", "url": url, "size": len(body)}))
    http.pages[url] = Resp(content=body)
    assert inbox.poll(con, make(tmp_path, http), CFG.bridge_config()) == 1


def test_oversized_attachment_not_downloaded(tmp_path):
    http = FakeHttp()
    http.pages[f"{CFG.server}/{TO_DESK}/json"] = Resp(
        ev("m1", "s", "", attachment={"name": "x", "url": "https://ntfy.sh/file/x", "size": 10_000_000}))
    mails = make(tmp_path, http).fetch_unseen(200 * 1024)
    assert mails[0].oversized and mails[0].body == ""
    assert all("file/x" not in u for u, _ in http.gets)


def test_attachment_fetch_failure_is_a_transport_error_not_a_crash(tmp_path):
    http = FakeHttp()
    http.pages[f"{CFG.server}/{TO_DESK}/json"] = Resp(
        ev("m1", "s", "", attachment={"name": "x", "url": "https://ntfy.sh/file/missing", "size": 10}))
    # no page registered for the attachment URL -> KeyError inside fetch is contained
    mails = make(tmp_path, http).fetch_unseen(200 * 1024)
    assert mails[0].error.startswith("attachment fetch failed")


def test_send_posts_title_and_body_to_the_muse_topic(tmp_path):
    http = FakeHttp()
    box = make(tmp_path, http)
    msg = Report(msg_id="rp-0000000001", sent_at="2026-10-12T20:00:00+00:00", title="t", body_markdown="hello")
    outbox.send(box, msg, CFG.bridge_config())
    p = http.posts[0]
    assert p["url"] == f"{CFG.server}/{TO_MUSE}"
    assert p["headers"]["Title"] == f"[SMD/REPORT/rp-0000000001] {TOKEN}"
    assert json.loads(p["data"])["type"] == "REPORT"


def test_transport_failure_propagates(tmp_path):
    http = FakeHttp()
    http.pages[f"{CFG.server}/{TO_DESK}/json"] = Resp(status=500)
    with pytest.raises(RuntimeError):
        make(tmp_path, http).fetch_unseen(1000)
