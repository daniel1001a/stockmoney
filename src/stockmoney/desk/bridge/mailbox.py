"""Mail transport for the Muse bridge (see docs/desk/MUSE_BRIDGE.md).

Only the transport lives here: fetch unread mail, mark it seen, send mail.
Nothing in this module interprets message content. Everything fetched is
untrusted bytes; validation is inbox.py's job.

Stdlib only (imaplib / smtplib / email). IMAP over SSL, SMTP over SSL (465)
or STARTTLS (587); every connection has a timeout.
"""
from __future__ import annotations

import imaplib
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from typing import Callable, Protocol

DEFAULT_TIMEOUT = 30.0
MIN_TOKEN_LENGTH = 16


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class BridgeConfig:
    desk_address: str
    app_password: str = ""
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465  # 465 = implicit SSL, 587 = STARTTLS
    muse_address: str = ""
    shared_token: str = ""

    def __repr__(self) -> str:  # never leak secrets into logs
        return (f"BridgeConfig(desk={self.desk_address!r}, muse={self.muse_address!r}, "
                f"imap={self.imap_host}:{self.imap_port}, smtp={self.smtp_host}:{self.smtp_port})")

    @classmethod
    def from_env(cls, env=None) -> "BridgeConfig":
        env = os.environ if env is None else env
        missing = [k for k in ("DESK_MAIL_ADDRESS", "DESK_MAIL_APP_PASSWORD", "MUSE_ADDRESS", "MUSE_SHARED_TOKEN")
                   if not env.get(k)]
        if missing:
            raise ConfigError("missing environment variables: " + ", ".join(missing))
        token = env["MUSE_SHARED_TOKEN"]
        if len(token) < MIN_TOKEN_LENGTH or re.search(r"\s", token):
            raise ConfigError(f"MUSE_SHARED_TOKEN must be >= {MIN_TOKEN_LENGTH} chars with no whitespace")
        return cls(
            desk_address=env["DESK_MAIL_ADDRESS"].strip(),
            app_password=env["DESK_MAIL_APP_PASSWORD"],
            imap_host=env.get("DESK_MAIL_IMAP") or "imap.gmail.com",
            imap_port=int(env.get("DESK_MAIL_IMAP_PORT") or 993),
            smtp_host=env.get("DESK_MAIL_SMTP") or "smtp.gmail.com",
            smtp_port=int(env.get("DESK_MAIL_SMTP_PORT") or 465),
            muse_address=env["MUSE_ADDRESS"].strip().lower(),
            shared_token=token,
        )


@dataclass(frozen=True)
class RawMail:
    """One fetched message, untrusted. ``body`` is the decoded text/plain part.

    ``oversized`` mails are never downloaded (body is empty). ``error`` is set
    when the transport could not read the mail; inbox.py turns both into
    rejects.
    """
    uid: str
    sender: str  # lower-cased address from the single From header, "" if absent/ambiguous
    subject: str
    body: str
    size: int
    oversized: bool = False
    error: str = ""


class Mailbox(Protocol):
    def fetch_unseen(self, max_bytes: int) -> list[RawMail]: ...
    def mark_seen(self, uid: str) -> None: ...
    def send(self, to: str, subject: str, body: str) -> None: ...


# ---------------------------------------------------------------- parsing helpers

def _sender_of(msg) -> str:
    froms = msg.get_all("From") or []
    if len(froms) != 1:
        return ""  # multiple From headers are a spoofing trick; treat as no sender
    return parseaddr(str(froms[0]))[1].strip().lower()


def _subject_of(msg) -> str:
    return " ".join(str(msg.get("Subject", "")).split())


def _text_body(msg) -> str:
    part = msg.get_body(preferencelist=("plain",))
    if part is None:
        raise ValueError("no text/plain body")
    payload = part.get_payload(decode=True)
    if payload is None:
        raise ValueError("empty body")
    return payload.decode(part.get_content_charset() or "utf-8")


def parse_raw_mail(uid: str, raw: bytes, size: int) -> RawMail:
    """Turn raw RFC822 bytes into a RawMail. Never raises."""
    try:
        msg = message_from_bytes(raw, policy=policy.default)
        sender, subject = _sender_of(msg), _subject_of(msg)
    except Exception as e:  # malformed headers
        return RawMail(uid, "", "", "", size, error=f"unparseable: {type(e).__name__}")
    try:
        body = _text_body(msg)
    except (ValueError, LookupError, UnicodeDecodeError) as e:
        return RawMail(uid, sender, subject, "", size, error=f"body: {type(e).__name__}: {e}"[:200])
    return RawMail(uid, sender, subject, body, size)


def _fetch_meta_and_literal(data) -> tuple[bytes, bytes | None]:
    """Flatten an imaplib FETCH response: (metadata bytes, first literal)."""
    meta, literal = b"", None
    for item in data or []:
        if isinstance(item, tuple):
            meta += item[0] + b" "
            if literal is None:
                literal = item[1]
        elif isinstance(item, (bytes, bytearray)):
            meta += bytes(item) + b" "
    return meta, literal


def _size_from(meta: bytes) -> int | None:
    m = re.search(rb"RFC822\.SIZE\s+(\d+)", meta, re.IGNORECASE)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- real transport

