"""Inbound side of the Muse bridge: read mail, validate, store.

Every message is untrusted. A message is accepted only if ALL of these hold:
sender is the allowlisted Muse address, subject is ``[SMD/<TYPE>/<msg_id>] <token>``
with the shared token (constant-time compare), size is under the limit, the
body is exactly one JSON object that validates against the strict ``Inbound``
schema, TYPE and msg_id in the subject equal the body's, and msg_id is new.
OPTIONS_PLAN is the one exception to "Inbound schema": its body is the options
sleeve's OptionsPlan, whose plan_id plays the msg_id role (see _process_plan).

Accepted messages are stored; everything else goes to ``bridge_rejects`` with a
reason. ``poll`` never raises on bad mail. Free text inside messages is stored
for display only; it is never evaluated, formatted into a command or passed to
a model. This module produces no orders and imports nothing from execution.
"""
from __future__ import annotations

import hmac
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from pydantic import TypeAdapter, ValidationError

from stockmoney.desk import notify
from stockmoney.desk.bridge.mailbox import BridgeConfig, Mailbox, RawMail
from stockmoney.desk.bridge.schema import Inbound
from stockmoney.desk.options import plan as oplan
from stockmoney.desk.options.config import OptionsConfig

log = logging.getLogger(__name__)

MAX_RAW_BYTES = 200 * 1024
EXCERPT_CHARS = 2000
# Tolerates reply/forward prefixes an email client may add; the tag itself stays exact.
# Group 1 is the prefix (possibly empty): a prefixed mail is a reply to something the
# desk sent, so one that fails validation is never answered with feedback (see _process).
SUBJECT_RE = re.compile(r"^((?:(?i:re|fw|fwd)\s*:\s*)*)\[SMD/([A-Z_]{1,32})/([A-Za-z0-9_\-]{6,64})\] (\S{1,256})\s*$")
# Types only the desk sends. Mail carrying one is an echo of our own mail (a reply
# or forward whose client stripped the prefix) and is never correctable.
OUTBOUND_ONLY_TYPES = frozenset({"WATCHLIST", "RESEARCH_REQUEST", "REPORT", "FEEDBACK"})
_INBOUND = TypeAdapter(Inbound)

BRIDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_messages (
  msg_id TEXT PRIMARY KEY, type TEXT NOT NULL, sender TEXT, sent_at TEXT, received_at TEXT NOT NULL,
  raw_body TEXT NOT NULL, parsed_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_bridge_messages_received ON bridge_messages (received_at);
CREATE TABLE IF NOT EXISTS bridge_rejects (
  id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL, sender TEXT, subject TEXT,
  msg_id TEXT, reason TEXT NOT NULL, body_excerpt TEXT);
CREATE INDEX IF NOT EXISTS idx_bridge_rejects_received ON bridge_rejects (received_at);
"""


def ensure_schema(con) -> None:
    con.executescript(BRIDGE_SCHEMA)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _clean(text: str, limit: int) -> str:
    """Printable, bounded text for storage of attacker-controlled strings."""
    return "".join(c if c.isprintable() else " " for c in text)[:limit]


def _redact(text: str, token: str) -> str:
    return text.replace(token, "<token>") if token else text


def _token_ok(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _reject(con, received_at: datetime, mail: RawMail, reason: str, config: BridgeConfig, msg_id: str = "") -> None:
    log.warning("bridge reject uid=%s sender=%s reason=%s", mail.uid, _clean(mail.sender, 100), reason)
    con.execute(
        "INSERT INTO bridge_rejects (received_at, sender, subject, msg_id, reason, body_excerpt) VALUES (?,?,?,?,?,?)",
        (_iso(received_at), _clean(mail.sender, 200), _clean(_redact(mail.subject, config.shared_token), 300),
         _clean(msg_id, 64) or None, reason, _clean(_redact(mail.body, config.shared_token), EXCERPT_CHARS)),
    )


def is_echo_subject(subject: str | None) -> bool:
    """True for a subject that quotes the desk's own mail: reply/forward
    prefix or an outbound-only type. Such rejects must never trigger feedback."""
    m = SUBJECT_RE.match(subject or "")
    return bool(m) and (bool(m.group(1)) or m.group(2) in OUTBOUND_ONLY_TYPES)


def _validation_reason(e: ValidationError, prefix: str = "") -> str:
    # location + error type only: error text can echo attacker-controlled input.
    # ``prefix`` (OPTIONS_PLAN) names the model. Our own validators' fixed
    # "value_error" messages are appended for every type, so the sender sees
    # WHICH rule (a GROK_DAILY consistency reject was a bare value_error).
    parts = []
    for err in e.errors()[:5]:
        loc = ".".join(str(p) for p in err["loc"])
        if prefix:
            loc = f"{prefix}.{loc or '(plan)'}"
        part = loc + ":" + err["type"]
        if err["type"] == "value_error":
            part += " (" + re.sub(r"[^A-Za-z0-9 ._:,()<>=/'\\-]", "", str(err.get("msg", "")).removeprefix("Value error, "))[:140] + ")"
        if err["type"] == "json_invalid":
            # the parser message names the problem and a line/column, not the input
            detail = re.sub(r"[^A-Za-z0-9 ._:,()\\-]", "", str((err.get("ctx") or {}).get("error", "")))[:100]
            if detail:
                part += f" ({detail})"
        parts.append(part)
    return "schema_invalid " + "; ".join(parts)


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_json(body: str) -> str:
    """The JSON object inside a mail body. LLM agents often wrap it in a code
    fence or add a greeting/signature; take the fenced block if present, else
    the span from the first '{' to the last '}'. Validation stays strict."""
    m = _FENCE_RE.search(body)
    if m:
        return m.group(1)
    start, end = body.find("{"), body.rfind("}")
    return body[start:end + 1] if 0 <= start < end else body


def repair_string_newlines(text: str) -> str:
    """Undo mail-tool line wrapping inside JSON string values.

    JSON forbids raw control characters in strings, but some mail composers turn
    the space between a Latin word and CJK text into a newline (seen in real
    Muse mail: "AI\n概念股"). Outside strings whitespace is untouched; inside a
    string a raw CR/LF becomes one space, or is dropped when the string is a URL
    (a wrap mid-URL has no space to restore). Nothing else changes, and the
    result still goes through strict validation."""
    out: list[str] = []
    in_str = escaped = False
    start = 0  # index in `out` where the current string's content begins
    for ch in text:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            elif ch in "\r\n":
                body = "".join(out[start:])
                if body.startswith(("http://", "https://")):
                    continue  # inside a URL: drop the break
                if out and out[-1] != " ":
                    out.append(" ")  # collapse CRLF and runs into one space
                continue
        elif ch == '"':
            in_str, start = True, len(out) + 1
        out.append(ch)
    return "".join(out)


def _process(con, mail: RawMail, config: BridgeConfig, received_at: datetime) -> bool:
    """Validate and store one mail. True if accepted. Rejects are recorded."""
    if mail.error:
        _reject(con, received_at, mail, "transport_error " + _clean(mail.error, 150), config)
        return False
    if not mail.sender or mail.sender != config.muse_address.strip().lower():
        _reject(con, received_at, mail, "sender_not_allowed", config)
        return False
    m = SUBJECT_RE.match(mail.subject)
    if not m:
        _reject(con, received_at, mail, "bad_subject", config)
        return False
    prefix, subj_type, subj_id, token = m.groups()
    if not _token_ok(token, config.shared_token):
        _reject(con, received_at, mail, "bad_token", config, subj_id)
        return False
    if subj_type in OUTBOUND_ONLY_TYPES:
        # an echo of the desk's own mail: a correction request would be answered
        # by another echo. Kept under the historical "schema_invalid" code (an
        # outbound type never validates as Inbound); muse_ops never answers it
        # because is_echo_subject() recognises the stored subject.
        _reject(con, received_at, mail, "schema_invalid outbound_only_type", config, subj_id)
        return False
    replied = bool(prefix)

    def fail(reason: str) -> bool:
        # a Re:/Fwd: mail that is not valid as it stands is a reply to something
        # the desk sent; "reply_prefix" is not in muse_ops.FIXES, so no feedback
        _reject(con, received_at, mail, f"reply_prefix {reason}" if replied else reason, config, subj_id)
        return False

    if mail.oversized or mail.size > MAX_RAW_BYTES or len(mail.body.encode("utf-8", "replace")) > MAX_RAW_BYTES:
        return fail("oversized")
    if subj_type == "OPTIONS_PLAN":
        return _process_plan(con, mail, config, received_at, subj_id, fail)
    try:
        msg = _INBOUND.validate_json(repair_string_newlines(extract_json(mail.body)), strict=True)
    except ValidationError as e:
        return fail(_validation_reason(e))
    except ValueError:  # not JSON at all / lone surrogates etc.
        return fail("body_not_json")
    if msg.type != subj_type:
        return fail("type_mismatch")
    if msg.msg_id != subj_id:
        return fail("msg_id_mismatch")
    if con.execute("SELECT 1 FROM bridge_messages WHERE msg_id=?", (msg.msg_id,)).fetchone():
        return fail("duplicate_msg_id")
    con.execute(
        "INSERT INTO bridge_messages (msg_id, type, sender, sent_at, received_at, raw_body, parsed_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (msg.msg_id, msg.type, mail.sender, _iso(msg.sent_at), _iso(received_at), mail.body, msg.model_dump_json()),
    )
    return True


def _process_plan(con, mail: RawMail, config: BridgeConfig, received_at: datetime, subj_id: str, fail) -> bool:
    """OPTIONS_PLAN (docs/desk/MUSE_BRIDGE.md): the body is validated by the
    options sleeve's own strict ``OptionsPlan`` and stored by the SAME
    ``options.plan.ingest`` the CLI uses (received_at = our clock). Its only
    effect is the stored plan the options engine already consumes; policy.py
    reads no OPTIONS_PLAN rows. Returns True only for a newly stored plan; an
    identical resend is a silent no-op."""
    text = repair_string_newlines(extract_json(mail.body)).strip()
    if len(text.encode("utf-8", "replace")) > oplan.MAX_PLAN_BYTES:
        return fail("schema_invalid OPTIONS_PLAN.(plan):plan_larger_than_64000_bytes")
    try:
        p = oplan.OptionsPlan.model_validate_json(text)
    except ValidationError as e:
        return fail(_validation_reason(e, "OPTIONS_PLAN"))
    except (ValueError, RecursionError):
        return fail("body_not_json")
    if p.plan_id != subj_id:
        return fail("msg_id_mismatch")
    if p.plan_id.lower().startswith("grok-"):
        return fail("schema_invalid OPTIONS_PLAN.plan_id:reserved_prefix_grok-")
    other = con.execute("SELECT type FROM bridge_messages WHERE msg_id=?", (p.plan_id,)).fetchone()
    if other is not None and other[0] != "OPTIONS_PLAN":
        return fail("duplicate_msg_id")
    res = oplan.ingest(con, text, received_at, OptionsConfig())
    if res.status == "rejected":  # parse passed above, so: the id belongs to a different accepted plan
        return fail("plan_id_reused")
    con.execute(
        "INSERT OR IGNORE INTO bridge_messages (msg_id, type, sender, sent_at, received_at, raw_body, parsed_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (p.plan_id, "OPTIONS_PLAN", mail.sender, _iso(p.generated_at), _iso(received_at), mail.body,
         p.model_dump_json()),
    )
    if res.status == "duplicate":
        return False
    note = " (shadow_only: 09:20 ET 後收到,當天只進影子帳戶)" if res.shadow_only else ""
    try:
        notify.send("OPTIONS_PLAN 已收到", f"OPTIONS_PLAN 已收到: {len(p.plans)} 個劇本{note}", notify.INFO)
    except Exception:  # a notification failure never undoes a stored plan
        log.exception("bridge: OPTIONS_PLAN notification failed")
    return True


def poll(con, mailbox: Mailbox, config: BridgeConfig | None = None, now: datetime | None = None) -> int:
    """Read unread mail, store accepted messages and rejects. Returns the number
    accepted. Bad mail never raises; each processed mail is committed and then
    marked seen. Transport failures while listing mail propagate to the caller
    (the orchestrator decides what an unreachable mailbox means)."""
    config = config or BridgeConfig.from_env()
    ensure_schema(con)
    accepted = 0
    t0 = time.monotonic()
    for mail in mailbox.fetch_unseen(MAX_RAW_BYTES):
        # set by us, never taken from the message. A frozen job `now` is advanced by the time
        # actually elapsed in this poll, so a plan fetched after the 09:20 ET cutoff is not
        # stamped with the job's earlier start time (shadow_only depends on it).
        received_at = (now + timedelta(seconds=time.monotonic() - t0)) if now else datetime.now(timezone.utc)
        try:
            if _process(con, mail, config, received_at):
                accepted += 1
            con.commit()
        except Exception:  # defensive: one poisoned mail must not stop the batch
            log.exception("bridge: unexpected error processing uid=%s", mail.uid)
            con.rollback()
            try:
                _reject(con, received_at, mail, "internal_error", config)
                con.commit()
            except Exception:
                log.exception("bridge: could not record reject for uid=%s", mail.uid)
                con.rollback()
        try:
            mailbox.mark_seen(mail.uid)
        except Exception:
            log.exception("bridge: could not mark uid=%s seen", mail.uid)
    return accepted
