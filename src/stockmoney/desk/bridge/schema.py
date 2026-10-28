"""Muse bridge message schema (see docs/desk/MUSE_BRIDGE.md). Grok's daily
digest (GROK_DAILY, docs/desk/GROK_PROMPT.md) rides the same channel.

Muse is an external 24/7 agent. Everything it sends is DATA, never
instructions: only these typed fields are read, free text is stored for
humans and never executed or fed to a tool-using model.

Authority rule (enforced by the consumer, not by Muse): inbound messages can
only REDUCE risk (block entries, raise flags) or feed the incubator. They can
never open a position, raise a size or change a parameter.
"""
from __future__ import annotations

import datetime as _dt
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union
from zoneinfo import ZoneInfo

from pydantic import (AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, StringConstraints,
                      UrlConstraints, model_validator)

PROTOCOL_VERSION = "1"
Ticker = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9.\-]{0,9}(/USD)?$")]
ShortText = Annotated[str, StringConstraints(max_length=280)]


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Literal["1"] = PROTOCOL_VERSION
    msg_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_\-]{6,64}$")]
    sent_at: datetime  # when Muse sent it; we separately record received_at

    @model_validator(mode="after")
    def _grok_prefix_is_reserved(self):
        # "grok-" ids belong to GROK_DAILY only, so a message's source is never ambiguous
        if self.msg_id.lower().startswith("grok-") and getattr(self, "type", None) != "GROK_DAILY":
            raise ValueError("msg_id prefix grok- is reserved for GROK_DAILY")
        return self


# ---------------------------------------------------------------- inbound (Muse -> desk)

class Platform(str, Enum):
    threads = "threads"
    instagram = "instagram"
    x = "x"
    reddit = "reddit"
    youtube = "youtube"
    stocktwits = "stocktwits"
    other = "other"


class SocialItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: Ticker
    platform: Platform
    window_start: datetime  # the period the counts cover
    window_end: datetime
    mention_count: int = Field(ge=0)
    unique_authors: int = Field(ge=0)
    bullish_ratio: float | None = Field(default=None, ge=0, le=1)  # of posts with a stance
    engagement: int | None = Field(default=None, ge=0)  # likes+replies+shares
    top_themes: list[ShortText] = Field(default_factory=list, max_length=5)
    sample_urls: list[HttpUrl] = Field(default_factory=list, max_length=5)


class SocialDaily(_Msg):
    type: Literal["SOCIAL_DAILY"] = "SOCIAL_DAILY"
    collected_at: datetime  # when Muse actually observed the data
    items: list[SocialItem] = Field(max_length=500)


class Severity(str, Enum):
    info = "info"
    elevated = "elevated"
    critical = "critical"


class Alert(_Msg):
    """Breaking event on a watched symbol (Muse watches 24/7, incl. crypto)."""
    type: Literal["ALERT"] = "ALERT"
    ticker: Ticker
    severity: Severity
    headline: ShortText
    source_urls: list[HttpUrl] = Field(min_length=1, max_length=5)  # no source, no alert
    observed_at: datetime
    # optional (2026-10-27): Grok's ALERT says whether a primary source confirms
    # it; veto.py counts a grok_verified ALERT as an independent event only when
    # this is true. Muse's alerts omit it and are read exactly as before.
    confirmed: bool | None = None


class ResearchReply(_Msg):
    type: Literal["RESEARCH_REPLY"] = "RESEARCH_REPLY"
    request_id: str
    ticker: Ticker
    risk_flags: list[ShortText] = Field(default_factory=list, max_length=10)
    sentiment_shift: Literal["more_bullish", "more_bearish", "unchanged", "unknown"] = "unknown"
    summary: Annotated[str, StringConstraints(max_length=2000)] = ""
    source_urls: list[HttpUrl] = Field(default_factory=list, max_length=10)


class Idea(_Msg):
    """Muse-sourced trade idea. Goes to the incubator log only."""
    type: Literal["IDEA"] = "IDEA"
    ticker: Ticker
    direction: Literal["long", "short"]
    horizon_days: int = Field(ge=1, le=180)
    thesis: Annotated[str, StringConstraints(max_length=1000)]
    source_urls: list[HttpUrl] = Field(default_factory=list, max_length=10)
    # same closed set as GrokChain.invalidations (defined below); optional, stored with the idea
    invalidations: list["GrokInvalidation"] = Field(default_factory=list, max_length=3)


