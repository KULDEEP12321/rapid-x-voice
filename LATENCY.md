# LATENCY.md — voice agent latency roadmap

Voice latency is the single dominant UX metric for this agent. Every 100ms of caller-stop → bot-audio is felt. This doc captures the diagnosis, what's been done (Tier 1), and the two further tiers available if Tier 1 isn't enough.

Read [CLAUDE.md](CLAUDE.md) first — this doc assumes you've internalized the architecture rules there. This doc is the *latency-specific* roadmap; CLAUDE.md is the architectural guardrails. For platform-level reference on what LiveKit itself supports, see [LIVEKIT_RESEARCH.md](LIVEKIT_RESEARCH.md).

---

## 0. The target

| Metric | Target | Stretch | Hard limit |
|---|---|---|---|
| Caller speech end → first bot audio | 800ms | 600ms | 1500ms |
| Caller interruption → bot stops | <200ms | <100ms | 500ms |
| Tool call duration | <300ms | <150ms | 800ms |
| Cost per minute (₹) | <2 | <1 | 3 |

These match CLAUDE.md SLOs. The 800ms target is **aspirational** — LiveKit itself does not officially promise sub-second on telephony. Their own published numbers ([Voice Agent Foundations](https://livekit.worksh.app/tutorials/livekit-voice-agent/introduction)) are "best case ~415ms, typical 1.1–2.0s" for a stitched cascade.

---

## 1. Where the latency comes from

For a turn (caller stops talking → bot speaks), time is spent in this order:

```
caller stops talking
   │
   ├── 1. endpointing wait (VAD layers debating "is the user done?")
   │
   ├── 2. caller audio in flight to model API (network RTT)
   │
   ├── 3. model time-to-first-token (STT + LLM internal processing)
   │
   ├── 4. first audio frame back over network (RTT again)
   │
   ├── 5. LiveKit bridge + jitter buffer
   │
   └── caller hears first bot audio
```

Each layer has a knob. The Tier 1 fixes attacked layer 1 hard (down from ~1000ms to ~250ms). Tier 2 attacks layer 3. Tier 3 replaces layer 3 entirely.

---

## 2. Tier 1 — DONE (PR #1)

Status: **shipped on branch `latency-tier-1`, pending merge.**
Goal: drop from 3000–4000ms to 1000–1500ms by removing self-inflicted delay.

### What changed and why

| Change | File | Reason | Expected saving |
|---|---|---|---|
| Swap default model from `gemini-3.1-flash-live-preview` to `gemini-2.5-flash-native-audio-preview-12-2025` | [config.py](config.py) | LiveKit docs flag 3.1 with "known compatibility limitations" — required a dual-model greeting/main swap. 2.5 supports `generate_reply` natively. | 100–300ms (kills mid-call swap), plus eliminates a class of bugs |
| Delete dual-model dance | [agent.py](agent.py) | One WebSocket per call, not two. ~70 lines removed. | Same as above |
| Remove `endpointing` block from `_build_turn_handling` | [agent.py](agent.py) | With `turn_detection: "realtime_llm"`, Gemini's server-side VAD owns turn end. The `min_delay=0.25, max_delay=0.5` was stacking on top. | 250–500ms per turn |
| Tighten Gemini `AutomaticActivityDetection`: `silence_duration_ms` 300→200, `prefix_padding_ms` 80→20 | [agent.py](agent.py) | Aligns with Google's own Live API examples. 300ms is too patient for phone audio. | ~150ms per turn |
| Loosen Silero VAD + load at module import | [agent.py](agent.py) | Silero now only drives barge-in (Gemini owns turn end). Module-level load = faster cold start. LiveKit-documented prewarm pattern. | ~50ms first-call cold start; reduces VAD noise |
| Enable `preemptive_tts=True` | [agent.py](agent.py) | TTS speculatively starts on partial transcript. LiveKit-documented latency win. | 100–300ms per turn |

### What this DIDN'T do

- It did not change the audio frame size. CLAUDE.md says 20ms; LiveKit's documented default is 50ms. The 20ms code already exists at [agent.py:607](agent.py#L607); there's no published evidence going lower wins.
- It did not pre-warm the Gemini WebSocket during SIP ringing. CLAUDE.md Rule 9 calls for it, but the LiveKit plugin doesn't expose a hook to open the connection before `entrypoint()` runs. This is *aspirational*, not implementable today without forking the plugin.
- It did not enforce a system-prompt token cap in the dashboard. That's deferred to a separate guardrail PR.

### How to verify Tier 1 worked

After merge + deploy:

```bash
# On the VPS:
sudo truncate -s 0 /tmp/voice-agent-metrics.jsonl
# Place a test call, talk for ~30s
grep caller_speech_end_to_first_bot_audio /tmp/voice-agent-metrics.jsonl
```

Each line will show `"duration_ms": <number>`. **Median across 5 calls should be 1000–1500ms.** If it's still over 2000ms, something else is wrong — most likely the worker isn't actually in `ap-south` Mumbai, or the system prompt is bloated.

---

## 3. Tier 2 — model swaps (try if Tier 1 plateaus above 1.2s)

Status: **not done.** Open a new PR (`latency-tier-2`) when ready.

The hypothesis: Gemini 2.5 native-audio is still inherently slow because it's a preview model, and native-audio models do more work per turn than text-modality models. Two model alternatives within the same architecture.

### Option A: switch to `gemini-2.5-flash-live` (non-native-audio variant)

Same Gemini Live API surface, but uses an internal TTS path instead of the model speaking directly. Slightly worse voice quality, often 300–600ms faster.

**Code change:** one line in [config.py:70](config.py#L70):

```python
GEMINI_LIVE_MODEL = os.getenv(
    "GEMINI_LIVE_MODEL",
    "gemini-2.5-flash-live",  # was: gemini-2.5-flash-native-audio-preview-12-2025
)
```

**Tradeoffs:**
- Pro: Faster TTS path, often more stable.
- Pro: Better cost (text tokens are cheaper than audio output tokens).
- Con: Less natural prosody, less emotional range.
- Con: Code-switching (Hindi-English mid-sentence) may degrade.
- Con: Still preview-tier — Google has not committed sub-second SLA.

**Expected gain:** 200–500ms per turn. Most likely lands at **800–1100ms total**.

### Option B: try `gemini-3.1-flash-live-preview` again with explicit `disable thinking`

3.1 is faster than 2.5 in raw inference, but the LiveKit plugin issues forced us off it. If a future plugin version (`livekit-plugins-google >= 1.6`?) lifts the warning, retest.

**Don't do this until LiveKit removes the warning from their plugin docs.**

### Verifying Tier 2

Same metrics-tail flow as Tier 1. The single number to watch is `caller_speech_end_to_first_bot_audio.duration_ms` across 5 calls.

---

## 4. Tier 3 — cascade architecture (last resort)

Status: **not done.** This is the path when Tier 1 + Tier 2 still leave you above 1s and the business needs sub-800ms.

**Important: this violates [CLAUDE.md](CLAUDE.md) Rule 1** ("native audio-to-audio only"). Going this route is a *conscious architectural decision*, not a casual swap. CLAUDE.md needs to be amended in the same PR.

### Why cascade can be faster than native audio (today)

Native audio models do everything in one round-trip — that *sounds* faster, and theoretically is. But preview-tier native audio (Gemini 2.5/3.1, OpenAI Realtime) is not yet optimized for sub-second. Each component in a cascade is individually optimized for streaming and they pipeline cleanly:

```
Caller audio
   ↓ [streaming, 80–150ms partial → 250–400ms final]
Deepgram Nova-3 STT
   ↓ partial transcript every 100–200ms
Groq / Cerebras LLM
   ↓ first token at 100–250ms
Cartesia Sonic / Aura-2 TTS
   ↓ first audio byte at 80–180ms
Caller hears bot
```

Total reaction window: **500–800ms**. Production-grade benchmarks ([CloudX](https://dev.to/cloudx/cracking-the-1-second-voice-loop-what-we-learned-after-30-stack-benchmarks-427), [Cerebrium](https://cerebrium.ai/blog/deploying-a-global-scale-ai-voice-agent-with-500ms-latency)) consistently report 730–800ms first-syllable on this stack.

### Recommended vendor combos for Indian voice

LiveKit Agents has plugins for all of these, so the change is moderate, not deep. Three combos, ranked:

| Combo | STT | LLM | TTS | Indian-language quality | Cost (₹/min, rough) |
|---|---|---|---|---|---|
| **A — Deepgram + Groq + Cartesia** | Deepgram Nova-3 (multilingual) | Groq Llama 3.3 70B | Cartesia Sonic-Turbo | Hindi: good. Indian-English: excellent. | 0.9–1.4 |
| **B — Sarvam end-to-end** | Sarvam Saarika v2 | Sarvam-M | Sarvam Bulbul v2 | Hindi/Indic: best in class | 1.2–1.8 |
| **C — Mixed: Sarvam STT + Groq LLM + Cartesia TTS** | Sarvam Saarika | Groq Llama 3.3 70B | Cartesia Sonic | Hindi: very good. English: excellent. | 1.0–1.5 |

For an India-first sales/support bot, **Combo A** is the most reliable starting point — Deepgram and Cartesia have measured production telephony deployments in India, Groq has the lowest LLM TTFT in the industry. Combo B is the right pick if your callers are pure Hindi/Indic speakers without code-switching.

### What changes in the code

The realtime model path goes away. `AgentSession` takes separate `stt`, `llm`, `tts` plugins. Sketch:

```python
# instead of: AgentSession(vad=_VAD, turn_handling=...)
from livekit.plugins import deepgram, groq, cartesia
from livekit.plugins.turn_detector.multilingual import MultilingualModel

session = AgentSession(
    vad=_VAD,
    stt=deepgram.STT(model="nova-3", language="multi"),  # auto Hindi/English
    llm=groq.LLM(model="llama-3.3-70b-versatile", temperature=0.7),
    tts=cartesia.TTS(model="sonic-turbo", voice="<voice_id>"),
    turn_detection=MultilingualModel(),  # Hindi-aware, ~50–160ms
    turn_handling={
        "interruption": {"enabled": True, "mode": "adaptive"},
        "preemptive_generation": {"enabled": True, "preemptive_tts": True},
    },
)
```

Plus:

- New requirements: `livekit-plugins-deepgram`, `livekit-plugins-groq`, `livekit-plugins-cartesia`, `livekit-plugins-turn-detector`.
- New `.env` keys: `DEEPGRAM_API_KEY`, `GROQ_API_KEY`, `CARTESIA_API_KEY`.
- Delete the entire `_build_realtime_model`, `_build_realtime_input_config` machinery.
- Update CLAUDE.md Rule 1 to reflect the architectural change.
- Pre-warm strategy: load all three plugin clients at module level, not inside `entrypoint()`.

### Tradeoffs vs. native audio

| Aspect | Native audio (Gemini Live) | Cascade (Deepgram+Groq+Cartesia) |
|---|---|---|
| Latency floor (Indian phone) | 1000–1500ms (with Tier 1 fixes) | 500–800ms |
| Voice prosody | Excellent — model speaks "naturally" | Good but not great — TTS-style |
| Code-switching mid-sentence | Excellent | Limited (LLM has to explicitly write the right script) |
| Non-verbals (laughter, "umm") | Yes | No |
| Tool-call latency in mid-conversation | Same | Same |
| Cost per minute | ₹1.5–2.5 | ₹0.9–1.5 |
| Vendor risk | One vendor (Google) | Three vendors |
| Implementation effort | Tier 1 done | Net new — 2–3 days of engineering |
| Failure modes | One thing breaks → whole call dies | Any of three breaks → whole call dies |

### Verifying Tier 3

Same metrics-tail flow. Plus: A/B test against a small caller cohort before flipping default. The voice quality difference is audible — confirm your customers don't notice / care.

---

## 5. Cost analysis (rough order of magnitude)

A typical 3-minute outbound sales call, with the bot speaking ~40% of the time:

| Stack | Audio in | Audio out | LLM tokens | Total/call | ₹/min |
|---|---|---|---|---|---|
| **Native audio (current, Tier 1)** | $0.005 | $0.04 | included | $0.045 | ₹1.25 |
| **Native audio (Gemini 2.5-flash-live, Tier 2)** | $0.003 | $0.015 (text-modality) | $0.005 | $0.023 | ₹0.65 |
| **Cascade (Tier 3 Combo A)** | Deepgram $0.013 | Cartesia $0.018 | Groq $0.003 | $0.034 | ₹0.95 |

Numbers are USD→INR at ₹83/USD, rough. **All three paths can hit your ₹2/min target** as long as the bot speaks in 1–2 sentence replies (which CLAUDE.md already mandates).

The biggest cost lever is **bot output length**, not vendor choice. If the system prompt allows the bot to monologue, every path blows the budget.

---

## 6. Recommendation matrix

| You want… | Path |
|---|---|
| Best result with least risk | Tier 1 (done) → measure → stop if <1.5s is acceptable |
| Sub-1s on a budget | Tier 1 → Tier 2 Option A (`gemini-2.5-flash-live`) |
| Sub-800ms guaranteed, willing to spend engineering effort | Tier 1 → Tier 3 Combo A (cascade) |
| Best Hindi-only quality | Tier 3 Combo B (Sarvam) |
| Lowest cost regardless of latency | Tier 2 Option A (cheapest) |

---

## 7. Things to NEVER do (to keep latency low)

These are the anti-patterns CLAUDE.md and LiveKit's own docs warn against:

- **Stacking VAD layers.** Pick one source of truth for "user is done talking." Today it's Gemini's server-side VAD. Don't add another.
- **Synchronous tools mid-call.** Every tool must complete in <300ms. If it can't, prefetch at dispatch time (load context into room metadata before `entrypoint()`).
- **Long system prompts.** Realtime first-token latency scales linearly with prompt length. Cap at ~300 tokens. Move knowledge into RAG/tools.
- **Verbose bot replies.** Audio output tokens are the most expensive token type AND add latency. "Reply in 1–2 sentences" must stay in the prompt forever.
- **Burstable VPS instances.** Vultr "Cloud Compute" (shared) → CPU credits run out under sustained load → inference timeouts. Use "High Frequency" or "CPU-Optimized" tiers.
- **Region drift.** Worker, LiveKit project, SIP edge, model API endpoint — all four must be in or near `ap-south` (Mumbai). Verify on every deploy.
- **Serverless for the media path.** Cold starts kill voice. Keep the worker on `nohup` / systemd. Already enforced by [start.sh](start.sh) and the deploy script.
- **Writing audio to disk or buffering full sentences.** If you ever see `tempfile`, `wav`, or "buffer until X" in the audio path, that's a regression.

---

## 8. Sources

LiveKit official:
- [LiveKit Agents — Turns](https://docs.livekit.io/agents/build/turns/)
- [LiveKit Agents — Audio (preemptive_generation)](https://docs.livekit.io/agents/build/audio/)
- [LiveKit Agents — Gemini Live plugin](https://docs.livekit.io/agents/models/realtime/plugins/gemini/)
- [Voice agent latency blog](https://livekit.com/blog/understand-and-improve-agent-latency)
- [Region pinning for telephony](https://docs.livekit.io/telephony/features/region-pinning/)
- [Noise & echo cancellation (BVCTelephony)](https://docs.livekit.io/transport/media/noise-cancellation/)
- [Improved end-of-turn model](https://livekit.com/blog/improved-end-of-turn-model-cuts-voice-ai-interruptions-39/)

Community:
- [High latency (5–8s) Gemini Realtime over SIP — community thread](https://community.livekit.io/t/high-latency-5-8-seconds-with-google-gemini-realtime-plugin-over-sip/238)
- [Issue #4423 — Gemini 2.5 native audio + text modality limitation](https://github.com/livekit/agents/issues/4423)

Third-party benchmarks:
- [CloudX — Cracking the 1-second voice loop](https://dev.to/cloudx/cracking-the-1-second-voice-loop-what-we-learned-after-30-stack-benchmarks-427)
- [Cerebrium — Sub-500ms voice agent](https://cerebrium.ai/blog/deploying-a-global-scale-ai-voice-agent-with-500ms-latency)

Vendor docs (Tier 3):
- [Deepgram Nova-3 model](https://developers.deepgram.com/docs/nova-3)
- [Groq Llama 3.3 70B](https://console.groq.com/docs/models)
- [Cartesia Sonic-Turbo](https://docs.cartesia.ai/api-reference/tts/tts)
- [Sarvam AI Saarika / Bulbul](https://docs.sarvam.ai/)

Google:
- [Gemini Live API capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities)

---

## 9. How to update this doc

When you ship a tier or change the architecture:

1. Update the **Status** line at the top of the affected tier section.
2. Add the actual measured median `caller_speech_end_to_first_bot_audio` after deploy in a "Measured" line.
3. If a tier turns out to be wrong (vendor changes pricing, model is retired), strike it through and link to the replacement.
4. Don't delete superseded sections — keep them for institutional memory.

This file is part of the project's load-bearing context. Treat it the same as CLAUDE.md.
