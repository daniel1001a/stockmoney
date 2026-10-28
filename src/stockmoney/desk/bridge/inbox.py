"""Inbound side of the Muse bridge: read mail, validate, store.

Every message is untrusted. A message is accepted only if ALL of these hold:
sender is an allowlisted address, subject is ``[SMD/<TYPE>/<msg_id>] <token>``
with that sender's token (constant-time compare), TYPE is one that sender may
send, size is under the limit, the
body is exactly one JSON object that validates against the strict ``Inbound``
schema, TYPE and msg_id in the subject equal the body's, and msg_id is new.
A resend whose JSON is identical to the stored message is dropped silently
(no reject, no feedback); same msg_id with different content is duplicate_msg_id.
OPTIONS_PLAN is the one exception to "Inbound schema": its body is the options
sleeve's OptionsPlan, whose plan_id plays the msg_id role (see _process_plan).

Senders: MUSE_ADDRESS (Muse types, and Grok types on the legacy channel) and,
when GROK_ADDRESS + GROK_SHARED_TOKEN are both set, GROK_ADDRESS
(GROK_SENDER_TYPES: GROK_DAILY, OPTIONS_PLAN, ALERT, RESEARCH_REPLY, PONG). Each
stored row carries ``source``: ``muse``, ``grok_verified`` (mail over Grok's own
channel, Grok's token, and a Gmail-written dkim=pass for the GROK_ADDRESS
domain) or ``grok_legacy`` (Grok type over the Muse channel, or Grok's channel
without that DKIM pass; the reason is in ``source_reason``). Only grok_verified
may count as an independent source in veto.py / postmortem. msg_id prefix
convention for Grok: ``grok-`` is reserved for GROK_DAILY; ALERT uses
``alert-grok-``, RESEARCH_REPLY ``rr-grok-``, PONG ``pong-grok-`` (convention
only; the source comes from the channel, never from the id).

Leniency (2026-10-27): an information type (INFO_TYPES) that fails strict
validation is pruned instead of rejected when the problem is confined to
optional parts: unknown fields are removed, a bad item is removed from a list
whose minimum still holds, an optional field with a bad value is removed, an
over-long list is cut. A missing required field, or a bad required field,
still rejects. What was removed is stored in ``dropped_json`` and the sender
gets a non-fatal FEEDBACK. OPTIONS_PLAN is never pruned.

Accepted messages are stored; everything else goes to ``bridge_rejects`` with a
reason. ``poll`` never raises on bad mail. Free text inside messages is stored
for display only; it is never evaluated, formatted into a command or passed to
a model. This module produces no orders and imports nothing from execution.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from pydantic import TypeAdapter, ValidationError

from stockmoney.desk import notify
from stockmoney.desk.bridge.mailbox import BridgeConfig, Mailbox, RawMail, dkim_problem
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
OUTBOUND_ONLY_TYPES = frozenset({"WATCHLIST", "RESEARCH_REQUEST", "REPORT", "FEEDBACK", "PING", "DESK_BRIEF"})
_INBOUND = TypeAdapter(Inbound)

SOURCE_MUSE, SOURCE_GROK_VERIFIED, SOURCE_GROK_LEGACY = "muse", "grok_verified", "grok_legacy"
GROK_TYPES = frozenset({"GROK_DAILY", "OPTIONS_PLAN"})  # Grok-only types (source_of fallback)
# everything GROK_ADDRESS may send; ALERT / RESEARCH_REPLY / PONG are shared with Muse
GROK_SENDER_TYPES = GROK_TYPES | {"ALERT", "RESEARCH_REPLY", "PONG"}
# information-only types: stored, never an order input; these may be pruned (see above)
INFO_TYPES = frozenset({"GROK_DAILY", "SOCIAL_DAILY", "CALLS", "ALERT", "RESEARCH_REPLY", "IDEA", "PONG"})

BRIDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_messages (
  msg_id TEXT PRIMARY KEY, type TEXT NOT NULL, sender TEXT, sent_at TEXT, received_at TEXT NOT NULL,
  raw_body TEXT NOT NULL, parsed_json TEXT NOT NULL, source TEXT, source_reason TEXT, dropped_json TEXT);
CREATE INDEX IF NOT EXISTS idx_bridge_messages_received ON bridge_messages (received_at);
CREATE TABLE IF NOT EXISTS bridge_rejects (
  id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL, sender TEXT, subject TEXT,
  msg_id TEXT, reason TEXT NOT NULL, body_excerpt TEXT);
CREATE INDEX IF NOT EXISTS idx_bridge_rejects_received ON bridge_rejects (received_at);
"""