class Call(BaseModel):
    """One concrete, public call by a social-media account, recorded verbatim
    so we can grade every account's track record later."""
    model_config = ConfigDict(extra="forbid")
    author: Annotated[str, StringConstraints(max_length=80)]  # handle as shown on the platform
    platform: Platform
    followers: int | None = Field(default=None, ge=0)
    ticker: Ticker
    direction: Literal["long", "short", "exit"]
    target_price: float | None = Field(default=None, gt=0)
    horizon_text: ShortText = ""  # what they said, e.g. "this week", "by earnings"
    posted_at: datetime  # when the post went up, as shown on the platform
    post_url: HttpUrl


class Calls(_Msg):
    """Daily log of influencer calls. Stored for grading only; never traded."""
    type: Literal["CALLS"] = "CALLS"
    collected_at: datetime
    calls: list[Call] = Field(max_length=300)


# ---------------------------------------------------------------- GROK_DAILY (Grok -> desk)
# Sent from GROK_ADDRESS with GROK_SHARED_TOKEN (or, legacy, over the Muse
# channel); see docs/desk/GROK_PROMPT.md. Store-only: no policy effect. Limits
# keep a full message under ~123 KB of compact UTF-8 (~167 KiB after base64 +
# headers) against the 200 KiB inbox cap; the
# worst case is computed in tests/desk/test_grok_daily.py.

_ET = ZoneInfo("America/New_York")


def _et_offset(v: datetime) -> datetime:
    if v.utcoffset() != v.astimezone(_ET).utcoffset():
        raise ValueError("offset must be America/New_York at that instant (-04:00 EDT / -05:00 EST)")
    return v


def _no_bare_crypto(v: str) -> str:
    if v in {"BTC", "ETH"}:
        raise ValueError("crypto is written BTC/USD")
    return v


def _text(n: int, min_len: int = 1):
    return Annotated[str, StringConstraints(min_length=min_len, max_length=n)]


EtDatetime = Annotated[AwareDatetime, AfterValidator(_et_offset)]
GrokTicker = Annotated[str, StringConstraints(pattern=r"^(?:[A-Z]{1,5}(?:-[A-Z]{1,2})?|BTC/USD)$"),
                       AfterValidator(_no_bare_crypto)]
GrokUrl = Annotated[HttpUrl, UrlConstraints(max_length=250, allowed_schemes=["https"])]
XHandle = Annotated[str, StringConstraints(pattern=r"^@[A-Za-z0-9_]{1,15}$")]


class GrokEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: GrokTicker
    headline: _text(160)
    severity: Severity
    confirmed: bool
    first_seen: EtDatetime
    urls: list[GrokUrl] = Field(min_length=1, max_length=3)  # no source, no event
    # optional (2026-10-21): "post" = a post states it, "inference" = Grok's reading
    basis: Literal["post", "inference"] | None = None


class GrokImpact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: GrokTicker
    direction: Literal["up", "down"]
    horizon_days: Literal[5, 20, 60]


class GrokCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    what: _text(100)
    date: _dt.date  # "YYYY-MM-DD" only


class GrokPriceInvalidation(BaseModel):
    """Mechanically checkable: any daily close on or before ``by_date`` that is
    below (close_below) or above (close_above) ``level`` invalidates the chain."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["close_below", "close_above"]
    symbol: GrokTicker
    level: float = Field(gt=0, le=1_000_000)
    by_date: _dt.date  # "YYYY-MM-DD" only


class GrokDataInvalidation(BaseModel):
    """Free-text expectation that must show up by ``by_date``. Code cannot check
    it, so the desk judges it "no_data" and never counts it as a falsification."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["data_not_by"]
    description: _text(100)
    by_date: _dt.date


GrokInvalidation = Annotated[Union[GrokPriceInvalidation, GrokDataInvalidation], Field(discriminator="kind")]
Idea.model_rebuild()  # resolves Idea.invalidations now that the condition models exist


