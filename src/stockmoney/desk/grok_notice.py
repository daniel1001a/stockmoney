"""GROK_NOTICE sending and NOTICE_ACK tracking (GROK_RELIABILITY_SPEC B.3).

A notice is an owner-written JSON file under ``docs/desk/notices/`` with the
GrokNotice fields except ``msg_id`` / ``sent_at`` (msg_id is the notice_id,
sent_at is the send time). Only ``scripts/desk_grok_notice.py`` sends one, and
only when the owner runs it: notices are outward-facing.

bridge_ops keys:
- ``notice:<id>``: the notice went out (value: its sent_at, ISO UTC).
- ``noticeack:<id>``: latest ack status (done / partial / cannot, with
  " unverified" appended when the ack was not grok_verified), or
  ``not_required`` for a notice sent with ack_required false.
- ``noticenoack:<id>``: the owner was pushed once about a missing ack.

An ack is a report only: it never changes rules, parameters or authority.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import TypeAdapter

from stockmoney.desk import muse_ops, notify
from stockmoney.desk.bridge import outbox
from stockmoney.desk.bridge.schema import GrokNotice, Outbound

ACK_WINDOW = timedelta(hours=24)
_OUTBOUND = TypeAdapter(Outbound)


def load(path: str | Path, now: datetime) -> GrokNotice:
    """Validate a notice file; refuses unknown fields and a msg_id/sent_at in the file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("notice file must be a JSON object")
    for field in ("msg_id", "sent_at", "type", "protocol"):
        if field in data:
            raise ValueError(f"notice file must not set {field}; it is filled in at send time")
    return GrokNotice(msg_id=data.get("notice_id", ""), sent_at=now, **data)


def render(notice: GrokNotice) -> tuple[str, str]:
    """The exact subject and body outbox.send mails (GROK_NOTICE subjects carry no token)."""
    checked = _OUTBOUND.validate_python(notice, strict=True)
    return outbox.build_subject(checked.type, checked.msg_id), checked.model_dump_json()


def sent_at(con, notice_id: str) -> str | None:
    muse_ops.ensure_schema(con)
    row = con.execute("SELECT at FROM bridge_ops WHERE key=?", (f"notice:{notice_id}",)).fetchone()
    return row[0] if row else None


def send(con, box, notice: GrokNotice, config=None) -> None:
    """Mail one notice to Grok and record it. A notice id is never sent twice."""
    muse_ops.ensure_schema(con)
    if sent_at(con, notice.notice_id):
        raise ValueError(f"{notice.notice_id} was already sent; write a new notice_id")
    outbox.send(box, notice, config, channel="grok")
    with con:
        muse_ops.log_outbound(con, notice, channel="grok")
        con.execute("INSERT INTO bridge_ops VALUES (?,?)", (f"notice:{notice.notice_id}", muse_ops._iso(notice.sent_at)))
        if not notice.ack_required:
            con.execute("INSERT OR REPLACE INTO bridge_ops VALUES (?,?)",
                        (f"noticeack:{notice.notice_id}", "not_required"))


def record_ack(con, ack, source: str) -> bool:
    """Mark a notice acked. Called by the inbox after storing a NOTICE_ACK.
    The latest ack wins, except that an unverified ack never replaces a
    verified one. An ack for a notice that was never sent is stored by the
    inbox as data but marks nothing."""
    muse_ops.ensure_schema(con)
    if not sent_at(con, ack.notice_id):
        return False
    verified = source == "grok_verified"
    prior = ack_status(con, ack.notice_id)
    if not verified and prior is not None and not _unverified(prior) and prior != "not_required":
        return False
    status = ack.status if verified else f"{ack.status} unverified"
    con.execute("INSERT OR REPLACE INTO bridge_ops VALUES (?,?)", (f"noticeack:{ack.notice_id}", status))
    return True


def _unverified(status: str) -> bool:
    return status.endswith(" unverified")


def ack_status(con, notice_id: str) -> str | None:
    row = con.execute("SELECT at FROM bridge_ops WHERE key=?", (f"noticeack:{notice_id}",)).fetchone()
    return row[0] if row else None


def check_acks(con, now: datetime) -> list[str]:
    """One owner push per notice with no verified ack 24 h after it was sent
    (an unverified ack is not enough: anyone could have sent it)."""
    muse_ops.ensure_schema(con)
    lines = []
    rows = con.execute("SELECT key, at FROM bridge_ops WHERE key LIKE 'notice:%'").fetchall()
    for key, at in rows:
        notice_id = key.split(":", 1)[1]
        status = ack_status(con, notice_id)
        if status is not None and not _unverified(status):
            continue
        try:
            sent = datetime.fromisoformat(at)
        except (TypeError, ValueError):
            continue
        if now - sent < ACK_WINDOW:
            continue
        if con.execute("SELECT 1 FROM bridge_ops WHERE key=?", (f"noticenoack:{notice_id}",)).fetchone():
            continue
        line = f"Grok 未在 24 小時內回覆 NOTICE_ACK:{notice_id}"
        with con:  # record before the push, so a crash cannot push twice
            con.execute("INSERT INTO bridge_ops VALUES (?,?)", (f"noticenoack:{notice_id}", muse_ops._iso(now)))
        notify.send("Grok notice 未確認", line, notify.WARN)
        lines.append(line)
    return lines
