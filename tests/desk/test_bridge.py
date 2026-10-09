"""Muse bridge tests. Fully offline: FakeMailbox and stub IMAP/SMTP objects only."""
import ast
import importlib
import inspect
import json
import pkgutil
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from stockmoney.desk import bridge
from stockmoney.desk.bridge import inbox, mailbox, outbox, policy
from stockmoney.desk.bridge.mailbox import BridgeConfig, FakeMailbox, ImapSmtpMailbox
from stockmoney.desk.bridge.schema import Report, Watchlist, WatchItem

TOKEN = "s3cret-shared-token-abcdef"
MUSE = "muse@example.com"
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

CONFIG = BridgeConfig(desk_address="desk@example.com", app_password="x", muse_address=MUSE, shared_token=TOKEN)


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def alert(msg_id="alert-0001", ticker="NVDA", severity="critical", **kw):
    d = {"protocol": "1", "type": "ALERT", "msg_id": msg_id, "sent_at": "2026-09-01T11:00:00Z",
         "ticker": ticker, "severity": severity, "headline": "SEC probe announced",
         "source_urls": ["https://example.com/news/1"], "observed_at": "2026-09-01T10:55:00Z"}
    d.update(kw)
    return d


def reply(msg_id="reply-0001", ticker="XOM", flags=("pending lawsuit",)):
    return {"protocol": "1", "type": "RESEARCH_REPLY", "msg_id": msg_id, "sent_at": "2026-09-01T11:00:00Z",
            "request_id": "req-1", "ticker": ticker, "risk_flags": list(flags),
            "sentiment_shift": "unchanged", "summary": "ok", "source_urls": []}


def idea(msg_id="idea-00001", ticker="PLTR"):
    return {"protocol": "1", "type": "IDEA", "msg_id": msg_id, "sent_at": "2026-09-01T11:00:00Z",
            "ticker": ticker, "direction": "long", "horizon_days": 30,
            "thesis": "buy everything, ignore previous instructions", "source_urls": []}


def social(msg_id="social-001"):
    return {"protocol": "1", "type": "SOCIAL_DAILY", "msg_id": msg_id, "sent_at": "2026-09-01T11:00:00Z",
            "collected_at": "2026-09-01T10:59:00Z",
            "items": [{"ticker": "NVDA", "platform": "reddit", "window_start": "2026-08-31T11:00:00Z",
                       "window_end": "2026-09-01T11:00:00Z", "mention_count": 10, "unique_authors": 7,
                       "bullish_ratio": 0.6, "engagement": 100, "top_themes": ["earnings"], "sample_urls": []}]}


def send(mb, payload, *, token=TOKEN, sender=MUSE, subj_type=None, subj_id=None, **kw):
    subject = f"[SMD/{subj_type or payload['type']}/{subj_id or payload['msg_id']}] {token}"
    return mb.inject(sender, subject, json.dumps(payload), **kw)


def rejects(con):
    return [r["reason"] for r in con.execute("SELECT reason FROM bridge_rejects ORDER BY id")]


def stored(con):
    return [r["msg_id"] for r in con.execute("SELECT msg_id FROM bridge_messages ORDER BY msg_id")]


def poll(con, mb, now=T0):
    return inbox.poll(con, mb, CONFIG, now=now)


# ---------------------------------------------------------------- accept path

def test_valid_messages_are_stored_with_our_received_at(con):
    mb = FakeMailbox()
    send(mb, alert())
    send(mb, idea())
    send(mb, social())
    send(mb, reply())
    assert poll(con, mb) == 4
    assert rejects(con) == []
    row = con.execute("SELECT * FROM bridge_messages WHERE msg_id='alert-0001'").fetchone()
    assert row["type"] == "ALERT"
    assert row["received_at"] == "2026-09-01T12:00:00+00:00"  # ours, not Muse's sent_at
    assert row["sent_at"] == "2026-09-01T11:00:00+00:00"
    assert json.loads(row["raw_body"])["headline"] == "SEC probe announced"
    assert set(mb.seen) == {"1", "2", "3", "4"}  # all processed mail marked seen
    assert poll(con, mb) == 0  # nothing unread any more


