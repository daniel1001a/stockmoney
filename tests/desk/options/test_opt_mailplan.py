"""OPTIONS_PLAN delivered by email through the bridge: same strict validator
and the same ingest as the CLI, no policy effect, no stock-side rows. Only a
grok_verified plan (GROK_ADDRESS + GROK_SHARED_TOKEN + DKIM pass) can become the
active plan; Muse and grok_legacy plans are stored for audit only.
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
MUSE = "owner@example.com"
GROK = "pika@mail.grokbot.com"
GTOKEN = "g" * 40
GOOD_AR = ("mx.google.com; dkim=pass header.i=@mail.grokbot.com header.s=k7abc header.b=AbC; "
           "spf=pass smtp.mailfrom=x@amazonses.com; dmarc=pass header.from=mail.grokbot.com")
CONFIG = BridgeConfig(desk_address="desk@example.com", app_password="x", muse_address=MUSE, shared_token=TOKEN,
                      grok_address=GROK, grok_token=GTOKEN)
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
                 ("MUSE_ADDRESS", MUSE), ("MUSE_SHARED_TOKEN", TOKEN),
                 ("GROK_ADDRESS", GROK), ("GROK_SHARED_TOKEN", GTOKEN)):
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(notify, "send", lambda title, msg, prio=notify.INFO, tags=None: out.append((title, msg)) or True)
    return out


def plan(**over) -> dict:
    d = json.loads(_plan(plan_id=PID))
    d.update(over)
    return d


def mail(mb, d: dict | None = None, *, body: str | None = None, subject_id: str | None = None,
         sender: str = GROK, token: str = GTOKEN, **kw):
    if sender == GROK:
        kw.setdefault("auth_results", GOOD_AR)
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
    assert out.sent[0][0] == GROK  # straight to Grok's own channel, no relay prefix
    assert body["type"] == "FEEDBACK" and not body["problem"].startswith("【給 Grok】")
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
    assert out.sent[0][0] == GROK and "plan_id" in body["how_to_fix"]
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
    con.execute("INSERT INTO bridge_messages (msg_id, type, sender, sent_at, received_at, raw_body, parsed_json) "
                "VALUES (?,?,?,?,?,?,?)",
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


def test_plan_stamped_with_elapsed_poll_time_not_frozen_job_now(monkeypatch):
    """A frozen job `now` just before the 09:20 ET cutoff must not make a plan fetched after
    it live: the stamp advances by the time elapsed while polling."""
    from datetime import timedelta
    from stockmoney.desk.bridge import inbox
    seen = []
    real = inbox._process
    monkeypatch.setattr(inbox, "_process", lambda con, mail, config, received_at: (seen.append(received_at), True)[1])
    ticks = iter([100.0, 100.0 + 45.0])
    monkeypatch.setattr(inbox.time, "monotonic", lambda: next(ticks))

    class _MB:
        def fetch_unseen(self, n):
            return [type("M", (), {"uid": "1"})()]
        def mark_seen(self, uid):
            pass

    import sqlite3
    from datetime import datetime
    from zoneinfo import ZoneInfo
    con = sqlite3.connect(":memory:")
    now = datetime(2026, 10, 15, 9, 19, 50, tzinfo=ZoneInfo("America/New_York"))
    cfg = type("C", (), {"shared_token": "t"})()
    inbox.poll(con, _MB(), cfg, now=now)
    assert seen and seen[0] == now + timedelta(seconds=45)
    assert seen[0].astimezone(ZoneInfo("America/New_York")).time() > datetime(2026, 10, 15, 9, 20).time()


@pytest.mark.parametrize("channel", ["muse", "grok_dkim_fail"])
def test_unverified_plan_is_stored_but_never_active_or_live(tmp_path, con, pushes, channel):
    """CLAUDE.md section 0: only grok_verified may propose live candidates. A
    plan from Muse, or from GROK_ADDRESS with DKIM failing, is kept for audit
    (shadow_only, source tagged) but active_plan() never returns it, so no
    grok_plan setup (live or shadow) and no prior-added rule setups exist."""
    mb = FakeMailbox()
    if channel == "muse":
        mail(mb, sender=MUSE, token=TOKEN)
        want = "grok_legacy"  # a Grok type relayed by Muse
    else:
        mail(mb, auth_results=GOOD_AR.replace("dkim=pass", "dkim=fail"))
        want = "grok_legacy"
    assert poll(con, mb) == 1
    row = con.execute("SELECT status, shadow_only, source FROM options_plans").fetchone()
    assert tuple(row) == ("accepted", 1, want)
    assert oplan.active_plan(con, DAY) is None
    assert "未驗證來源" in pushes[-1][1]
    r = Rig(tmp_path, con)
    r.run(et(10, 30))
    assert [s for s in ostore.setups(con, str(DAY)) if s["source"] == "grok_plan"] == []
    assert [o for o in r.orders() if "grok" in (o.get("client_order_id") or "")] == []


def test_verified_plan_supersedes_only_trusted_plans(con):
    """A later unverified plan cannot replace the verified one in force."""
    mb = FakeMailbox()
    mail(mb)
    mail(mb, json.loads(_plan(level=601.5, plan_id="gp-20261013-0810")), sender=MUSE, token=TOKEN)
    assert poll(con, mb) == 2
    assert oplan.active_plan(con, DAY).plan.plan_id == PID


@pytest.mark.parametrize("now, shadow", [(EARLY, 0), (LATE, 1)])
def test_verified_resend_upgrades_an_unverified_plan(con, now, shadow):
    """The same plan first arrives with DKIM failing, then over the verified
    channel: it becomes trusted as of the resend (the shadow cutoff is judged
    on the resend's receipt), and the bridge row is upgraded too."""
    mb = FakeMailbox()
    mail(mb, auth_results=GOOD_AR.replace("dkim=pass", "dkim=fail"))
    assert poll(con, mb, now=et(8, 0)) == 1
    assert oplan.active_plan(con, DAY) is None
    mail(mb)
    assert poll(con, mb, now=now) == 1
    row = con.execute("SELECT source, shadow_only, received_at FROM options_plans WHERE status='accepted'").fetchall()
    assert len(row) == 1 and row[0]["source"] == "grok_verified" and row[0]["shadow_only"] == shadow
    ap = oplan.active_plan(con, DAY)
    assert ap.plan.plan_id == PID and ap.shadow_only == bool(shadow) and ap.received_at > et(8, 0)
    b = con.execute("SELECT source, source_reason FROM bridge_messages WHERE msg_id=?", (PID,)).fetchone()
    assert b["source"] == "grok_verified" and b["source_reason"].startswith("verified_by_resend")
    assert rejects(con) == []


def test_unverified_resend_never_downgrades_or_upgrades(con):
    mb = FakeMailbox()
    mail(mb)
    mail(mb, sender=MUSE, token=TOKEN)
    assert poll(con, mb) == 1
    assert con.execute("SELECT source FROM options_plans").fetchone()[0] == "grok_verified"
    assert oplan.active_plan(con, DAY).plan.plan_id == PID


def test_plan_row_without_source_is_untrusted(con):
    """A row stored before the source column existed (NULL) is not owner."""
    oplan.ingest(con, json.dumps(plan(), ensure_ascii=False), EARLY)
    con.execute("UPDATE options_plans SET source=NULL")
    assert oplan.active_plan(con, DAY) is None
