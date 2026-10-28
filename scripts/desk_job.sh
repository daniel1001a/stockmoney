#!/bin/zsh
# Wrapper used by launchd: loads .env, runs one desk job, appends to a dated log.
# Usage: scripts/desk_job.sh <premarket|postopen|postclose|recover|crypto|bridge|learn|scanshadow|options|options-recover|intraday_shadow>
# "scanshadow" ingests RSS/Reddit then runs classify_scan.py --engine shadow (Jev logging only).
# "learn" is the read-only learning job (own module, own lock; never touches orders).
# "options" is the intraday options session (own module, own lock, runs until the close).
# "options-recover" sells options leftovers of a dead session (same lock; exits if a session runs).
# "intraday_shadow" is a read-only shadow tick (stockmoney.desk.intraday): no order path, own timeout alarm;
# the defined-risk spread shadow (stockmoney.desk.options.spreads, H6, no order path) runs after it.
set -u
ROOT="${0:A:h:h}"
cd "$ROOT" || exit 1
mkdir -p data/desk/logs
LOG="data/desk/logs/$(date +%Y-%m-%d).log"
if [[ -f .env ]]; then set -a; source .env; set +a; fi
# launchd starts jobs with a bare PATH (/usr/bin:/bin:/usr/sbin:/sbin); the claude CLI that
# scanshadow shells out to lives in ~/.local/bin (native installer)
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
# Options jobs default to shadow-only (no paper orders) until the owner sets DESK_OPTIONS_ORDERS=paper in .env.
if [[ "$1" == options* ]]; then export DESK_OPTIONS_ORDERS="${DESK_OPTIONS_ORDERS:-off}"; fi
# "bridge" starts every 60s (stockmoney.desk.bridge_job decides whether a poll is due) and
# logs only when it printed something, so the idle starts leave no trace in the dated log.
if [[ "$1" == "bridge" ]]; then
  OUT="$(.venv/bin/python -m stockmoney.desk.bridge_job 2>&1)"
  STATUS=$?
  if [[ -n "$OUT" || $STATUS -ne 0 ]]; then
    { echo "=== $(date '+%F %T %Z') job=bridge ==="; print -r -- "$OUT"; echo "=== exit $STATUS ==="; } >> "$LOG"
  fi
  exit $STATUS
fi
echo "=== $(date '+%F %T %Z') job=$1 ===" >> "$LOG"
if [[ "$1" == "learn" ]]; then
  .venv/bin/python -m stockmoney.desk.learn.job >> "$LOG" 2>&1
elif [[ "$1" == "scanshadow" ]]; then
  { .venv/bin/python scripts/nightly_scan_ingest.py \
    && .venv/bin/python scripts/classify_scan.py --engine shadow --hours 36 --limit 200; } >> "$LOG" 2>&1
elif [[ "$1" == "options" ]]; then
  .venv/bin/python -m stockmoney.desk.options.run session >> "$LOG" 2>&1
elif [[ "$1" == "options-recover" ]]; then
  .venv/bin/python -m stockmoney.desk.options.run recover >> "$LOG" 2>&1
elif [[ "$1" == "intraday_shadow" ]]; then
  .venv/bin/python -m stockmoney.desk.intraday tick --feed sip >> "$LOG" 2>&1
  S1=$?
  .venv/bin/python -m stockmoney.desk.options.spreads tick >> "$LOG" 2>&1
  S2=$?
  (exit $(( S1 ? S1 : S2 )))
else
  .venv/bin/python -m stockmoney.desk.run "$1" >> "$LOG" 2>&1
fi
STATUS=$?
echo "=== exit $STATUS ===" >> "$LOG"
exit $STATUS
