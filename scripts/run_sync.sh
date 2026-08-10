#!/bin/bash
# Wrapper used by the launchd agent. Logs each run so a silent failure
# at 18:30 does not go unnoticed for a week.
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

mkdir -p logs
LOG="logs/t212-sync.log"

{
  echo "--- $(date '+%Y-%m-%d %H:%M:%S') ---"
  "$PROJECT_DIR/venv/bin/python" -m t212.sync
  echo "exit: $?"
} >>"$LOG" 2>&1

# Keep the log from growing forever.
tail -n 2000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
