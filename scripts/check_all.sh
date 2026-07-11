#!/usr/bin/env bash
# Runs everything HANDOFF.md's "跑全部測試" section lists by hand, as one
# fail-fast gate: backend tests, frontend tests, frontend type check. Exists
# because those three were three separate manual commands nobody was
# guaranteed to run together before a change was considered done.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "== backend: uv run pytest =="
uv run pytest tests/ -q

echo "== frontend: npm test (vitest) =="
npm --prefix frontend test

echo "== frontend: tsc --noEmit =="
(cd frontend && npx tsc -b --noEmit)

echo "== all checks passed =="
