# MONITORING.md — in-house observability roadmap

Companion doc to [LATENCY.md](LATENCY.md). The goal here is operational visibility into running calls — *what happened, why, and can I play it back* — without depending on LiveKit Cloud's observability features.

Why in-house instead of LiveKit Cloud:
- Free Build plan keeps anonymized data for model training; calls contain PII.
- Recordings stored externally are a compliance friction we don't need.
- The dashboard already exists and runs in our region — extending it is cheaper than a third-party integration.

---

## Status

| Capability | Status | Where |
|---|---|---|
| Per-call transcripts | ✅ live | `/tmp/transcripts.jsonl`, served via dashboard `/api/transcripts` |
| Per-call metrics (turn latency, tool calls, SDK metrics) | ✅ live | `/tmp/voice-agent-metrics.jsonl`, no API yet |
| Caller audio recording (WAV) | ✅ **new** | `$VOICE_RECORDINGS_DIR/{room}.wav` (default `/opt/livekit-ai-voice/recordings`) |
| Agent-side audio recording | ❌ deferred | Local-track subscription is more SDK-internal; transcripts already capture what the agent said |
| Sessions list + per-session timeline UI | ❌ planned | §1 below |
| Per-call log file isolation | ❌ planned | §2 below |
| Disk retention / cleanup | ❌ planned | §3 below |

What ships now: caller WAV recording. The rest is captured below as concrete planned work.

---

## 1. Sessions list + per-session timeline UI

The dashboard ([dashboard_server.py](dashboard_server.py) + [web/index.html](web/index.html)) currently shows transcripts. It does not surface metrics, sessions as first-class objects, or audio playback. This section ships those.

### Backend changes

Add three endpoints to [dashboard_server.py](dashboard_server.py):

| Endpoint | Returns |
|---|---|
| `GET /api/sessions` | List of sessions with summary fields (room name, phone, started_at, duration_s, turn_count, median_turn_latency_ms, has_recording). Built by scanning `/tmp/transcripts.jsonl` + `/tmp/voice-agent-metrics.jsonl` and grouping by room name. |
| `GET /api/sessions/{room}/timeline` | Merged time-ordered events: transcripts + metrics + log entries for one room. Drives the detail view. |
| `GET /api/sessions/{room}/recording` | Stream the WAV file from `$VOICE_RECORDINGS_DIR/{room}.wav` with proper MIME and `Range` support so the browser audio element can scrub. |

Rough size: 80–150 lines of Python.

### Frontend changes

In [web/index.html](web/index.html), add a "Sessions" tab next to the existing dashboard view:

- **List view**: table of recent sessions, sortable by started_at / duration / median latency. Color the `median_turn_latency_ms` column (green <1500, amber <2500, red >2500) — at-a-glance health.
- **Detail view**: clicking a row opens a side panel with three sub-tabs:
  - *Transcript* — caller and agent turns side-by-side
  - *Timeline* — chronological event list (speech start, speech end, first bot audio, tool calls, errors) with ms gaps
  - *Recording* — `<audio controls>` playing the WAV

Rough size: 200–300 lines of HTML + Tailwind + vanilla JS.

### When to build

After we know Tier 1 latency fixes worked. Building a UI on top of the wrong data wastes effort. Wait until the metrics file shows post-Tier-1 numbers, then build.

---

## 2. Per-call log file isolation

Right now all logs go to a single `logs/agent.log` (per [start.sh](start.sh)) — debugging a specific call means grepping for the room name. With a few calls per minute, the file gets noisy fast.

### Plan

In [agent.py](agent.py) `entrypoint()`, attach a per-room file handler at the start and detach it at shutdown:

```python
# Sketch — not implemented yet
log_dir = os.getenv("VOICE_LOG_DIR", "/opt/livekit-ai-voice/logs/calls")
os.makedirs(log_dir, exist_ok=True)
handler = logging.FileHandler(os.path.join(log_dir, f"{room_name}.log"))
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
root_logger = logging.getLogger()
root_logger.addHandler(handler)
try:
    # ... existing entrypoint body ...
finally:
    root_logger.removeHandler(handler)
    handler.close()
```

