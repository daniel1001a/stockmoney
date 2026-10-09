#!/bin/zsh
# Wrapper used by launchd: loads .env, runs one desk job, appends to a dated log.
# Usage: scripts/desk_job.sh <premarket|postopen|postclose|crypto|bridge>
set -u
ROOT="${0:A:h:h}"
cd "$ROOT" || exit 1
mkdir -p data/desk/logs
LOG="data/desk/logs/$(date +%Y-%m-%d).log"
if [[ -f .env ]]; then set -a; source .env; set +a; fi
echo "=== $(date '+%F %T %Z') job=$1 ===" >> "$LOG"
.venv/bin/python -m stockmoney.desk.run "$1" >> "$LOG" 2>&1
STATUS=$?
echo "=== exit $STATUS ===" >> "$LOG"
exit $STATUS
