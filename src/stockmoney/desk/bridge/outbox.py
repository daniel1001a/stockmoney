"""Outbound side of the Muse bridge: desk -> Muse.

Subject ``[SMD/<TYPE>/<msg_id>] <token>``, body is exactly one JSON object.
Only the typed ``Outbound`` messages (WATCHLIST / RESEARCH_REQUEST / REPORT)
can be sent. The caller is responsible for not putting anything secret in
them: Muse is an external party.
"""
from __future__ import annotations

import re
import uuid

from pydantic import TypeAdapter

from stockmoney.desk.bridge.mailbox import BridgeConfig, Mailbox
from stockmoney.desk.bridge.schema import Outbound

_OUTBOUND = TypeAdapter(Outbound)
_TOKEN_OK = re.compile(r"^\S{16,256}$")


def new_msg_id(prefix: str = "desk") -> str:
    """A fresh msg_id that satisfies the schema pattern (6-64 of A-Za-z0-9_-)."""
    clean = re.sub(r"[^A-Za-z0-9_\-]", "", prefix)[:20]
    return f"{clean}-{uuid.uuid4().hex[:16]}"


def build_subject(msg_type: str, msg_id: str, token: str) -> str:
    return f"[SMD/{msg_type}/{msg_id}] {token}"


def send(mailbox: Mailbox, msg, config: BridgeConfig | None = None) -> None:
    """Validate ``msg`` against the Outbound schema and mail it to Muse."""
    config = config or BridgeConfig.from_env()
    if not config.muse_address:
        raise ValueError("MUSE_ADDRESS is not configured")
    if not _TOKEN_OK.match(config.shared_token):
        raise ValueError("shared token missing or malformed")
    checked = _OUTBOUND.validate_python(msg, strict=True)  # reject anything not a valid Outbound message
    body = checked.model_dump_json()
    mailbox.send(config.muse_address, build_subject(checked.type, checked.msg_id, config.shared_token), body)