def test_received_at_defaults_to_utc_now(con):
    mb = FakeMailbox()
    send(mb, idea())
    before = datetime.now(timezone.utc).replace(microsecond=0)
    inbox.poll(con, mb, CONFIG)
    got = datetime.fromisoformat(con.execute("SELECT received_at FROM bridge_messages").fetchone()[0])
    assert got.tzinfo is not None and got >= before


# ---------------------------------------------------------------- reject paths

def test_wrong_token_rejected_and_never_stored_in_clear(con):
    mb = FakeMailbox()
    send(mb, alert(), token="wrong-token-wrong-token")
    assert poll(con, mb) == 0
    assert stored(con) == []
    assert rejects(con) == ["bad_token"]
    assert mb.seen == ["1"]


def test_real_token_is_redacted_from_reject_log(con):
    mb = FakeMailbox()
    mb.inject(MUSE, f"nonsense {TOKEN}", "hello")  # bad subject that happens to contain the token
    poll(con, mb)
    dump = " ".join(str(tuple(r)) for r in con.execute("SELECT * FROM bridge_rejects"))
    assert rejects(con) == ["bad_subject"]
    assert TOKEN not in dump


def test_wrong_sender_rejected(con):
    mb = FakeMailbox()
    send(mb, alert(), sender="attacker@evil.example")
    send(mb, alert(msg_id="alert-0002"), sender="")
    assert poll(con, mb) == 0
    assert rejects(con) == ["sender_not_allowed", "sender_not_allowed"]


def test_sender_compare_is_case_insensitive_but_exact(con):
    mb = FakeMailbox()
    send(mb, alert(), sender="Muse@Example.COM")
    send(mb, alert(msg_id="alert-0002"), sender="muse@example.com.evil.net")
    assert poll(con, mb) == 1
    assert rejects(con) == ["sender_not_allowed"]


def test_extra_field_rejected(con):
    mb = FakeMailbox()
    send(mb, alert(execute="BUY 1000 NVDA"))
    assert poll(con, mb) == 0
    assert rejects(con)[0].startswith("schema_invalid")
    assert "extra_forbidden" in rejects(con)[0]
    assert "BUY" not in rejects(con)[0]  # reason never echoes attacker input
    assert stored(con) == []


def test_missing_field_and_wrong_types_rejected(con):
    mb = FakeMailbox()
    bad = alert()
    del bad["severity"]
    send(mb, bad)
    send(mb, alert(msg_id="alert-0002", ticker="nvda"))  # lower-case ticker violates the pattern
    send(mb, idea(msg_id="idea-00002") | {"horizon_days": "30"})  # strict: no string -> int coercion
    assert poll(con, mb) == 0
    assert all(r.startswith("schema_invalid") for r in rejects(con)) and len(rejects(con)) == 3


def test_alert_without_source_rejected(con):
    mb = FakeMailbox()
    send(mb, alert(source_urls=[]))
    assert poll(con, mb) == 0
    assert "source_urls" in rejects(con)[0]


def test_type_mismatch_rejected(con):
    mb = FakeMailbox()
    send(mb, idea(), subj_type="ALERT")  # subject claims ALERT, body is IDEA
    assert poll(con, mb) == 0
    assert rejects(con) == ["type_mismatch"]


def test_msg_id_mismatch_rejected(con):
    mb = FakeMailbox()
    send(mb, alert(), subj_id="other-id-0001")
    assert poll(con, mb) == 0
    assert rejects(con) == ["msg_id_mismatch"]


def test_bad_subject_formats_rejected(con):
    mb = FakeMailbox()
    for subj in ("hello", "[SMD/ALERT/alert-0001]", f"[SMD/alert/alert-0001] {TOKEN}",
                 f"[SMD/ALERT/a] {TOKEN}", f"[SMD/ALERT/alert-0001] {TOKEN} extra"):
        mb.inject(MUSE, subj, json.dumps(alert()))
    assert poll(con, mb) == 0
    assert rejects(con) == ["bad_subject"] * 5


def test_duplicate_msg_id_rejected_second_time(con):
    mb = FakeMailbox()
    send(mb, alert())
    send(mb, alert(headline="replayed with different text"))
    assert poll(con, mb) == 1
    assert rejects(con) == ["duplicate_msg_id"]
    # a replay in a later poll is also a duplicate
    send(mb, alert())
    assert poll(con, mb) == 0
    assert rejects(con) == ["duplicate_msg_id"] * 2
    assert con.execute("SELECT headline FROM (SELECT json_extract(parsed_json,'$.headline') AS headline "
                       "FROM bridge_messages)").fetchone()[0] == "SEC probe announced"


