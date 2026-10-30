# AI Rule Maintainer Spec (L1 now, L2 written but disabled)

Status: approved by owner 2026-10-30 (grilling session). Implementation is split into GitHub issues labelled `rule-maintainer`; each issue links back to a section here. Companion specs: `DASHBOARD_SPEC.md` (Rule Lab screen, section 6.8), `EVIDENCE_SPEC.md` (where the evidence comes from), `GROK_RELIABILITY_SPEC.md`.

## 1. Goal

Today the learning loop stops at a draft: `learn/hypotheses.py` records weekly symptoms, `learn/h_reports.py` writes `DRAFT INSUFFICIENT` rows into `param_changes`, and nothing moves until the owner edits a parameter by hand in the console. Every hypothesis so far (H1, H1b, H2, H5, H5b) is stuck because each side needs 30 closed trades and has 0 to 4.

Build a weekly AI researcher that:

1. reads the week's losses, postmortems, hypotheses, scoreboard and shadow books,
2. proposes parameter changes and new skip/shrink filters,
3. tests each proposal against pre-registered evidence (backtest, holdout, random-entry control, then forward shadow),
4. sends the owner one weekly digest with one-tap approve/reject,
5. applies approved changes to **paper only**, watches them, and auto-reverts on a tripwire.

The AI never writes code that runs, never touches risk limits, and never places or sizes orders.

## 2. Owner decisions (locked)

| # | Decision |
|---|---|
| R1 | AI = Claude on the owner's subscription via headless `claude -p` (no API credits). |
| R2 | Approval: Streamlit console **and** email. Email is one tap: mailto links pre-fill `APPROVE P-<id> <nonce>` / `REJECT P-<id> <nonce>`. The web dashboard stays read-only. |
| R3 | Evidence: pre-registered hypothesis, in-sample backtest, holdout (most recent 12 months, scored once per hypothesis), must beat the random-entry control percentile, then forward shadow alongside the incumbent. |
| R4 | Owner sees a proposal once backtest + holdout + control pass. It goes live on paper only after shadow agrees, or immediately on paper if the owner approves with `APPROVE-NOW` (shadow keeps running and the tripwire still applies). |
| R5 | Scope: parameters inside declared bounds, plus new **declarative** filters that can only SKIP or SHRINK an entry. No AI-written code executes. |
| R6 | Risk limits are not AI-tunable: everything in `profiles.py` (`risk_per_trade`, `max_positions`, `max_per_sector`, `max_position_pct`, `max_order_notional_pct`, `max_crypto_exposure_pct`), drawdown kill switch, `DESK_OPTIONS_ORDERS`, `ALPACA_ENV`, live flags, options `max_premium_per_trade`, `sleeve_pct`, `max_open_positions`, `force_flat`. |
| R7 | Cadence: weekly, Saturday after the Friday learn job. Unlimited proposals, but the pass bar is deflated by the number of variants tried (section 7). Replaces EDGE_PLAN "max 3 hypotheses at once". |
| R8 | Rollback: automatic on tripwire (section 10), with an email to the owner. |
| R9 | Paper only. Carrying an approved change to live is part of the owner's explicit live decision, not this system. |
| R10 | L2 (auto-apply without owner) is specified in section 12 and ships **disabled**. L3 is not planned. |

## 3. Architecture

```
Fri  desk_learn.py (existing)  -> postmortems, weekly_hypotheses, scoreboard, shadow books
Sat 07:00 local  launchd: desk_maintainer.py
   1. build research pack (read-only SQL export -> JSON files in the worktree)
   2. claude -p  (strict allowlist)  -> proposals.json (schema-validated)
   3. for each proposal: register -> backtest -> holdout -> control -> verdict   (pure Python, no AI)
   4. passing proposals -> status 'awaiting_owner'; start shadow arm (EVIDENCE_SPEC section 4)
   5. weekly digest email to owner (all proposals, variants tried, verdicts, approve links)
Any time  bridge job: owner APPROVE/REJECT mail -> proposal status
          console: approve/reject buttons
Daily postclose: tripwire check for every 'applied' proposal -> auto-revert if tripped
```

The AI is used in exactly one step (2). Steps 3 to 5 are deterministic code, so a hallucinated claim in the AI output cannot pass a gate.

## 4. Running Claude headless on the subscription

Verified against Claude Code 2.1.314 `claude --help`: `-p/--print`, `--output-format`, `--allowedTools`, `--disallowedTools`, `--permission-mode` (choices: acceptEdits, auto, bypassPermissions, manual, dontAsk, plan), `--model`, `--append-system-prompt`, `--add-dir`, `--max-budget-usd`.

