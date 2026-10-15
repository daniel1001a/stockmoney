#!/bin/zsh
# Wrapper used by launchd: loads .env, runs one desk job, appends to a dated log.
# Usage: scripts/desk_job.sh <premarket|postopen|postclose|recover|crypto|bridge|learn>
# "scanshadow" ingests RSS/Reddit then runs classify_scan.py --engine shadow (Jev logging only).
# "learn" is the read-only learning job (own module, own lock; never touches orders).
set -u
ROOT="${0:A:h:h}"
cd "$ROOT" || exit 1
mkdir -p data/desk/logs
LOG="data/desk/logs/$(date +%Y-%m-%d).log"
if [[ -f .env ]]; then set -a; source .env; set +a; fi
echo "=== $(date '+%F %T %Z') job=$1 ===" >> "$LOG"
if [[ "$1" == "learn" ]]; then
  .venv/bin/python -m stockmoney.desk.learn.job >> "$LOG" 2>&1
elif [[ "$1" == "scanshadow" ]]; then
  { .venv/bin/python scripts/nightly_scan_ingest.py \
    && .venv/bin/python scripts/classify_scan.py --engine shadow --hours 36 --limit 200; } >> "$LOG" 2>&1
else
  .venv/bin/python -m stockmoney.desk.run "$1" >> "$LOG" 2>&1
fi
STATUS=$?
echo "=== exit $STATUS ===" >> "$LOG"
exit $STATUS
