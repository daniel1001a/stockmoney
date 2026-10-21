"""Grok's own sender channel (GROK_ADDRESS + GROK_SHARED_TOKEN + Gmail DKIM),
source tagging, resend rules, optional GROK_DAILY fields, market_risk scoring
and the morning-report status. Offline: FakeMailbox, a stub IMAP and
in-memory sqlite."""
import copy
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from grok_helpers import SENDER, TOKEN, grok_daily, subject
from pydantic import TypeAdapter, ValidationError

from stockmoney.desk import muse_ops, store, veto
from stockmoney.desk.bridge import inbox
from stockmoney.desk.bridge.mailbox import (BridgeConfig, ConfigError, FakeMailbox, ImapSmtpMailbox, dkim_problem,
                                            parse_raw_mail)
from stockmoney.desk.bridge.schema import Inbound
from stockmoney.desk.data import FIELDS, Bars
from stockmoney.desk.learn import corroboration, market_risk

GROK = "pika@mail.grokbot.com"
GTOKEN = "g" * 40
CONFIG = BridgeConfig(desk_address="desk@example.com", app_password="x", muse_address=SENDER, shared_token=TOKEN,
                      grok_address=GROK, grok_token=GTOKEN)
MUSE_ONLY = BridgeConfig(desk_address="desk@example.com", app_password="x", muse_address=SENDER, shared_token=TOKEN)
NOW = datetime(2026, 10, 12, 11, 40, tzinfo=timezone.utc)  # 07:40 ET
# what Gmail writes for an SES mail signed by the sender's domain and by SES itself
GOOD_AR = ("mx.google.com; dkim=pass header.i=@mail.grokbot.com header.s=k7abc header.b=AbC; "
           "dkim=pass header.i=@amazonses.com header.s=ug7n header.b=XyZ; "
           "spf=pass (google.com: domain of x@amazonses.com designates 1.2.3.4 as permitted sender) "
           "smtp.mailfrom=x@amazonses.com; dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=mail.grokbot.com")
SES_ONLY_AR = "mx.google.com; dkim=pass header.i=@amazonses.com header.s=ug7n header.b=XyZ; spf=pass smtp.mailfrom=x"
INBOUND = TypeAdapter(Inbound)


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(store.SCHEMA)
    muse_ops.ensure_schema(c)
    yield c
    c.close()


def send(box, payload, *, sender=GROK, token=GTOKEN, auth=GOOD_AR, body=None):
    return box.inject(sender, subject(payload, token), body if body is not None else json.dumps(payload),
                      auth_results=auth)


def rows(con):
    return [dict(r) for r in con.execute("SELECT msg_id, type, source, source_reason FROM bridge_messages "
                                         "ORDER BY received_at, msg_id")]


def rejects(con):
    return [r["reason"] for r in con.execute("SELECT reason FROM bridge_rejects ORDER BY id")]


def validate(d):
    return INBOUND.validate_json(json.dumps(d, ensure_ascii=False), strict=True)


# ---------------------------------------------------------------- config

def _env(**kw):
    base = {"DESK_MAIL_ADDRESS": "desk@example.com", "DESK_MAIL_APP_PASSWORD": "pw", "MUSE_ADDRESS": SENDER,
            "MUSE_SHARED_TOKEN": "m" * 40}
    return {**base, **kw}


def test_config_without_grok_vars_is_unchanged():
    c = BridgeConfig.from_env(_env())
    assert not c.grok_enabled and c.grok_address == "" and c.grok_token == ""


def test_config_reads_and_validates_grok_vars():
    c = BridgeConfig.from_env(_env(GROK_ADDRESS=" Pika@Mail.GrokBot.com ", GROK_SHARED_TOKEN=GTOKEN))
    assert c.grok_enabled and c.grok_address == GROK and c.grok_token == GTOKEN
    for bad in ({"GROK_ADDRESS": GROK},  # one without the other
                {"GROK_SHARED_TOKEN": GTOKEN},
                {"GROK_ADDRESS": GROK, "GROK_SHARED_TOKEN": "short"},
                {"GROK_ADDRESS": GROK, "GROK_SHARED_TOKEN": "g" * 20 + " " + "g" * 20},
                {"GROK_ADDRESS": SENDER, "GROK_SHARED_TOKEN": GTOKEN},  # same identity as Muse
                {"GROK_ADDRESS": GROK, "GROK_SHARED_TOKEN": "m" * 40}):
        with pytest.raises(ConfigError):
            BridgeConfig.from_env(_env(**bad))