# Columns added after the first release; ensure_schema adds them to old databases.
ADDED_COLUMNS = {"bridge_messages": {"source": "TEXT", "source_reason": "TEXT", "dropped_json": "TEXT"}}


def ensure_schema(con) -> None:
    con.executescript(BRIDGE_SCHEMA)
    for table, cols in ADDED_COLUMNS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols.items():
            if name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def source_of(source: str | None, msg_type: str) -> str:
    """The stored source, or the fallback for rows written before the column
    existed: a Grok type is grok_legacy (it came over the Muse channel), else muse."""
    if source:
        return source
    return SOURCE_GROK_LEGACY if msg_type in GROK_TYPES else SOURCE_MUSE


def has_source_column(con) -> bool:
    return "source" in {r[1] for r in con.execute("PRAGMA table_info(bridge_messages)")}


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _clean(text: str, limit: int) -> str:
    """Printable, bounded text for storage of attacker-controlled strings."""
    return "".join(c if c.isprintable() else " " for c in text)[:limit]


def _redact(text: str, *tokens: str) -> str:
    for token in tokens:
        if token:
            text = text.replace(token, "<token>")
    return text


def _token_ok(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _reject(con, received_at: datetime, mail: RawMail, reason: str, config: BridgeConfig, msg_id: str = "") -> None:
    log.warning("bridge reject uid=%s sender=%s reason=%s", mail.uid, _clean(mail.sender, 100), reason)
    con.execute(
        "INSERT INTO bridge_rejects (received_at, sender, subject, msg_id, reason, body_excerpt) VALUES (?,?,?,?,?,?)",
        (_iso(received_at), _clean(mail.sender, 200),
         _clean(_redact(mail.subject, config.shared_token, config.grok_token), 300),
         _clean(msg_id, 64) or None, reason,
         _clean(_redact(mail.body, config.shared_token, config.grok_token), EXCERPT_CHARS)),
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


def content_hash(body: str) -> str | None:
    """sha256 of the body's JSON object in canonical form (sorted keys, compact),
    so a resend that differs only in greeting, fence or line wrapping matches.
    None when the body holds no parseable JSON."""
    try:
        obj = json.loads(repair_string_newlines(extract_json(body)))
    except (ValueError, RecursionError):
        return None
    canon = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


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
    from_muse = bool(mail.sender) and mail.sender == config.muse_address.strip().lower()
    from_grok = (not from_muse and config.grok_enabled and bool(mail.sender)
                 and mail.sender == config.grok_address.strip().lower())
    if not (from_muse or from_grok):
        _reject(con, received_at, mail, "sender_not_allowed", config)
        return False
    m = SUBJECT_RE.match(mail.subject)
    if not m:
        _reject(con, received_at, mail, "bad_subject", config)
        return False
    prefix, subj_type, subj_id, token = m.groups()
    # each sender is checked against its own token only
    if not _token_ok(token, config.grok_token if from_grok else config.shared_token):
        _reject(con, received_at, mail, "bad_token", config, subj_id)
        return False
    if subj_type in OUTBOUND_ONLY_TYPES:
        # an echo of the desk's own mail: a correction request would be answered
        # by another echo. Kept under the historical "schema_invalid" code (an
        # outbound type never validates as Inbound); muse_ops never answers it
        # because is_echo_subject() recognises the stored subject.
        _reject(con, received_at, mail, "schema_invalid outbound_only_type", config, subj_id)
        return False
    if from_grok and subj_type not in GROK_SENDER_TYPES:
        # not in muse_ops.FIXES: never answered with feedback
        _reject(con, received_at, mail, "type_not_allowed_for_sender", config, subj_id)
        return False
    # grok_verified needs all three: GROK_ADDRESS sender, GROK_SHARED_TOKEN
    # (checked above) and Gmail's own DKIM pass for the GROK_ADDRESS domain.
    # A DKIM failure is not a reject: the mail is kept as grok_legacy (which
    # never votes) and the reason is stored next to it.
    source_reason = None
    if from_grok:
        source_reason = dkim_problem(mail.auth_results, config.grok_address.rsplit("@", 1)[-1]) or None
        source = SOURCE_GROK_LEGACY if source_reason else SOURCE_GROK_VERIFIED
        if source_reason:
            log.warning("bridge: %s from GROK_ADDRESS kept as grok_legacy: %s", subj_id, source_reason)
    else:
        source = SOURCE_GROK_LEGACY if subj_type in GROK_TYPES else SOURCE_MUSE
    replied = bool(prefix)

    def fail(reason: str) -> bool:
        # a Re:/Fwd: mail that is not valid as it stands is a reply to something
        # the desk sent; "reply_prefix" is not in muse_ops.FIXES, so no feedback
        _reject(con, received_at, mail, f"reply_prefix {reason}" if replied else reason, config, subj_id)
        return False

    if mail.oversized or mail.size > MAX_RAW_BYTES or len(mail.body.encode("utf-8", "replace")) > MAX_RAW_BYTES:
        return fail("oversized")
    if subj_type == "OPTIONS_PLAN":
        return _process_plan(con, mail, config, received_at, subj_id, fail, source, source_reason)
    dropped: list[dict] = []
    try:
        msg = _INBOUND.validate_json(repair_string_newlines(extract_json(mail.body)), strict=True)
    except ValidationError as e:
        msg, dropped = lenient_validate(repair_string_newlines(extract_json(mail.body)), subj_type)
        if msg is None:
            return fail(_validation_reason(e))
    except ValueError:  # not JSON at all / lone surrogates etc.
        return fail("body_not_json")
    if msg.type != subj_type:
        return fail("type_mismatch")
    if msg.msg_id != subj_id:
        return fail("msg_id_mismatch")
    prior = con.execute("SELECT type, raw_body, source FROM bridge_messages WHERE msg_id=?",
                        (msg.msg_id,)).fetchone()
    if prior is not None:
        if prior[0] == msg.type and content_hash(prior[1]) == content_hash(mail.body):
            # an identical resend: already received, nothing to store or correct,
            # except that a verified copy upgrades a stored grok_legacy one (the
            # first copy failed DKIM); received_at stays the first arrival and
            # source_reason records when the verification arrived (a NULL source
            # from before the column existed reads as grok_legacy too)
            if source_of(prior[2], prior[0]) == SOURCE_GROK_LEGACY and source == SOURCE_GROK_VERIFIED:
                con.execute("UPDATE bridge_messages SET source=?, source_reason=?, sender=? WHERE msg_id=?",
                            (source, f"verified_by_resend {_iso(received_at)}", mail.sender, msg.msg_id))
                log.info("bridge: verified resend of %s upgraded it from grok_legacy", msg.msg_id)
                return False
            log.info("bridge: identical resend of %s ignored", msg.msg_id)
            return False
        return fail("duplicate_msg_id")
    if dropped and replied:
        # a pruned Re:/Fwd: mail is still stored, but never answered (see fail())
        log.info("bridge: %s accepted with %d drops (reply prefix, no feedback)", msg.msg_id, len(dropped))
    con.execute(
        "INSERT INTO bridge_messages (msg_id, type, sender, sent_at, received_at, raw_body, parsed_json, source, "
        "source_reason, dropped_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (msg.msg_id, msg.type, mail.sender, _iso(msg.sent_at), _iso(received_at), mail.body, msg.model_dump_json(),
         source, source_reason,
         json.dumps({"dropped": dropped, "feedback": not replied}) if dropped else None),
    )
    return True


# ---------------------------------------------------------------- leniency

_MAX_PRUNE_STEPS = 200
_MAX_DROPS_LISTED = 50


def _model_for(msg_type: str):
    from stockmoney.desk.bridge import schema as S
    return {"GROK_DAILY": S.GrokDaily, "SOCIAL_DAILY": S.SocialDaily, "CALLS": S.Calls, "ALERT": S.Alert,
            "RESEARCH_REPLY": S.ResearchReply, "IDEA": S.Idea, "PONG": S.Pong}.get(msg_type)


def _get(obj, path):
    for p in path:
        obj = obj[p]
    return obj


def _still_bad(model, data, kind: str, loc: tuple) -> bool:
    """After a removal: does strict validation now report ``kind`` at ``loc``?"""
    try:
        model.model_validate_json(json.dumps(data), strict=True)
    except ValidationError as e:
        return any(err["type"] == kind and tuple(err["loc"]) == loc for err in e.errors())
    return False


def _try_remove(model, data, loc: list) -> str:
    """Remove the deepest removable thing on ``loc`` (a list element or a dict
    key), trying the deepest first. A removal is undone, and the next level up
    tried, when strict validation then reports the key as missing (it was
    required) or its list as too short. Returns the removed path, "" if none."""
    for depth in range(len(loc), 0, -1):
        path, last = tuple(loc[:depth - 1]), loc[depth - 1]
        try:
            parent = _get(data, path)
        except (KeyError, IndexError, TypeError):
            continue
        if isinstance(parent, list) and isinstance(last, int) and 0 <= last < len(parent):
            item = parent.pop(last)
            if not _still_bad(model, data, "too_short", path):
                return ".".join(map(str, loc[:depth]))
            parent.insert(last, item)
        elif isinstance(parent, dict) and isinstance(last, str) and last in parent:
            if depth == 1 and last in ("type", "msg_id", "protocol", "sent_at"):
                return ""
            value = parent.pop(last)
            if not _still_bad(model, data, "missing", tuple(loc[:depth])):
                return ".".join(map(str, loc[:depth]))
            parent[last] = value
    return ""


def lenient_validate(text: str, subj_type: str):
    """(msg, dropped) after pruning an information-type body, or (None, [])
    when it cannot be made valid without touching a required field. The final
    check is the same strict validation as the normal path."""
    if subj_type not in INFO_TYPES:
        return None, []
    model = _model_for(subj_type)
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None, []
    if not isinstance(data, dict) or data.get("type") != subj_type or model is None:
        return None, []
    dropped: list[dict] = []
    for _ in range(_MAX_PRUNE_STEPS):
        try:
            msg = _INBOUND.validate_json(json.dumps(data), strict=True)
            return msg, dropped
        except ValidationError as e:
            errors = e.errors()
        except ValueError:
            return None, []
        # union errors carry the tag first ("ALERT.ticker"): strip it
        progressed = False
        for err in sorted(errors, key=lambda er: [str(x) for x in er["loc"]], reverse=True):
            loc = tuple(err["loc"])
            if loc and loc[0] == subj_type:
                loc = loc[1:]
            if not loc:
                return None, []  # model-level rule (e.g. a consistency check): not prunable
            if err["type"] == "too_long":
                try:
                    target = _get(data, loc)
                except (KeyError, IndexError, TypeError):
                    return None, []
                limit = (err.get("ctx") or {}).get("max_length")
                if isinstance(target, list) and isinstance(limit, int):
                    dropped.append({"path": ".".join(map(str, loc)), "why": f"too_long: cut to {limit}"})
                    del target[limit:]
                    progressed = True
                    break
            path = _try_remove(model, data, list(loc))
            if path:
                dropped.append({"path": path, "why": err["type"]})
                progressed = True
                break
        if not progressed:
            return None, []
    return None, []


def drop_summary(dropped: list[dict]) -> str:
    """Short, input-free description of what was pruned (paths + error codes)."""
    parts = [f"{d['path']}:{d['why']}" for d in dropped[:_MAX_DROPS_LISTED]]
    more = f"; +{len(dropped) - _MAX_DROPS_LISTED} more" if len(dropped) > _MAX_DROPS_LISTED else ""
    return "; ".join(parts) + more


def _process_plan(con, mail: RawMail, config: BridgeConfig, received_at: datetime, subj_id: str, fail,
                  source: str = SOURCE_GROK_LEGACY, source_reason: str | None = None) -> bool:
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
        "INSERT OR IGNORE INTO bridge_messages (msg_id, type, sender, sent_at, received_at, raw_body, parsed_json, "
        "source, source_reason) VALUES (?,?,?,?,?,?,?,?,?)",
        (p.plan_id, "OPTIONS_PLAN", mail.sender, _iso(p.generated_at), _iso(received_at), mail.body,
         p.model_dump_json(), source, source_reason),
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
