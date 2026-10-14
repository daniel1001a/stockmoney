"""OPTIONS_PLAN delivered by email through the Muse bridge: same strict
validator and the same ingest as the CLI, no policy effect, no stock-side rows.
Fully offline (FakeMailbox, no real mail, no broker)."""
import json
from datetime import datetime, timezone

import pytest

from stockmoney.desk import muse_ops, notify, store
from stockmoney.desk.bridge import inbox, policy
from stockmoney.desk.bridge.mailbox import BridgeConfig, FakeMailbox
from stockmoney.desk.options import plan as oplan
from stockmoney.desk.options import store as ostore

from opt_fakes import DAY, et
from test_opt_engine import Rig, _plan

TOKEN = "s3cret-shared-token-abcdef"
SENDER = "owner@example.com"
CONFIG = BridgeConfig(desk_address="desk@example.com", app_password="x", muse_address=SENDER, shared_token=TOKEN)
PID = "gp-20261013-0800"
EARLY = et(8, 30)  # received before 09:20 ET: live-eligible
LATE = et(9, 25)


@pytest.fixture
def con(tmp_path):
    with store.connect(tmp_path / "desk.sqlite") as c:
        yield c


@pytest.fixture(autouse=True)
def pushes(monkeypatch):
    out = []
    for k, v in (("DESK_MAIL_ADDRESS", "desk@example.com"), ("DESK_MAIL_APP_PASSWORD", "x"),
                 ("MUSE_ADDRESS", SENDER), ("MUSE_SHARED_TOKEN", TOKEN)):
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(notify, "send", lambda title, msg, prio=notify.INFO, tags=None: out.append((title, msg)) or True)
    return out


def plan(**over) -> dict:
    d = json.loads(_plan(plan_id=PID))
    d.update(over)
    return d


def mail(mb, d: dict | None = None, *, body: str | None = None, subject_id: str | None = None,
         sender: str = SENDER, token: str = TOKEN, **kw):
    d = d if d is not None else plan()
    sid = subject_id or d.get("plan_id", PID)
    return mb.inject(sender, f"[SMD/OPTIONS_PLAN/{sid}] {token}",
                     body if body is not None else json.dumps(d, ensure_ascii=False), **kw)


def poll(con, mb, now=EARLY):
    return inbox.poll(con, mb, CONFIG, now=now)


def plans_rows(con):
    ostore.ensure_schema(con)
    return [tuple(r) for r in con.execute(
        "SELECT plan_id, for_date, received_at, status, shadow_only, error, sha, raw FROM options_plans ORDER BY id")]


def rejects(con):
    return [r[0] for r in con.execute("SELECT reason FROM bridge_rejects ORDER BY id")]


def test_valid_mail_is_stored_identically_to_cli_ingest(con, tmp_path, pushes):
    mb = FakeMailbox()
    mail(mb)
    assert poll(con, mb) == 1
    with store.connect(tmp_path / "cli.sqlite") as c2:
        res = oplan.ingest(c2, json.dumps(plan(), ensure_ascii=False), EARLY)
        assert res.status == "accepted"
        assert plans_rows(con) == plans_rows(c2)
    assert rejects(con) == []
    row = con.execute("SELECT type, msg_id, received_at FROM bridge_messages").fetchone()
    assert (row[0], row[1]) == ("OPTIONS_PLAN", PID)
    assert row[2] == ostore.iso(EARLY) or row[2].startswith("2026-10-13T12:30")  # desk clock, not Grok's
    assert pushes == [("OPTIONS_PLAN 已收到", "OPTIONS_PLAN 已收到: 1 個劇本")]


def test_code_fenced_and_wrapped_body_is_accepted_like_other_types(con):
    mb = FakeMailbox()
    mail(mb, body="Hi\n```json\n" + json.dumps(plan()) + "\n```\nthanks")
    assert poll(con, mb) == 1