# ---------------------------------------------------------------- sender allowlist and tokens

def test_verified_needs_grok_sender_grok_token_and_dkim(con):
    box = FakeMailbox()
    send(box, grok_daily())
    assert inbox.poll(con, box, CONFIG, now=NOW) == 1
    assert rows(con) == [{"msg_id": "grok-20261012-0730", "type": "GROK_DAILY", "source": "grok_verified",
                          "source_reason": None}]


def test_grok_sender_with_wrong_or_muse_token_is_rejected(con):
    box = FakeMailbox()
    send(box, grok_daily("grok-20261012-0731"), token="x" * 40)
    send(box, grok_daily("grok-20261012-0732"), token=TOKEN)  # Muse's token is not Grok's
    assert inbox.poll(con, box, CONFIG, now=NOW) == 0
    assert rejects(con) == ["bad_token", "bad_token"] and rows(con) == []
    excerpt = " ".join(r[0] or "" for r in con.execute("SELECT subject FROM bridge_rejects"))
    assert GTOKEN not in excerpt and TOKEN not in excerpt  # both tokens redacted


def test_grok_sender_may_send_only_grok_types(con):
    box = FakeMailbox()
    alert = {"protocol": "1", "msg_id": "alert-0001", "sent_at": "2026-10-12T07:30:00-04:00", "type": "ALERT",
             "ticker": "NVDA", "severity": "critical", "headline": "x", "source_urls": ["https://a.example/1"],
             "observed_at": "2026-10-12T07:00:00-04:00"}
    send(box, alert)
    assert inbox.poll(con, box, CONFIG, now=NOW) == 0
    assert rejects(con) == ["type_not_allowed_for_sender"]
    assert "type_not_allowed_for_sender" not in muse_ops.FIXES  # never answered with feedback


def test_muse_channel_grok_daily_is_legacy_with_or_without_grok_vars(con):
    box = FakeMailbox()
    send(box, grok_daily("grok-20261012-0733"), sender=SENDER, token=TOKEN, auth="")
    assert inbox.poll(con, box, MUSE_ONLY, now=NOW) == 1
    send(box, grok_daily("grok-20261012-0734"), sender=SENDER, token=TOKEN, auth=GOOD_AR)
    assert inbox.poll(con, box, CONFIG, now=NOW) == 1
    assert [r["source"] for r in rows(con)] == ["grok_legacy", "grok_legacy"]


def test_grok_address_is_not_allowed_when_grok_vars_are_unset(con):
    box = FakeMailbox()
    send(box, grok_daily())
    assert inbox.poll(con, box, MUSE_ONLY, now=NOW) == 0
    assert rejects(con) == ["sender_not_allowed"]


def test_muse_alert_is_tagged_muse(con):
    box = FakeMailbox()
    alert = {"protocol": "1", "msg_id": "alert-0002", "sent_at": "2026-10-12T07:30:00-04:00", "type": "ALERT",
             "ticker": "NVDA", "severity": "critical", "headline": "x", "source_urls": ["https://a.example/1"],
             "observed_at": "2026-10-12T07:00:00-04:00"}
    send(box, alert, sender=SENDER, token=TOKEN, auth="")
    assert inbox.poll(con, box, CONFIG, now=NOW) == 1
    assert rows(con)[0]["source"] == "muse"


# ---------------------------------------------------------------- DKIM