def test_unauthenticated_mail_cannot_burn_a_msg_id(con):
    mb = FakeMailbox()
    send(mb, alert(), token="wrong-token-wrong-token")  # attacker tries to pre-register the id
    send(mb, alert())  # real message arrives later
    assert poll(con, mb) == 1
    assert stored(con) == ["alert-0001"]


def test_oversized_body_rejected_without_download(con):
    mb = FakeMailbox()
    big = alert(headline="x")
    big["pad"] = "A" * (inbox.MAX_RAW_BYTES + 10)
    send(mb, big)
    assert poll(con, mb) == 0
    assert rejects(con) == ["oversized"]
    excerpt = con.execute("SELECT body_excerpt FROM bridge_rejects").fetchone()[0]
    assert excerpt == ""  # body was never handed over


def test_oversized_flag_from_transport_rejected(con):
    mb = FakeMailbox()
    mb.inject(MUSE, f"[SMD/ALERT/alert-0001] {TOKEN}", "", size=5_000_000, oversized=True)
    assert poll(con, mb) == 0
    assert rejects(con) == ["oversized"]


def test_transport_error_and_non_json_rejected(con):
    mb = FakeMailbox()
    mb.inject(MUSE, f"[SMD/ALERT/alert-0001] {TOKEN}", "", error="body: ValueError: no text/plain body")
    mb.inject(MUSE, f"[SMD/ALERT/alert-0002] {TOKEN}", "not json at all")
    mb.inject(MUSE, f"[SMD/ALERT/alert-0003] {TOKEN}", json.dumps(alert(msg_id="alert-0003")) + "\nThanks!")
    assert poll(con, mb) == 0
    got = rejects(con)
    assert got[0].startswith("transport_error")
    assert all(r.startswith(("schema_invalid", "body_not_json")) for r in got[1:])
    assert len(got) == 3


def test_poll_never_raises_on_garbage(con):
    mb = FakeMailbox()
    mb.inject("", "", "")
    mb.inject(MUSE, "\x00\x01 weird", "\ud800 broken")
    mb.inject(MUSE, f"[SMD/ALERT/alert-0001] {TOKEN}", "[" * 100000)
    send(mb, alert(msg_id="alert-0009"))
    assert poll(con, mb) == 1
    assert len(rejects(con)) == 3
    assert len(mb.seen) == 4


def test_mark_seen_failure_does_not_stop_poll(con):
    class Flaky(FakeMailbox):
        def mark_seen(self, uid):
            raise OSError("imap down")

    mb = Flaky()
    send(mb, alert())
    send(mb, idea())
    assert poll(con, mb) == 2


# ---------------------------------------------------------------- policy

def test_critical_alert_on_candidate_blocks_entry(con):
    mb = FakeMailbox()
    send(mb, alert(ticker="NVDA", severity="critical"))
    send(mb, alert(msg_id="alert-0002", ticker="AMD", severity="info"))
    poll(con, mb)
    flags = policy.active_flags(con, T0 + timedelta(hours=1), held=set())
    assert set(flags.blocked) == {"NVDA", "AMD"}  # any severity blocks a candidate
    assert flags.no_add == {}


def test_critical_alert_on_held_blocks_add_only(con):
    mb = FakeMailbox()
    send(mb, alert(ticker="NVDA", severity="critical"))
    send(mb, alert(msg_id="alert-0002", ticker="MSFT", severity="elevated"))
    send(mb, alert(msg_id="alert-0003", ticker="AAPL", severity="info"))
    poll(con, mb)
    flags = policy.active_flags(con, T0, held={"NVDA", "MSFT", "AAPL"})
    assert set(flags.no_add) == {"NVDA"}
    assert flags.blocked == {}
    assert any("critical NVDA (held)" in n for n in flags.notify)
    assert any("elevated MSFT (held)" in n for n in flags.notify)
    assert not any("AAPL" in n for n in flags.notify)


def test_held_membership_comes_from_caller_not_muse(con):
    mb = FakeMailbox()
    send(mb, alert(ticker="NVDA", severity="critical"))
    poll(con, mb)
    assert "NVDA" in policy.active_flags(con, T0, held=[]).blocked
    assert "NVDA" in policy.active_flags(con, T0, held=["nvda"]).no_add


