"""Regression for real Muse mail: the composer turned the space between a
Latin word and CJK text into a raw newline inside JSON strings, so a valid
snapshot was rejected four times with json_invalid."""
import json
import sqlite3

import pytest

from stockmoney.desk import muse_ops, store
from stockmoney.desk.bridge import inbox
from stockmoney.desk.bridge.inbox import extract_json, poll, repair_string_newlines
from stockmoney.desk.bridge.mailbox import BridgeConfig, FakeMailbox

TOKEN = "s3cret-shared-token-abcdef"
MUSE = "muse@example.com"
CONFIG = BridgeConfig(desk_address="desk@example.com", app_password="x", muse_address=MUSE, shared_token=TOKEN)


def snapshot(themes, url="https://www.threads.com/@a/post/AbC"):
    return {
        "collected_at": "2026-10-10T08:35:00-04:00",
        "items": [{
            "bullish_ratio": None, "engagement": 1, "mention_count": 2, "platform": "threads",
            "sample_urls": [url], "ticker": "NVDA", "top_themes": themes, "unique_authors": 2,
            "window_end": "2026-10-10T08:35:00-04:00", "window_start": "2026-10-09T20:00:00-04:00",
        }],
        "msg_id": "hs-20261010-0830", "sent_at": "2026-10-10T08:40:00-04:00", "type": "SOCIAL_DAILY",
    }


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


def test_newline_inside_string_becomes_a_space():
    raw = '{"a":["AI\n概念股比較","x\r\ny"]}'
    assert json.loads(repair_string_newlines(raw)) == {"a": ["AI 概念股比較", "x y"]}


def test_newline_inside_url_is_dropped():
    raw = '{"u":"https://example.com/ab\ncd"}'
    assert json.loads(repair_string_newlines(raw)) == {"u": "https://example.com/abcd"}


def test_structure_whitespace_escapes_and_valid_json_are_untouched():
    pretty = '{\n  "a": [\n    "x\\ny",\n    "q\\"z"\n  ]\n}'
    assert repair_string_newlines(pretty) == pretty
    assert json.loads(repair_string_newlines(pretty))["a"] == ["x\ny", 'q"z']


def test_real_shape_of_the_rejected_mail_is_accepted(con):
    body = json.dumps(snapshot(["抽樣計數", "AI 概念股比較", "wallstreetbets 日常討論"]), ensure_ascii=False)
    broken = body.replace("AI 概念", "AI\n概念").replace("wallstreetbets 日常", "wallstreetbets\n日常")
    with pytest.raises(json.JSONDecodeError):
        json.loads(broken)  # this is what the old parser saw
    box = FakeMailbox()
    box.inject(MUSE, f"[SMD/SOCIAL_DAILY/hs-20261010-0830] {TOKEN}", broken)
    assert poll(con, box, CONFIG) == 1
    stored = json.loads(con.execute("SELECT parsed_json FROM bridge_messages").fetchone()[0])
    assert stored["items"][0]["top_themes"][1] == "AI 概念股比較"


def test_other_json_errors_still_rejected_with_a_position_hint(con):
    box = FakeMailbox()
    box.inject(MUSE, f"[SMD/SOCIAL_DAILY/hs-20261010-0831] {TOKEN}", '{"type": "SOCIAL_DAILY", "items": [}')
    assert poll(con, box, CONFIG) == 0
    reason = con.execute("SELECT reason FROM bridge_rejects").fetchone()[0]
    assert reason.startswith("schema_invalid") and "json_invalid" in reason
    assert "line" in reason or "column" in reason  # says where, never what the input said


def test_feedback_for_json_invalid_is_specific(con):
    box = FakeMailbox()
    box.inject(MUSE, f"[SMD/SOCIAL_DAILY/hs-20261010-0832] {TOKEN}", '{"type": "SOCIAL_DAILY", "items": [}')
    poll(con, box, CONFIG)
    from datetime import datetime, timezone
    assert muse_ops.send_feedback(con, box, datetime.now(timezone.utc)) == 1
    fb = json.loads(box.sent[-1][2])
    assert "換行" in fb["how_to_fix"] and "protocol" in fb["how_to_fix"]