@pytest.mark.parametrize("auth, reason", [
    ("", "dkim_header_missing"),
    (SES_ONLY_AR, "dkim_domain_mismatch"),  # the SES signature alone does not count
    ("mx.google.com; dkim=fail header.i=@mail.grokbot.com header.s=k7abc; spf=pass", "dkim_no_pass"),
    ("mx.google.com; dkim=pass header.i=@grokbot.com.evil.example header.s=k", "dkim_domain_mismatch"),
    ("mx.google.com; dkim=pass header.i=@notmail.grokbot.com.example", "dkim_domain_mismatch"),
])
def test_dkim_failure_keeps_the_mail_as_legacy_with_a_reason(con, auth, reason):
    box = FakeMailbox()
    send(box, grok_daily(), auth=auth)
    assert inbox.poll(con, box, CONFIG, now=NOW) == 1  # stored, not dropped
    assert rows(con)[0]["source"] == "grok_legacy" and rows(con)[0]["source_reason"] == reason
    assert rejects(con) == []


def test_dkim_pass_with_wrong_token_is_still_rejected(con):
    box = FakeMailbox()
    send(box, grok_daily(), token="z" * 40, auth=GOOD_AR)
    assert inbox.poll(con, box, CONFIG, now=NOW) == 0
    assert rejects(con) == ["bad_token"]


@pytest.mark.parametrize("auth", [
    "mx.google.com; dkim=pass header.i=@mail.grokbot.com header.s=any-selector-2027",  # selector ignored
    "mx.google.com; dkim=pass header.d=mail.grokbot.com",
    "mx.google.com; dkim=pass header.i=@out.mail.grokbot.com header.s=k",  # subdomain
    "mx.google.com; DKIM = PASS header.i=@MAIL.GROKBOT.COM.",  # case, spacing, trailing dot
    "mx.google.com; dkim=fail header.i=@mail.grokbot.com; dkim=pass header.i=@mail.grokbot.com",
])
def test_dkim_pass_forms_that_count(auth):
    assert dkim_problem(auth, "mail.grokbot.com") == ""


def test_dkim_problem_needs_a_domain():
    assert dkim_problem(GOOD_AR, "") == "dkim_no_domain"


def _raw(*auth_headers: str) -> bytes:
    head = "".join(f"Authentication-Results: {a}\r\n" for a in auth_headers)
    return (head + f"From: Pika <{GROK}>\r\nTo: desk@example.com\r\n"
            f"Subject: [SMD/GROK_DAILY/grok-20261012-0730] {GTOKEN}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n{}").encode()


def test_only_the_top_gmail_authentication_results_is_trusted():
    forged = "mx.google.com; dkim=pass header.i=@mail.grokbot.com header.s=k"
    # Gmail's own header on top says SES only; a forged pass below it is never read
    assert parse_raw_mail("1", _raw(SES_ONLY_AR, forged), 100).auth_results == SES_ONLY_AR
    # top-most header from another server: no trusted result at all, even if a Gmail-looking one follows
    m = parse_raw_mail("1", _raw("evil.example; dkim=pass header.i=@mail.grokbot.com", forged), 100)
    assert m.auth_results == "" and dkim_problem(m.auth_results, "mail.grokbot.com") == "dkim_header_missing"
    assert parse_raw_mail("1", _raw(), 100).auth_results == ""
    # comments (which may hold ';') are stripped and folding is undone
    folded = "mx.google.com;\r\n       dkim=pass (test; mode) header.i=@mail.grokbot.com\r\n header.s=k"
    got = parse_raw_mail("1", _raw(folded), 100).auth_results
    assert got == "mx.google.com; dkim=pass header.i=@mail.grokbot.com header.s=k"
    assert dkim_problem(got, "mail.grokbot.com") == ""


def test_a_forged_header_does_not_make_a_mail_verified(con):
    box = FakeMailbox()
    m = parse_raw_mail("1", _raw(SES_ONLY_AR, "mx.google.com; dkim=pass header.i=@mail.grokbot.com"), 100)
    send(box, grok_daily(), auth=m.auth_results)
    inbox.poll(con, box, CONFIG, now=NOW)
    assert rows(con)[0]["source"] == "grok_legacy" and rows(con)[0]["source_reason"] == "dkim_domain_mismatch"


# ---------------------------------------------------------------- IMAP searches both senders

