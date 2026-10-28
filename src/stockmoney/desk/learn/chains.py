"""Event chains: written BEFORE the outcome, scored AFTER, so the ability to
reason from events to unpriced consequences becomes a measured number.

A chain = trigger event -> first/second-order steps -> impacts (ticker,
direction, horizon in {5,20,60} sessions, order 1 or 2) -> checkpoints ->
confidence. Scoring: excess return vs SPY from the last close known when the
chain was written; hit = direction matched the sign. Brier uses ``confidence``
as the probability the declared direction is right, only at the declared
horizon. The excess vs the stock's sector ETF is recorded beside it
(``excess_sector`` / ``hit_sector``, NULL for ETFs and names without a
sector) so a call that only rode its sector can be told apart.

Authority: none. Chains are logged and scored. They do not touch orders. The
permission ladder in docs/desk/EDGE_PLAN.md 2.3 is reported by ``authority``;
widening it is the owner's decision and a change to MUSE_BRIDGE.md.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

from stockmoney.desk import store
from stockmoney.desk.data import Bars
from stockmoney.desk.universe import MARKET, sector_of

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
    order: Literal[1, 2] = 1  # 1: directly hit by the trigger; 2: second-order (supplier, customer, competitor)


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    what: Text
    date: str | None = None


PRICE_KINDS = ("close_below", "close_above")


class PriceInvalidation(BaseModel):
    """Chain is invalidated if any daily close on or before ``by_date`` is below
    (close_below) or above (close_above) ``level``. Judged by learn/invalidation.py."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["close_below", "close_above"]
    symbol: Ticker
    level: float = Field(gt=0)
    by_date: date


class DataInvalidation(BaseModel):
    """A free-text expectation. Code cannot verify it: it is judged "no_data"
    on the due date and never counts as a falsification."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["data_not_by"]
    description: Text
    by_date: date


Invalidation = Annotated[PriceInvalidation | DataInvalidation, Field(discriminator="kind")]


class Chain(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger: Text
    urls: list[str] = Field(default_factory=list, max_length=10)
    steps: list[Text] = Field(min_length=1, max_length=8)
    impacts: list[Impact] = Field(min_length=1, max_length=6)
    checkpoints: list[Checkpoint] = Field(default_factory=list, max_length=6)
    invalidations: list[Invalidation] = Field(default_factory=list, max_length=6)
    confidence: float = Field(ge=0, le=1)
    source: Literal["claude", "owner", "grok", "muse"] = "claude"

    @property
    def falsifiable(self) -> bool:
        """Only a price condition can be proven true by code. A chain without
        one is accepted and scored, but kept out of the evidence counts."""
        return any(i.kind in PRICE_KINDS for i in self.invalidations)


def _valid_invalidations(raw) -> list[dict]:
    """Keep the invalidation conditions that validate and are checkable against
    the stock cache; drop the rest instead of rejecting the whole chain."""
    ta = TypeAdapter(Invalidation)
    out = []
    for r in raw or []:
        try:
            if "/" in str(r.get("symbol", "")):
                continue  # crypto has no price series here, same rule as impacts
            out.append(ta.validate_python(r).model_dump(mode="json"))
        except Exception:
            continue
    return out


def _grok_chains(con):
    """Every GROK_DAILY chain in the order ``ingest_grok`` stores them (receipt
    time, then arrival order): yields (message row, Chain or None if it does
    not validate). Shared by ``grok_chain_owners`` so a chain row can be traced
    to the message that actually wrote it."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='bridge_messages'").fetchone() is None:
        return
    has_source = "source" in {r[1] for r in con.execute("PRAGMA table_info(bridge_messages)")}
    src = "source" if has_source else "NULL"
    for r in con.execute(f"SELECT msg_id, type, parsed_json, received_at, {src} AS source FROM bridge_messages "
                         "WHERE type='GROK_DAILY' ORDER BY received_at, rowid").fetchall():
        try:
            msg = json.loads(r["parsed_json"])
            raw_chains = msg.get("chains", [])
        except (ValueError, AttributeError):
            yield r, None
            continue
        for c in raw_chains:
            try:
                chain = Chain.model_validate({
                    "trigger": c["trigger"], "urls": [str(u) for u in c.get("urls", [])], "steps": c["steps"],
                    # crypto (BTC/USD) has no price series in the stock cache; dropping it beats
                    # renaming it to "BTC", which is also the ticker of a listed bitcoin ETF
                    "impacts": [i for i in c["impacts"] if "/" not in i["ticker"]],
                    "checkpoints": [{"what": k["what"], "date": k.get("date")} for k in c.get("checkpoints", [])],
                    # absent in older messages; a bad condition is dropped, never fatal to the chain
                    "invalidations": _valid_invalidations(c.get("invalidations")),
                    "confidence": c["confidence"], "source": "grok"})
            except Exception:
                chain = None
            yield r, chain


