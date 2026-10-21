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
    # Grok's own channel (optional). Only active when both are set; otherwise
    # Grok types keep arriving over the Muse channel (tagged grok_legacy).
    grok_address: str = ""
    grok_token: str = ""

    @property
    def grok_enabled(self) -> bool:
        return bool(self.grok_address and self.grok_token)

    def __repr__(self) -> str:  # never leak secrets into logs
        return (f"BridgeConfig(desk={self.desk_address!r}, muse={self.muse_address!r}, "
                f"grok={self.grok_address!r}, "
                f"imap={self.imap_host}:{self.imap_port}, smtp={self.smtp_host}:{self.smtp_port})")

    @classmethod
    def from_env(cls, env=None) -> "BridgeConfig":
        env = os.environ if env is None else env
        missing = [k for k in ("DESK_MAIL_ADDRESS", "DESK_MAIL_APP_PASSWORD", "MUSE_ADDRESS", "MUSE_SHARED_TOKEN")
                   if not env.get(k)]
        if missing:
            raise ConfigError("missing environment variables: " + ", ".join(missing))
        token = env["MUSE_SHARED_TOKEN"]
        _check_token("MUSE_SHARED_TOKEN", token)
        muse_address = env["MUSE_ADDRESS"].strip().lower()
        grok_address = (env.get("GROK_ADDRESS") or "").strip().lower()
        grok_token = env.get("GROK_SHARED_TOKEN") or ""
        if bool(grok_address) != bool(grok_token):
            raise ConfigError("set both GROK_ADDRESS and GROK_SHARED_TOKEN, or neither")
        if grok_token:
            _check_token("GROK_SHARED_TOKEN", grok_token)
        if grok_address and grok_token:
            # Separate identities or the per-sender allowlist means nothing.
            if grok_address == muse_address:
                raise ConfigError("GROK_ADDRESS must differ from MUSE_ADDRESS")
            if grok_token == token:
                raise ConfigError("GROK_SHARED_TOKEN must differ from MUSE_SHARED_TOKEN")
        return cls(
            desk_address=env["DESK_MAIL_ADDRESS"].strip(),
            app_password=env["DESK_MAIL_APP_PASSWORD"],
            imap_host=env.get("DESK_MAIL_IMAP") or "imap.gmail.com",
            imap_port=int(env.get("DESK_MAIL_IMAP_PORT") or 993),
            smtp_host=env.get("DESK_MAIL_SMTP") or "smtp.gmail.com",
            smtp_port=int(env.get("DESK_MAIL_SMTP_PORT") or 465),
            muse_address=muse_address,
            shared_token=token,
            grok_address=grok_address,
            grok_token=grok_token,
        )


def _check_token(name: str, token: str) -> None:
    if len(token) < MIN_TOKEN_LENGTH or re.search(r"\s", token):
        raise ConfigError(f"{name} must be >= {MIN_TOKEN_LENGTH} chars with no whitespace")


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
    # value of the top-most Authentication-Results header, only when Gmail
    # wrote it (authserv-id mx.google.com); "" otherwise. See trusted_auth_results.
    auth_results: str = ""


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


GMAIL_AUTHSERV_ID = "mx.google.com"


def _unfold(value: str) -> str:
    text = " ".join(str(value).split())
    while True:  # RFC 8601 comments, e.g. "(p=REJECT sp=REJECT dis=NONE)"; may contain ';'
        stripped = re.sub(r"\([^()]*\)", " ", text)
        if stripped == text:
            return " ".join(text.split())
        text = stripped


def trusted_auth_results(msg) -> str:
    """The Authentication-Results value Gmail itself added, or "".

    Gmail prepends its header above everything the sender wrote, so only the
    top-most Authentication-Results header is considered, and only when its
    authserv-id is mx.google.com. A same-named header placed in the message
    by the sender sits below Gmail's and is never read; if the top-most one is
    not Gmail's, there is no trusted result at all."""
    headers = msg.get_all("Authentication-Results") or []
    if not headers:
        return ""
    top = _unfold(headers[0])
    authserv = top.split(";", 1)[0].strip().split(" ")[0].lower()
    return top if authserv == GMAIL_AUTHSERV_ID else ""


