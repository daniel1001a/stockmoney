"""TypeSafe Jev engine for the scan classification pass.

Drop-in `classify_fn(items, symbols) -> list[dict]` for
`stockmoney.data.scan_classify.run_classification_pass`. The result dicts have
exactly the shape `apply_result` already consumes, so the validated write
boundary (`stockmoney.data.classification`) and the idempotency ledger
(`scan_classifications`) are untouched.

Division of labour (code owns the workflow, Jev supplies semantic judgment):
- Code finds candidate tickers in the text (aliases / uppercase symbols that
  are on the active watchlist). Jev never generates a ticker, it only judges
  the candidates it is handed, so a hallucinated ticker cannot occur.
- Jev answers typed questions per item in ONE request: relevance (Noul),
  candidate-worthiness (Noul), and per candidate ticker: "substantively
  discussed" (Noul) and sentiment (Score).
- Code routes with explicit thresholds. Anything uncertain, and everything
  candidate-worthy (it needs a generated `rationale`, which Jev cannot
  produce), is escalated to `escalate_fn` (the existing Claude CLI path).
- Raw probabilities/confidences go to `jev_judgments` before routing, so
  thresholds can be re-calibrated from shadow-run data.

Scraped text is untrusted. It is only ever placed in `state`, never in
instructions or criteria.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

import duckdb

from stockmoney.data.news_synthesis import COMPANY_ALIASES
from stockmoney.data.scan_classify import ScanItem

JEV_MODEL = "jev-1.13.0"  # pinned; never an alias (reproducibility)
JEV_MODEL_VERSION = "scan-classify-jev-1.13.0-v1"
ESCALATED_MODEL_VERSION = "scan-classify-jev-escalated-v1"

# Routing thresholds. Provisional: re-calibrate on shadow-run data.
IRRELEVANT_MAX = 0.30   # relevant_p below this -> direct irrelevant
RELEVANT_MIN = 0.70     # relevant_p at/above this -> eligible for direct sentiment
CANDIDATE_MIN = 0.50    # candidate_p at/above this -> escalate (needs rationale)
DISCUSSES_MIN = 0.60    # symbol must be substantively discussed to be scored
DISCUSSES_MAX_ABSENT = 0.40  # below this -> symbol clearly not discussed
SENTIMENT_CONF_MIN = 0.60

MAX_TEXT_CHARS = 4000
MAX_WORKERS = 4

SENTIMENT_LEVELS = [
    "strongly bearish for this company's stock",
    "mildly bearish",
    "neutral or mixed",
    "mildly bullish",
    "strongly bullish for this company's stock",
]

_UPPER_TOKEN_RE = re.compile(r"\b[A-Z]{2,5}\b")


def find_candidate_symbols(text: str, symbols: list[str]) -> list[str]:
    """Active watchlist symbols mentioned in `text` by uppercase token, $cashtag,
    or known company alias. Deterministic, code-owned."""
    active = set(symbols)
    found: set[str] = set()
    for tok in _UPPER_TOKEN_RE.findall(text):
        if tok in active:
            found.add(tok)
    low = f" {text.lower()} "
    for sym, aliases in COMPANY_ALIASES.items():
        if sym in active and any(a in low for a in aliases):
            found.add(sym)
    return sorted(found)


def _questions(candidates: list[str]) -> dict:
    from typesafe_sdk import Noul, Score

    qs: dict = {
        "relevant": Noul(
            instructions=(
                "Is `item` about publicly traded companies, financial markets, "
                "or economic conditions that could matter to a stock or ETF trader?"
            )
        ),
        "candidate_worthy": Noul(
            instructions=(
                "Does `item` give concrete evidence of a company, ticker, or "
                "investment theme gaining real traction or facing a real catalyst "
                "(not a one-off passing mention, joke, or vague hype)?"
            )
        ),
    }
    for sym in candidates:
        qs[f"discusses:{sym}"] = Noul(
            instructions=(
                f"Is `item` substantively about {sym} (the company or its stock), "
                "rather than mentioning it only in passing?"
            )
        )
        qs[f"sentiment:{sym}"] = Score(
            instructions=(
                f"What does `item` imply for the stock price outlook of {sym}?"
            ),
            criteria=SENTIMENT_LEVELS,
        )
    return qs


def _item_text(item: ScanItem) -> str:
    return f"{item.title or ''}\n{item.body or ''}"


def _judge(client, item: ScanItem, symbols: list[str], model: str) -> dict:
    """One Jev request for one item. Returns the raw judgment dict (no routing)."""
    text = _item_text(item)
    candidates = find_candidate_symbols(text, symbols)
    state = {
        "item": {
            "type": item.item_type,
            "title": (item.title or "")[:MAX_TEXT_CHARS],
            "body": (item.body or "")[:MAX_TEXT_CHARS],
        }
    }
    resp = client.system_one(state=state, questions=_questions(candidates), model=model)
    ans = resp.answers
    sym_j: dict[str, dict] = {}
    for sym in candidates:
        sc = ans[f"sentiment:{sym}"]
        sym_j[sym] = {
            "discusses_p": float(ans[f"discusses:{sym}"].noul),
            # Score is the probability-weighted level in [0, 4]; map to [-1, 1].
            "sentiment": (float(sc.score) - 2.0) / 2.0,
            "confidence": float(sc.confidence),
        }
    return {
        "model": resp.model,
        "relevant_p": float(ans["relevant"].noul),
        "candidate_p": float(ans["candidate_worthy"].noul),
        "symbols": sym_j,
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }


def route(j: dict) -> tuple[str, str | None, dict | None]:
    """Pure routing policy. Returns (route, reason, result_dict_or_None).
    result_dict is the apply_result-shaped verdict for direct routes."""
    if j["candidate_p"] >= CANDIDATE_MIN:
        return "escalated", "candidate_worthy", None
    rel = j["relevant_p"]
    if rel < IRRELEVANT_MAX:
        return "direct_irrelevant", None, {"verdict": "irrelevant"}
    if rel < RELEVANT_MIN:
        return "escalated", "uncertain_relevance", None

    entries = []
    for sym, s in j["symbols"].items():
        d = s["discusses_p"]
        if d < DISCUSSES_MAX_ABSENT:
            continue
        if d < DISCUSSES_MIN or s["confidence"] < SENTIMENT_CONF_MIN:
            return "escalated", f"uncertain_symbol:{sym}", None
        entries.append({"symbol": sym, "score": round(s["sentiment"], 4)})
    if not entries:
        return "direct_irrelevant", None, {"verdict": "irrelevant"}
    return "direct_sentiment", None, {"verdict": "sentiment", "sentiment": entries}


def _log(conn: duckdb.DuckDBPyConnection, item: ScanItem, j: dict | None,
         route_: str, reason: str | None) -> None:
    conn.execute(
        """
        INSERT INTO jev_judgments
            (item_id, item_type, judged_at, model, relevant_p, candidate_p,
             symbols_json, route, reason, input_tokens, output_tokens)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            item.item_id, item.item_type, datetime.now(timezone.utc),
            j and j["model"], j and j["relevant_p"], j and j["candidate_p"],
            j and json.dumps(j["symbols"]), route_, reason,
            j and j["input_tokens"], j and j["output_tokens"],
        ],
    )