def ingest_grok(con) -> dict:
    """Copy chains out of stored GROK_DAILY messages into the chains table.

    Reads ``bridge_messages`` (already sender/token/schema validated by the
    bridge) and never writes to it. ``created_at`` is OUR ``received_at``, not
    anything Grok wrote, so a chain cannot be back-dated. Idempotent: the chain
    id is derived from source + trigger + received day. A chain that does not
    validate here is skipped and counted, never raised."""
    added = skipped = 0
    for r, chain in _grok_chains(con):
        if chain is None:
            skipped += 1
            continue
        try:
            before = con.total_changes
            cid = add_chain(con, chain, datetime.fromisoformat(r["received_at"]))
            if con.total_changes > before:
                # the message whose insert actually wrote the row owns it
                con.execute("INSERT OR REPLACE INTO chain_owners (chain_id, msg_id) VALUES (?,?)", (cid, r["msg_id"]))
                added += 1
        except Exception:
            skipped += 1
    backfill_chain_owners(con)
    return {"added": added, "skipped": skipped}


def backfill_chain_owners(con) -> int:
    """One-shot backfill for chains that were ingested before ``chain_owners``
    existed, so they never got an owner row at insert time. When exactly one
    GROK_DAILY message would produce a given chain_id (same trigger landing in
    the same ET day), that message is the only candidate that could have
    written it, so it is recorded as the owner. A chain_id produced by two or
    more candidate messages stays unowned, since there is no way to tell which
    one actually inserted it; a genuine collision must never be guessed at.
    Safe to call every run: a chain_id that already has an owner, or that
    does not exist in ``chains``, is left untouched. Returns the count newly
    owned."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='chain_owners'").fetchone() is None:
        return 0
    candidates: dict[str, set[str]] = {}
    for r, chain in _grok_chains(con):
        if chain is None:
            continue
        cid = chain_id("grok", chain.trigger, datetime.fromisoformat(r["received_at"]))
        candidates.setdefault(cid, set()).add(r["msg_id"])
    owned = {row[0] for row in con.execute("SELECT chain_id FROM chain_owners")}
    existing = {row[0] for row in con.execute("SELECT chain_id FROM chains")}
    n = 0
    for cid, msg_ids in candidates.items():
        if cid in owned or cid not in existing or len(msg_ids) != 1:
            continue
        con.execute("INSERT OR REPLACE INTO chain_owners (chain_id, msg_id) VALUES (?,?)", (cid, next(iter(msg_ids))))
        n += 1
    return n


def grok_chain_owners(con) -> dict[str, object]:
    """chain_id -> the bridge_messages row recorded as owning the chain: either
    the message whose insert actually wrote it, or, for a chain ingested
    before ``chain_owners`` existed, the single unambiguous GROK_DAILY message
    that ``backfill_chain_owners`` found as the only candidate (see there). A
    message whose insert failed, or that found the id already taken, is never
    the owner, whatever its urls. A chain still unowned (a genuine collision
    on trigger + day) has none, so it is never treated as verified.
    Read-only."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='chain_owners'").fetchone() is None:
        return {}
    rows = {r["msg_id"]: r for r, _ in _grok_chains(con)}
    return {cid: rows[m] for cid, m in con.execute("SELECT chain_id, msg_id FROM chain_owners") if m in rows}


def chain_id(source: str, trigger: str, created_at: datetime) -> str:
    created = created_at.astimezone(timezone.utc)
    return hashlib.sha1(f"{source}|{trigger}|{created.astimezone(ET).date()}".encode()).hexdigest()[:12]


def _sym(t: str) -> str:
    return t.replace(".", "-")