- Do **not** use `--bare`: it only reads `ANTHROPIC_API_KEY` / `apiKeyHelper` and ignores the subscription OAuth login.
- Work dir: a dedicated git worktree `../stockmoney-maintainer` on branch `maintainer/work`, reset to `main` at the start of each run. The research pack is written into `maintainer_run/<week>/` inside it. Claude never sees `.env` or `data/desk/desk.sqlite` (the pack is a JSON export).
- Invocation (exact flags to be confirmed by the smoke test in 4.1):

```
claude -p "$(cat prompts/maintainer.md)" \
  --output-format json \
  --permission-mode dontAsk \
  --allowedTools "Read,Grep,Glob,Write(maintainer_run/**)" \
  --disallowedTools "Bash,WebFetch,WebSearch,Edit" \
  --model <owner-chosen, default opus> \
  --append-system-prompt "$(cat prompts/maintainer_rules.md)"
```

- Output contract: Claude writes `maintainer_run/<week>/proposals.json`; the job validates it with a pydantic model (section 6). Invalid file = no proposals this week + owner push "maintainer output invalid".
- Timeout 30 min (wrapper kills the process). Exit code and stderr tail are logged to the trace layer (`DASHBOARD_SPEC` section 4) as job `maintainer`, label `AI`.
- The prompt says what Claude may propose (registry params, filter DSL), what it may not (risk limits, code), and that every proposal must cite the pack rows it is based on (`evidence_refs`).
- This satisfies CLAUDE.md section 13: the model runs in Anthropic's cloud; nothing thinks locally.

### 4.1 Smoke test (first issue, blocks everything else)

`scripts/desk_maintainer.py --smoke` run from launchd (not from a terminal) calls `claude -p "reply with the word ok" --output-format json` and records whether it authenticated. Known risk: launchd jobs may not see the login keychain entry that holds the OAuth token. If it fails, the fallback is to run the job from a user LaunchAgent with `SessionCreate`/`LimitLoadToSessionType Aqua`, or a long-lived token from `claude setup-token` stored by the owner in the keychain. The owner sets any token; Claude/Codex never handle it.

## 5. Parameter registry

New module `src/stockmoney/desk/learn/registry.py`. One entry per tunable parameter:

```python
@dataclass(frozen=True)
class Tunable:
    key: str            # "pullback.trail_atr", "options.orb15.min_gap_pct", "filter.*" not here
    sleeve: str         # stocks | options | spreads | crypto
    location: str       # "strategy_params" (store.change_param) | "options_params" | ...
    bound: Bound        # reuse strategies.Bound
    max_step: float     # largest change per proposal, e.g. 0.5 for trail_atr
    ai_tunable: bool = True
```

- Built from the existing `Strategy.bounds` dicts in `strategies.py`, plus explicit entries for options/spreads knobs that already exist in `options/config.py` (entry window, playbook thresholds). Risk-limit fields listed in R6 are registered with `ai_tunable=False` so the registry is the single list the owner can read.
- Current value = default overridden by `store.strategy_params(con)` (stocks) or the matching store for other sleeves. Options/spreads knobs that are today only dataclass defaults get a settings-backed override in the same style as `store.change_param`; this is a prerequisite slice.
- `python scripts/desk_maintainer.py --registry` prints the table (key, sleeve, current, bound, max_step, ai_tunable). The research pack includes it.

## 6. Proposal format and filter DSL

### 6.1 Proposal (pydantic, strict)

```json
{
  "id_hint": "trail-35",
  "sleeve": "stocks",
  "kind": "param",                       // param | filter
  "param": {"key": "pullback.trail_atr", "new": 3.5},
  "filter": null,
  "hypothesis": "Wider trail reduces stop-outs that recover within 10 days ...",
  "expected_effect": "higher avg R per trade, max DD no worse",
  "evidence_refs": ["postmortem:412", "weekly_hypotheses:2026-W43:stopped_then_recovered"],
  "variant_of": null
}
```

Validation: `key` must exist in the registry with `ai_tunable=True`; `new` inside the bound and within `max_step` of current; at most one change per proposal (EDGE_PLAN "one change at a time" still holds per proposal).

### 6.2 Filter DSL

A filter is data, evaluated by a small interpreter in `src/stockmoney/desk/learn/filters.py`:

```json
{"name": "earn5_headwind", "sleeve": "stocks", "strategies": ["breakout", "pullback"],
 "when": {"all": [{"field": "days_to_earnings", "op": "<", "value": 5},
                  {"field": "wind", "op": "==", "value": "headwind"}]},
 "action": "skip"}                        // skip | shrink:0.5 (factor in [0.25, 0.75])
```

