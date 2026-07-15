# News/Sentiment Backfill for Experiment 3 (news-as-signal / risk-gate)

Status date: 2026-07-13. Author: Worker-1 (data).

## 0. TL;DR

- **Chosen source: GDELT 2.0 GKG via Google BigQuery's public `gdelt-bq.gdeltv2.gkg_partitioned` dataset.** Free, real (not synthetic), genuinely per-symbol (via organization-name matching), and covers our full 2018-2026 window.
- **Real pilot fetched and stored**: 2018-01-01 -> 2019-12-31, all 9 tickers, **495,950 symbol-tagged rows** (416,874 unique underlying articles; some articles co-mention 2+ of our tickers) at `data/news_backfill/source=gdelt_gkg/`.
- **Full 8-year backfill is FREE-feasible**, not paid-only: a single combined BigQuery query for 2018-01-01..2026-07-13 across all 9 tickers is measured (dry-run) at **~357 GB billed**, comfortably inside BigQuery's 1 TB/month free tier. It was not executed in this pass (scope was a 1-2yr pilot per the task); running it is a ~10-minute follow-up with the same script.
- **What you don't get for free**: real headline text (GKG exposes only the article URL + structured metadata, not the headline), and a genuinely low-noise, deduplicated feed (GKG double/triple-counts the same real-world story across syndication and translation -- see caveats below). If low-noise curated headlines matter more than 8y depth, a paid feed (Finnhub premium, Benzinga, RavenPack, etc.) is the honest alternative; free options (Finnhub free tier, Alpha Vantage) top out at ~1yr history or ~25 requests/day and cannot cover 2018-2026.
- **GDELT's own free public HTTP API (DOC 2.0) could not be exercised from this sandboxed environment** -- every request returned HTTP 429 ("limit requests to one every 5 seconds") even after 40+ second waits between single requests, consistent with a shared/already-saturated sandbox egress IP. This is an environment limitation to revisit from an unshared IP, not evidence the API itself is unusable -- see Section 1.1.

---

## 1. Sources evaluated

### 1.1 GDELT 2.0 DOC 2.0 API (free public HTTP endpoint) -- NOT USABLE from this environment

- Endpoint: `https://api.gdeltproject.org/api/v2/doc/doc`, `mode=artlist`, `format=json`, explicit `startdatetime`/`enddatetime` (`YYYYMMDDHHMMSS`).
- Documented coverage: **2017-01-01 to present** (full-year searching was added in a 2018 update; see GDELT project blog). This would have covered essentially our whole window (2018-2026) and, uniquely among the options here, returns the **real article URL and is queryable by free-text company name**.
- Hard limits: max ~250 records per query (pagination is not well supported beyond re-querying narrower time windows), and a documented "one request per 5 seconds" rate limit.
- **What actually happened**: every test request from this sandbox -- including single, isolated requests spaced 40-45+ seconds apart -- returned HTTP 429 with GDELT's own rate-limit message. This strongly suggests the sandbox's outbound IP is shared across many concurrent sessions/users and was already over GDELT's per-IP budget before this task ever sent a request. It is an environment artifact, not proof the API is broken; a normal single-user laptop should be able to use it directly, and it would additionally provide real headlines that GKG (below) cannot. Flagged here for whoever re-runs this from an unshared network.

### 1.2 Finnhub `/company-news` -- real, ticker-tagged, but too shallow

- Real per-ticker news with a `from`/`to` date range and a free tier. Documented free-tier historical depth is **~1 year** -- nowhere near the 2018-2026 window needed for an 8y walk-forward test.
- Requires creating a Finnhub account to get an API key. Per this task's constraints, account creation is something only the user should do (not performed here without explicit go-ahead).

### 1.3 Alpha Vantage `NEWS_SENTIMENT` -- real, ticker-tagged, but rate-limited to uselessness for backfill

- Free tier: **25 requests/day**, 5/minute. Backfilling 9 tickers over ~8 years (even at one request per ticker per month, ~800+ months of coverage across the universe) is not achievable within this budget in any reasonable timeframe. Also requires account signup (same constraint as 1.2).

### 1.4 yfinance `.news` -- no historical query at all

