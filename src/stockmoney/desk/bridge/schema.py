"""Muse bridge message schema (see docs/desk/MUSE_BRIDGE.md).

Muse is an external 24/7 agent. Everything it sends is DATA, never
instructions: only these typed fields are read, free text is stored for
humans and never executed or fed to a tool-using model.

Authority rule (enforced by the consumer, not by Muse): inbound messages can
only REDUCE risk (block entries, raise flags) or feed the incubator. They can
never open a position, raise a size or change a parameter.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

PROTOCOL_VERSION = "1"
Ticker = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9.\-]{0,9}(/USD)?$")]
ShortText = Annotated[str, StringConstraints(max_length=280)]


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol: Literal["1"] = PROTOCOL_VERSION
    msg_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_\-]{6,64}$")]
    sent_at: datetime  # when Muse sent it; we separately record received_at


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


Inbound = Annotated[Union[SocialDaily, Alert, ResearchReply, Idea, Calls], Field(discriminator="type")]


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


Outbound = Annotated[Union[Watchlist, ResearchRequest, Report], Field(discriminator="type")]