- Whitelisted fields only, each with a type: `days_to_earnings`, `wind`, `regime`, `atr_pct`, `rs_rank`, `vol_ratio`, `gap_pct`, `sector`, `vix`, `spy_above_200d`, `grok_verdict`, `minutes_since_open` (options), `iv_rank` (options). The list lives in code; adding a field is a code change by Codex, reviewed by the owner.
- Operators: `== != < <= > >= in not_in`, combinators `all`, `any`, `not`. Max depth 3, max 6 leaves.
- Missing field value = filter does not fire (fail open for the filter, so a filter can never block everything by accident; the risk layer is unchanged).
- Actions can only make an entry smaller or absent. The interpreter returns `(skip: bool, size_factor: float <= 1.0)`; the entry path multiplies size and still calls `RiskDecision.allow` afterwards. There is no action that adds, enlarges or forces a trade.
- Active filters live in a new `rule_filters` table (id, proposal_id, json, status, applied_at, reverted_at). The entry path loads `status='active'` rows for the current account kind (paper only, R9).

## 7. Evidence pipeline (deterministic)

Per proposal, in order. A failed step stops the pipeline and records the reason.

1. **Register**: write the proposal row (section 8) with `registered_at` before any test runs. Hash of the proposal JSON is stored; the test result is only valid for that hash.
2. **In-sample backtest**: `desk_backtest.py` (stocks) or replay (options/spreads, `EVIDENCE_SPEC` section 3) on history up to 12 months ago. Pass = better avg R per trade than incumbent and max drawdown not worse by more than 10% relative, with at least 30 trades on each side.
3. **Holdout**: the most recent 12 months, scored **once** per proposal hash. A second run on the same hash returns the stored result. Re-proposing the same change with a cosmetic tweak counts as a new variant (step 5), not a free retry.
4. **Random-entry control**: the variant must sit at or above the 90th percentile of `learn/controls.py` random-entry runs on the holdout (EDGE_PLAN 1.2 method).
5. **Deflation**: every variant ever tested is logged in `proposal_variants` (proposal family = same `param.key` or same filter fields). Required holdout margin grows with the family's variant count N: `required_t = 1.65 + 0.35 * ln(N)` on the per-trade return difference (t-stat of the mean difference). Shown as `variants tried: N` in the digest. The constants are owner-tunable settings, not AI-tunable.
6. Pass of 2 to 5 -> `awaiting_owner`; start a shadow arm on live data (`EVIDENCE_SPEC` section 4).
7. **Forward shadow agreement**: after `shadow_min_trades` (default 20) closed trades per arm or 20 trading days, the arm's avg R per trade is not worse than the incumbent arm's by more than `shadow_margin` (default 0.1 R). Until then the proposal shows "shadow pending".

## 8. Storage

New table `proposals` (keeps `param_changes` as the audit log of applied values):

```
proposals(
  id INTEGER PK, week TEXT, sleeve TEXT, kind TEXT, family TEXT, body_json TEXT, body_hash TEXT,
  status TEXT,   -- registered | rejected_by_test | awaiting_owner | approved | applied | reverted | rejected_by_owner | expired
  stage_results_json TEXT, variants_in_family INTEGER,
  nonce TEXT, nonce_expires_at TEXT, decided_by TEXT, decided_at TEXT, decided_via TEXT,  -- console | email
  applied_at TEXT, reverted_at TEXT, revert_reason TEXT, created_at TEXT)
proposal_variants(id, family, body_hash, holdout_result_json, tested_at)
```

- Applying writes through the existing writers (`store.change_param` for stocks; the new settings-backed override for options/spreads; `rule_filters` for filters) and therefore lands in `param_changes` with `reason = "maintainer P-<id> approved via <console|email>"`.
- Existing DRAFT rows from `h_reports.record_draft` are imported once as `proposals` with status `registered` so H1/H2/H5 enter the same pipeline.
- Sync: `proposals`, `proposal_variants`, `rule_filters` are added to the Supabase sync (`DASHBOARD_SPEC` section 5) for the Rule Lab screen.

## 9. Approval

### 9.1 Weekly digest email

Sent Saturday after the run, from the desk mailbox to `DESK_OWNER_ADDRESS` (new env var, set by the owner). One email per week, all proposals:

```
Subject: [SMD/DIGEST/2026-W44] 3 to review, 7 rejected by tests
P-31  stocks  pullback.trail_atr 3.0 -> 3.5
      backtest +0.18R/trade, holdout +0.11R (t=2.4, need 2.2, variants tried: 4), control pct 93
      shadow: pending (6/20 trades)
      [Approve]  [Approve now]  [Reject]
```

