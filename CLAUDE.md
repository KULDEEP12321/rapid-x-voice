# CLAUDE.md — design guardrails for this project

This is an outbound voice agent. Voice is unforgiving: every 100ms of latency is felt by the caller. Every change to this codebase must respect the rules below. If a change conflicts with one of these rules, call it out before merging.

## Architecture (must remain a streaming pipeline, never request/response)

```
Caller (PSTN)
  ↓
SIP trunk (Twilio / Vobiz / Plivo)
  ↓
LiveKit media bridge  ← always-on, region-pinned, NOT serverless
  ↓
Voice stack selected by `VOICE_STACK`
  ├─ `gemini`: Gemini Live WebSocket (native audio-to-audio)
  └─ `cascade`: streaming STT → Groq LLM → streaming TTS
  ↓
Same path back to caller
```

Do not introduce a step that buffers a full sentence, makes a synchronous round-trip, or runs on cold-start serverless. Voice quality dies in those places.

## Rules

### 1. Keep the selected stack streaming
Default to `VOICE_STACK=gemini` with `livekit.plugins.google.realtime.RealtimeModel` for native audio-to-audio. For sub-second Indian telephony, `VOICE_STACK=cascade` is an approved Tier 3 path using streaming STT → Groq LLM → streaming TTS. **Do not** add request/response buffering, full-sentence waits, disk audio handoffs, or serverless hops to either path.

### 2. Bridge stays close to caller + model vendors
LiveKit worker runs in the region nearest both the SIP origin and model endpoints. For Indian callers: India West / Mumbai / Singapore. **Verify on every deploy** by checking the worker registration log line `region: India West`. If the region drifts (e.g. to `us-east-1`), fix it immediately — network distance is silent latency.

### 3. Stream tiny audio frames (~20ms)
Don't add buffering, batching, or "stability" wrappers around the audio path. Every buffer is latency disguised as robustness. LiveKit and Gemini Live already chunk correctly — leave them alone.

### 4. Aggressive turn detection + barge-in
Endpointing must be fast but not rude. Targets:
- silence threshold: **300–500ms**
- barge-in: **enabled**
- when user starts speaking, **cancel any model audio in flight immediately**
- never let the assistant talk over the caller

When configuring `AgentSession` or `RealtimeModel`, set explicit turn-detection / endpointing params; do not rely on defaults to be correct.

### 5. Keep system prompts compact
Realtime prompts directly increase first-token latency. Rules:
- Persona, tone, flow, escalation rules only — short imperative sentences.
- **Move large knowledge into tools or RAG**, not the prompt.
- The dashboard's System Prompt textarea is the source of truth (see [web/index.html](web/index.html)). Keep dashboard prompts under ~300 tokens. Encourage users with placeholder hints.

### 6. No slow tools during speech
- **Prefetch CRM/order/payment data when the call starts**, not mid-conversation. The dispatch metadata already supports this — load context into the agent's room metadata before `agent.py` starts the session.
- During the call, every tool call must be **<200ms**. Cache, index, or pre-compute.
- If a tool is unavoidably slow, the bot must say a short filler ("Let me check that…") **before** the tool fires, so dead air doesn't form.

### 7. Make the bot speak less
Short answers, one question at a time, no verbose confirmations. This wins on both **cost** (audio output is the most expensive token type) and **latency** (less to generate, less to play). Bake this into the system prompt: "Reply in 1–2 sentences."

### 8. Transcode once, in memory
Phone audio arrives as 8kHz μ-law/A-law. Gemini Live wants raw PCM. LiveKit handles the conversion automatically — do not add a second transcode hop, do not write audio to disk, do not buffer to a file. If you ever see `tempfile` or `wav` in the audio path, that's a regression.

### 9. Warm sessions
- **Outbound:** open the Gemini Live websocket as soon as we know we're dialing — ideally during the SIP `ringing` state, not after `answered`. Saves ~300–500ms of silence after pickup.
- **Inbound:** keep the LiveKit worker process warm (always-on `nohup`, never serverless). The current `start.sh` does this — keep it.

### 10. Always-on infrastructure, no serverless for the media path
The bridge / agent worker MUST run as a long-lived process. **No** Lambda / Cloud Run / Vercel functions for the media path — cold starts kill voice. Boring always-on `nohup python agent.py start` (current setup) wins.

## SLOs (track these — if you can't measure it, you can't keep it fast)

| Metric | Target | Where to measure |
|---|---|---|
| Caller speech end → first bot audio | **600–900ms** | session event timestamps in `agent.py` |
| Caller interruption → bot stops speaking | **<200ms** | barge-in events |
| Tool call duration | **<300ms** | wrap each tool in `time.perf_counter()` |
| Roundtrip network time to active model provider | **<150ms** for India West | provider client timing |
| Audio buffer depth | minimal | LiveKit metrics / debug logs |
| Bridge processing | **<50ms** | agent.py event loop |

These are not aspirational — they are the bar. If a change pushes any of these past target, it's a regression.

## Current code state vs. these rules (audit, as of last commit)

| Rule | Status | Notes |
|---|---|---|
| 1. Streaming voice stack | ✅ | `VOICE_STACK=gemini` keeps native audio; `VOICE_STACK=cascade` uses streaming STT/LLM/TTS for Tier 3 |
| 2. Region close to callers | ✅ | LiveKit worker registers in `India West` (verify on each deploy) |
| 3. Tiny frames | ✅ | LiveKit/Gemini handle this; nothing custom buffering |
| 4. Aggressive turn detection | ✅ | Gemini uses realtime model VAD; cascade uses multilingual turn detection with fast endpointing |
| 5. Compact prompts | ⚠️ **PARTIAL** | Dashboard exposes a free-form textarea — no length guard. **Action:** add a soft warning when prompt > 300 tokens |
| 6. Fast tools / prefetch | ⚠️ **GAP** | `lookup_user` is a stub returning a hardcoded string. No prefetch path. **Action:** when CRM is wired up, load lead context into `meta` at dispatch time, not via mid-call tool call |
| 7. Bot speaks less | ⚠️ **PROMPT-DEPENDENT** | The fallback `config.SYSTEM_PROMPT` says "1-2 sentences" but dashboard prompts may not. **Action:** add to dashboard placeholder/help text |
| 8. Transcode once | ✅ | LiveKit owns the audio path; no file I/O |
| 9. Warm sessions | ⚠️ **GAP** | `_build_realtime_model()` runs inside `entrypoint()` after dispatch, not before SIP answer. **Action:** see if we can lazy-init the websocket earlier in the LiveKit lifecycle |
| 10. Always-on infra | ✅ | `start.sh` runs `nohup`, no serverless |
| Metrics tracking | ✅ | Session events, tool calls, SDK metrics, and caller-stop → bot-audio timings are emitted to structured JSONL |

When making changes, **fix gaps you touch**. Don't paper over them, don't add new gaps.

## What this means for code review

Before merging any change, check:

1. Did I add a buffering / batching step? → revisit
2. Did I add a synchronous network call in the call path? → revisit
3. Did I write audio to disk? → revisit
4. Did I extend the system prompt? → can the new content go into a tool instead?
5. Did I add a tool call without considering prefetch? → can it run at dispatch time?
6. Did I introduce serverless for the media path? → no
7. Did I measure latency before and after? → if not, do it

If any answer is "yes" / "no" in the wrong direction, the change is a regression even if the feature works.
