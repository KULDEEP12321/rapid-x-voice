#!/usr/bin/env bash
# Stop the voice agent + dashboard background processes.

set -u

killed=0
for pat in "agent\.py start" "dashboard_server\.py"; do
    if pgrep -f "$pat" > /dev/null; then
        pkill -f "$pat" || true
        echo "  stopped: $pat"
        killed=1
    fi
done

if [ "$killed" -eq 0 ]; then
    echo "Nothing to stop."
fi
