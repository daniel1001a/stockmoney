"""Muse bridge transport over ntfy topics (https://docs.ntfy.sh).

Two topics, one per direction:
  to_desk: Muse POSTs here (curl). The desk polls it.
  to_muse: the desk POSTs here. Muse polls it.

The ntfy ``title`` carries the same subject line the email transport uses
(``[SMD/<TYPE>/<msg_id>] <token>``) and the message body carries the JSON, so
inbox.poll() validates both transports with exactly the same code. Bodies over
4096 bytes arrive as ntfy attachments (``attachment.url``); those are fetched
here, size-capped, so the rest of the bridge never sees the difference.

Authentication differs from email: there is no sender address to check (the
topic name is the only access control), so the shared token in the title is
what authenticates a message. Every RawMail from this transport carries the
configured channel tag as its sender so the existing allowlist check passes
only for traffic that came through the configured topic.

Cursor: ntfy supports ``since=<message id>``; the last processed id is kept in
a small JSON file so a poll never re-reads old traffic. Replays are harmless
anyway because inbox.poll dedupes on msg_id.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import requests

from stockmoney.desk.bridge.mailbox import BridgeConfig, ConfigError, MIN_TOKEN_LENGTH, RawMail

log = logging.getLogger(__name__)

DEFAULT_SERVER = "https://ntfy.sh"
CHANNEL_TAG = "ntfy-channel"
DEFAULT_CURSOR = Path("data/desk/ntfy_cursor.json")
DEFAULT_TIMEOUT = 20.0
FIRST_SINCE = "12h"  # ntfy.sh caches messages ~12h; older traffic does not exist anyway


class NtfyConfig:
    def __init__(self, to_desk: str, to_muse: str, token: str, server: str = DEFAULT_SERVER):
        for name, val in (("to_desk", to_desk), ("to_muse", to_muse)):
            if len(val) < 16 or not val.replace("-", "").replace("_", "").isalnum():
                raise ConfigError(f"{name} topic must be 16+ chars of letters, digits, - and _")
        if to_desk == to_muse:
            raise ConfigError("to_desk and to_muse topics must differ")
        if len(token) < MIN_TOKEN_LENGTH or any(c.isspace() for c in token):
            raise ConfigError(f"MUSE_SHARED_TOKEN must be >= {MIN_TOKEN_LENGTH} chars with no whitespace")
        self.to_desk, self.to_muse, self.token = to_desk, to_muse, token
        self.server = server.rstrip("/")

    def __repr__(self) -> str:  # topics act as passwords; never log them
        return f"NtfyConfig(server={self.server})"

    @classmethod
    def from_env(cls, env=None) -> "NtfyConfig":
        env = os.environ if env is None else env
        missing = [k for k in ("NTFY_BRIDGE_TO_DESK", "NTFY_BRIDGE_TO_MUSE", "MUSE_SHARED_TOKEN") if not env.get(k)]
        if missing:
            raise ConfigError("missing environment variables: " + ", ".join(missing))
        return cls(env["NTFY_BRIDGE_TO_DESK"].strip(), env["NTFY_BRIDGE_TO_MUSE"].strip(),
                   env["MUSE_SHARED_TOKEN"], env.get("NTFY_SERVER") or DEFAULT_SERVER)

    def bridge_config(self) -> BridgeConfig:
        """BridgeConfig for inbox.poll / outbox.send when this transport is used."""
        return BridgeConfig(desk_address="ntfy", muse_address=CHANNEL_TAG, shared_token=self.token)


def configured(env=None) -> bool:
    env = os.environ if env is None else env
    return all(env.get(k) for k in ("NTFY_BRIDGE_TO_DESK", "NTFY_BRIDGE_TO_MUSE", "MUSE_SHARED_TOKEN"))


class NtfyMailbox:
    """Mailbox Protocol implementation over two ntfy topics."""

    def __init__(self, config: NtfyConfig, cursor_path: Path = DEFAULT_CURSOR, http=None,
                 timeout: float = DEFAULT_TIMEOUT):
        self.config = config
        self.cursor_path = Path(cursor_path)
        self.http = http or requests
        self.timeout = timeout

    # ------------------------------------------------------------- cursor
    def _cursor(self) -> str:
        try:
            return json.loads(self.cursor_path.read_text())["since"] or FIRST_SINCE
        except (OSError, ValueError, KeyError):
            return FIRST_SINCE

    def _save_cursor(self, event_id: str) -> None:
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cursor_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"since": event_id}))
        tmp.replace(self.cursor_path)

    # ------------------------------------------------------------ Mailbox
    def fetch_unseen(self, max_bytes: int) -> list[RawMail]:
        url = f"{self.config.server}/{self.config.to_desk}/json"
        r = self.http.get(url, params={"poll": "1", "since": self._cursor()}, timeout=self.timeout)
        r.raise_for_status()  # transport failure: the orchestrator decides what it means
        out: list[RawMail] = []
        for line in r.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict) or ev.get("event") != "message" or not ev.get("id"):
                continue
            out.append(self._to_mail(ev, max_bytes))
        return out

    def _to_mail(self, ev: dict, max_bytes: int) -> RawMail:
        uid = str(ev["id"])
        subject = str(ev.get("title") or "")
        att = ev.get("attachment")
        if isinstance(att, dict):
            size = att.get("size")
            if isinstance(size, int) and size > max_bytes:
                return RawMail(uid, CHANNEL_TAG, subject, "", size, oversized=True)
            try:
                resp = self.http.get(str(att.get("url", "")), timeout=self.timeout, stream=False)
                resp.raise_for_status()
                content = resp.content if hasattr(resp, "content") else resp.text.encode("utf-8")
            except Exception as e:  # one bad attachment must not hide the others
                return RawMail(uid, CHANNEL_TAG, subject, "", 0, error=f"attachment fetch failed: {type(e).__name__}")
            if len(content) > max_bytes:
                return RawMail(uid, CHANNEL_TAG, subject, "", len(content), oversized=True)
            try:
                return RawMail(uid, CHANNEL_TAG, subject, content.decode("utf-8"), len(content))
            except UnicodeDecodeError:
                return RawMail(uid, CHANNEL_TAG, subject, "", len(content), error="attachment is not utf-8")
        body = str(ev.get("message") or "")
        return RawMail(uid, CHANNEL_TAG, subject, body, len(body.encode("utf-8", "replace")))

    def mark_seen(self, uid: str) -> None:
        self._save_cursor(uid)

    def send(self, to: str, subject: str, body: str) -> None:
        """``to`` is ignored: this transport has a single outbound topic."""
        try:
            subject.encode("latin-1")
        except UnicodeEncodeError as e:
            raise ValueError("subject must be latin-1 safe for an HTTP header") from e
        r = self.http.post(
            f"{self.config.server}/{self.config.to_muse}",
            data=body.encode("utf-8"),
            headers={"Title": subject, "Content-Type": "text/plain; charset=utf-8", "Cache": "yes"},
            timeout=self.timeout,
        )
        r.raise_for_status()

    # context manager parity with ImapSmtpMailbox
    def __enter__(self) -> "NtfyMailbox":
        return self

    def __exit__(self, *exc) -> None:
        return None