- Returns only the *current* set of recent headlines for a ticker (a rolling window of maybe the last 1-2 weeks, no `start`/`end` parameters). Structurally cannot backfill history; useful only as a going-forward live feed (already the role `scripts/ingest_news.py` fills for `news_items`).

### 1.5 GDELT 2.0 GKG via BigQuery (`gdelt-bq.gdeltv2.gkg_partitioned`) -- CHOSEN

- Same free public dataset family as `scripts/backfill_gdelt.py`'s `events` table, but the **Global Knowledge Graph** table instead: one row per (15-minute-batch, article), with structured fields including `V2Organizations` (parsed named-entity organization mentions), `V2Tone` (avg tone + 6 other stats), `DocumentIdentifier` (the article URL), `SourceCommonName` (publisher domain), and `DATE` (the ingestion-batch timestamp, `YYYYMMDDHHMMSS`).
- Not natively ticker-tagged, but **substring-matching `V2Organizations` against each company's full legal name** (e.g. `"advanced micro devices"`, `"nvidia corporation"`, not bare tickers/short names -- see Section 3) gives a real, defensible per-symbol tag.
- Reuses the exact same `GDELT_BQ_PROJECT_ID` / `GOOGLE_APPLICATION_CREDENTIALS` already configured for `scripts/backfill_gdelt.py` -- no new credentials needed.
- Table is **DAY time-partitioned** (`_PARTITIONTIME`), unlike the `events` table used by `backfill_gdelt.py` (confirmed via `client.get_table()`: `events` has `time_partitioning=None`; `gkg_partitioned` has `TimePartitioning(type_='DAY')`). Filtering on `_PARTITIONTIME` actually reduces bytes scanned.
- Licensing: GDELT states its datasets are free for **unrestricted academic, commercial, or governmental use**, with a citation-on-redistribution requirement (attribution to the GDELT Project + link). Internal use for our own trading signal research is unrestricted.

---

## 2. Real cost measurements (BigQuery, dry-run verified 2026-07-13)

Cost lesson carried over from `gdelt_events.py`'s docstring: BigQuery bytes-billed depends on **which columns are referenced and which date partitions are touched**, not on how selective the `WHERE` predicates are. Concretely measured:

| Query | Columns | Date range | Bytes billed |
|---|---|---|---|
| `COUNT(*)` w/ loose name patterns (bare `"apple"`, `"google"`, etc.) | `V2Organizations` only | 2018-01-01..2019-12-31 | 25.8 GB, **12,221,234 rows matched** (bare-name false-positive rate is enormous -- see Section 3) |
| `COUNT(*)` w/ precise legal-name patterns | `V2Organizations` only | 2018-01-01..2019-12-31 | 25.8 GB, **416,917 rows matched** |
| `COUNT(*)` w/ precise legal-name patterns | `V2Organizations` only | 2018-01-01..2026-07-13 (full 8.5y) | 80.7 GB, **1,072,788 rows matched** |
| **Real pilot extraction** (all 6 output columns) | `GKGRECORDID, DATE, SourceCommonName, DocumentIdentifier, V2Tone, V2Organizations` | 2018-01-01..2019-12-31 | **110.0 GB** (executed for real, not just dry-run) |
| Full extraction (dry-run only, not executed) | same 6 columns | 2018-01-01..2026-07-13 (full 8.5y) | **357.1 GB** (dry-run estimate) |

All figures are for **one single query covering all 9 tickers combined** via one large OR'd `LIKE` clause -- querying per-ticker separately would multiply the same bytes cost by 9 for zero benefit (verified: adding/removing OR terms did not change the dry-run estimate at all, confirming cost is columns x partitions, independent of predicate complexity).

**Bottom line: full 2018-2026 backfill for all 9 tickers costs ~357 GB in one query, well inside BigQuery's 1 TB/month free sandbox tier.** It was not executed in this pass (task scope was a 1-2yr pilot); running `scripts/backfill_news_real.py --start 2018-01-01 --end 2026-07-13` (no other flags) will do it and write partitioned Parquet the same way the pilot did, in one BigQuery call.

---

## 3. Real pilot results (2018-01-01 to 2019-12-31, all 9 tickers)

Fetched via `scripts/backfill_news_real.py`, written to `data/news_backfill/source=gdelt_gkg/symbol=<TICKER>/year=<YYYY>/part.parquet` (18 partition files, 24 MB total on disk).