class _Imap:
    def __init__(self):
        self.searches = []

    def login(self, u, p):
        return "OK", [b"ok"]

    def select(self, box):
        return "OK", [b"1"]

    def logout(self):
        pass

    def uid(self, command, *args):
        assert command == "SEARCH"
        self.searches.append(args)
        return "OK", [b""]


@pytest.mark.parametrize("config, senders", [(MUSE_ONLY, [SENDER]), (CONFIG, [SENDER, GROK])])
def test_imap_search_covers_each_allowed_sender(config, senders):
    stub = _Imap()
    assert ImapSmtpMailbox(config, imap_factory=lambda: stub).fetch_unseen(1000) == []
    assert [s[s.index("FROM") + 1] for s in stub.searches] == [f'"{x}"' for x in senders]
    assert all("UNSEEN" in s and '"SMD"' in s for s in stub.searches)


# ---------------------------------------------------------------- migration

def test_migration_adds_source_columns_and_old_rows_fall_back():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE bridge_messages (msg_id TEXT PRIMARY KEY, type TEXT NOT NULL, sender TEXT, sent_at TEXT, "
              "received_at TEXT NOT NULL, raw_body TEXT, parsed_json TEXT)")
    c.execute("INSERT INTO bridge_messages (msg_id, type, received_at) VALUES ('grok-1', 'GROK_DAILY', 'x')")
    assert not inbox.has_source_column(c)
    inbox.ensure_schema(c)
    inbox.ensure_schema(c)  # idempotent
    assert inbox.has_source_column(c)
    assert {"source", "source_reason"} <= {r[1] for r in c.execute("PRAGMA table_info(bridge_messages)")}
    assert inbox.source_of(c.execute("SELECT source FROM bridge_messages").fetchone()[0], "GROK_DAILY") == "grok_legacy"
    assert inbox.source_of(None, "OPTIONS_PLAN") == "grok_legacy" and inbox.source_of(None, "ALERT") == "muse"


# ---------------------------------------------------------------- votes: only verified Grok with URLs

def _alert(msg_id="alert-0003", url="https://a.example/1"):
    return {"protocol": "1", "msg_id": msg_id, "sent_at": "2026-10-12T07:30:00-04:00", "type": "ALERT",
            "ticker": "NVDA", "severity": "critical", "headline": "x", "source_urls": [url],
            "observed_at": "2026-10-12T07:00:00-04:00"}


@pytest.mark.parametrize("auth, grok_votes", [(GOOD_AR, 1), ("", 0)])
def test_veto_counts_grok_only_when_verified(con, auth, grok_votes):
    veto.ensure_schema(con)
    box = FakeMailbox()
    send(box, _alert(), sender=SENDER, token=TOKEN, auth="")
    send(box, grok_daily(), auth=auth)
    assert inbox.poll(con, box, CONFIG, now=NOW) == 2
    items = veto.collect(con, "NVDA", now=NOW)
    assert len(items.get("grok", [])) == grok_votes and len(items["muse"]) == 1
    assert veto.vote_count(items) == 1 + grok_votes


def test_veto_ignores_muse_channel_grok_and_unsourced_events(con):
    veto.ensure_schema(con)
    box = FakeMailbox()
    send(box, grok_daily("grok-20261012-0735"), sender=SENDER, token=TOKEN, auth=GOOD_AR)  # legacy channel
    assert inbox.poll(con, box, CONFIG, now=NOW) == 1
    assert "grok" not in veto.collect(con, "NVDA", now=NOW)
    # a verified row whose event has no URL does not vote either
    con.execute("UPDATE bridge_messages SET source='grok_verified', parsed_json=json_set(parsed_json, "
                "'$.events[0].urls', json('[]'))")
    assert "grok" not in veto.collect(con, "NVDA", now=NOW)


