#!/bin/zsh
# Wrapper used by launchd: loads .env, runs one desk job, appends to a dated log.
# Usage: scripts/desk_job.sh <premarket|postopen|postclose|recover|crypto|bridge|learn|scanshadow|options|options-recover|intraday_shadow>
# "scanshadow" ingests RSS/Reddit then runs classify_scan.py --engine shadow (Jev logging only).
# "learn" is the read-only learning job (own module, own lock; never touches orders).
# "options" is the intraday options session (own module, own lock, runs until the close).
# "options-recover" sells options leftovers of a dead session (same lock; exits if a session runs).
# "intraday_shadow" is a read-only shadow tick (stockmoney.desk.intraday): no order path, own timeout alarm.
set -u
ROOT="${0:A:h:h}"
cd "$ROOT" || exit 1
mkdir -p data/desk/logs
LOG="data/desk/logs/$(date +%Y-%m-%d).log"
if [[ -f .env ]]; then set -a; source .env; set +a; fi
# Options jobs default to shadow-only (no paper orders) until the owner sets DESK_OPTIONS_ORDERS=paper in .env.
if [[ "$1" == options* ]]; then export DESK_OPTIONS_ORDERS="${DESK_OPTIONS_ORDERS:-off}"; fi
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
else
  .venv/bin/python -m stockmoney.desk.run "$1" >> "$LOG" 2>&1
fi
STATUS=$?
echo "=== exit $STATUS ===" >> "$LOG"
exit $STATUS