Each button is a `mailto:` link to the desk address with subject `[SMD/OWNER/P-31]` and body `APPROVE P-31 k3f9` (or `APPROVE-NOW` / `REJECT`). Nonce: 4+ base32 chars per proposal, single use, expires after 14 days (status then `expired`). The rejected-by-test list is included in short form so the owner sees what the AI tried.

### 9.2 Bridge parsing

New sender class in `bridge/inbox.py`: `from_owner = sender == DESK_OWNER_ADDRESS` with DKIM pass for that domain (`mailbox.dkim_problem`). Only type `OWNER` is allowed for the owner; body must match `^(APPROVE|APPROVE-NOW|REJECT) P-(\d+) ([a-z2-7]{4,})$` (first non-empty line). Checks: proposal exists, status is `awaiting_owner`, nonce matches (constant-time), not expired, not used. Anything else goes to `bridge_rejects` with a reason and the owner gets a push. A valid decision gets a confirmation reply email and a push.

### 9.3 Console

`app/desk_app.py` gets a "Proposals" tab: same content as the digest, Approve / Approve now / Reject buttons, calling the same `maintainer.decide(con, proposal_id, action, via="console")` function the bridge uses.

## 10. Tripwire and auto-revert

- On apply, the incumbent keeps running as a shadow arm (`shadow:incumbent:P-<id>`).
- Daily postclose check for each `applied` proposal over a window of `tripwire_trades` (default 15 closed trades) or 20 trading days, whichever first:
  - revert if new arm avg R per trade < incumbent arm avg R - `tripwire_margin` (default 0.15 R), or
  - revert if new arm max drawdown > incumbent max drawdown x 1.25.
- Revert = write the prior value back through the same writer (`param_changes` reason `maintainer P-<id> auto-revert: <reason>`), set filter `status='reverted'`, proposal `reverted`, email + push to owner.
- After the window passes without a trip, the proposal is `applied` + `survived_at` set; this counts toward the L2 gate.
- All tripwire constants are owner settings, not AI-tunable.

## 11. EDGE_PLAN amendment

Replace in EDGE_PLAN 2.5 the rule "at most 3 hypotheses at once" with: "Unlimited proposals per week. Each family's holdout bar tightens with the number of variants tried (`RULE_MAINTAINER_SPEC.md` section 7.5); every variant is logged; one weekly digest." Add a pointer from 2.2 to this spec. This edit is part of the first slice so the docs never disagree with the code.

## 12. L2: auto-apply (written now, ships disabled)

L2 lets a proposal that passed every stage including forward shadow apply to paper without waiting for the owner. It is off unless all of these hold, checked per sleeve at run time:

1. the sleeve has 50+ closed paper trades,
2. at least 3 owner-approved L1 changes in that sleeve survived their tripwire window without auto-revert,
3. the owner has amended CLAUDE.md to allow L2 for that sleeve, **and** set `DESK_MAINTAINER_L2=<sleeve list>` in `.env`.

Code reads the env flag; if absent or the counts are not met, L2 code paths are unreachable and the proposal waits for the owner as in L1. Even under L2: paper only, risk limits untouchable, tripwire active, digest still lists every auto-applied change with a one-tap `REVERT P-<id>` link. Tests must cover "flag set but counts not met -> no auto-apply".

## 13. Testing

- Unit: registry built from `Strategy.bounds`; proposal validation (unknown key, risk key, out of bound, step too large); DSL interpreter (each op, depth/leaf caps, missing field fails open, only skip/shrink, factor range); deflation formula; nonce single-use and expiry; owner mail parsing (wrong sender, DKIM fail, bad nonce, wrong status, replay).
- Integration: fake `claude` binary (script on PATH printing a fixed proposals.json) drives a full weekly run on a fixture DB: register -> backtest stub -> holdout stored once -> digest built -> approve via console -> `param_changes` row -> tripwire trip -> revert row.
- Invariant test: for every filter action the entry path still calls `RiskDecision.allow` and size never increases.
- L2 disabled test (section 12).

## 14. Delivery order (issues)

1. Smoke test of `claude -p` under launchd (4.1). Blocks 6.
2. Parameter registry + settings-backed overrides for options/spreads knobs (5).
3. Filter DSL interpreter + `rule_filters` + entry-path hook (6.2).
4. `proposals` / `proposal_variants` storage + DRAFT import + EDGE_PLAN amendment (8, 11).
5. Evidence pipeline: register, backtest, holdout-once, control, deflation (7.1 to 7.5).
6. Weekly job: research pack export, `claude -p` call, output validation, launchd plist (3, 4).
7. Digest email + owner `OWNER` channel in the bridge + console tab (9).
8. Apply + shadow incumbent + tripwire auto-revert (10).
9. L2 gate code, disabled by default (12).
