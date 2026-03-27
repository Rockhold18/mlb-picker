#!/bin/bash
# MLB Picker — Daily automation wrapper for cron
# Usage: ./daily_picks.sh <morning|lineup_lock|results>
#
# Cron entries (add with: crontab -e):
#   0 8 * * *   /Users/danielrockhold/Claude\ Code\ Sandbox/mlb-picker/daily_picks.sh morning
#   0 11 * * *  /Users/danielrockhold/Claude\ Code\ Sandbox/mlb-picker/daily_picks.sh lineup_lock
#   0 14 * * *  /Users/danielrockhold/Claude\ Code\ Sandbox/mlb-picker/daily_picks.sh lineup_lock
#   0 17 * * *  /Users/danielrockhold/Claude\ Code\ Sandbox/mlb-picker/daily_picks.sh lineup_lock
#   0 20 * * *  /Users/danielrockhold/Claude\ Code\ Sandbox/mlb-picker/daily_picks.sh lineup_lock
#   0 1 * * *   /Users/danielrockhold/Claude\ Code\ Sandbox/mlb-picker/daily_picks.sh results

set -e

MODE="${1:-morning}"
PROJECT_DIR="/Users/danielrockhold/Claude Code Sandbox/mlb-picker"
PYTHON="/usr/bin/python3"
LOG_DIR="${PROJECT_DIR}/logs"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="${LOG_DIR}/mlb_${MODE}_${TIMESTAMP}.log"

# Ensure log directory exists
mkdir -p "${LOG_DIR}"

# Run scheduler and capture output
cd "${PROJECT_DIR}"
RESULT=$("${PYTHON}" scheduler.py "${MODE}" 2>&1 | tee "${LOG_FILE}" | tail -1)

# Send macOS notification
if [ $? -eq 0 ]; then
    case "${MODE}" in
        morning)
            TITLE="MLB Picks Ready"
            MSG="${RESULT}"
            SOUND="Glass"
            ;;
        lineup_lock)
            TITLE="MLB Lineup Lock"
            MSG="${RESULT}"
            SOUND="Tink"
            ;;
        results)
            TITLE="MLB Results"
            MSG="${RESULT}"
            SOUND="Hero"
            ;;
    esac
else
    TITLE="MLB Picker Error"
    MSG="Check log: ${LOG_FILE}"
    SOUND="Basso"
fi

osascript -e "display notification \"${MSG}\" with title \"${TITLE}\" sound name \"${SOUND}\""

# Clean up logs older than 30 days
find "${LOG_DIR}" -name "mlb_*.log" -mtime +30 -delete 2>/dev/null || true