def test_postmortem_grok_items_keep_only_verified(con):
    box = FakeMailbox()
    send(box, grok_daily("grok-20261012-0736"))
    send(box, grok_daily("grok-20261012-0737"), auth="")
    send(box, grok_daily("grok-20261012-0738"), sender=SENDER, token=TOKEN)
    inbox.poll(con, box, CONFIG, now=NOW)
    con.execute("CREATE TABLE chains (chain_id TEXT, created_at TEXT, source TEXT, trigger_text TEXT, urls_json TEXT, "
                "impacts_json TEXT)")
    events = {i["ref"].split(":")[1] for i in corroboration._grok_items(con, verified_only=True)
              if i["kind"] == "event"}
    assert events == {"grok-20261012-0736"}
    assert len({i["ref"] for i in corroboration._grok_items(con) if i["kind"] == "event"}) == 3


def test_a_legacy_chain_received_in_the_same_second_is_not_counted_verified(con):
    from stockmoney.desk.learn import chains, tables
    tables.ensure_schema(con)
    box = FakeMailbox()
    send(box, grok_daily("grok-20261012-0736"))
    legacy = grok_daily("grok-20261012-0737")
    legacy["chains"][0]["trigger"] = "Unsigned rumor"
    send(box, legacy, auth="")
    inbox.poll(con, box, CONFIG, now=NOW)
    con.execute("UPDATE bridge_messages SET received_at=?", (NOW.isoformat(),))  # same second
    assert chains.ingest_grok(con)["added"] == 2
    kept = {con.execute("SELECT trigger_text FROM chains WHERE chain_id=?", (i["ref"].split(":")[1],)).fetchone()[0]
            for i in corroboration._grok_items(con, verified_only=True) if i["kind"] == "chain"}
    assert kept == {"Export curbs on AI GPUs"}


# ---------------------------------------------------------------- resends

def test_identical_resend_is_silent_and_changed_resend_is_rejected(con):
    box = FakeMailbox()
    d = grok_daily()
    send(box, d)
    assert inbox.poll(con, box, CONFIG, now=NOW) == 1
    # same content, different whitespace and key order: silently ignored
    send(box, d, body=json.dumps(dict(reversed(list(d.items()))), indent=2))
    assert inbox.poll(con, box, CONFIG, now=NOW) == 0
    assert rejects(con) == [] and muse_ops.send_feedback(con, box, NOW) == 0
    changed = copy.deepcopy(d)
    changed["notes"] = "fixed"
    send(box, changed)
    assert inbox.poll(con, box, CONFIG, now=NOW) == 0
    assert rejects(con) == ["duplicate_msg_id"]
    fix = muse_ops.FIXES["duplicate_msg_id"]
    assert "-r2" in fix and "grok-20261021-0730-r2" in fix and "安靜" in fix


def test_identical_verified_resend_upgrades_a_legacy_row_but_never_downgrades(con):
    box = FakeMailbox()
    d = grok_daily()
    send(box, d, auth="")  # DKIM missing: stored as legacy
    assert inbox.poll(con, box, CONFIG, now=NOW) == 1
    assert rows(con)[0]["source"] == "grok_legacy"
    send(box, d)  # the same mail, now DKIM-signed
    assert inbox.poll(con, box, CONFIG, now=NOW) == 0
    r = rows(con)
    assert len(r) == 1 and r[0]["source"] == "grok_verified"
    assert r[0]["source_reason"].startswith("verified_by_resend ")
    assert rejects(con) == []
    send(box, d, auth="")  # a later unsigned copy does not take it back
    assert inbox.poll(con, box, CONFIG, now=NOW) == 0
    assert rows(con)[0]["source"] == "grok_verified" and rejects(con) == []


def test_r2_suffix_msg_id_is_valid_and_accepted(con):
    assert validate(grok_daily("grok-20261021-0730-r2")).msg_id == "grok-20261021-0730-r2"
    box = FakeMailbox()
    send(box, grok_daily("grok-20261012-0730-r2"))
    assert inbox.poll(con, box, CONFIG, now=NOW) == 1


# ---------------------------------------------------------------- optional GROK_DAILY fields

def test_old_format_without_new_fields_is_accepted():
    msg = validate(grok_daily())
    assert msg.link_check is None and msg.market_risk is None and msg.events[0].basis is None


def test_attention_may_be_missing():
    d = grok_daily()
    del d["attention"]
    assert validate(d).attention == []