def add_chain(con, chain: Chain, created_at: datetime | None = None) -> str:
    """Store a chain; returns its id. Re-adding the same trigger on the same
    day is a no-op (same id)."""
    created = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cid = chain_id(chain.source, chain.trigger, created)
    con.execute(
        "INSERT OR IGNORE INTO chains (chain_id, created_at, source, trigger_text, urls_json, steps_json, "
        "impacts_json, checkpoints_json, confidence, invalidations_json, falsifiable) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (cid, created.isoformat(), chain.source, chain.trigger, json.dumps(chain.urls),
         json.dumps(chain.steps, ensure_ascii=False), json.dumps([i.model_dump() for i in chain.impacts]),
         json.dumps([c.model_dump() for c in chain.checkpoints], ensure_ascii=False), chain.confidence,
         json.dumps([i.model_dump(mode="json") for i in chain.invalidations], ensure_ascii=False),
         int(chain.falsifiable)))
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
                up = imp["direction"] == "up"
                ret = float(px1 / px0 - 1)
                ex = ret - float(s1 / s0 - 1)
                hit = int((ex > 0) == up)
                etf = sector_of(sym)
                ex_sec = hit_sec = None
                if etf in bars.close.columns:
                    e0, e1 = bars.close.at[base, etf], bars.close.at[t, etf]
                    if not (pd.isna(e0) or pd.isna(e1)):
                        ex_sec = ret - float(e1 / e0 - 1)
                        hit_sec = int((ex_sec > 0) == up)
                con.execute("INSERT INTO chain_scores (chain_id, ticker, horizon, declared, direction, base_date, "
                            "scored_date, excess_ret, hit, confidence, scored_at, order_n, sector_etf, excess_sector, "
                            "hit_sector) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (r["chain_id"], imp["ticker"], h, int(h == imp["horizon_days"]), imp["direction"],
                             str(base.date()), str(t.date()), ex, hit, r["confidence"], store.now(),
                             int(imp.get("order", 1)), etf if ex_sec is not None else None, ex_sec, hit_sec))
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
    """Evidence counts use only chains that carry a checkable price invalidation
    (``falsifiable``). Scores of unfalsifiable chains are kept and reported
    separately in ``scored_unfalsifiable`` but never feed hit rate or Brier."""
    rows = con.execute("SELECT s.* FROM chain_scores s JOIN chains c ON c.chain_id=s.chain_id "
                       "WHERE s.declared=1 AND c.falsifiable=1").fetchall()
    n = len(rows)
    hits = sum(r["hit"] for r in rows)
    brier = sum((r["confidence"] - r["hit"]) ** 2 for r in rows) / n if n else None
    total = con.execute("SELECT COUNT(*) FROM chains").fetchone()[0]
    unfals = con.execute("SELECT COUNT(*) FROM chains WHERE falsifiable=0").fetchone()[0]
    declared = con.execute("SELECT COUNT(*) FROM chain_scores WHERE declared=1").fetchone()[0]
    return {"chains": total, "unfalsifiable": unfals, "scored": n, "scored_unfalsifiable": declared - n,
            "hits": hits, "hit_rate": hits / n if n else None,
            "hit_lo": _wilson_lo(hits, n), "brier": brier, "brier_coin": 0.25}


def by_order(con) -> dict[int, dict]:
    """Declared-horizon scores of falsifiable chains split by impact order, with
    the sector-relative hit rate where a sector ETF applies. Report only."""
    out: dict[int, dict] = {}
    for r in con.execute("SELECT COALESCE(s.order_n, 1) AS o, COUNT(*) AS n, SUM(s.hit) AS hits, "
                         "COUNT(s.hit_sector) AS sn, SUM(s.hit_sector) AS shits FROM chain_scores s "
                         "JOIN chains c ON c.chain_id=s.chain_id WHERE s.declared=1 AND c.falsifiable=1 "
                         "GROUP BY o ORDER BY o").fetchall():
        out[r["o"]] = {"n": r["n"], "hit_rate": r["hits"] / r["n"], "sector_n": r["sn"],
                       "sector_hit_rate": (r["shits"] / r["sn"]) if r["sn"] else None}
    return out


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
             f"已記錄 {s['chains']} 條(其中 {s['unfalsifiable']} 條沒有可檢驗的價格推翻條件,不列入證據),"
             f"可列入證據的已評分(宣告時間窗){s['scored']} 筆。" + (
                 f" 命中率 {s['hit_rate']:.0%},Brier {s['brier']:.3f}(丟銅板 0.250)。" if s["scored"] else ""),
             f"權限階段:{level}。{why}"]
    if s["scored_unfalsifiable"]:
        lines.append(f"另有 {s['scored_unfalsifiable']} 筆來自不可證偽事件鏈的評分,只記錄,不列入命中率與 Brier。")
    split = by_order(con)
    if split:
        lines.append("依傳導層級(宣告時間窗,可列入證據):" + ";".join(
            f"第 {o} 層 {d['n']} 筆,對 SPY 命中 {d['hit_rate']:.0%}"
            + (f",對板塊 ETF 命中 {d['sector_hit_rate']:.0%}({d['sector_n']} 筆)" if d["sector_n"] else "")
            for o, d in split.items()) + "。少於 30 筆不下結論。")
    from stockmoney.desk.learn import corroboration, invalidation
    lines += [x for x in (invalidation.report(con), corroboration.report(con)) if x]
    pending = con.execute(
        "SELECT chain_id, trigger_text FROM chains c WHERE NOT EXISTS (SELECT 1 FROM chain_scores s "
        "WHERE s.chain_id=c.chain_id AND s.declared=1) ORDER BY created_at DESC LIMIT 5").fetchall()
    if pending:
        lines += ["", "等待驗證:"] + [f"- {p['trigger_text'][:60]}" for p in pending]
    return "\n".join(lines)