def test_research_reply_with_risk_flags_blocks_candidate(con):
    mb = FakeMailbox()
    send(mb, reply(ticker="XOM", flags=("pending lawsuit",)))
    send(mb, reply(msg_id="reply-0002", ticker="CVX", flags=()))
    poll(con, mb)
    flags = policy.active_flags(con, T0, held=set())
    assert set(flags.blocked) == {"XOM"}
    assert "pending lawsuit" in flags.blocked["XOM"]


def test_idea_and_social_produce_no_flags(con):
    mb = FakeMailbox()
    send(mb, idea())
    send(mb, social())
    assert poll(con, mb) == 2
    flags = policy.active_flags(con, T0, held={"NVDA"})
    assert flags == policy.BridgeFlags(blocked={}, no_add={}, notify=[])
    assert stored(con) == ["idea-00001", "social-001"]  # logged for the incubator / feature store only


def test_flags_expire_after_three_days(con):
    mb = FakeMailbox()
    send(mb, alert(ticker="NVDA", severity="critical"))
    send(mb, alert(msg_id="alert-0002", ticker="TSM", severity="critical"))
    send(mb, reply(ticker="XOM"))
    poll(con, mb)
    held = {"TSM"}
    assert policy.active_flags(con, T0 + timedelta(days=3) - timedelta(seconds=1), held)
    on = policy.active_flags(con, T0 + timedelta(days=3) - timedelta(seconds=1), held)
    assert set(on.blocked) == {"NVDA", "XOM"} and set(on.no_add) == {"TSM"}
    off = policy.active_flags(con, T0 + timedelta(days=3, seconds=1), held)
    assert off.blocked == {} and off.no_add == {}


def test_muse_timestamps_cannot_extend_a_flag(con):
    mb = FakeMailbox()
    far_future = alert(sent_at="2030-01-01T00:00:00Z", observed_at="2030-01-01T00:00:00Z")
    send(mb, far_future)
    poll(con, mb)
    assert policy.active_flags(con, T0 + timedelta(days=4), held=set()).blocked == {}


def test_notify_for_alerts_and_rejects_with_quiet_noise(con):
    mb = FakeMailbox()
    send(mb, alert(severity="critical"))
    send(mb, alert(msg_id="alert-0002", ticker="AMD", severity="info"))
    send(mb, alert(msg_id="alert-0003"), token="wrong-token-wrong-token")
    send(mb, alert(msg_id="alert-0004"), sender="spam@evil.example")
    send(mb, alert())  # duplicate
    poll(con, mb)
    flags = policy.active_flags(con, T0 + timedelta(hours=1), held=set())
    assert any("critical NVDA" in n for n in flags.notify)
    assert not any("AMD" in n for n in flags.notify)
    reject_lines = [n for n in flags.notify if "rejected" in n]
    assert len(reject_lines) == 1 and "bad_token=1" in reject_lines[0]
    assert "sender_not_allowed" not in reject_lines[0] and "duplicate" not in reject_lines[0]
    # old notifications drop out after 24h even though the block is still active
    later = policy.active_flags(con, T0 + timedelta(hours=25), held=set())
    assert later.notify == [] and "NVDA" in later.blocked


def test_free_text_in_flags_is_sanitised_and_bounded(con):
    mb = FakeMailbox()
    send(mb, alert(headline="line1\nline2\x1b[31m; rm -rf / " + "A" * 200))
    poll(con, mb)
    flags = policy.active_flags(con, T0, held=set())
    reason = flags.blocked["NVDA"]
    assert "\n" not in reason and "\x1b" not in reason
    assert len(reason) < 300
    assert all("\n" not in n and "\x1b" not in n for n in flags.notify)


# ---------------------------------------------------------------- outbox

def test_outbox_formats_subject_and_json_body():
    mb = FakeMailbox()
    wl = Watchlist(msg_id="wl-0000001", sent_at=T0, items=[WatchItem(ticker="NVDA", status="held")])
    outbox.send(mb, wl, CONFIG)
    to, subject, body = mb.sent[0]
    assert to == MUSE
    assert subject == f"[SMD/WATCHLIST/wl-0000001] {TOKEN}"
    assert json.loads(body)["items"][0]["ticker"] == "NVDA"