def test_new_optional_fields_validate():
    d = grok_daily()
    d["events"][0]["basis"] = "post"
    d["link_check"] = {"total": 4, "passed": 3, "removed": 1}
    d["market_risk"] = {"level": "elevated", "summary": "x", "source_urls": ["https://x.com/a/status/9"]}
    msg = validate(d)
    assert msg.events[0].basis == "post" and msg.link_check.removed == 1 and msg.market_risk.level == "elevated"
    d["market_risk"] = {"level": "low", "summary": "x", "source_urls": []}
    assert validate(d).market_risk.source_urls == []


@pytest.mark.parametrize("path, value", [
    ("events.0.basis", "rumor"),
    ("link_check", {"total": 1, "passed": 1}),
    ("link_check", {"total": 1, "passed": 1, "removed": 0, "extra": 1}),
    ("link_check", {"total": 1001, "passed": 1, "removed": 0}),
    ("market_risk", {"level": "high", "summary": "x", "source_urls": []}),
    ("market_risk", {"level": "low", "summary": "", "source_urls": []}),
    ("market_risk", {"level": "low", "summary": "x"}),
    ("market_risk", {"level": "low", "summary": "x" * 301}),
    ("market_risk", {"level": "low", "summary": "x", "source_urls": ["https://a.example/1"] * 6}),
    ("market_risk", {"level": "low", "summary": "x", "source_urls": ["http://a.example/1"]}),
    ("unknown_field", 1),
])
def test_bad_optional_fields_and_unknown_fields_are_rejected(path, value):
    d = grok_daily()
    if path.startswith("events."):
        d["events"][0]["basis"] = value
    else:
        d[path] = value
    with pytest.raises(ValidationError):
        validate(d)


def test_notes_limit_is_2000():
    d = grok_daily()
    d["notes"] = "中" * 2000
    assert len(validate(d).notes) == 2000
    d["notes"] = "中" * 2001
    with pytest.raises(ValidationError):
        validate(d)


def test_readers_tolerate_missing_attention(con):
    from stockmoney.desk.learn import views
    box = FakeMailbox()
    d = grok_daily()
    del d["attention"]
    send(box, d)
    assert inbox.poll(con, box, CONFIG, now=NOW) == 1
    stored = json.loads(con.execute("SELECT parsed_json FROM bridge_messages").fetchone()[0])
    assert stored["attention"] == []
    del stored["attention"]  # a reader must also cope with a row that lacks it entirely
    assert muse_ops.grok_off_list(stored) == []
    assert "attention" not in Path(veto.__file__).read_text()
    assert views is not None


# ---------------------------------------------------------------- market_risk: store and score only

def _bars(days: pd.DatetimeIndex, closes: list[float]) -> Bars:
    close = pd.DataFrame({"SPY": closes}, index=days)
    return Bars(*(close.copy() for _ in FIELDS))


DAYS = pd.bdate_range("2026-10-05", periods=30)  # 10-05 .. 11-13, Mondays-Fridays


@pytest.mark.parametrize("sent_at, base", [
    ("2026-10-12T11:30:00+00:00", "2026-10-09"),  # 07:30 ET Monday: Friday's close
    ("2026-10-12T19:00:00+00:00", "2026-10-09"),  # 15:00 ET: today's close not out yet (UTC hour >= 16)
    ("2026-10-12T20:30:00+00:00", "2026-10-12"),  # 16:30 ET: today's close
    ("2026-10-13T00:00:00+00:00", "2026-10-12"),  # 20:00 ET the evening before
])
def test_market_risk_base_day_uses_et_and_the_close(sent_at, base):
    assert str(market_risk.base_day(DAYS, sent_at).date()) == base


