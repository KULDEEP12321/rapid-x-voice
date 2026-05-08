#!/usr/bin/env bash
# Show status of the voice agent + dashboard.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPECTED_LIVEKIT_REGION="${EXPECTED_LIVEKIT_REGION:-India West}"
METRICS_LOG="${VOICE_METRICS_LOG:-/tmp/voice-agent-metrics.jsonl}"

# Find the agent log wherever it lives (start.sh writes to logs/, manual runs often use /tmp/).
AGENT_LOG=""
for candidate in "$ROOT/logs/agent.log" "/tmp/agent.log"; do
    if [ -s "$candidate" ]; then
        AGENT_LOG="$candidate"
        break
    fi
done

echo "=== agent ==="
pgrep -af "agent\.py start" | grep -v grep | head -1 || echo "  not running"

echo
echo "=== dashboard ==="
pgrep -af "dashboard_server\.py" | grep -v grep | head -1 || echo "  not running"

echo
echo "=== last agent log lines (${AGENT_LOG:-none}) ==="
if [ -n "$AGENT_LOG" ]; then
    grep -E '"registered worker"|ERROR|Exception' "$AGENT_LOG" 2>/dev/null | tail -3
else
    echo "  no log yet"
fi

echo
echo "=== worker region check (expected: $EXPECTED_LIVEKIT_REGION) ==="
if [ -n "$AGENT_LOG" ]; then
    LAST_REGION_LINE="$(grep -Ei 'registered worker|region' "$AGENT_LOG" 2>/dev/null | tail -1 || true)"
    if [ -z "$LAST_REGION_LINE" ]; then
        echo "  no worker registration line found yet"
    elif printf '%s' "$LAST_REGION_LINE" | grep -q "$EXPECTED_LIVEKIT_REGION"; then
        echo "  ok: $LAST_REGION_LINE"
    else
        echo "  WARNING: latest worker line does not mention $EXPECTED_LIVEKIT_REGION"
        echo "  $LAST_REGION_LINE"
    fi
else
    echo "  no log yet"
fi

echo
echo "=== last voice metrics (${METRICS_LOG}) ==="
if [ -s "$METRICS_LOG" ]; then
    tail -3 "$METRICS_LOG"
else
    echo "  no metrics yet"
fi

echo
echo "=== /api/calls health (10s timeout) ==="
curl -sS -m 10 http://localhost:3000/api/calls 2>&1 | head -c 300
echo