class GrokChain(BaseModel):
    """Field names and types are read by learn/chains.py ingest_grok(); keep them in sync.
    ``invalidations`` is optional (older messages omit it); a chain without a
    price condition is stored but marked unfalsifiable and kept out of evidence counts."""
    model_config = ConfigDict(extra="forbid")
    trigger: _text(200)
    urls: list[GrokUrl] = Field(min_length=1, max_length=3)
    steps: list[_text(120)] = Field(min_length=1, max_length=6)
    impacts: list[GrokImpact] = Field(min_length=1, max_length=6)
    checkpoints: list[GrokCheckpoint] = Field(max_length=4)
    invalidations: list[GrokInvalidation] = Field(default_factory=list, max_length=3)
    confidence: float = Field(ge=0, le=1)


class GrokCall(Call):
    """The CALLS ``Call`` structure, narrowed for Grok: X only, ET timestamps,
    the desk's ticker notation, an @handle as author."""
    author: XHandle
    platform: Literal["x"]
    ticker: GrokTicker
    target_price: float | None = Field(default=None, gt=0, le=1_000_000)
    horizon_text: _text(60, 0) = ""
    posted_at: EtDatetime
    post_url: GrokUrl


class GrokAttention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: GrokTicker
    trend: Literal["rising", "falling"]
    why: _text(120)


class GrokLinkCheck(BaseModel):
    """Grok's own link audit for this message (counts only; the desk stores it)."""
    model_config = ConfigDict(extra="forbid")
    total: int = Field(ge=0, le=1000)
    passed: int = Field(ge=0, le=1000)
    removed: int = Field(ge=0, le=1000)


class GrokMarketRisk(BaseModel):
    """Market-level risk read (2026-10-21). Stored and scored against later SPY
    moves (learn/market_risk.py); never read by veto, risk or any order path."""
    model_config = ConfigDict(extra="forbid")
    level: Literal["low", "elevated", "critical"]
    summary: _text(300)
    source_urls: list[GrokUrl] = Field(max_length=5)


class GrokInPlay(BaseModel):
    """A name Grok expects to move today on a dated catalyst (2026-10-27).
    Stored in parsed_json only; read through muse_ops.grok_in_play(). Never
    an order: rules and the risk layer decide, paper first."""
    model_config = ConfigDict(extra="forbid")
    ticker: GrokTicker
    direction: Literal["up", "down"]
    catalyst: _text(160)
    catalyst_kind: Literal["earnings", "guidance", "mna", "regulatory", "analyst", "product", "macro", "legal",
                           "other"]
    catalyst_time: AwareDatetime
    confirmed: bool
    urls: list[GrokUrl] = Field(min_length=1, max_length=3)
    confidence: float = Field(ge=0, le=1)


class GrokDaily(_Msg):
    """Grok's daily pre-market digest. Stored only: produces no policy flags.
    Grok's own timestamps are audit data; the desk's clock is ``received_at``."""
    type: Literal["GROK_DAILY"] = "GROK_DAILY"
    msg_id: Annotated[str, StringConstraints(pattern=r"^grok-[A-Za-z0-9_\-]{1,59}$")]
    sent_at: EtDatetime
    collected_at: EtDatetime  # when Grok ran its search
    events: list[GrokEvent] = Field(max_length=25)
    chains: list[GrokChain] = Field(max_length=6)
    calls: list[GrokCall] = Field(max_length=50)
    # optional since 2026-10-21: the post-count source changed scale; nothing
    # computes from it
    attention: list[GrokAttention] = Field(default_factory=list, max_length=20)
    tracked_accounts: list[XHandle] = Field(max_length=60)
    notes: Annotated[str, StringConstraints(max_length=2000)] = ""
    link_check: GrokLinkCheck | None = None
    market_risk: GrokMarketRisk | None = None
    in_play: list[GrokInPlay] = Field(default_factory=list, max_length=15)  # optional since 2026-10-27

    @model_validator(mode="after")
    def _consistency(self):
        if not (self.sent_at - _dt.timedelta(hours=6) <= self.collected_at <= self.sent_at):
            raise ValueError("collected_at must be within 6h before sent_at")
        if len(set(self.tracked_accounts)) != len(self.tracked_accounts):
            raise ValueError("tracked_accounts has duplicates")
        return self


