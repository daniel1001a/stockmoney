# jev-lint rules for stockmoney

Semantic checks run by jev-lint (TypeSafe Jev) after each agent edit. Rules are in
`python.rules.json`, labeled cases in `cases.jsonl`, scoping in `config.json`.
TypeScript uses only the built-in hygiene/practices packs.

## Rules and sources

| id | what it catches | source |
|---|---|---|
| repo-order-outside-broker | direct Alpaca order calls outside the Broker interface (alpaca.py exempt via skipPaths) | CLAUDE.md section 0, PHASE0_SPEC |
| repo-live-flag-bypass | defaulting/forcing live, setting the live confirmation in code, `paper=False` | CLAUDE.md section 0 |
| repo-hardcoded-secret | literal broker/AI/mail/bridge secrets | CLAUDE.md section 13 |
| repo-bridge-raises-risk | Muse/Grok message data opening or growing risk | MUSE_BRIDGE.md |
| repo-lookahead-timestamp | event-time filters, negative shifts used as features | CLAUDE.md sections 2, 12 |
| repo-block-exit | risk checks that can block sells | PHASE0_SPEC W2 |
| repo-retry-without-client-order-id | submit retries with no client_order_id lookup or a fresh id | PHASE0_SPEC W1 |
| repo-offladder-strike | strikes computed without `snap_strike` | data-integrity-audit-2026-07-15 |
| repo-untrusted-text-to-agent | scraped/inbound text into tool-enabled prompts or executors | scanner SKILL safety rule, MUSE_BRIDGE |
| repo-test-cannot-fail | tests that cannot fail | jev-lint best practices (python) |
| repo-test-hits-live | tests reaching real services or placing orders | PHASE0_SPEC shared rules |
| repo-broad-except-silent | broad except that hides the failure | jev-lint best practices (python) |

## Dropped

- AGENTS.md chat/heartbeat etiquette: not visible in code.
- Counting limits (0.75%, 20%, 8 names): needs counting.
- "reconcile was run before trading": needs tracing across files.

## Next candidates (not enabled)

sql-string-formatting (repo has an allow-listed TABLES idiom), param-change-unrecorded
(`param_changes`), backtest-without-costs, human-override-written-back.

## Suggested non-Jev config

No ruff/mypy/CI exists. Consider ruff with S (S608, S110, S112), BLE001, B.

## Validation (2026-10-15, 66 cases, 30 clean, clean false-alarm 3%)

All 12 rules `keep`. Two rules reworded once (live-flag-bypass, block-exit);
block-exit sits at the P@0.5 floor (75%), watch it in the findings log.

## Changelog

- 2026-10-15: initial 12 rules.
- 2026-10-15: disabled built-in `py-perf-orm-write-in-loop` (fires on duckdb inserts; repo has no ORM).
