# Desk Mission Control: Web Dashboard Spec

Status: approved by owner 2026-10-30 (grilling session). Implementation is split into GitHub issues labelled `dashboard`; each issue links back to a section here. This file is the design; the issues are the work queue.

## 1. Goal

The desk runs about ten launchd jobs on the owner's Mac and writes everything to `data/desk/desk.sqlite`. From the outside it is a black box: the owner cannot see during the day what the system is doing, why it took or skipped a trade, what Grok/Muse sent, or what it learned.

Build a private, read-only web dashboard on Vercel that unboxes the desk:

- what is running right now and what each job decided,
- how every idea travels from source to gates to broker to result,
- balance, positions and risk usage,
- what the system learned (postmortems, hypotheses, scoreboard, rule proposals).

Non-goals for v1: controlling the desk from the web (Streamlit `app/desk_app.py` stays the local control panel), public access, multi-user login, tick-level streaming, AI rule maintenance (that is spec #2, see section 11).

## 2. Owner decisions (locked)

| # | Decision |
|---|---|
| D1 | Data path: the Mac pushes to a new free-tier Supabase project; Vercel reads from Supabase. |
| D2 | Read-only. No write path from the web to the desk or to the broker. |
| D3 | Private. v1 = one shared password checked in Next.js middleware. Real login is a later add-on. |
| D4 | The browser never holds a Supabase key. Next.js server routes read Supabase with the service key (Vercel env only); the page polls those routes every 15 s. No Supabase Realtime in v1. |
| D5 | Freshness: push at the end of every job, plus a heartbeat and new events every minute from the bridge job. |
| D6 | Push everything except secrets, including full Grok/Muse message bodies. |
| D7 | Retention: raw `events` pruned after 90 days; daily rollups kept forever. |
| D8 | Honesty: every decision is labelled `RULE`, `AI`, `HUMAN` or `BROKER`. No fake "AI is thinking" animation for a rule. |
| D9 | Visual style: dark sci-fi mission control (neon graph, glow, motion) for the System Map, Bloomberg-terminal restraint (dense, monospace, amber/green) for numbers and tables. |
| D10 | Mobile is a first-class target (the owner checks it during the day). |
| D11 | Language: content stays as stored (mostly Chinese); UI chrome in English. Toggle later. |
| D12 | Fresh Next.js app in `web/`. The July Vite cockpit in `frontend/` is archived, not migrated. |
| D13 | The Mac is always on. The site still shows `DESK OFFLINE since HH:MM` when the heartbeat is older than 3 minutes on a market day (5 minutes otherwise). |
| D14 | The reconcile "two stop orders" bug (section 10) ships in the same issue set. |

## 3. Architecture

```
Mac (launchd jobs)                         Supabase (Postgres)            Vercel (Next.js, web/)
 desk_job.sh <job>                          mirrored tables                 middleware: password cookie
   trace start  ──▶ events (sqlite)         events, heartbeat     ◀── read ── /api/* server routes (service key)
   python job   ──▶ events + desk tables    ...                               │
   trace end    ──▶ events                                                    ▼
   sync push    ──────── HTTPS (PostgREST upsert) ──▶                       pages poll /api every 15 s
 bridge (every 60 s) ── heartbeat + events push ──▶
```

- Sync uses `requests` (already a dependency) against Supabase PostgREST with the service-role key. No new Python dependency.
- Sync never blocks or fails a job: it runs after the job, with a 20 s total timeout, and its own failures are logged and retried next push (watermarks only advance on success).
- The desk never reads from Supabase. Supabase is a one-way mirror.

## 4. Layer 0: the trace (`stockmoney.desk.trace`)

Nothing can be visualised that is not recorded. Today jobs write results to tables and reasons to log files; `runs` has one row.

### 4.1 Local table (in `desk.sqlite`, created by `store.connect` schema)

```sql
CREATE TABLE IF NOT EXISTS events (
  event_id   TEXT PRIMARY KEY,      -- uuid4 hex
  ts         TEXT NOT NULL,         -- UTC ISO-8601, store.now()
  run_id     TEXT NOT NULL,         -- one per job invocation: '<job>-<YYYYMMDDTHHMMSS>-<4 hex>'
  job        TEXT NOT NULL,         -- premarket|postopen|postclose|recover|bridge|crypto|learn|scanshadow|options|options-recover|intraday_shadow
  kind       TEXT NOT NULL,         -- job_start|job_end|step|decision|order|fill|message|error
  node       TEXT NOT NULL,         -- System Map node id, section 6.1
  subject    TEXT,                  -- symbol, msg_id, plan_id, client_order_id, hypothesis id
  actor      TEXT NOT NULL,         -- RULE|AI|HUMAN|BROKER|SYSTEM
  verdict    TEXT,                  -- pass|block|info|warn|fail (decision/order rows)
  rule       TEXT,                  -- short rule name, e.g. 'risk.max_position_pct', 'veto.two_sources'
  value      TEXT,                  -- observed value as text (keeps units, e.g. '12.4%')
  threshold  TEXT,                  -- limit it was compared to
  message    TEXT NOT NULL,         -- one human line, Chinese allowed
  data_json  TEXT,                  -- optional structured payload, no secrets
  trade_key  TEXT,                  -- links all events of one idea, section 4.4
  synced     INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_unsynced ON events(synced) WHERE synced = 0;
CREATE INDEX IF NOT EXISTS events_trade ON events(trade_key);

CREATE TABLE IF NOT EXISTS heartbeat (
  host TEXT PRIMARY KEY, ts TEXT NOT NULL, last_job TEXT, last_status TEXT, desk_mode TEXT);
```

### 4.2 API

```python
from stockmoney.desk import trace

with trace.job("options") as t:          # emits job_start / job_end (status ok|fail, duration_s)
    t.step("options.scan", "掃描 14 檔", node="brain.options", data={"n": 14})
    t.decision(node="gate.risk", subject="NVDA", actor="RULE", verdict="block",
               rule="risk.max_position_pct", value="12.4%", threshold="10%",
               message="單一部位超過上限", trade_key=key)
    t.order(...); t.fill(...); t.message(...); t.error(exc, node=...)
```

Rules:
- Every `trace.*` call swallows its own exceptions (logs at WARNING). A trace failure must never change job behaviour. Test this.
- `run_id` comes from env `DESK_RUN_ID` when set (desk_job.sh sets it), else generated. Nested calls in one process share it.
- `trace.job` is also callable from the shell: `python -m stockmoney.desk.trace start <job>` / `end <job> <exit_status>` so `desk_job.sh` covers every job, including `scanshadow`, uniformly.
- `message` is at most 300 chars; `data_json` at most 8 KB (truncate with a `"truncated": true` flag).
- No secrets: a unit test greps emitted rows for the values of every env var whose name contains `KEY`, `SECRET`, `TOKEN` or `PASSWORD` and fails if found.

### 4.3 Where to instrument (minimum set for v1)

| Place | Events |
|---|---|
| `scripts/desk_job.sh` | `job_start` before, `job_end` with exit status after, for every job. Sets `DESK_RUN_ID`. Bridge idle starts emit nothing (keep the current "log only when it printed" behaviour) but always update `heartbeat`. |
| `execution/risk.py` (`RiskEngine.check`) | one `decision` per check: pass/block, the rule that decided, value vs threshold. actor `RULE`. |
| `veto.py` | `decision` per veto evaluation; Jev shadow judgments as actor `AI`, verdict `info` (shadow, does not decide). |
| `bridge/inbox.py`, `bridge/policy.py` | `message` on receipt (type, sender, source), `decision` accept/reject with reason (this is where Grok plan mails get rejected for field shape). |
| `options/plan.py` (ingest), `options/select.py`, `options/engine.py` | plan accepted/rejected/duplicate; per setup: filtered (reason) or selected; entries, exits with `exit_reason`. |
| `run.py` (`_submit`, `sync_fills`, `ensure_stops`, `_send_exits`) | `order` on submit, `fill` on booked fill (price, slippage bps vs ref), stop placed/replaced/adopted. actor `BROKER` for fills. |
| `strategies.py` / `engine.py` signal generation | `decision` per candidate signal: strategy, score, thesis, layers summary. |
| `learn/job.py` | `step` per stage (postmortem, scoreboard, hypotheses, counterfactual) with counts. |
| `intraday.py`, `options/spreads.py` | `step` per tick with counts; shadow entries as `decision` verdict `info` and `data_json.shadow = true`. |

Shadow and paper must be distinguishable everywhere: `data_json.mode` in (`shadow`, `paper`, `live`).

### 4.4 `trade_key`

One key follows one idea end to end so the Trade Story (section 6.4) is a single query:
- stock signals: `signal_id`;
- options: `setup_id` (falls back to `plan_id:underlying`);
- bridge-originated ideas: the `msg_id` is in `data_json.origin_msg_id` of the first event, and the derived signal/setup id is the key.

## 5. Sync (`stockmoney.desk.sync_web`)

CLI: `python -m stockmoney.desk.sync_web push [--tables all|events] [--dry-run]`, called by `desk_job.sh` after every job (`--tables all`) and by the bridge job each minute (`--tables events`, plus heartbeat).

Env (in `.env`, set by the owner): `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `DESK_WEB_SYNC=on` (anything else = sync disabled, exit 0). Missing env = skip quietly with one INFO line.

### 5.1 Mirrored tables

Mirror these desk tables 1:1 (same columns, Postgres types: TEXT, DOUBLE PRECISION, BIGINT; JSON-in-TEXT columns stay TEXT, the API parses them):

`events`, `heartbeat`, `equity_history`, `shadow_equity`, `sleeve_positions`, `position_meta`, `fills`, `submitted`, `planned_orders`, `signals`, `decisions`, `veto_log`, `journal`, `learn_reports`, `trade_postmortem`, `weekly_hypotheses`, `param_changes`, `source_scores`, `blocked_counterfactual`, `reconcile_daily`, `risk_state`, `bridge_messages`, `bridge_rejects`, `options_plans`, `options_positions`, `options_fills`, `options_spreads_shadow`, `intraday_shadow`, `wind_daily`, `settings` (excluding any key matching the secret pattern in 4.2).

Each mirrored table gets an extra `synced_at TIMESTAMPTZ DEFAULT now()` column in Postgres.

### 5.2 Strategy

- `events`: push rows with `synced = 0` in batches of 500, upsert on `event_id`, then set `synced = 1` locally for exactly the pushed ids.
- Append-only tables with a time column (`fills`, `bridge_messages`, `bridge_rejects`, `veto_log`, `equity_history`, `shadow_equity`, `journal`, `learn_reports`): watermark per table in `settings` key `web_sync_wm:<table>`; upsert rows newer than the watermark minus 1 day (re-sends the overlap, upsert makes it idempotent).
- Mutable small tables (positions, plans, postmortems, hypotheses, param_changes, risk_state, settings, reconcile_daily, ...): full replace per push when the local row count is under 5,000: upsert all rows, then delete remote rows whose primary key is no longer local. Over 5,000 rows: fall back to watermark on `updated_at` where present and log a WARNING.
- Upsert = `POST /rest/v1/<table>` with header `Prefer: resolution=merge-duplicates` and `on_conflict=<pk columns>`.
- Pruning: a daily Supabase SQL function (`pg_cron` if available on the free tier, else run by the `learn` job via RPC) deletes `events` older than 90 days after writing `events_daily(day, job, node, kind, verdict, n)` rollups.

### 5.3 Supabase schema

`web/supabase/migrations/0001_init.sql` holds the full DDL for every mirrored table, `events_daily`, and the prune function. RLS is ENABLED on every table with NO policies, so only the service key can read (the anon key reads nothing). The owner applies it once to the new project.

## 6. Web app (`web/`)

Next.js App Router + TypeScript, deployed to Vercel. Pin exact versions in `package.json` at install time (no ranges). Libraries: `@xyflow/react` (graph), `motion` (animation), `lightweight-charts` (equity/price charts), `swr` (polling), Tailwind CSS. Verify each package's current API from its docs before use.

Server-only module `web/lib/db.ts` reads Supabase over PostgREST with `SUPABASE_SERVICE_KEY`; it imports `server-only` so a client import fails the build.

Auth: `middleware.ts` protects every route except `/login`. `POST /login` compares against `DASHBOARD_PASSWORD` (constant-time), sets an HttpOnly, Secure, SameSite=Strict cookie containing an HMAC (`DASHBOARD_SESSION_SECRET`) of an expiry 30 days out. Wrong password: 1 s delay, generic error.

Global chrome: top bar with desk mode (paper/live, from `settings`), system equity and day P&L, market clock (ET, open/closed), last heartbeat age, `DESK OFFLINE` banner (D13). Bottom nav on mobile, left rail on desktop.

### 6.1 Screen 1: System Map (`/`) - home

Node ids (fixed, used by `events.node`):

| Column | Nodes |
|---|---|
| inputs | `in.market` (prices, wind), `in.grok`, `in.muse`, `in.news` (scan/earnings/filings) |
| brains | `brain.stocks` (pullback/breakout/rotation), `brain.options` (playbooks), `brain.spreads` (shadow), `brain.intraday` (shadow) |
| gates | `gate.bridge` (message policy), `gate.veto`, `gate.risk` |
| broker | `broker.alpaca` |
| ledger | `ledger.book` (positions, fills, reconcile) |
| learning | `learn.postmortem`, `learn.scoreboard`, `learn.hypotheses`, `learn.rulelab` |

Behaviour:
- React Flow graph, left to right, with a feedback edge from learning back to brains.
- A node glows and pulses while a `job_start` without `job_end` is open for a job that maps to it (map in `web/lib/nodes.ts`).
- Each new event since the last poll animates a dot along the edge from its node to the next node in its `trade_key` path (or to the next column when there is no key).
- Edge labels show today's funnel counts, e.g. `in → brains 14`, `brains → gates 6`, `gates → broker 1 (5 blocked)`, `broker → ledger 1 fill`.
- Shadow flows draw dashed and dimmer than paper flows.
- Clicking a node opens a side drawer (bottom sheet on mobile): today's events for that node, newest first, with actor badge (RULE/AI/HUMAN/BROKER), verdict colour, value vs threshold.
- Mobile (< 768 px): the graph becomes a vertical stack of the six columns, each a card with its counts and a pulsing dot when active; tap opens the same drawer.
- A live ticker under the map: last 20 events, one line each.

### 6.2 Screen 2: Day Timeline (`/timeline?day=YYYY-MM-DD`)

- Horizontal ET clock 08:30 to 18:00 (vertical on mobile). One lane per job.
- Expected slots come from the launchd schedule. Generate `web/lib/schedule.json` from `scripts/desk_schedule.py` (add a `--json` output there if it has none) and commit it; a test fails if it drifts from the plists in `data/desk/launchd/`.
- Each slot: ran ok (green), ran late more than 2 min (amber, shows the delay), failed (red), missing (grey outline), running (pulse). Bridge shows as a thin density strip, not 390 slots.
- Click a slot: the run's full event list (by `run_id`).
- Day picker; default today (ET).

### 6.3 Screen 3: Portfolio (`/portfolio`)

- Equity curve (`equity_history.system_equity`, with `broker_equity` as a thin second line), drawdown band from peak, shadow strategies from `shadow_equity` as toggleable overlays, SPY baseline when present.
- Sleeve breakdown: stocks, options, crypto (exposure and realised P&L).
- Positions table: symbol, strategy, qty, avg price, last price (from latest event/fill data; no live quote fetch in v1), stop, distance to stop in %, days held, link to its Trade Story.
- Risk gauges: drawdown vs limit, total exposure vs regime cap, per-position max vs limit, from `risk_state` and `settings`. Amber at 80% of a limit, red at 100%.
- Reconcile strip: last 10 `reconcile_daily` rows, zero-diff streak.

### 6.4 Screen 4: Trade Story (`/trade/[key]`, plus list at `/trades`)

A vertical story for one `trade_key`:
1. Origin: who proposed it (rule strategy, Grok msg, Muse msg), thesis, invalidation, score.
2. Each gate as a row: actor badge, rule, value vs threshold, pass/block.
3. Order(s): submitted time, type, qty, stop.
4. Fill(s): price, slippage bps vs ref.
5. Management: stop moves, partial exits.
6. Exit: reason, P&L, R multiple.
7. Postmortem: symptoms, lesson (`trade_postmortem`).

Blocked ideas get a story too, ending in the counterfactual from `blocked_counterfactual` ("blocked by veto; would have made +4.1% in 5 days").

`/trades`: filterable list (open, closed, blocked, shadow; by strategy; by source).

For trades closed before the trace existed, build the story from the existing tables (`signals`, `planned_orders`, `submitted`, `fills`, `trade_postmortem`) and label it `reconstructed`.

### 6.5 Screen 5: Agent Inbox (`/inbox`)

- One feed of `bridge_messages` and `bridge_rejects`, newest first. Filter by sender (Grok, Muse, other) and type (`GROK_DAILY`, `OPTIONS_PLAN`, `SOCIAL_DAILY`, `CALLS`, `ALERT`, `IDEA`).
- Each card: type, sender, verified badge (`grok_verified`), received time, stamp ACCEPTED / REJECTED / PARTIAL with the reason; `dropped_json` fields shown as "dropped: ...".
- Expand: parsed fields and raw body (monospace).
- Linked outcomes: trades whose `data_json.origin_msg_id` matches.
- Per-source stats header from `source_scores`.

### 6.6 Screen 6: Learning Lab (`/learn`)

- Mistakes → lessons: `trade_postmortem` grouped by symptom, with loss totals and lesson text.
- Hypotheses: `weekly_hypotheses` cards with status (candidate → backtest → shadow → approved/rejected).
- Rule Lab: `param_changes` timeline including DRAFT records (H1 wide stop, H2 no-entry-before-earnings, allocator), each with its evidence report (from `learn_reports`) and status. Read-only view of the existing L1 pipeline.
- Scoreboard: `source_scores` per source and metric over windows; rules vs random-entry controls from `shadow_equity` (EDGE_PLAN gate tracking).
- Daily journal reader: `journal` rendered as markdown, day picker.

### 6.7 Visual system

- Dark only in v1. Tokens in `web/app/globals.css`: background near-black blue, panel slightly lighter, neon accent cyan for flow, amber for warnings and numbers in terminal tables, green pass, red block, violet for AI actor.
- Numbers and tables: monospace, tabular figures, right-aligned, dense rows.
- Motion: purposeful only (an event arriving, a job running). Respect `prefers-reduced-motion` (no dots, no pulse; static highlights instead).
- Every screen must have an empty state ("no events yet today; next job: options at 09:00 ET") and an error state that does not hide stale data (show data age).

### 6.8 API routes (server, all GET, all behind auth)

`/api/status` (heartbeat, mode, equity, open runs), `/api/events?since=<ts>&node=&run_id=&day=`, `/api/map?day=` (funnel counts per edge, active nodes), `/api/timeline?day=`, `/api/portfolio`, `/api/trades?filter=`, `/api/trade/[key]`, `/api/inbox?sender=&type=&before=`, `/api/learn`. Responses are JSON with a `generated_at` field and `Cache-Control: no-store`.

## 7. Testing and acceptance

Python (`pytest`):
- trace never raises; trace off when the DB is locked still lets the job finish; run_id propagation; secret scan test.
- sync: watermark advance only on success; idempotent re-push; full-replace deletes stale remote rows; `--dry-run` sends nothing; disabled when `DESK_WEB_SYNC` is not `on`. Use a fake HTTP session, never the network.
- instrumentation: for each instrumented module, one test asserting the expected event rows for a known scenario (reuse the existing fake broker in `execution/broker.py`).

Web (`vitest` + Testing Library, and `next build` in CI):
- middleware rejects without cookie, accepts a valid HMAC, rejects an expired one.
- `lib/db.ts` cannot be imported from a client component (build fails).
- each screen renders with fixture JSON, an empty fixture, and an error.
- funnel count and node-activity logic unit-tested.

Acceptance for v1 (owner checks on the phone):
1. During a market day the System Map shows the options session node active and events arriving within 2 minutes of happening.
2. The Timeline for 2026-10-30 onward shows every scheduled job slot with the right status.
3. Any closed trade opens a Trade Story from origin to postmortem.
4. A rejected Grok `OPTIONS_PLAN` is visible in the Inbox with its rejection reason.
5. Visiting the site without the password shows only `/login`; the Supabase anon key reads zero rows from every table.

## 8. Ops

- Owner does once: create the Supabase project, apply `0001_init.sql`, put `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` / `DESK_WEB_SYNC=on` in `.env`; create the Vercel project with root `web/`, env `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `DASHBOARD_PASSWORD`, `DASHBOARD_SESSION_SECRET`. Claude/Codex never handle these values.
- `docs/desk/OPERATING_PLAN.md` gets a short "Web dashboard" section pointing here (no parallel status doc).
- Free-tier budget: expect a few thousand events per day; with 90-day pruning the DB stays well under 500 MB. The sync logs its row counts so growth is visible.

## 9. Delivery order (GitHub issues)

1. Fix reconcile duplicate-stop false positive and stray-stop cleanup (section 10). Independent; do first.
2. Trace module, `events`/`heartbeat` tables, `desk_job.sh` start/end, heartbeat from bridge.
3. Instrument decisions (section 4.3).
4. Supabase schema + `sync_web` + pruning.
5. `web/` scaffold: Next.js, auth middleware, `lib/db.ts`, `/api/status`, global chrome, offline banner, Vercel config.
6. System Map.
7. Day Timeline (+ schedule JSON).
8. Portfolio.
9. Trade Story (+ reconstructed stories).
10. Agent Inbox.
11. Learning Lab + Rule Lab.
12. Archive `frontend/` (move to `archive/frontend-2026-07/`, remove from any scripts/CI), OPERATING_PLAN pointer.

Issues 2 → 3 → 4 are sequential. 5 can start in parallel with 2-4 using fixture JSON. 6-11 depend on 5 and can run in parallel with each other once 4 has real data.

Every non-trivial PR: tests pass, `/grok-build:review` passes (CLAUDE.md AI Collaboration rules), evidence (test output, screenshot for UI) in the PR body.

## 10. Bug: reconcile reports "券商同時有 2 張停損單" for 7 days

Evidence (2026-10-30):
- `reconcile_daily` flagged QCOM and TMO with 2 broker stops on 10-23 .. 10-29.
- Alpaca order history (read-only check) shows a clean chain: each postclose cancels the old stop (`canceled_at` 20:45:08.2) and creates one new stop (20:45:08.4-.8). At no point were two stops live. The local `submitted` table agrees (exactly one `new` stop per symbol).
- `_reconcile_summary` (`run.py:799`) reads `broker.open_orders()` about one second after `ensure_stops` replaced the stops; `reconcile.summarize` (`execution/reconcile.py:166-180`) counts every SELL STOP/STOP_LIMIT in that list without looking at status.

Likely cause: the open-orders read races with the just-cancelled order (still listed, or listed as `pending_cancel`), so reconcile counts the old stop and the new one. This is a false alarm, but it trains the owner to ignore reconcile alerts.

Second, real gap found while reading: `_ensure_stop` (`run.py:591-592`) returns early when the tracked stop matches, so it never cancels stray extra stops for that symbol. If a real duplicate ever appears, nothing cleans it up.

Fix:
1. In `reconcile.summarize`, ignore orders in `PENDING_CANCEL` or terminal status. For a symbol that still shows more than one stop, re-read each one with `broker.get_order` before flagging; only flag if two or more are still working.
2. In `_ensure_stop`, when `cur` matches, still cancel (and wait for) any `strays` for the symbol; emit an event per cancelled stray.
3. Tests with the fake broker: (a) pending-cancel old stop + new stop → no diff; (b) two genuinely live stops → diff flagged; (c) matching tracked stop + one stray → stray cancelled, tracked one kept.
4. Confirm on the next postclose that `reconcile_daily.n_stop_diff` is 0 for QCOM/TMO-like replacements.

## 11. Out of scope: spec #2, AI rule maintenance

Owner direction: rules should be maintained by AI so the desk adapts to the market. Agreed path:
- L1 (next): the AI runs the existing hypothesis pipeline itself (hypothesis → backtest → shadow → `param_changes` DRAFT with evidence); the owner approves with one click.
- L2 (later, after an evidence threshold such as 50+ closed trades in the sleeve): the AI applies changes inside owner-set parameter bounds after the shadow beats the incumbent, with automatic rollback. This changes a CLAUDE.md boundary and needs the owner's explicit amendment.
- L3 (AI writes new rules): not planned.

Spec #2 is a separate grilling session. The Rule Lab screen (6.6) is built so L1/L2 proposals appear there without UI changes.