def dkim_problem(auth_results: str, domain: str) -> str:
    """"" when ``auth_results`` (from trusted_auth_results) holds a dkim=pass
    whose header.d or header.i domain is ``domain`` or a subdomain of it; else
    a short reason. The selector (header.s) is ignored on purpose: SES
    generates it and may rotate it. A pass for another domain (e.g. the SES
    d=amazonses.com signature alone) does not count."""
    domain = domain.strip().lower().rstrip(".")
    if not auth_results:
        return "dkim_header_missing"
    if not domain:
        return "dkim_no_domain"
    passed: set[str] = set()
    for res in auth_results.split(";")[1:]:
        m = re.match(r"\s*dkim\s*=\s*([a-z]+)\b(.*)$", res, re.I)
        if not m or m.group(1).lower() != "pass":
            continue
        for key, val in re.findall(r"\bheader\.([di])\s*=\s*(\S+)", m.group(2), re.I):
            d = val.rsplit("@", 1)[-1] if key.lower() == "i" else val
            passed.add(d.strip().strip('"').lower().rstrip("."))
    if not passed:
        return "dkim_no_pass"
    if any(d == domain or d.endswith("." + domain) for d in passed):
        return ""
    return "dkim_domain_mismatch"


def _subject_of(msg) -> str:
    return " ".join(str(msg.get("Subject", "")).split())


def _text_body(msg) -> str:
    """text/plain if present; otherwise text/html reduced to its text (agents
    sending through Gmail sometimes produce HTML-only mail)."""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        raise ValueError("no text body")
    payload = part.get_payload(decode=True)
    if payload is None:
        raise ValueError("empty body")
    text = payload.decode(part.get_content_charset() or "utf-8")
    if part.get_content_type() == "text/html":
        text = _html_to_text(text)
    return text


def _html_to_text(markup: str) -> str:
    import html
    from html.parser import HTMLParser

    chunks: list[str] = []

    class _P(HTMLParser):
        def handle_data(self, d):
            chunks.append(d)

        def handle_starttag(self, tag, attrs):
            if tag in ("br", "p", "div"):
                chunks.append("\n")

    _P(convert_charrefs=True).feed(markup)
    return html.unescape("".join(chunks))


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
    try:
        auth = trusted_auth_results(msg)
    except Exception:  # a malformed header means "no trusted result", never a crash
        auth = ""
    return RawMail(uid, sender, subject, body, size, auth_results=auth)


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
        """Unread bridge mail in INBOX (from the configured sender, subject tagged
        SMD). Uses BODY.PEEK so nothing is marked seen until ``mark_seen``.
        Messages larger than ``max_bytes`` are not downloaded."""
        imap = self._conn()
        # Server-side filter: only unread mail FROM the configured sender whose
        # subject carries the bridge tag. The mailbox may be a personal inbox;
        # everything else is never downloaded, stored or marked read.
        clean = lambda a: a.replace('"', "").replace("\\", "").strip()
        sender = clean(self.config.muse_address)
        if not sender:
            raise imaplib.IMAP4.error("MUSE_ADDRESS is required to search the mailbox")
        senders = [sender]
        if self.config.grok_enabled and clean(self.config.grok_address) not in ("", sender):
            senders.append(clean(self.config.grok_address))
        found: set[int] = set()
        for s in senders:
            typ, data = imap.uid("SEARCH", None, "UNSEEN", "FROM", f'"{s}"', "SUBJECT", '"SMD"')
            if typ != "OK":
                raise imaplib.IMAP4.error("UID SEARCH failed")
            found.update(int(u) for u in (data[0] or b"").split())
        uids = [str(u) for u in sorted(found)]
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
               oversized: bool = False, error: str = "", auth_results: str = "") -> str:
        """``auth_results`` stands for the trusted Gmail Authentication-Results
        value (what ``trusted_auth_results`` would return for the real mail)."""
        uid = str(self._next)
        self._next += 1
        n = len(body.encode("utf-8", "replace")) if size is None else size
        self._mails[uid] = RawMail(uid, sender.lower(), subject, body, n, oversized=oversized, error=error,
                                   auth_results=auth_results)
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