def make_jev_classify_fn(
    conn: duckdb.DuckDBPyConnection,
    *,
    escalate_fn: Callable[[list[ScanItem], list[str]], list[dict]],
    client=None,
    model: str = JEV_MODEL,
    max_workers: int = MAX_WORKERS,
) -> Callable[[list[ScanItem], list[str]], list[dict]]:
    """Build the Jev `classify_fn`. `conn` is used only for `jev_judgments`
    logging, always from the calling thread. `escalate_fn` handles uncertain
    and candidate-worthy items; if it raises, those items are simply omitted
    (unmatched -> retried next pass, same as a failed batch)."""

    def classify(items: list[ScanItem], symbols: list[str]) -> list[dict]:
        nonlocal client
        if client is None:
            from typesafe_sdk import TypeSafeClient

            client = TypeSafeClient()  # reads TYPESAFE_API_KEY

        def one(item: ScanItem):
            try:
                return item, _judge(client, item, symbols, model), None
            except Exception as exc:  # TypeSafeError, network, validation
                return item, None, f"{type(exc).__name__}"

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            judged = list(pool.map(one, items))

        out: list[dict] = []
        to_escalate: list[ScanItem] = []
        for item, j, err in judged:
            if j is None:
                _log(conn, item, None, "jev_error", err)
                to_escalate.append(item)
                continue
            route_, reason, result = route(j)
            _log(conn, item, j, route_, reason)
            if result is None:
                to_escalate.append(item)
            else:
                out.append({"item_id": item.item_id, "_model_version": JEV_MODEL_VERSION, **result})

        if to_escalate:
            try:
                for r in escalate_fn(to_escalate, symbols):
                    if isinstance(r, dict):
                        out.append({**r, "_model_version": ESCALATED_MODEL_VERSION})
            except (ValueError, TypeError, json.JSONDecodeError):
                pass  # left unprocessed; next pass retries
        return out

    return classify


def make_shadow_classify_fn(
    primary_fn: Callable[[list[ScanItem], list[str]], list[dict]],
    jev_fn: Callable[[list[ScanItem], list[str]], list[dict]],
) -> Callable[[list[ScanItem], list[str]], list[dict]]:
    """Run Jev for its `jev_judgments` log only; the primary engine's results
    are the ones applied. Jev failure never affects the primary result.
    Build `jev_fn` with a no-op `escalate_fn` so shadow mode adds no extra
    Claude calls."""

    def classify(items: list[ScanItem], symbols: list[str]) -> list[dict]:
        # Jev runs first so its judgments are logged even when the primary
        # engine fails the batch (the failure still propagates unchanged).
        try:
            jev_fn(items, symbols)
        except Exception:
            pass
        return primary_fn(items, symbols)

    return classify