def test_wrong_sender_ignored_and_wrong_token_silent(con, pushes):
    mb = FakeMailbox()
    mail(mb, sender="evil@example.com")
    mail(mb, token="wrong-token-wrong-token")
    assert poll(con, mb) == 0
    assert rejects(con) == ["sender_not_allowed", "bad_token"]
    assert plans_rows(con) == [] and pushes == []
    muse_ops.ensure_schema(con)
    assert muse_ops.pending_feedback(con) == []  # neither is correctable: never answered


def test_subject_plan_id_mismatch_rejected(con):
    mb = FakeMailbox()
    mail(mb, subject_id="gp-20261013-0801")
    assert poll(con, mb) == 0
    assert rejects(con) == ["msg_id_mismatch"]
    assert plans_rows(con) == []


@pytest.mark.parametrize("name,mutate", [
    ("window_0930", lambda d: d["plans"][0]["trigger"].update(earliest="09:30")),
    ("z_offset", lambda d: d.update(generated_at="2026-10-13T11:45:00Z")),
    ("extra_field", lambda d: d.update(msg_id="gp-20261013-0800")),
    ("bias_neutral", lambda d: d["plans"][0].update(bias="neutral")),
    ("thirteen_plans", lambda d: d.update(plans=[d["plans"][0]] * 13)),
])
def test_schema_violation_gives_feedback_and_stores_nothing(con, name, mutate):
    d = plan()
    mutate(d)
    mb = FakeMailbox()
    mail(mb, d)
    assert poll(con, mb) == 0
    (reason,) = rejects(con)
    assert reason.startswith("schema_invalid OPTIONS_PLAN.")
    assert plans_rows(con) == []
    assert con.execute("SELECT COUNT(*) FROM bridge_messages").fetchone()[0] == 0
    muse_ops.ensure_schema(con)
    out = FakeMailbox()
    assert muse_ops.send_feedback(con, out, EARLY) == 1
    body = json.loads(out.sent[0][2])
    assert body["type"] == "FEEDBACK" and body["problem"].startswith("【給 Grok】")
    assert body["about_msg_id"] == PID and "OPTIONS_PLAN" in body["problem"]
    assert "OPTIONS_PLAN" in body["how_to_fix"]


def test_failed_field_is_named_in_the_feedback(con):
    d = plan()
    d["plans"][0]["trigger"]["earliest"] = "09:30"
    mb = FakeMailbox()
    mail(mb, d)
    poll(con, mb)
    assert "plans.0.trigger" in rejects(con)[0] and "09:35" in rejects(con)[0]


def test_identical_duplicate_is_a_silent_noop(con, pushes):
    mb = FakeMailbox()
    mail(mb)
    mail(mb)
    assert poll(con, mb) == 1
    assert len(plans_rows(con)) == 1 and rejects(con) == []
    assert len(pushes) == 1
    muse_ops.ensure_schema(con)
    assert muse_ops.pending_feedback(con) == []


def test_duplicate_of_a_cli_ingested_plan_is_a_noop(con, pushes):
    oplan.ingest(con, json.dumps(plan(), ensure_ascii=False), EARLY)
    mb = FakeMailbox()
    mail(mb)
    assert poll(con, mb) == 0
    assert len(plans_rows(con)) == 1 and rejects(con) == [] and pushes == []


def test_same_id_different_content_rejected_with_feedback(con):
    mb = FakeMailbox()
    mail(mb)
    mail(mb, plan(notes="changed"))
    assert poll(con, mb) == 1
    assert rejects(con) == ["plan_id_reused"]
    assert [r[3] for r in plans_rows(con)] == ["accepted", "rejected"]
    muse_ops.ensure_schema(con)
    out = FakeMailbox()
    assert muse_ops.send_feedback(con, out, EARLY) == 1
    body = json.loads(out.sent[0][2])
    assert body["problem"].startswith("【給 Grok】") and "plan_id" in body["how_to_fix"]
    assert oplan.active_plan(con, DAY).plan.notes == ""  # the first plan stays in force