# ---------------------------------------------------------------- handshake (PING out, PONG in)

Nonce = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_\-]{8,64}$")]


class Capabilities(BaseModel):
    """What the agent says it can do. Stored for the owner; nothing is gated on it."""
    model_config = ConfigDict(extra="forbid")
    can_read_email: bool
    can_send_email: bool
    can_http_get: bool
    can_http_post: bool
    can_schedule: bool
    schedule_times: list[Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]] = Field(
        default_factory=list, max_length=20)
    timezone: _text(64)


class Pong(_Msg):
    """Answer to a desk PING: echoes the PING's nonce."""
    type: Literal["PONG"] = "PONG"
    nonce: Nonce
    capabilities: Capabilities
    notes: Annotated[str, StringConstraints(max_length=2000)] = ""


Inbound = Annotated[Union[SocialDaily, Alert, ResearchReply, Idea, Calls, GrokDaily, Pong],
                    Field(discriminator="type")]


# ---------------------------------------------------------------- outbound (desk -> Muse)

class WatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: Ticker
    status: Literal["held", "candidate", "watch"]
    note: ShortText = ""


class Watchlist(_Msg):
    """Daily: what Muse should monitor for the next 24h."""
    type: Literal["WATCHLIST"] = "WATCHLIST"
    items: list[WatchItem]


class ResearchRequest(_Msg):
    """Ask Muse to check a ticker before/after a signal. Reply within deadline."""
    type: Literal["RESEARCH_REQUEST"] = "RESEARCH_REQUEST"
    ticker: Ticker
    questions: list[ShortText] = Field(max_length=5)
    deadline: datetime


class Report(_Msg):
    """Human-readable daily report, for Muse to relay to the owner."""
    type: Literal["REPORT"] = "REPORT"
    title: ShortText
    body_markdown: Annotated[str, StringConstraints(max_length=20000)]


class Feedback(_Msg):
    """Tells Muse exactly why one of its messages was rejected, so it can
    correct itself. Only sent for mail that passed sender and token checks."""
    type: Literal["FEEDBACK"] = "FEEDBACK"
    about_msg_id: str
    problem: Annotated[str, StringConstraints(max_length=1000)]
    how_to_fix: Annotated[str, StringConstraints(max_length=1000)]


class Ping(_Msg):
    """Handshake probe (scripts/muse_check.py ping). The answer is a PONG with the same nonce."""
    type: Literal["PING"] = "PING"
    nonce: Nonce
    ask: ShortText


class SleevePnl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sleeve: _text(64)
    realized: float


class ClosedTrade(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: _text(32)
    sleeve: _text(64)
    strategy: _text(64)  # playbook for options, strategy for stocks
    entry: float | None = None
    exit: float | None = None
    qty: float
    pnl: float | None = None
    exit_reason: _text(120, 0) = ""


class OpenPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: _text(32)
    sleeve: _text(64)
    qty: float
    avg_price: float | None = None


class CalendarItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    what: _text(120)
    when: _text(40, 0) = ""


class DeskBrief(_Msg):
    """Morning brief to Grok (06:30 ET, NYSE trading days): yesterday's results,
    open positions, today's calendar and a few questions. Information only."""
    type: Literal["DESK_BRIEF"] = "DESK_BRIEF"
    day: _dt.date
    prev_day: _dt.date
    realized_pnl: list[SleevePnl] = Field(default_factory=list, max_length=20)
    closed_trades: list[ClosedTrade] = Field(default_factory=list, max_length=50)
    open_positions: list[OpenPosition] = Field(default_factory=list, max_length=100)
    calendar: list[CalendarItem] = Field(default_factory=list, max_length=30)
    questions: list[ShortText] = Field(default_factory=list, max_length=5)


Outbound = Annotated[Union[Watchlist, ResearchRequest, Report, Feedback, Ping, DeskBrief],
                     Field(discriminator="type")]
