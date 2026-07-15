"""Turn raw RSS articles (news_articles_raw) into the unified, human-facing
`news_items` feed the 消息雷達 reads.

This is the "constantly updating, more sources" half of the news redesign: the
RSS ingester (data/ingestion/rss_news.py) pulls fresh free-feed headlines every
run; this module classifies each one (type / symbol / sentiment / importance),
keeps only the market-relevant ones for the watchlist, and upserts them into
news_items with an honest `available_at` look-ahead stamp (the moment we saw
it, not the article's own timestamp -- CLAUDE.md section 2).

Everything here is lexicon/heuristic, deliberately: it is a discretion-layer
display aid (CLAUDE.md section 1), never a model feature, so it needs no API
key and no significance test. Swap the heuristics for the Haiku/Sonnet
classify+synthesis passes later without changing the news_items contract.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import duckdb

# Watchlist tickers -> the names/aliases that actually appear in headlines.
# Must stay in sync with watchlist_members (migrations 001 + 038) -- a symbol
# missing here silently gets ZERO news tagged (tag_symbol only matches on these
# aliases), which is exactly the gap that made the 20-symbol watchlist expansion
# only show news for the original 11 until this was caught and fixed.
COMPANY_ALIASES: dict[str, list[str]] = {
    "NVDA": ["nvidia", "nvda"],
    "AVGO": ["broadcom", "avgo"],
    "AMD": ["amd", "advanced micro"],
    "TSM": ["tsmc", "taiwan semiconductor", "tsm"],
    "MU": ["micron", " mu "],
    "QCOM": ["qualcomm", "qcom"],
    "MRVL": ["marvell", "mrvl"],
    "INTC": ["intel", "intc"],
    "AAPL": ["apple", "aapl", "iphone"],
    "MSFT": ["microsoft", "msft", "azure"],
    "GOOGL": ["alphabet", "google", "googl", "gemini"],
    "META": ["meta platforms", "facebook", "instagram", " meta ", "meta's"],
    "AMZN": ["amazon", "amzn", " aws "],
    "TSLA": ["tesla", "tsla", "elon musk"],
    "NFLX": ["netflix", "nflx"],
    "ORCL": ["oracle", "orcl"],
    "CRM": ["salesforce", " crm "],
    "PLTR": ["palantir", "pltr"],
    "JPM": ["jpmorgan", "jp morgan", " jpm "],
    "BAC": ["bank of america", " bac "],
    "GS": ["goldman sachs", " gs "],
    "MS": ["morgan stanley"],
    "WFC": ["wells fargo", " wfc "],
    "XOM": ["exxon", "exxonmobil", " xom "],
    "CVX": ["chevron", " cvx "],
    "COP": ["conocophillips", " cop "],
    "SLB": ["schlumberger", " slb "],
    "SOXL": ["soxl", "semiconductor etf"],
    "SOXS": ["soxs"],
    "SOXX": ["soxx"],
    "QQQ": ["qqq", "nasdaq-100 etf", "nasdaq 100 etf"],
}

EARNINGS_KW = ["earnings", "revenue", "guidance", "quarterly", "q1", "q2", "q3", "q4",
               "eps", "results", "profit", "forecast", "outlook", "beats", "misses"]
ANALYST_KW = ["upgrade", "downgrade", "price target", "initiates", "initiated", "overweight",
              "underweight", "buy rating", "sell rating", "neutral rating", "analyst",
              "raises target", "cuts target", "reiterates", "outperform"]

# Genuine US-equity-moving macro themes. Root-cause fix (superseding the old
# cockpit-layer `_MACRO_RELEVANT`/`_MACRO_NOISE` backstop in api/cockpit.py):
# short tokens like "fed" must be WHOLE-WORD matched (see MACRO_RE below) so
# they don't false-positive on substrings like "Federal monitor" or "confeder-
# ate" -- and a denylist catches lifestyle/off-topic filler that otherwise
# slips through on a stray macro-sounding word (confirmed on real ingested
# headlines: coffee-tariff spats, UAW disputes, student-loan explainers,
# "cheapest states 2026" listicles, air-taxi puff pieces).
MACRO_KW = [
    "fed", "fomc", "federal reserve", "interest rate", "rate hike", "rate cut",
    "monetary", "powell",
    "inflation", "cpi", "ppi", "pce", "jobs report", "nonfarm", "payroll",
    "unemployment", "treasury", "yield", "bond market", "gdp",
    "dollar index", "dxy", "greenback", "oil", "crude", "opec",
    "recession", "tariff", "geopolit", "iran", "hormuz", "ukraine",
    "russia", "trade war", "export control", "selloff", "sell-off",
    "stock market", "market selloff",
]
MACRO_NOISE_KW = [
    "cheapest states", "expensive states", "student loan", "where to put cash",
    "air taxi", "coffee", "rap plan", "beta wraps", "uaw",
]

_MACRO_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in MACRO_KW) + r")\b", re.I)


def _is_genuine_macro(text: str) -> bool:
    """Whole-word macro-theme match, minus the denylisted lifestyle/off-topic
    filler -- the source-of-truth check for item_type='macro' (see
    classify_type below). Whole-word matching matters for short tokens like
    "fed" or "rate", which would otherwise substring-match unrelated words
    ("Federal monitor", "accelerate")."""
    low = text.lower()
    if any(bad in low for bad in MACRO_NOISE_KW):
        return False
    return bool(_MACRO_RE.search(low))

POS_KW = ["surge", "soar", "jump", "rally", "beat", "beats", "record", "upgrade", "gains",
          "climbs", "boost", "strong", "raises", "outperform", "bullish", "tops", "wins"]
NEG_KW = ["plunge", "plummet", "slump", "drop", "falls", "miss", "misses", "downgrade",
          "cuts", "weak", "warns", "warning", "lawsuit", "probe", "recall", "bearish",
          "tumbles", "slides", "sinks", "selloff", "layoffs"]
BIG_MOVE_KW = ["surge", "plunge", "soar", "crash", "record", "historic", "biggest", "sinks"]

MODEL_VERSION = "news-heuristic-v1"


@dataclass
class NewsItemRow:
    item_id: str
    symbol: str | None
    item_type: str
    headline: str
    summary: str | None
    url: str | None
    source_name: str | None
    published_at: datetime
    sentiment_score: float | None
    importance: float | None
    novelty_score: float | None
    priced_in_estimate: float | None
    transmission_chain: str | None
    source_refs: str  # JSON array


def _clean(text: str | None) -> str:
    if not text:
        return ""
    # RSS summaries are frequently HTML; strip tags for the short synopsis.
    return re.sub(r"<[^>]+>", "", text).strip()


# Sell-side banks whose name appears in financial headlines FAR more often as
# the SOURCE of a rating on some other company ("Goldman Sachs says [other
# stock] is a buy") than as the article's own subject. A plain alias match
# mis-tags every such headline to the bank's own ticker -- confirmed on real
# ingested headlines (e.g. "American Express is a buy..., JPMorgan says" got
# tagged JPM, "...has made a big comeback, Goldman Sachs says" got tagged GS).
# Scoped to just these banks: for a product company, "$SYMBOL says X" (e.g.
# "Nvidia says it will ship Blackwell") genuinely IS news about that company,
# so the same exclusion would wrongly suppress real self-announcements there.
_RATING_SOURCE_SYMBOLS = {"JPM", "GS", "MS", "BAC", "WFC"}
_SOURCE_ATTRIBUTION_RE = re.compile(r"^'?s?\W*(says?|said)\b")


def tag_symbol(text: str) -> str | None:
    low = f" {text.lower()} "
    for symbol, aliases in COMPANY_ALIASES.items():
        for alias in aliases:
            idx = low.find(alias)
            if idx == -1:
                continue
            if symbol in _RATING_SOURCE_SYMBOLS:
                after = low[idx + len(alias): idx + len(alias) + 16]
                if _SOURCE_ATTRIBUTION_RE.match(after):
                    continue  # "<bank> says ..." -> source citation, not the subject
            return symbol
    return None


def classify_type(text: str, symbol: str | None) -> str:
    low = text.lower()
    if any(k in low for k in ANALYST_KW):
        return "analyst_rating"
    if any(k in low for k in EARNINGS_KW):
        return "earnings"
    if symbol is None and _is_genuine_macro(text):
        return "macro"
    return "headline"


def score_sentiment(text: str) -> float | None:
    low = text.lower()
    pos = sum(1 for k in POS_KW if k in low)
    neg = sum(1 for k in NEG_KW if k in low)
    if pos == 0 and neg == 0:
        return None
    return max(-1.0, min(1.0, (pos - neg) / 2.0))


def score_importance(text: str, item_type: str) -> float:
    low = text.lower()
    base = 0.45
    if item_type in ("earnings", "analyst_rating", "macro"):
        base += 0.2
    if any(k in low for k in BIG_MOVE_KW):
        base += 0.25
    return round(min(1.0, base), 2)


def _recency_novelty(published_at: datetime, now: datetime) -> float:
    hours = max(0.0, (now - published_at).total_seconds() / 3600)
    if hours <= 6:
        return 0.8
    if hours <= 24:
        return 0.6
    if hours <= 72:
        return 0.4
    return 0.2


def build_news_items(
    articles: list[dict], *, now: datetime | None = None, macro_generic: bool = True
) -> list[NewsItemRow]:
    """Classify raw articles into news_items rows, keeping only the ones that
    mention a watchlist name or are clearly macro (everything else is noise for
    this watchlist-scoped product)."""
    now = now or datetime.now(timezone.utc)
    out: list[NewsItemRow] = []
    for a in articles:
        title = _clean(a.get("title"))
        if not title:
            continue
        summary = _clean(a.get("summary"))
        blob = f"{title} {summary}"
        # A symbol-scoped source (Google News per-ticker query) already knows
        # the symbol; general feeds are matched from the text.
        symbol = a.get("symbol_hint") or tag_symbol(blob)
        item_type = classify_type(blob, symbol)
        is_macro = item_type == "macro"
        if symbol is None and not (is_macro and macro_generic):
            continue  # not about our universe and not a macro story -> drop
        published = a.get("published_at") or now
        item = NewsItemRow(
            item_id=f"rss:{a['article_id']}",
            symbol=symbol,
            item_type=item_type,
            headline=title[:280],
            summary=(summary[:400] or None),
            url=a.get("url"),
            source_name=(a.get("source_name") or "rss").upper(),
            published_at=published,
            sentiment_score=score_sentiment(blob),
            importance=score_importance(blob, item_type),
            novelty_score=_recency_novelty(published, now),
            priced_in_estimate=0.5,
            transmission_chain=None,
            source_refs=f'["{a["article_id"]}"]',
        )
        out.append(item)
    return out


def refresh_news_items(conn: duckdb.DuckDBPyConnection) -> dict:
    """One-call live refresh used by both scripts/ingest_news.py and the nightly
    job: pull general + per-symbol RSS, classify, upsert. Kept here so the
    orchestration is a single import and both callers stay in sync."""
    from stockmoney.data.ingestion.rss_news import ingest_news
    from stockmoney.data.ingestion.symbol_news import fetch_symbol_news

    raw_written = ingest_news(conn)
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT article_id, source_name, title, summary, url, published_at,
                   row_number() OVER (PARTITION BY article_id ORDER BY ingested_at DESC) AS rn
            FROM news_articles_raw
        )
        SELECT article_id, source_name, title, summary, url, published_at
        FROM latest WHERE rn = 1 AND title IS NOT NULL
        ORDER BY published_at DESC NULLS LAST LIMIT 400
        """
    ).fetchall()
    cols = ["article_id", "source_name", "title", "summary", "url", "published_at"]
    articles = [dict(zip(cols, r)) for r in rows] + fetch_symbol_news()
    items = build_news_items(articles)
    upserted = upsert_news_items(conn, items)
    return {"rss_raw": raw_written, "news_items_upserted": upserted,
            "symbol_tagged": sum(1 for it in items if it.symbol is not None)}


def upsert_news_items(conn: duckdb.DuckDBPyConnection, items: list[NewsItemRow]) -> int:
    """Idempotent upsert keyed on item_id. Re-running the ingester refreshes a
    story's scores rather than duplicating it."""
    now = datetime.now(timezone.utc)
    n = 0
    for it in items:
        conn.execute(
            """
            INSERT INTO news_items
            (item_id, symbol, item_type, headline, summary, url, source_name, published_at,
             sentiment_score, importance, novelty_score, priced_in_estimate, transmission_chain,
             source_refs, available_at, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (item_id) DO UPDATE SET
                sentiment_score = excluded.sentiment_score,
                importance = excluded.importance,
                novelty_score = excluded.novelty_score,
                summary = excluded.summary
            """,
            [
                it.item_id, it.symbol, it.item_type, it.headline, it.summary, it.url,
                it.source_name, it.published_at, it.sentiment_score, it.importance,
                it.novelty_score, it.priced_in_estimate, it.transmission_chain,
                it.source_refs, now, now,
            ],
        )
        n += 1
    return n
