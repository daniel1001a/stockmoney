"""Event chains: written BEFORE the outcome, scored AFTER, so the ability to
reason from events to unpriced consequences becomes a measured number.

A chain = trigger event -> first/second-order steps -> impacts (ticker,
direction, horizon in {5,20,60} sessions) -> checkpoints -> confidence.
Scoring: excess return vs SPY from the last close known when the chain was
written; hit = direction matched the sign. Brier uses ``confidence`` as the
probability the declared direction is right, only at the declared horizon.

Authority: none. Chains are logged and scored. They do not touch orders. The
permission ladder in docs/desk/EDGE_PLAN.md 2.3 is reported by ``authority``;
widening it is the owner's decision and a change to MUSE_BRIDGE.md.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from stockmoney.desk import store
from stockmoney.desk.data import Bars
from stockmoney.desk.universe import MARKET

ET = ZoneInfo("America/New_York")
HORIZONS = (5, 20, 60)
MIN_FOR_REVIEW = 50
Ticker = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9.\-]{0,9}$")]
Text = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class Impact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: Ticker
    direction: Literal["up", "down"]
    horizon_days: Literal[5, 20, 60]


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    what: Text
    date: str | None = None


class Chain(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger: Text
    urls: list[str] = Field(default_factory=list, max_length=10)
    steps: list[Text] = Field(min_length=1, max_length=8)
    impacts: list[Impact] = Field(min_length=1, max_length=6)
    checkpoints: list[Checkpoint] = Field(default_factory=list, max_length=6)
    confidence: float = Field(ge=0, le=1)
    source: Literal["claude", "owner", "grok", "muse"] = "claude"


def ingest_grok(con) -> dict:
    """Copy chains out of stored GROK_DAILY messages into the chains table.

    Reads ``bridge_messages`` (already sender/token/schema validated by the
    bridge) and never writes to it. ``created_at`` is OUR ``received_at``, not
    anything Grok wrote, so a chain cannot be back-dated. Idempotent: the chain
    id is derived from source + trigger + received day. A chain that does not
    validate here is skipped and counted, never raised."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='bridge_messages'").fetchone() is None:
        return {"added": 0, "skipped": 0}
    added = skipped = 0
    for r in con.execute("SELECT parsed_json, received_at FROM bridge_messages WHERE type='GROK_DAILY' "
                         "ORDER BY received_at").fetchall():
        try:
            msg = json.loads(r["parsed_json"])
            raw_chains = msg.get("chains", [])
        except (ValueError, AttributeError):
            skipped += 1
            continue
        for c in raw_chains:
            try:
                chain = Chain.model_validate({
                    "trigger": c["trigger"], "urls": [str(u) for u in c.get("urls", [])], "steps": c["steps"],
                    "impacts": [{**i, "ticker": i["ticker"].replace("/USD", "")} for i in c["impacts"]],
                    "checkpoints": [{"what": k["what"], "date": k.get("date")} for k in c.get("checkpoints", [])],
                    "confidence": c["confidence"], "source": "grok"})
                before = con.total_changes
                add_chain(con, chain, datetime.fromisoformat(r["received_at"]))
                added += con.total_changes > before
            except Exception:
                skipped += 1
    return {"added": added, "skipped": skipped}


def _sym(t: str) -> str:
    return t.replace(".", "-")


def add_chain(con, chain: Chain, created_at: datetime | None = None) -> str:
    """Store a chain; returns its id. Re-adding the same trigger on the same
    day is a no-op (same id)."""
    created = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cid = hashlib.sha1(f"{chain.source}|{chain.trigger}|{created.astimezone(ET).date()}".encode()).hexdigest()[:12]
    con.execute(
        "INSERT OR IGNORE INTO chains VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, created.isoformat(), chain.source, chain.trigger, json.dumps(chain.urls),
         json.dumps(chain.steps, ensure_ascii=False), json.dumps([i.model_dump() for i in chain.impacts]),
         json.dumps([c.model_dump() for c in chain.checkpoints], ensure_ascii=False), chain.confidence))
    return cid