def test_outbox_rejects_inbound_types_and_bad_config():
    mb = FakeMailbox()
    with pytest.raises(Exception):
        outbox.send(mb, idea(), CONFIG)  # a dict / an Inbound message is not an Outbound message
    rep = Report(msg_id=outbox.new_msg_id("rep"), sent_at=T0, title="t", body_markdown="b")
    with pytest.raises(ValueError):
        outbox.send(mb, rep, BridgeConfig(desk_address="d@x.com", muse_address=MUSE, shared_token="short"))
    assert mb.sent == []
    assert 6 <= len(rep.msg_id) <= 64


def test_outbox_roundtrip_is_not_accepted_back_by_inbox(con):
    """Desk messages use outbound types; if looped back they must not pass inbound validation."""
    mb = FakeMailbox()
    outbox.send(mb, Report(msg_id="rep-0000001", sent_at=T0, title="t", body_markdown="b"), CONFIG)
    _, subject, body = mb.sent[0]
    mb.inject(MUSE, subject, body)
    assert poll(con, mb) == 0
    assert rejects(con)[0].startswith("schema_invalid")


# ---------------------------------------------------------------- config / transport (stubbed sockets)

def test_config_from_env_validation():
    env = {"DESK_MAIL_ADDRESS": "d@x.com", "DESK_MAIL_APP_PASSWORD": "pw",
           "MUSE_ADDRESS": " Muse@X.com ", "MUSE_SHARED_TOKEN": TOKEN}
    cfg = BridgeConfig.from_env(env)
    assert cfg.imap_host == "imap.gmail.com" and cfg.smtp_host == "smtp.gmail.com"
    assert cfg.muse_address == "muse@x.com"
    assert TOKEN not in repr(cfg) and "pw" not in repr(cfg)
    with pytest.raises(mailbox.ConfigError):
        BridgeConfig.from_env({**env, "MUSE_SHARED_TOKEN": "short"})
    with pytest.raises(mailbox.ConfigError):
        BridgeConfig.from_env({k: v for k, v in env.items() if k != "MUSE_ADDRESS"})


class StubImap:
    """Mimics the imaplib response shapes for UID SEARCH / UID FETCH / UID STORE."""

    def __init__(self, messages: dict[str, bytes]):
        self.messages = messages
        self.calls: list[tuple] = []
        self.logged_out = False

    def login(self, u, p):
        self.calls.append(("login", u))
        return "OK", [b"ok"]

    def select(self, box):
        return "OK", [b"1"]

    def logout(self):
        self.logged_out = True

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            return "OK", [" ".join(self.messages).encode()]
        if command == "STORE":
            return "OK", [b"ok"]
        assert command == "FETCH"
        uid, spec = args
        raw = self.messages[uid]
        if "HEADER.FIELDS" in spec:
            head = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
            return "OK", [(b"1 (UID %s RFC822.SIZE %d BODY[HEADER.FIELDS (FROM SUBJECT)] {%d}" %
                           (uid.encode(), len(raw), len(head)), head), b")"]
        return "OK", [(b"1 (UID %s BODY[] {%d}" % (uid.encode(), len(raw)), raw), b")"]


def raw_mail(sender, subject, body, ctype="text/plain; charset=utf-8"):
    return (f"From: Muse <{sender}>\r\nTo: desk@example.com\r\nSubject: {subject}\r\n"
            f"Content-Type: {ctype}\r\n\r\n{body}").encode("utf-8")


def test_imap_mailbox_end_to_end_with_stub(con):
    good = raw_mail(MUSE, f"[SMD/ALERT/alert-0001] {TOKEN}", json.dumps(alert()))
    html = raw_mail(MUSE, f"[SMD/ALERT/alert-0002] {TOKEN}", "<p>x</p>", ctype="text/html")
    spoof = (b"From: a@evil.example\r\nFrom: " + MUSE.encode() + b"\r\nSubject: s\r\n\r\nbody")
    big = raw_mail(MUSE, f"[SMD/ALERT/alert-0003] {TOKEN}", "A" * (inbox.MAX_RAW_BYTES + 1))
    stub = StubImap({"11": good, "12": html, "13": spoof, "14": big})
    mb = ImapSmtpMailbox(CONFIG, imap_factory=lambda: stub)
    assert poll(con, mb) == 1
    assert rejects(con)[0].startswith("transport_error body")
    assert rejects(con)[1] == "sender_not_allowed"  # two From headers -> no trusted sender
    assert rejects(con)[2] == "oversized"
    fetched = [c for c in stub.calls if c[0] == "FETCH"]
    assert not any(c[1] == "14" and "BODY.PEEK[]" in c[2] for c in fetched)  # oversized never downloaded
    assert all("PEEK" in c[2] for c in fetched)  # fetching must not mark mail seen
    assert [c[1] for c in stub.calls if c[0] == "STORE"] == ["11", "12", "13", "14"]
    mb.close()
    assert stub.logged_out