Rough size: 15 lines.

### Caveat

Adding/removing root handlers from inside `entrypoint()` is racy if the worker handles concurrent calls. The clean approach is a `LogRecord.room` filter that tags each log line with the room and writes to per-room files via a custom handler. Defer until we actually run concurrent calls — the Vultr worker today runs one at a time.

---

## 3. Disk retention / cleanup

Local-disk recordings will fill up. Plan:

| Storage | Default location | Growth rate | Retention strategy |
|---|---|---|---|
| Recordings (WAV) | `/opt/livekit-ai-voice/recordings/` | ~4 MB/min mono 16kHz | Cron: delete files >30 days old |
| Transcripts (JSONL) | `/tmp/transcripts.jsonl` | ~500 bytes / turn | Logrotate by size (10 MB) |
| Metrics (JSONL) | `/tmp/voice-agent-metrics.jsonl` | ~1 KB / turn | Logrotate by size (10 MB) |
| Per-call logs (planned) | `/opt/livekit-ai-voice/logs/calls/` | ~50 KB / call | Cron: delete files >30 days old |

### Cron sketch

```bash
# /etc/cron.daily/livekit-cleanup
find /opt/livekit-ai-voice/recordings -type f -name '*.wav' -mtime +30 -delete
find /opt/livekit-ai-voice/logs/calls -type f -mtime +30 -delete
```

### Disk-space monitoring

Add a `df -h` check to `status.sh` and surface in the dashboard. If free space drops below 10 GB, log a warning.

---

## 4. What about Supabase S3 (already in `.env.example`)?

The repo's [.env.example](.env.example) has Supabase S3 credentials wired but the code never uses them. Reasons to revive in a future PR:

- **Cross-VPS access**: today recordings only live on the box that handled the call. If you scale to multiple workers, recordings become unfindable. S3 fixes that.
- **Off-machine backup**: a single VPS is a single point of failure.
- **Compliance**: Supabase has stricter retention controls than ad-hoc cron cleanup.

When to add: when concurrent worker count > 1, or when retention compliance requires it. For now, single-VPS local disk is fine.

---

## 5. What we explicitly are NOT doing

- **Agent-side audio recording.** The agent's TTS output is reconstructible from transcripts + voice config. Recording it adds complexity (subscribing to local-published tracks) for diminishing return.
- **Real-time audio streaming to the dashboard.** Live-listening to active calls is a manager feature for outbound sales floors. Add only if explicitly requested — it complicates the audio path.
- **LiveKit Cloud session timeline.** We are deliberately not using it (PII, free-plan training program). All state stays in-house.
- **External APMs (Datadog, NewRelic, OpenTelemetry).** Tempting and clean, but adds a vendor and a cost. Until the dashboard view in §1 is shipped and proves insufficient, don't.

---

## 6. Operational checklist (use during incidents)

When a call regresses or a customer complains:

1. Get the room name from the dashboard (or the caller's phone number → grep `/tmp/transcripts.jsonl`).
2. `cat /tmp/voice-agent-metrics.jsonl | grep <room>` — full event timeline, with `caller_speech_end_to_first_bot_audio` durations.
3. Listen to `$VOICE_RECORDINGS_DIR/{room}.wav` — confirms whether the issue was the bot, the caller's audio quality, or the network.
4. Grep `logs/agent.log` for the room name and same time window — exception stack traces.
5. If it's a regression introduced by a recent deploy, `git log --oneline -10` and bisect.

Once §1 (dashboard sessions view) ships, this whole flow collapses to "click the failed session, read everything in one panel."

---

## 7. See also

- **[CLAUDE.md](CLAUDE.md)** — architectural rules
- **[LATENCY.md](LATENCY.md)** — latency tier roadmap
- **[LIVEKIT_RESEARCH.md](LIVEKIT_RESEARCH.md)** — what LiveKit Cloud offers (and why we're not using it)
