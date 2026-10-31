"""Grok's pre-market OPTIONS_PLAN (protocol "1", schema v1.1;
docs/desk/GROK_SETUP.md and GROK_OPTIONS_PROMPT.md part 2): strict schema
and ingest.

Schema v1.1 (owner, 2026-10-12): Grok cannot see premarket prices or
volume, so it supplies direction and catalyst; the system computes gap,
RVOL and the trigger itself. ``trigger`` / ``invalidation`` are optional:
an item WITHOUT a trigger is a directional prior (engine: adds the
underlying to the watch universe and tags the rule setups on it with
grok_stance), an item WITH a trigger is a grok_plan setup as before and
then also needs an invalidation. key_events must be high-impact releases
between 09:00 and 15:30 ET (each one blocks live entries for 30 minutes).

The plan is DATA, never instructions. Only the typed fields below are read;
free text (thesis, catalyst, summary, notes, why) is stored for humans and is
never executed, interpolated into a command or fed to a tool-using model.
Any unknown field, wrong type or limit violation rejects the WHOLE plan (the
rejected text is still stored with the error so it can be reviewed).

Authority (OPTIONS_PLAN.md section 2): a plan only proposes candidates. It
cannot size an order beyond the risk limits, place an order or bypass the
risk layer. The desk's clock is ``received_at`` (system time at ingest); a
plan received after 09:20 ET on its date is stored but flagged
``shadow_only`` and never sends an order.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import (AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, StringConstraints,
                      UrlConstraints, ValidationError, model_validator)

from stockmoney.desk.options import store as ostore
from stockmoney.desk.options.config import OptionsConfig

ET = ZoneInfo("America/New_York")
MAX_PLAN_BYTES = 64_000


def _et_offset(v: datetime) -> datetime:
    if v.utcoffset() != v.astimezone(ET).utcoffset():
        raise ValueError("offset must be America/New_York at that instant (-04:00 EDT / -05:00 EST)")
    return v


def _hhmm(v: str) -> str:
    h, m = int(v[:2]), int(v[3:])
    if h > 23 or m > 59:
        raise ValueError("time must be HH:MM")
    return v


def _text(n: int, min_len: int = 0):
    return Annotated[str, StringConstraints(min_length=min_len, max_length=n)]


EtDatetime = Annotated[AwareDatetime, AfterValidator(_et_offset)]
Ticker = Annotated[str, StringConstraints(pattern=r"^[A-Z]{1,5}(?:\.[A-Z])?$")]
# [0-9], not \d: \d also matches non-ASCII digits (e.g. Arabic-Indic), which int() parses
HHMM = Annotated[str, StringConstraints(pattern=r"^[0-9]{2}:[0-9]{2}$"), AfterValidator(_hhmm)]
PlanUrl = Annotated[HttpUrl, UrlConstraints(max_length=250, allowed_schemes=["https"])]
Price = Annotated[float, Field(gt=0, le=1_000_000)]

WINDOW_START, WINDOW_END = "09:35", "11:30"  # no entry of any kind before 09:35 ET


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


KEY_EVENT_START, KEY_EVENT_END = "09:00", "15:30"  # a premarket release must not block entries


class KeyEvent(_Strict):
    time: HHMM
    what: _text(100, 1)

    @model_validator(mode="after")
    def _window(self):
        if not (KEY_EVENT_START <= self.time <= KEY_EVENT_END):
            raise ValueError(f"key_events time must be within {KEY_EVENT_START} to {KEY_EVENT_END} ET")
        return self


class MarketContext(_Strict):
    summary: _text(300)
    risk_tone: _text(20)
    key_events: list[KeyEvent] = Field(max_length=8)


class Trigger(_Strict):
    kind: Literal["break_above", "break_below", "reclaim_above", "lose_below", "opening_range_break"]
    level: Price | None
    confirm_closes: int = Field(ge=1, le=3)
    earliest: HHMM
    latest: HHMM

    @model_validator(mode="after")
    def _check(self):
        if self.kind == "opening_range_break":
            if self.level is not None:
                raise ValueError("opening_range_break takes level null")
        elif self.level is None:
            raise ValueError(f"{self.kind} needs a level")
        if not (WINDOW_START <= self.earliest <= self.latest <= WINDOW_END):
            raise ValueError(f"earliest <= latest, both within {WINDOW_START} to {WINDOW_END} ET")
        return self


class Invalidation(_Strict):
    kind: Literal["below", "above"]
    underlying_level: Price


class PlanItem(_Strict):
    underlying: Ticker
    bias: Literal["long", "short"]
    playbook: Literal["gap_go", "gap_fade", "orb", "vwap_reclaim", "momentum", "catalyst", "other"]
    thesis: _text(300, 1)
    catalyst: _text(150)
    confidence: float = Field(ge=0, le=1)  # stored for scoring only, never used for sizing
    evidence_urls: list[PlanUrl] = Field(max_length=3)
    # optional (schema v1.1): no trigger = a directional prior (see the module docstring)
    trigger: Trigger | None = None
    invalidation: Invalidation | None = None
    target_underlying_pct: float | None = Field(default=None, ge=0.2, le=10)
    max_hold_minutes: int | None = Field(default=None, ge=5, le=180)  # None: the config time stop
    preferred_dte: Literal["0dte", "1-3dte", "any"] = "any"
    min_rvol: float | None = Field(default=None, ge=1, le=10)  # raises the live RVOL floor; shadow unaffected
    regime: Literal["trend", "range", "any"] | None = None     # stored and reported only
    daily_ref: _text(120) | None = None                       # GROK_DAILY event / attention it belongs to

    @property
    def directional(self) -> bool:
        """No trigger: a directional prior, not a setup of its own."""
        return self.trigger is None

    @model_validator(mode="after")
    def _consistent(self):
        if self.trigger is not None and self.invalidation is None:
            raise ValueError("an item with a trigger needs an invalidation (the exit of its own setup)")
        if self.invalidation is None:
            return self
        # a long (call) is proven wrong BELOW, a short (put) ABOVE; anything
        # else is an internally contradictory plan and rejected
        want = "below" if self.bias == "long" else "above"
        if self.invalidation.kind != want:
            raise ValueError(f"bias {self.bias} needs invalidation.kind {want}")
        lvl = self.trigger.level if self.trigger is not None else None
        inv = self.invalidation.underlying_level
        if lvl is not None and ((self.bias == "long" and inv >= lvl) or (self.bias == "short" and inv <= lvl)):
            raise ValueError("invalidation level must be on the losing side of the trigger level")
        return self


class AvoidItem(_Strict):
    ticker: Ticker
    why: _text(200)


class OptionsPlan(_Strict):
    protocol: Literal["1"]
    type: Literal["OPTIONS_PLAN"]
    plan_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_\-]{6,64}$")]
    for_date: _dt.date
    generated_at: EtDatetime
    market_context: MarketContext
    plans: list[PlanItem] = Field(max_length=12)
    avoid: list[AvoidItem] = Field(max_length=10)
    notes: _text(500)

    @model_validator(mode="after")
    def _dates(self):
        gen = self.generated_at.astimezone(ET)
        open_ = datetime.combine(self.for_date, _dt.time(9, 30), ET)
        if not (open_ - _dt.timedelta(days=3) <= gen <= open_):
            raise ValueError("generated_at must be within 3 days before the open of for_date")
        avoid = {a.ticker for a in self.avoid}
        clash = sorted({p.underlying for p in self.plans} & avoid)
        if clash:
            raise ValueError(f"tickers both planned and avoided: {clash}")
        return self


def parse(text: str) -> OptionsPlan:
    """Strict parse of one JSON object. Raises ValueError with the reason."""
    try:
        if len(text.encode("utf-8")) > MAX_PLAN_BYTES:
            raise ValueError(f"plan larger than {MAX_PLAN_BYTES} bytes")
        return OptionsPlan.model_validate_json(text.strip())
    except ValidationError as e:
        raise ValueError(_short(e)) from None
    except (RecursionError, UnicodeError) as e:  # absurd nesting, a lone surrogate: a reason, never a crash
        raise ValueError(f"unparseable plan text: {type(e).__name__}") from None


def _short(e: ValidationError) -> str:
    parts = []
    for err in e.errors()[:8]:
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg')}")
    return "; ".join(parts)


@dataclass(frozen=True)
class IngestResult:
    status: str  # accepted | rejected | duplicate | upgraded (a trusted resend of an untrusted plan)
    plan_id: str
    for_date: str
    shadow_only: bool
    error: str = ""


def is_shadow_only(for_date: _dt.date, received_at: datetime, cfg: OptionsConfig) -> bool:
    cutoff = datetime.combine(for_date, _dt.time(*cfg.plan_cutoff), ET)
    return received_at.astimezone(ET) > cutoff


# plan sources active_plan() reads; anything else (Muse, grok_legacy) is stored for audit only
TRUSTED_SOURCES = ("owner", "grok_verified")


def ingest(con, text: str, received_at: datetime, cfg: OptionsConfig | None = None,
           source: str = "owner") -> IngestResult:
    """Validate and store. Idempotent: the same text twice is reported as a
    duplicate of the first; a DIFFERENT plan reusing a plan_id is rejected.
    A plan from an untrusted ``source`` is stored shadow_only and is never
    returned by active_plan() (CLAUDE.md section 0: only grok_verified may
    propose live candidates)."""
    cfg = cfg or OptionsConfig()
    ostore.ensure_schema(con)
    # never raises on hostile text: a lone surrogate cannot be UTF-8 encoded (hash and store a
    # replaced copy), absurd nesting is a rejection reason (parse)
    safe = text.encode("utf-8", "replace").decode("utf-8")
    sha = hashlib.sha256(safe.strip().encode("utf-8")).hexdigest()
    at = ostore.iso(received_at)
    try:
        p = parse(text)
    except ValueError as e:
        text = safe
        con.execute("INSERT INTO options_plans (plan_id, for_date, received_at, status, shadow_only, error, sha, raw) "
                    "VALUES (?,?,?,?,?,?,?,?)", (_peek_id(text), None, at, "rejected", 1, str(e)[:2000], sha,
                                                 text[:MAX_PLAN_BYTES]))
        con.commit()
        return IngestResult("rejected", _peek_id(text), "", True, str(e))
    prior = con.execute("SELECT id, sha, shadow_only, source, received_at, raw FROM options_plans "
                        "WHERE plan_id=? AND status='accepted'", (p.plan_id,)).fetchone()
    if prior is not None:
        # the same plan re-serialized (key order, spacing) is the same plan
        if prior["sha"] == sha or _canonical(prior["raw"]) == _canonical(text):
            if source in TRUSTED_SOURCES and prior["source"] not in TRUSTED_SOURCES:
                # the same plan resent over a trusted channel: it becomes trusted
                # (the shadow cutoff is judged on the resend). It moves to the
                # resend clock only if no trusted plan for that day arrived after
                # it, so an old plan cannot displace a newer trusted one.
                shadow = is_shadow_only(p.for_date, received_at, cfg)
                newer = con.execute("SELECT 1 FROM options_plans WHERE for_date=? AND status='accepted' AND id<>? "
                                    "AND source IN (?, ?) AND received_at > ? LIMIT 1",
                                    (str(p.for_date), prior["id"], *TRUSTED_SOURCES,
                                     prior["received_at"])).fetchone()
                con.execute("UPDATE options_plans SET source=?, shadow_only=?, received_at=? WHERE id=?",
                            (source, int(shadow), prior["received_at"] if newer else at, prior["id"]))
                con.commit()
                return IngestResult("upgraded", p.plan_id, str(p.for_date), shadow)
            return IngestResult("duplicate", p.plan_id, str(p.for_date), bool(prior["shadow_only"]))
        err = "plan_id already used by a different accepted plan"
        con.execute("INSERT INTO options_plans (plan_id, for_date, received_at, status, shadow_only, error, sha, raw, "
                    "source) VALUES (?,?,?,?,?,?,?,?,?)",
                    (p.plan_id, str(p.for_date), at, "rejected", 1, err, sha, text, source))
        con.commit()
        return IngestResult("rejected", p.plan_id, str(p.for_date), True, err)
    shadow = is_shadow_only(p.for_date, received_at, cfg) or source not in TRUSTED_SOURCES
    con.execute("INSERT INTO options_plans (plan_id, for_date, received_at, status, shadow_only, error, sha, raw, "
                "source) VALUES (?,?,?,?,?,?,?,?,?)", (p.plan_id, str(p.for_date), at, "accepted", int(shadow), "",
                                                       sha, text.strip(), source))
    con.commit()
    return IngestResult("accepted", p.plan_id, str(p.for_date), shadow)


def _canonical(text: str) -> str | None:
    try:
        return json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (ValueError, RecursionError):
        return None


def _peek_id(text: str) -> str:
    try:
        v = json.loads(text).get("plan_id", "")
        return v[:64].encode("utf-8", "replace").decode("utf-8") if isinstance(v, str) else ""
    except (ValueError, AttributeError, RecursionError):
        return ""


@dataclass(frozen=True)
class ActivePlan:
    plan: OptionsPlan
    received_at: datetime
    shadow_only: bool


def active_plan(con, day: _dt.date) -> ActivePlan | None:
    """The plan in force for ``day``: the most recently RECEIVED accepted plan
    (a later plan supersedes an earlier one instead of doubling it). Re-parsed
    from the stored text, so the same strict schema applies on every read.
    Only trusted sources count; a row stored before the source column existed
    (NULL) is unknown and therefore untrusted."""
    ostore.ensure_schema(con)
    r = con.execute("SELECT raw, received_at, shadow_only FROM options_plans WHERE for_date=? AND status='accepted' "
                    "AND source IN (?, ?) ORDER BY received_at DESC, id DESC LIMIT 1",
                    (str(day), *TRUSTED_SOURCES)).fetchone()
    if r is None:
        return None
    return ActivePlan(parse(r["raw"]), datetime.fromisoformat(r["received_at"]), bool(r["shadow_only"]))