**Raw counts**: 416,917 unique GKG records matched at least one ticker's organization pattern -> **495,950 symbol-tagged rows** after exploding articles that mention 2+ of our companies into one row per matched symbol.

Per-symbol, per-year row counts:

| Symbol | 2018 | 2019 | 2yr total |
|---|---:|---:|---:|
| AAPL | 86,762 | 63,685 | 150,447 |
| AMD | 40,430 | 37,886 | 78,316 |
| AMZN | 2,522 | 1,317 | 3,839 |
| AVGO | 3,670 | 4,087 | 7,757 |
| GOOGL | 53,723 | 39,786 | 93,509 |
| META | 41,951 | 29,086 | 71,037 |
| MSFT | 37,738 | 30,386 | 68,124 |
| NVDA | 4,690 | 4,709 | 9,399 |
| TSM | 9,060 | 4,462 | 13,522 |

Date span confirmed exact: `2018-01-01 00:00:00 UTC` .. `2019-12-31 23:45:00 UTC`.

**Sentiment (`sentiment_score` = GDELT `V2Tone`'s avg-tone field / 100, so roughly -1..+1)** across the full pilot: mean -0.0122, std 0.0284, min -0.181, max 0.156 -- consistent with GDELT's own well-documented skew toward negative tone in news generally (also seen in the existing `event_news_gdelt` table).

### Honest caveats found while inspecting the pilot data

1. **Volume is dominated by syndication/translation duplication, not 9x more "real" stories than a curated feed would show.** E.g. AMD's 2018 count (40,430) is inflated by the Spectre/Meltdown CPU-vulnerability story cycle, which named Intel/AMD in thousands of near-identical syndicated and translated articles across small regional outlets -- a single real event, counted many times. A repeat offender in the pilot data: `thesivertimes.com` posts multiple near-duplicate NVDA articles within the same 15-30 minute window. This is a known GKG characteristic (it indexes every crawled copy, not deduplicated "stories") -- any downstream feature built on raw daily counts should dedupe/downweight by source diversity (GDELT's own `NumSources`/`NumArticles`-style aggregation, already used in `event_news_gdelt`) rather than trust raw row counts as "how much real news happened."
2. **AAPL's bare-name false-positive rate is large if you're not careful.** An early test using bare `"apple"`/`"google"` as match patterns returned **12.2M rows in 2 years** (`"Big Apple"`, `"apple pie"`, unrelated `"Apple Daily"` newspaper, etc.) vs. **417K** with precise legal-name matching (`"apple inc"`, `"alphabet inc"`, `"google llc"`). The shipped script only uses precise legal names -- documented in `TICKER_ORG_PATTERNS` in `scripts/backfill_news_real.py`. Even so, some residual noise is expected (any org-name substring match is a heuristic, not a verified tag).
3. **No real headline text.** `_headline_from_url()` reconstructs a best-effort label from the URL slug (works ~54% of the time in the pilot; the rest are `null`). Sample of ones that DID resolve, pulled from the actual pilot Parquet:
   - `META 2018-09-28 18:30 UTC | facebook reveals security incident affecting 50m users | ewn.co.za | tone=-3.01`
   - `GOOGL 2018-08-28 11:30 UTC | trump accuses google rigging search results against him | bostonglobe.com | tone=-4.46`
   - `META 2019-04-08 14:00 UTC | taming techs wild west imposes a cost worth paying | washingtonpost.com | tone=-1.01`
   - `AAPL 2019-09-12 13:45 UTC | us apple iphone cameras | reuters.com | tone=1.06` (Reuters wire slugs are terse -- lower-quality label, still timestamp-accurate)
   These are recognizably real events, but the label is an approximation, not verified headline text -- flag any UI/report that surfaces it as "reconstructed from URL, not the original headline."
4. **Top sources in the pilot** are legitimate, recognizable outlets (`reuters.com`, `marketwatch.com`, `bloomberg.com`, `nasdaq.com`, `yahoo.com`, `investorplace.com`, `siliconangle.com`, `thestreet.com`, plus wire-aggregator sites like `4-traders.com`/`openpr.com` that carry a lot of the syndicated volume) -- consistent with genuine financial-press coverage, not junk domains.

---

## 4. Recommended `news_events_real` schema