def base_session(created_at: str, sessions: pd.DatetimeIndex) -> pd.Timestamp | None:
    """Last close that was public when the chain was written: after 16:05 ET
    the same day's close counts, otherwise the previous session's."""
    c = datetime.fromisoformat(created_at).astimezone(ET)
    day = pd.Timestamp(c.date())
    after_close = (c.hour, c.minute) >= (16, 5)
    ok = sessions[sessions <= day] if after_close else sessions[sessions < day]
    return ok[-1] if len(ok) else None


def score_due(con, bars: Bars, through: pd.Timestamp) -> int:
    """Score every impact whose horizon has fully elapsed. Idempotent."""
    n = 0
    sessions = bars.dates[bars.dates <= through]
    spy = bars.close[MARKET]
    for r in con.execute("SELECT * FROM chains").fetchall():
        base = base_session(r["created_at"], sessions)
        if base is None:
            continue
        b = sessions.get_loc(base)
        for imp in json.loads(r["impacts_json"]):
            sym = _sym(imp["ticker"])
            if sym not in bars.close.columns:
                continue
            for h in HORIZONS:
                if b + h >= len(sessions):
                    continue
                if con.execute("SELECT 1 FROM chain_scores WHERE chain_id=? AND ticker=? AND horizon=?",
                               (r["chain_id"], imp["ticker"], h)).fetchone():
                    continue
                t = sessions[b + h]
                px0, px1 = bars.close.at[base, sym], bars.close.at[t, sym]
                s0, s1 = spy.at[base], spy.at[t]
                if any(pd.isna(x) for x in (px0, px1, s0, s1)):
                    continue
                ex = float(px1 / px0 - 1) - float(s1 / s0 - 1)
                hit = int((ex > 0) == (imp["direction"] == "up"))
                con.execute("INSERT INTO chain_scores VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (r["chain_id"], imp["ticker"], h, int(h == imp["horizon_days"]), imp["direction"],
                             str(base.date()), str(t.date()), ex, hit, r["confidence"], store.now()))
                n += 1
    return n


def _wilson_lo(k: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    a = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - a) / d


def summary(con) -> dict:
    rows = con.execute("SELECT * FROM chain_scores WHERE declared=1").fetchall()
    n = len(rows)
    hits = sum(r["hit"] for r in rows)
    brier = sum((r["confidence"] - r["hit"]) ** 2 for r in rows) / n if n else None
    total = con.execute("SELECT COUNT(*) FROM chains").fetchone()[0]
    return {"chains": total, "scored": n, "hits": hits, "hit_rate": hits / n if n else None,
            "hit_lo": _wilson_lo(hits, n), "brier": brier, "brier_coin": 0.25}


def authority(con) -> tuple[str, str]:
    """Where the permission ladder stands. Never grants anything by itself."""
    s = summary(con)
    if s["scored"] < MIN_FOR_REVIEW:
        return "log_only", f"已評分 {s['scored']} 筆,未達 {MIN_FOR_REVIEW} 筆,只記錄與評分。"
    if s["hit_lo"] > 0.5 and s["brier"] is not None and s["brier"] < 0.25:
        return "eligible_for_review", (f"命中率 {s['hit_rate']:.0%}(95% 下緣 {s['hit_lo']:.0%})、"
                                        f"Brier {s['brier']:.3f} < 0.25:可交給你審核是否微調部位。仍需你明確修改規則。")
    return "log_only", f"樣本夠但沒有顯著優勢(命中率 {s['hit_rate']:.0%},下緣 {s['hit_lo']:.0%},Brier {s['brier']:.3f})。"


def report(con) -> str:
    s = summary(con)
    level, why = authority(con)
    lines = ["## 事件鏈", "",
             f"已記錄 {s['chains']} 條,已評分(宣告時間窗){s['scored']} 筆。" + (
                 f" 命中率 {s['hit_rate']:.0%},Brier {s['brier']:.3f}(丟銅板 0.250)。" if s["scored"] else ""),
             f"權限階段:{level}。{why}"]
    pending = con.execute(
        "SELECT chain_id, trigger_text FROM chains c WHERE NOT EXISTS (SELECT 1 FROM chain_scores s "
        "WHERE s.chain_id=c.chain_id AND s.declared=1) ORDER BY created_at DESC LIMIT 5").fetchall()
    if pending:
        lines += ["", "等待驗證:"] + [f"- {p['trigger_text'][:60]}" for p in pending]
    return "\n".join(lines)