def test_market_risk_score_statuses():
    closes = [100.0] * 5 + [100.0, 98.0, 97.0, 99.0, 101.0] + [102.0] * 20
    bars = _bars(DAYS, closes)
    s = market_risk.score(bars, "2026-10-12T11:30:00+00:00")  # base 10-09 close 100
    assert s["status"] == "done" and s["base_day"] == "2026-10-09"
    assert s["ret_5"] == pytest.approx(0.01) and s["dd_5"] == pytest.approx(-0.03)
    assert s["ret_20"] == pytest.approx(0.02) and s["dd_20"] == pytest.approx(-0.03)
    assert market_risk.score(bars.upto(DAYS[12]), "2026-10-12T11:30:00+00:00")["status"] == "partial"
    p = market_risk.score(bars.upto(DAYS[6]), "2026-10-12T11:30:00+00:00")
    assert p["status"] == "pending" and p["ret_5"] is None


def test_market_risk_refresh_stores_and_is_idempotent_and_veto_ignores_it(con):
    veto.ensure_schema(con)
    box = FakeMailbox()
    d = grok_daily()
    d["events"] = []
    d["market_risk"] = {"level": "critical", "summary": "x", "source_urls": []}
    send(box, d)
    send(box, grok_daily("grok-20261012-0739"))  # no market_risk: no row
    assert inbox.poll(con, box, CONFIG, now=NOW) == 2
    bars = _bars(DAYS, [100.0] * 30)
    assert market_risk.refresh(con, bars, DAYS[-1]) == {"added": 1, "updated": 0, "rows": 1}
    assert market_risk.refresh(con, bars, DAYS[-1]) == {"added": 0, "updated": 0, "rows": 1}
    r = con.execute("SELECT * FROM grok_market_risk").fetchone()
    assert r["level"] == "critical" and r["source"] == "grok_verified" and r["status"] == "done"
    assert "critical" in market_risk.report(con)
    # a critical market_risk is not a vote, a flag or a block
    assert "grok" not in veto.collect(con, "SPY", now=NOW) and not veto.check(con, "SPY", now=NOW).blocked
    src = Path(veto.__file__).parent
    for f in ("veto.py", "bridge/policy.py", "risk.py"):
        p = src / f
        if p.exists():
            assert "market_risk" not in p.read_text(), f


# ---------------------------------------------------------------- morning report status

def _deliver_at(con, now, events=True, collected="07:05"):
    day = now.astimezone(muse_ops.ET).date().isoformat()
    d = grok_daily(f"grok-{day.replace('-', '')}-0730", f"{day}T07:30:00-04:00", f"{day}T{collected}:00-04:00")
    if not events:
        d["events"] = []
    box = FakeMailbox()
    send(box, d)
    assert inbox.poll(con, box, CONFIG, now=now) == 1


def test_grok_today_statuses(con):
    inbox.ensure_schema(con)
    tue = lambda h, m: datetime(2026, 10, 13, h + 4, m, tzinfo=timezone.utc)  # ET wall clock, EDT
    assert muse_ops.grok_today(con, tue(8, 0))["status"] == "pending"
    st = muse_ops.grok_today(con, tue(8, 45))
    assert st["status"] == "missed" and "不代表今天沒有風險" in muse_ops.grok_today_text(st)
    sat = datetime(2026, 10, 17, 13, 0, tzinfo=timezone.utc)
    assert muse_ops.grok_today(con, sat)["status"] == "not_due"
    _deliver_at(con, tue(7, 40), events=False)
    st = muse_ops.grok_today(con, tue(8, 45))
    assert st["status"] == "empty" and st["events"] == 0 and "空白日報" in muse_ops.grok_today_text(st)


def test_grok_today_delivered_and_missed_counts_rejects(con):
    inbox.ensure_schema(con)
    wed = lambda h, m: datetime(2026, 10, 14, h + 4, m, tzinfo=timezone.utc)
    box = FakeMailbox()
    send(box, grok_daily("grok-20261014-0730", "2026-10-14T07:30:00-04:00", "2026-10-14T07:05:00-04:00"),
         token="bad" * 14)
    inbox.poll(con, box, CONFIG, now=wed(7, 40))
    st = muse_ops.grok_today(con, wed(8, 45))
    assert st["status"] == "missed" and st["rejected"] == 1 and "被拒" in muse_ops.grok_today_text(st)
    _deliver_at(con, wed(8, 0))
    st = muse_ops.grok_today(con, wed(8, 45))
    assert st["status"] == "delivered" and st["events"] == 1
