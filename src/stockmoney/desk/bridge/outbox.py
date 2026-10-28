"""Outbound side of the bridge: desk -> Muse, or desk -> Grok.

Subject ``[SMD/<TYPE>/<msg_id>] <token>``, body is exactly one JSON object.
Only the typed ``Outbound`` messages (WATCHLIST / RESEARCH_REQUEST / REPORT /
FEEDBACK / PING / DESK_BRIEF) can be sent. ``channel`` picks the recipient and
its token: "muse" = MUSE_ADDRESS + MUSE_SHARED_TOKEN, "grok" = GROK_ADDRESS +
GROK_SHARED_TOKEN. No other address is ever used. The caller is responsible
for not putting anything secret in them: both are external parties.
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


CHANNELS = ("muse", "grok")


def send(mailbox: Mailbox, msg, config: BridgeConfig | None = None, channel: str = "muse") -> None:
    """Validate ``msg`` against the Outbound schema and mail it to ``channel``."""
    config = config or BridgeConfig.from_env()
    if channel == "grok":
        if not config.grok_enabled:
            raise ValueError("GROK_ADDRESS / GROK_SHARED_TOKEN are not configured")
        to, token = config.grok_address, config.grok_token
    elif channel == "muse":
        if not config.muse_address:
            raise ValueError("MUSE_ADDRESS is not configured")
        to, token = config.muse_address, config.shared_token
    else:
        raise ValueError(f"unknown channel {channel!r}")
    if not _TOKEN_OK.match(token or ""):
        raise ValueError(f"{channel} token missing or malformed")
    checked = _OUTBOUND.validate_python(msg, strict=True)  # reject anything not a valid Outbound message
    body = checked.model_dump_json()
    mailbox.send(to, build_subject(checked.type, checked.msg_id, token), body)