def test_late_arrival_is_shadow_only(con, pushes):
    mb = FakeMailbox()
    mail(mb)
    assert poll(con, mb, now=LATE) == 1
    assert plans_rows(con)[0][4] == 1
    assert "shadow_only" in pushes[0][1]


def test_oversize_rejected(con):
    mb = FakeMailbox()
    mail(mb, plan(notes="x"), body=json.dumps(plan(pad="A" * (inbox.MAX_RAW_BYTES + 10))))
    mail(mb, plan(), body=json.dumps(plan(pad="A" * 70_000)), subject_id=PID)
    assert poll(con, mb) == 0
    assert rejects(con)[0] == "oversized"
    assert rejects(con)[1].startswith("schema_invalid OPTIONS_PLAN.")
    assert plans_rows(con) == []


def test_grok_prefix_stays_reserved_and_cross_type_id_collision_rejected(con):
    d = plan(plan_id="grok-20261013-0800")
    mb = FakeMailbox()
    mail(mb, d)
    assert poll(con, mb) == 0
    assert "reserved_prefix" in rejects(con)[0]
    # a plan_id already used by a different message type is a duplicate id
    con.execute("INSERT INTO bridge_messages VALUES (?,?,?,?,?,?,?)",
                (PID, "IDEA", SENDER, "x", "2026-10-13T00:00:00+00:00", "{}", "{}"))
    mb2 = FakeMailbox()
    mail(mb2)
    assert poll(con, mb2) == 0
    assert rejects(con)[-1] == "duplicate_msg_id" and plans_rows(con) == []


def test_reply_prefixed_invalid_plan_is_never_answered(con):
    d = plan()
    d["plans"][0]["bias"] = "neutral"
    mb = FakeMailbox()
    mb.inject(SENDER, f"Re: [SMD/OPTIONS_PLAN/{PID}] {TOKEN}", json.dumps(d))
    poll(con, mb)
    assert rejects(con)[0].startswith("reply_prefix ")
    muse_ops.ensure_schema(con)
    assert muse_ops.pending_feedback(con) == []


@pytest.mark.parametrize("late", [False, True])
def test_options_plan_creates_no_flags_and_no_stock_side_rows(con, late):
    muse_ops.ensure_schema(con)
    inbox.ensure_schema(con)
    ostore.ensure_schema(con)

    def stock_counts():
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        keep = [n for n in names if not n.startswith(("options_", "bridge_", "sqlite_"))]
        return {n: con.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in keep}

    before = stock_counts()
    d = plan()
    d["plans"][0]["underlying"] = "NVDA"
    d["avoid"] = [{"ticker": "TSLA", "why": "no"}]
    mb = FakeMailbox()
    mail(mb, d)
    assert poll(con, mb, now=LATE if late else EARLY) == 1
    now = datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)
    for held in ((), ("NVDA", "TSLA")):
        flags = policy.active_flags(con, now, held)
        assert flags.blocked == {} and flags.no_add == {} and flags.notify == []
    assert stock_counts() == before


def test_plan_arriving_between_ticks_becomes_active(tmp_path, con):
    old = json.loads(_plan(level=605.0, plan_id="gp-20261013-0700"))
    oplan.ingest(con, json.dumps(old), et(7, 0))
    r = Rig(tmp_path, con)
    r.run(et(9, 26))
    mb = FakeMailbox()
    mail(mb, json.loads(_plan(level=601.5, plan_id="gp-20261013-0900")))
    assert poll(con, mb, now=et(9, 0)) == 1
    r.run(et(9, 27))
    st = {s["plan_id"]: s["state"] for s in ostore.setups(con, str(DAY)) if s["source"] == "grok_plan"}
    assert st == {"gp-20261013-0700": "SUPERSEDED", "gp-20261013-0900": "WAITING"}
    assert oplan.active_plan(con, DAY).plan.plan_id == "gp-20261013-0900"
