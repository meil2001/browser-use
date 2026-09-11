#!/bin/bash
# Step 0: start inference and keep a full copy of terminal output.
# tee = live terminal + file. PYTHONUNBUFFERED = lines appear immediately.

set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
source setup_env.sh

LOG_DIR="run_logs"
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="$LOG_DIR/inference_${STAMP}.log"

echo "$LOG_FILE" > "$LOG_DIR/LATEST.txt"
echo "Logging to: $LOG_FILE"
echo "Server: http://localhost:9000/docs"
echo "Stop with Ctrl+C"

export PYTHONUNBUFFERED=1
# Stage 7: set LOCAL_AUTOMATION_JSON=test_automation_cached.json for replay
optexity inference --port 9000 --child_process_id 0 2>&1 | tee "$LOG_FILE"