class ImapSmtpMailbox:
    """Gmail-style IMAP (read) + SMTP (send). Connections are opened lazily and
    reused until ``close()``; use as a context manager."""

    def __init__(self, config: BridgeConfig, timeout: float = DEFAULT_TIMEOUT,
                 imap_factory: Callable | None = None, smtp_factory: Callable | None = None):
        self.config = config
        self.timeout = timeout
        self._imap_factory = imap_factory or self._default_imap
        self._smtp_factory = smtp_factory or self._default_smtp
        self._imap = None

    @classmethod
    def from_env(cls, env=None) -> "ImapSmtpMailbox":
        return cls(BridgeConfig.from_env(env))

    # -- factories (the only places that open sockets)
    def _default_imap(self):
        return imaplib.IMAP4_SSL(self.config.imap_host, self.config.imap_port,
                                 ssl_context=ssl.create_default_context(), timeout=self.timeout)

    def _default_smtp(self):
        ctx = ssl.create_default_context()
        if self.config.smtp_port == 587:
            s = smtplib.SMTP(self.config.smtp_host, 587, timeout=self.timeout)
            s.starttls(context=ctx)
            return s
        return smtplib.SMTP_SSL(self.config.smtp_host, self.config.smtp_port, timeout=self.timeout, context=ctx)

    # -- lifecycle
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        imap, self._imap = self._imap, None
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass

    def _conn(self):
        if self._imap is None:
            imap = self._imap_factory()
            try:
                imap.login(self.config.desk_address, self.config.app_password)
                typ, _ = imap.select("INBOX")
                if typ != "OK":
                    raise imaplib.IMAP4.error("cannot select INBOX")
            except Exception:
                try:
                    imap.logout()
                except Exception:
                    pass
                raise
            self._imap = imap
        return self._imap

    # -- Mailbox protocol
    def fetch_unseen(self, max_bytes: int) -> list[RawMail]:
        """Unread mail in INBOX. Uses BODY.PEEK so nothing is marked seen until
        ``mark_seen``. Messages larger than ``max_bytes`` are not downloaded."""
        imap = self._conn()
        typ, data = imap.uid("SEARCH", None, "UNSEEN")
        if typ != "OK":
            raise imaplib.IMAP4.error("UID SEARCH UNSEEN failed")
        uids = [u.decode("ascii") for u in (data[0] or b"").split()]
        out: list[RawMail] = []
        for uid in uids:
            try:
                out.append(self._fetch_one(imap, uid, max_bytes))
            except (imaplib.IMAP4.error, OSError, ValueError) as e:
                out.append(RawMail(uid, "", "", "", 0, error=f"fetch failed: {type(e).__name__}"))
        return out

    def _fetch_one(self, imap, uid: str, max_bytes: int) -> RawMail:
        typ, data = imap.uid("FETCH", uid, "(RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
        if typ != "OK":
            raise imaplib.IMAP4.error("header fetch failed")
        meta, header_bytes = _fetch_meta_and_literal(data)
        size = _size_from(meta)
        if size is None or header_bytes is None:
            raise ValueError("unexpected FETCH response")
        if size > max_bytes:
            hdr = BytesParser(policy=policy.default).parsebytes(header_bytes, headersonly=True)
            return RawMail(uid, _sender_of(hdr), _subject_of(hdr), "", size, oversized=True)
        typ, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK":
            raise imaplib.IMAP4.error("body fetch failed")
        _, raw = _fetch_meta_and_literal(data)
        if raw is None:
            raise ValueError("unexpected FETCH response")
        return parse_raw_mail(uid, bytes(raw), size)

    def mark_seen(self, uid: str) -> None:
        typ, _ = self._conn().uid("STORE", uid, "+FLAGS", "(\\Seen)")
        if typ != "OK":
            raise imaplib.IMAP4.error("STORE +FLAGS \\Seen failed")

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()  # default policy rejects header values containing newlines
        msg["From"] = self.config.desk_address
        msg["To"] = to
        msg["Subject"] = subject
        msg["Auto-Submitted"] = "auto-generated"
        msg.set_content(body, charset="utf-8")
        with self._smtp_factory() as smtp:
            smtp.login(self.config.desk_address, self.config.app_password)
            smtp.send_message(msg)


# ---------------------------------------------------------------- test double

class FakeMailbox:
    """In-memory Mailbox for offline tests. ``inject`` simulates incoming mail;
    ``sent`` records outgoing mail; ``seen`` records marked uids."""

    def __init__(self):
        self._mails: dict[str, RawMail] = {}
        self._next = 1
        self.seen: list[str] = []
        self.sent: list[tuple[str, str, str]] = []

    def inject(self, sender: str, subject: str, body: str, *, size: int | None = None,
               oversized: bool = False, error: str = "") -> str:
        uid = str(self._next)
        self._next += 1
        n = len(body.encode("utf-8", "replace")) if size is None else size
        self._mails[uid] = RawMail(uid, sender.lower(), subject, body, n, oversized=oversized, error=error)
        return uid

    def fetch_unseen(self, max_bytes: int) -> list[RawMail]:
        out = []
        for uid, m in self._mails.items():
            if uid in self.seen:
                continue
            if m.size > max_bytes and not m.oversized:  # mimic the real transport: never hand over big bodies
                m = RawMail(m.uid, m.sender, m.subject, "", m.size, oversized=True)
            out.append(m)
        return out

    def mark_seen(self, uid: str) -> None:
        self.seen.append(uid)

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))