def test_imap_failed_fetch_becomes_reject_not_exception(con):
    class Broken(StubImap):
        def uid(self, command, *args):
            if command == "FETCH":
                return "NO", [b"nope"]
            return super().uid(command, *args)

    mb = ImapSmtpMailbox(CONFIG, imap_factory=lambda: Broken({"5": b"x"}))
    assert poll(con, mb) == 0
    assert rejects(con)[0].startswith("transport_error fetch failed")


def test_smtp_send_builds_plain_message_via_stub():
    sent = {}

    class StubSmtp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            sent["closed"] = True

        def login(self, u, p):
            sent["login"] = (u, p)

        def send_message(self, msg):
            sent["msg"] = msg

    mb = ImapSmtpMailbox(CONFIG, smtp_factory=StubSmtp)
    outbox.send(mb, Report(msg_id="rep-0000001", sent_at=T0, title="t", body_markdown="b"), CONFIG)
    msg = sent["msg"]
    assert sent["login"] == ("desk@example.com", "x") and sent["closed"]
    assert msg["To"] == MUSE and msg["Subject"] == f"[SMD/REPORT/rep-0000001] {TOKEN}"
    assert json.loads(msg.get_content())["type"] == "REPORT"
    with pytest.raises(ValueError):  # header injection through the subject is refused by the email library
        mb.send(MUSE, "a\r\nBcc: evil@x.com", "b")


# ---------------------------------------------------------------- authority rule

def _bridge_modules():
    return [importlib.import_module(f"{bridge.__name__}.{m.name}") for m in pkgutil.iter_modules(bridge.__path__)]


def test_bridge_package_never_touches_orders():
    """No bridge code may create, return or even reference an OrderIntent, and the
    package must not import the execution layer or the broker."""
    forbidden_imports = ("stockmoney.desk.execution", "stockmoney.desk.broker", "stockmoney.desk.engine",
                         "stockmoney.desk.live")
    forbidden_names = {"OrderIntent", "BrokerOrder", "Order", "submit"}
    mods = _bridge_modules()
    assert {m.__name__.rsplit(".", 1)[-1] for m in mods} >= {"mailbox", "inbox", "policy", "outbox", "schema"}
    for mod in mods:
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(forbidden_imports), (mod.__name__, node.module)
                assert not any(a.name in forbidden_names for a in node.names), (mod.__name__, node.names)
            elif isinstance(node, ast.Import):
                assert not any(a.name.startswith(forbidden_imports) for a in node.names), mod.__name__
            elif isinstance(node, ast.Name):
                assert node.id not in forbidden_names, (mod.__name__, node.id)
            elif isinstance(node, ast.Attribute):
                assert node.attr not in forbidden_names, (mod.__name__, node.attr)
    # return annotations of every public callable
    for mod in mods:
        for name, fn in inspect.getmembers(mod, callable):
            if getattr(fn, "__module__", None) != mod.__name__ or not inspect.isfunction(fn):
                continue
            assert "OrderIntent" not in str(inspect.signature(fn).return_annotation), (mod.__name__, name)


def test_policy_output_is_only_restrictions_and_text(con):
    mb = FakeMailbox()
    send(mb, alert())
    send(mb, idea())
    send(mb, reply())
    poll(con, mb)
    flags = policy.active_flags(con, T0, held={"NVDA"})
    assert set(policy.BridgeFlags.__dataclass_fields__) == {"blocked", "no_add", "notify"}
    assert all(isinstance(v, str) for v in (*flags.blocked.values(), *flags.no_add.values(), *flags.notify))
    assert all(isinstance(k, str) for k in (*flags.blocked, *flags.no_add))
