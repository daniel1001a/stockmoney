"""Review finding M3: the desk must never answer its own echo, and feedback
mail is capped. Plus the policy scan bound (finding d). Offline."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import get_args

import pytest

from stockmoney.desk import muse_ops, notify, store
from stockmoney.desk.bridge import policy
from stockmoney.desk.bridge.inbox import poll
from stockmoney.desk.bridge.mailbox import BridgeConfig, FakeMailbox
from stockmoney.desk.bridge.outbox import build_subject
from stockmoney.desk.bridge.schema import Outbound

TOKEN = "s3cret-shared-token-abcdef"
MUSE = "owner@example.com"
CONFIG = BridgeConfig(desk_address="desk@example.com", app_password="x", muse_address=MUSE, shared_token=TOKEN)
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


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


@pytest.fixture
def pushed(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "send", lambda title, msg, prio=notify.INFO, tags=None: calls.append((title, msg)) or True)
    return calls


def bad_social(msg_id):
    """Passes sender and token checks, fails the schema: a correctable reject.
    A required top-level field is missing, which leniency never repairs."""
    return {"msg_id": msg_id, "sent_at": NOW.isoformat(), "type": "SOCIAL_DAILY", "items": []}


def deliver(box, payload, *, prefix="", subject_type=None):
    subject = build_subject(subject_type or payload["type"], payload["msg_id"], TOKEN)
    box.inject(MUSE, prefix + subject, json.dumps(payload))


def reject_reasons(con):
    return [r[0] for r in con.execute("SELECT reason FROM bridge_rejects ORDER BY id")]


def reply_to_last(box, *, prefix="Re: ", strip_type_prefix=False):
    """What Muse's mail client does: quote the desk's mail under a Re: subject."""
    _, subject, body = box.sent[-1]
    quoted = "\n".join("> " + line for line in body.splitlines())
    if strip_type_prefix:
        prefix = ""
    box.inject(MUSE, prefix + subject, "Thanks, will fix.\n\nOn Mon the desk wrote:\n" + quoted)


@pytest.mark.parametrize("prefix", ["Re: ", "RE: Re: ", "Fwd: ", ""])
def test_quoted_reply_to_feedback_does_not_start_a_loop(con, prefix):
    box = FakeMailbox()
    deliver(box, bad_social("sd-bad-0001"))
    poll(con, box, CONFIG, now=NOW)
    assert muse_ops.send_feedback(con, box, NOW) == 1
    sent = []
    for round_ in range(4):  # the reviewer reproduced four rounds before the fix
        reply_to_last(box, prefix=prefix)
        poll(con, box, CONFIG, now=NOW + timedelta(minutes=round_))
        sent.append(muse_ops.send_feedback(con, box, NOW + timedelta(minutes=round_)))
    assert sent == [0, 0, 0, 0]
    assert len([m for m in box.sent if "[SMD/FEEDBACK/" in m[1]]) == 1


def test_quoted_reply_to_report_is_not_answered(con):
    from stockmoney.desk.bridge import outbox
    from stockmoney.desk.bridge.schema import Report
    box = FakeMailbox()
    outbox.send(box, Report(msg_id="rep-0000001", sent_at=NOW, title="t", body_markdown="b"), CONFIG)
    reply_to_last(box)
    poll(con, box, CONFIG, now=NOW)
    assert len(reject_reasons(con)) == 1
    assert muse_ops.send_feedback(con, box, NOW) == 0
    assert len(box.sent) == 1  # only the original report


def test_outbound_types_are_exactly_the_schema_outbound_types():
    from stockmoney.desk.bridge.inbox import OUTBOUND_ONLY_TYPES
    union = get_args(Outbound)[0]
    assert OUTBOUND_ONLY_TYPES == {m.model_fields["type"].default for m in get_args(union)}


def test_prefixed_mail_that_fails_validation_is_not_correctable_but_unprefixed_is(con):
    box = FakeMailbox()
    deliver(box, bad_social("sd-bad-0002"), prefix="Re: ")
    deliver(box, bad_social("sd-bad-0003"))
    poll(con, box, CONFIG, now=NOW)
    reasons = reject_reasons(con)
    assert reasons[0].startswith("reply_prefix schema_invalid") and reasons[1].startswith("schema_invalid")
    assert muse_ops.send_feedback(con, box, NOW) == 1
    assert json.loads(box.sent[0][2])["about_msg_id"] == "sd-bad-0003"


def test_valid_prefixed_mail_is_still_accepted(con):
    box = FakeMailbox()
    ok = {"msg_id": "sd-ok-00001", "sent_at": NOW.isoformat(), "type": "SOCIAL_DAILY",
          "collected_at": NOW.isoformat(), "items": []}
    deliver(box, ok, prefix="Re: ")
    assert poll(con, box, CONFIG, now=NOW) == 1


def test_stored_echo_rejects_from_before_the_fix_are_not_answered(con):
    """Rows written by the old inbox: schema_invalid with a Re:/outbound subject."""
    from stockmoney.desk.bridge.inbox import is_echo_subject
    for i, subj in enumerate([f"Re: [SMD/FEEDBACK/fb-{i:08d}] <token>" for i in range(2)]
                             + ["[SMD/REPORT/rep-00000001] <token>"]):
        con.execute("INSERT INTO bridge_rejects (received_at, sender, subject, msg_id, reason) VALUES (?,?,?,?,?)",
                    (NOW.isoformat(), MUSE, subj, f"x-{i:08d}", "schema_invalid type:union_tag_invalid"))
    con.execute("INSERT INTO bridge_rejects (received_at, sender, subject, msg_id, reason) VALUES (?,?,?,?,?)",
                (NOW.isoformat(), MUSE, "[SMD/SOCIAL_DAILY/sd-real-0001] <token>", "sd-real-0001", "schema_invalid items.0.x:missing"))
    assert [k for k, _, _ in muse_ops.pending_feedback(con)] == ["fb:4"]
    assert is_echo_subject("Re: [SMD/ALERT/alert-0001] <token>")
    assert not is_echo_subject("[SMD/ALERT/alert-0001] <token>")
    assert not is_echo_subject("hello")


def test_feedback_capped_at_ten_per_rolling_24h_and_owner_told_once(con, pushed):
    box = FakeMailbox()
    for i in range(13):
        deliver(box, bad_social(f"sd-bad-{i:04d}"))
    poll(con, box, CONFIG, now=NOW)
    assert muse_ops.send_feedback(con, box, NOW) == 10
    assert muse_ops.send_feedback(con, box, NOW + timedelta(hours=1)) == 0
    assert muse_ops.send_feedback(con, box, NOW + timedelta(hours=5)) == 0
    assert len(pushed) == 1 and "上限" in pushed[0][0]
    assert len([m for m in box.sent if "[SMD/FEEDBACK/" in m[1]]) == 10
    # the window rolls: the three left-over corrections go out, nothing is lost
    assert muse_ops.send_feedback(con, box, NOW + timedelta(hours=25)) == 3
    assert len(pushed) == 1


def test_run_checks_reports_sent_feedback_and_respects_cap(con, pushed):
    box = FakeMailbox()
    for i in range(11):
        deliver(box, bad_social(f"sd-bad-{i:04d}"))
    poll(con, box, CONFIG, now=NOW)
    out = muse_ops.run_checks(con, NOW, box)
    assert "10" in out


# ---------------------------------------------------------------- policy scan bound

def _alert(msg_id, ticker, when):
    return {"msg_id": msg_id, "sent_at": when.isoformat(), "type": "ALERT", "ticker": ticker,
            "severity": "critical", "headline": "h", "source_urls": ["https://example.com/a"],
            "observed_at": when.isoformat()}


def test_active_flags_does_not_rescan_old_messages(con, monkeypatch):
    from stockmoney.desk.bridge.inbox import ensure_schema
    ensure_schema(con)

    def put(i, when):
        con.execute("INSERT INTO bridge_messages (msg_id, type, sender, sent_at, received_at, raw_body, parsed_json) "
                "VALUES (?,?,?,?,?,?,?)",
                    (f"al-{i:06d}", "ALERT", MUSE, when.isoformat(), when.isoformat(timespec="seconds"), "",
                     json.dumps(_alert(f"al-{i:06d}", "NVDA", when))))

    for i in range(40):
        put(i, NOW - timedelta(days=30 + i))
    put(100, NOW - timedelta(hours=2))
    seen = []
    real = policy._parse_ts
    monkeypatch.setattr(policy, "_parse_ts", lambda s: seen.append(s) or real(s))
    flags = policy.active_flags(con, NOW, held=())
    assert "NVDA" in flags.blocked
    assert len(seen) == 1  # only the recent row was read and parsed



def test_feedback_lists_the_fields_of_the_rejected_type():
    from stockmoney.desk.muse_ops import fix_for
    reason = ("schema_invalid ALERT.window_start:extra_forbidden; ALERT.window_end:extra_forbidden; "
              "ALERT.ticker:missing; ALERT.observed_at:missing")
    text = fix_for(reason)
    for name in ("ticker", "severity", "headline", "source_urls", "observed_at"):
        assert name in text.split("選填")[0]
    assert "window_start" not in text.split("選填")[0]
    assert len(text) < 1000  # fits the FEEDBACK how_to_fix limit


def test_options_plan_feedback_says_where_each_wrong_field_goes():
    # the shape of Grok's 10-15..10-21 rejects: top-level daily_ref/key_events, ticker not underlying
    from stockmoney.desk.muse_ops import fix_for
    reason = ("schema_invalid OPTIONS_PLAN.daily_ref:extra_forbidden; OPTIONS_PLAN.key_events:extra_forbidden; "
              "OPTIONS_PLAN.market_context:missing; OPTIONS_PLAN.avoid:missing; "
              "OPTIONS_PLAN.plans.0.ticker:extra_forbidden; OPTIONS_PLAN.plans.0.rvol:extra_forbidden; "
              "OPTIONS_PLAN.plans.0.timing:extra_forbidden; OPTIONS_PLAN.plans.1.gap:extra_forbidden; "
              "OPTIONS_PLAN.plans.1.premarket_price:extra_forbidden")
    text = fix_for(reason)
    for fix in ("改名為 underlying", "key_events 要放進 market_context", "每個 plans 項目的 daily_ref",
                "avoid 必填", "min_rvol", "earliest/latest", "gap 不是欄位", "premarket_price 不是欄位"):
        assert fix in text
    assert len(text) <= 1000  # more errors than the 5-error cap: the field list is dropped to fit how_to_fix
    only_ticker = fix_for("schema_invalid OPTIONS_PLAN.plans.0.ticker:extra_forbidden")
    assert "underlying" in only_ticker and "key_events 要放進" not in only_ticker
    assert "每個劇本必填" in only_ticker  # the 5-error cap can hide a missing bias
    assert "每個劇本必填" not in fix_for("schema_invalid OPTIONS_PLAN.market_context:missing")


def test_options_plan_feedback_names_unlisted_extra_plan_fields():
    # 10-22 reject: a data-only plan with premarket_gap, which no fixed hint names
    from stockmoney.desk.muse_ops import fix_for
    text = fix_for("schema_invalid OPTIONS_PLAN.daily_ref:extra_forbidden; OPTIONS_PLAN.key_events:extra_forbidden; "
                   "OPTIONS_PLAN.market_context:missing; OPTIONS_PLAN.plans.0.ticker:extra_forbidden; "
                   "OPTIONS_PLAN.plans.0.premarket_gap:extra_forbidden")
    assert "premarket_gap 不是劇本欄位" in text and "plans 可以是 []" in text
    assert "ticker 不是劇本欄位" not in text  # ticker keeps its own rename hint
    assert len(text) < 1000


def test_options_plan_feedback_avoid_ticker_is_not_flagged():
    from stockmoney.desk.muse_ops import fix_for
    # an avoid item's ticker is a real field; a plain GROK_DAILY reject gets none of these
    assert "改名" not in fix_for("schema_invalid GROK_DAILY.ticker:extra_forbidden")