For the rest of the system (feature engineering, experiment 3's gate/signal) to consume, independent of which source(s) eventually populate it:

```sql
CREATE TABLE news_events_real (
    event_id        VARCHAR NOT NULL,       -- source-native id (GKG's GKGRECORDID, or a hash for other sources)
    symbol          VARCHAR NOT NULL,       -- one of the 9-ticker universe; one row per (article, matched symbol)
    event_time      TIMESTAMPTZ NOT NULL,   -- best-available real-world timestamp for the article
    available_ts    TIMESTAMPTZ NOT NULL,   -- CLAUDE.md Sec.2 rule: actual time this system could have known
                                             -- about the item, NOT the (possibly earlier/unknowable) true
                                             -- publish time. For the GKG pilot, event_time == available_ts
                                             -- (both set to GDELT's 15-min ingestion-batch timestamp -- the
                                             -- earliest verifiable "this existed" signal available for free).
    headline        VARCHAR,                -- best-effort label; NULL-able; NOT guaranteed to be the true
                                             -- headline text (see Sec.3 caveat #3) -- downstream consumers
                                             -- must not treat this as ground truth for e.g. NLP re-scoring.
    url             VARCHAR,                -- source article URL (GKG's DocumentIdentifier)
    source          VARCHAR,                -- publisher domain
    sentiment_score DOUBLE,                 -- normalized ~[-1, 1] tone (GDELT avg tone / 100 for this source)
    raw_tone        DOUBLE,                 -- source-native tone scale, kept alongside the normalized one so
                                             -- a future source with a different native scale doesn't force a
                                             -- lossy re-derivation of sentiment_score from an already-lossy value
    importance      DOUBLE,                 -- NOT populated by this pilot (left NULL) -- needs a real
                                             -- weighting scheme (e.g. source-diversity count, mention volume)
                                             -- to be meaningful; deliberately not fabricated here
    provenance      VARCHAR NOT NULL,       -- e.g. 'gdelt_gkg', 'finnhub', 'gdelt_doc2' -- so a future mixed-
                                             -- source table can be filtered/audited per source honestly
    ingested_at     TIMESTAMPTZ NOT NULL,   -- when THIS pipeline run wrote the row (audit trail, distinct
                                             -- from available_ts's "when the world could have known")
    PRIMARY KEY (event_id, symbol, provenance)
);
```

Notes:
- `importance` is deliberately left unpopulated by this pilot rather than faking a formula -- CLAUDE.md Sec.11's attribution engine is the natural place to derive a real weighting (e.g. cross-referencing `NumMentions`/`NumSources` the way `event_news_gdelt` already does) once experiment 3's actual predictive value is being measured.
- The pilot Parquet's actual columns (`symbol, event_time, available_ts, headline, url, source, sentiment_score, raw_tone, gkg_record_id`) map onto this schema directly; `provenance='gdelt_gkg'` and `event_id=gkg_record_id` on load.
- Given the syndication-duplication caveat (Sec.3 #1), whoever builds the feature-engineering layer on top of this should decide explicitly whether to dedupe near-identical rows (same symbol, same day, same/near-identical tone, different `source`) or to keep them and let `NumSources`-style diversity become part of the signal -- a modeling choice, not something this ingestion script should silently decide.

---

## 5. Feasibility verdict

| Question | Answer |
|---|---|
| Can we get REAL, per-ticker news/sentiment for 2018-2026 for free? | **Yes** -- GDELT GKG via BigQuery, ~357 GB for the whole range in one query, inside the 1 TB/month free tier. |
| Does free = high quality / low noise? | **No** -- expect several hundred thousand rows/ticker over 8y, meaningfully inflated by syndication/translation duplication and no real headline text. Fine for a tone/volume signal; not fine as a substitute for a curated headline feed. |
| Is a paid feed needed? | Only if the deliverable specifically requires clean, deduplicated, human-readable headlines at scale (e.g. Finnhub premium, Benzinga, RavenPack) -- not needed just to get 8y of coverage. |
| Was the full 8y range actually downloaded in this pass? | **No** -- pilot scope was 2018-2019 (2yr) per the task; full range is dry-run cost-verified but not yet executed. One command away (`scripts/backfill_news_real.py --start 2018-01-01 --end 2026-07-13`). |
