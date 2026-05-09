# LIVEKIT_RESEARCH.md — how LiveKit handles voice latency

Reference notes from a deep read of LiveKit's official docs, blog, GitHub, and community forum, focused on **how LiveKit (the platform) approaches voice agent latency** and what they actually expose to developers building on it.

This doc is platform-level reference material. For *this project's* tiered action plan, see [LATENCY.md](LATENCY.md). For architectural rules specific to this codebase, see [CLAUDE.md](CLAUDE.md).

**Health warning up front:** LiveKit does **not** officially promise sub-800ms in any documentation page reviewed. The closest concrete number from a primary LiveKit source is the Voice Agent Foundations workshop table, which puts the "best case" stitched cascade at **~415ms** and "typical" at **1.1–2.0s**. The "<800ms" target widely cited in the field is achievable by external benchmarkers, but LiveKit itself frames latency as *"monitor, identify dominant stage, swap component."*

---

## 1. LiveKit's regional architecture (global + India)

### Agent compute regions

LiveKit Cloud runs **agents** in three hard-coded regions:

| Code | City | Region purpose | India relevance |
|---|---|---|---|
| `us-east` | Ashburn, VA | Americas | — |
| `eu-central` | Frankfurt | Europe | — |
| `ap-south` | **Mumbai** | Asia / India | **the India region** |

Source: [Agent deployment regions](https://docs.livekit.io/deploy/admin/regions/agent-deployment/).

Singapore is **not** a deployment region for agent compute. Region assignment is **immutable** at agent creation time on LiveKit Cloud — you cannot move a deployed agent between regions. For self-hosted agents (this project's setup on Vultr), region is wherever the worker process happens to run.

### SIP regions

LiveKit's **SIP** infrastructure is broader than agent compute. Inbound endpoints exist in `india`, plus `eu`, `us`, `japan`, `aus`, `uk`, `sa`. Outbound trunks accept country-code pinning including `in` (with PoPs in Hyderabad and Mumbai).

Endpoint format: `{sip_subdomain}.{region_name}.sip.livekit.cloud`.

Source: [Region pinning for telephony](https://docs.livekit.io/telephony/features/region-pinning/).

### How a worker registers to a region

A LiveKit agent worker opens an authenticated WebSocket to `LIVEKIT_URL` and waits for job dispatch. On LiveKit Cloud:

```
lk agent create --region ap-south
```

This pin is permanent. For self-hosted (e.g. Vultr VPS), the worker registers from wherever it physically runs — LiveKit doesn't enforce a region tag on the registration log line. Verify region via network tools (traceroute, RTT to the LiveKit URL and to the model API endpoint).

### Network path for an Indian outbound caller

```
Caller (PSTN, IN)
   ↓
SIP trunk PoP (vendor-dependent — Twilio/Plivo/Exotel/Vobiz)
   ↓
LiveKit SIP edge  ← MUST be `india` for IN callers
   ↓
LiveKit media server  ← whichever region the LiveKit project lives in
   ↓
Agent worker  ← Vultr VPS (this project: Mumbai)
   ↓
Model API (Gemini Live)  ← Google's `asia-south1` Mumbai PoP for Vertex; default `generativelanguage.googleapis.com` routes via Google's global frontend
```

Latency hot spots, in observed order of size:

1. **Worker ↔ Model API**, if the worker is not in India.
2. **SIP trunk PoP ↔ LiveKit SIP edge** — fixed by region-pinning.
3. **LiveKit media server ↔ Agent worker** — fixed by co-locating both in `ap-south`.
4. **Caller ↔ SIP PoP** — controlled by the trunk vendor; Twilio routes Indian traffic via Singapore by default.

LiveKit's own field guide is explicit: *"Even if LiveKit media is pinned to a specific region, external model APIs may process requests elsewhere. Ensure your agent hosting and model endpoints are aligned."* — [Checklist for Regional Deployments](https://livekit.com/field-guides/guide/checklist-for-regional-deployments).

---

## 2. The pipelines LiveKit recommends

LiveKit publishes **two recipes** and is honest that neither is guaranteed sub-800ms.

### Recipe A — STT → LLM → TTS (cascade, the default in their quickstart)

Current default lineup in [Voice AI quickstart](https://docs.livekit.io/agents/start/voice-ai/):

- **STT:** `deepgram/nova-3` (multilingual)
- **LLM:** `openai/gpt-5.2-chat-latest`
- **TTS:** `cartesia/sonic-3`
- **VAD:** `silero.VAD.load()`
- **Turn detection:** `MultilingualModel()` (LiveKit's Hindi-capable end-of-turn model)

### Recipe B — Realtime / native-audio model (this project's path)

Single endpoint does STT+LLM+TTS. Documented options:

- **OpenAI Realtime API** — [guide](https://docs.livekit.io/agents/integrations/realtime/openai/)
- **Gemini Live** — [plugin](https://docs.livekit.io/agents/models/realtime/plugins/gemini/)

### Numbers LiveKit actually publishes

Almost none. The single primary LiveKit source with a concrete breakdown:

| Stage | Best case | Typical |
|---|---|---|
| VAD | 15–20ms | 20–30ms |
| STT | 200–300ms | 400–600ms |
| LLM | 100–200ms | 500–1000ms |
| TTS | 100–150ms | 200–300ms |
| **Total** | **~415ms** | **1.1–2.0s** |

Source: [Voice Agent Foundations workshop](https://livekit.worksh.app/tutorials/livekit-voice-agent/introduction).

The latency blog ([Understand and Improve Voice Agent Latency](https://livekit.com/blog/understand-and-improve-agent-latency)) gives **zero millisecond numbers**. It only enumerates stages and recommends measure-then-swap.

### LiveKit's honest take on which is faster

They do not declare a winner. The Gemini Live page says "low-latency, two-way interactions" with no number. A LiveKit maintainer in the [community thread on SIP latency](https://community.livekit.io/t/high-latency-5-8-seconds-with-google-gemini-realtime-plugin-over-sip/238) notes that **Gemini Realtime is "less reliable" than OpenAI's alternatives** and recommends disabling LiveKit-side turn detection when using Gemini's built-in.

External benchmarks ([CloudX](https://dev.to/cloudx/cracking-the-1-second-voice-loop-what-we-learned-after-30-stack-benchmarks-427), [Cerebrium](https://cerebrium.ai/blog/deploying-a-global-scale-ai-voice-agent-with-500ms-latency)) consistently report cascade pipelines hitting **730–800ms first-syllable** with GPT-Nano + Cartesia Sonic-Turbo. Gemini Live is reported faster on paper but unstable on phone audio.

---

## 3. The knobs LiveKit Agents exposes for latency

In v1.0+, almost everything funnels through `TurnHandlingOptions`. Several legacy `AgentSession` parameters (`turn_detection`, `allow_interruptions`, `min_endpointing_delay`, `max_endpointing_delay`, `preemptive_generation`, `false_interruption_timeout`) are **deprecated** as direct kwargs and now live inside `TurnHandlingOptions`.

Source: [agent_session.py](https://github.com/livekit/agents/blob/main/livekit-agents/livekit/agents/voice/agent_session.py). Verify against the installed SDK version.

### TurnHandlingOptions / EndpointingOptions / InterruptionOptions

| Field | Type | Default | Sub-second rec | Source |
|---|---|---|---|---|
| `turn_detection` | TurnDetectionMode | not given | `MultilingualModel()` (Hindi-capable, ~50–160ms) — OR `"realtime_llm"` if using Gemini/OpenAI Realtime | [turn-detector](https://docs.livekit.io/agents/build/turns/turn-detector/) |
| `endpointing.mode` | str | static | `"dynamic"` (Python-only — adapts to caller pause stats) | [turns](https://docs.livekit.io/agents/build/turns/) |
| `endpointing.min_delay` | float (s) | 0.5 | 0.2–0.3 for snappier turns | same |
| `endpointing.max_delay` | float (s) | 3.0 | 1.5–2.0 | same |
| `interruption.enabled` | bool | True | True | [turns](https://docs.livekit.io/agents/build/turns/) |
| `interruption.mode` | str | "adaptive" | "adaptive" (distinguishes backchannel from real interruption) | same |
| `interruption.discard_audio_if_uninterruptible` | bool | — | True | same |
| `interruption.false_interruption_timeout` | float (s) | — | 2.0 with `resume_false_interruption=True` | same |
| `preemptive_generation.enabled` | bool | **True** | True | [audio](https://docs.livekit.io/agents/build/audio/) |
| `preemptive_generation.preemptive_tts` | bool | **False** | **True** for lowest latency (extra TTS cost on discarded responses) | same |

**Key gotcha:** when `turn_detection: "realtime_llm"`, the realtime model owns turn end. The `endpointing` block then either gets ignored or stacks a redundant wait. **Remove the endpointing block entirely** when using realtime mode. (This was the single biggest config error in this project before Tier 1 — see [LATENCY.md](LATENCY.md).)

### VAD

`silero.VAD.load()` from `livekit-plugins-silero`. LiveKit explicitly says: *"You should still provide a VAD plugin for responsive interruption handling"* even when using a server-VAD realtime model.

So with Gemini Live: keep Silero locally to drive **interruption**, while Gemini's server VAD drives **turn end**. Don't let Silero's `min_silence_duration` debate turn end with Gemini.

Source: [turns](https://docs.livekit.io/agents/build/turns/).

### RoomInputOptions

(Verify against installed SDK version. These names come from the v1 reference.)

| Field | Type | Default |
|---|---|---|
| `text_enabled` | bool | NOT_GIVEN |
| `audio_enabled` | bool | NOT_GIVEN |
| `video_enabled` | bool | NOT_GIVEN |
| `audio_sample_rate` | int | 24000 |
| `audio_num_channels` | int | 1 |
| `audio_frame_size_ms` | int | **50** |
| `noise_cancellation` | NoiseCancellationOptions \| None | None |
| `pre_connect_audio` | bool | **True** |
| `pre_connect_audio_timeout` | float | 3.0 |
| `close_on_disconnect` | bool | True |

Source: [RoomInputOptions reference](https://docs.livekit.io/reference/python/v1/livekit/agents/voice/room_io/index.html).

**Notes:**
- `audio_frame_size_ms = 50` is LiveKit's documented default. The CLAUDE.md "20ms tiny frames" rule is *stricter than LiveKit's default* and not documented as a win — leave at default unless measured.
- `pre_connect_audio` defaults to True for client-side capture, **but is meaningful only for browser/mobile SDKs**; for SIP it's a no-op.

### Noise cancellation (telephony-specific)

For SIP/phone audio, use **`noise_cancellation.BVCTelephony()`**, not the default `NC()`.

From [Noise & echo cancellation](https://docs.livekit.io/transport/media/noise-cancellation/): *"Background voice cancellation optimized for telephony applications. Use for SIP participants."*

Krisp BVC begins billing extra after May 1, 2026 — already in effect. Latency cost not published.

### "Fast path" / "low-latency mode"

**Not a documented flag.** The closest equivalents are:
- `preemptive_generation.preemptive_tts=True`
- Dynamic endpointing with `min_delay` tuned down
- `MultilingualModel()` turn detection (or Gemini-owned for realtime)

There is no single "fast mode" switch. Latency comes from the sum of the knobs above.

---

## 4. Warm-session / pre-connect patterns

What LiveKit publishes:

- **Preemptive generation** (`preemptive_generation.enabled=True`, default on) — LLM begins generating from a partial transcript. *"In a best-case scenario, this reduces latency because by the time the user has finished speaking, the response is ready to go."* — [audio docs](https://docs.livekit.io/agents/build/audio/).

- **Preemptive TTS** (`preemptive_tts=True`, default off) — TTS also runs speculatively. Documented trade-off: more wasted compute when speculative response is discarded.

- **Pre-connect audio** — captures mic locally before agent connection lands. Topic: `lk.agent.pre-connect-audio-buffer`. **Browser/mobile only**, irrelevant for SIP.

- **VAD prewarming** — load `silero.VAD.load()` at module import / process start, **not** inside `entrypoint()`. The latency blog calls this out: *"VAD prewarming: Load model files before assignment."*

- **Worker pool warming** — Not formally documented as a feature. The LiveKit Cloud product page says infra *"auto-scales … places sessions by effective load to minimize end-to-end latency and jitter"* — opaque. For self-hosted, the standard practice is `nohup`/systemd long-lived workers (already done in this project — see [start.sh](start.sh)).

What LiveKit does **not** publish:

- A documented hook to open the Gemini Live WebSocket during SIP `ringing`. The plugin opens its connection inside `RealtimeModel` instantiation, which happens in `entrypoint()`. CLAUDE.md Rule 9 calling for a pre-ringing websocket is **aspirational; not a documented LiveKit feature**.
- "Cold start mitigation" — only addressed implicitly by the always-on worker pattern.

---

## 5. Gemini Live integration via `livekit-plugins-google`

### Latency LiveKit measures

None published. The plugin doc says only "low-latency, two-way interactions" — [Gemini plugin](https://docs.livekit.io/agents/models/realtime/plugins/gemini/).

### Known issues

- `gemini-3.1-flash-live-preview` has *"known compatibility limitations with LiveKit Agents"* — restrictions on `update_instructions()` and `generate_reply()` mid-session. **LiveKit recommends 2.5 versions for most use cases.**
- `gemini-2.5-flash-native-audio-preview-12-2025` cannot be used with `modalities=["text"]` for hybrid pipelines — returns *"Cannot extract voices from a non-audio request"*. See [issue #4423](https://github.com/livekit/agents/issues/4423).
- Reported 5–8s telephony latency on the Gemini Realtime plugin with default config — [community thread](https://community.livekit.io/t/high-latency-5-8-seconds-with-google-gemini-realtime-plugin-over-sip/238). Maintainer recommendation: **disable LiveKit-side turn detection** when using Gemini's built-in VAD; **disable Gemini provider tools** for testing.
- Gemini native audio output is 24 kHz; phone audio is 8 kHz μ-law. LiveKit handles transcoding, but the 24 kHz output path has been reported as a latency contributor in third-party telephony reports.

### Recommended model picks

| Model | LiveKit support | Use it for |
|---|---|---|
| `gemini-2.5-flash-native-audio-preview-12-2025` | Default in LiveKit examples | Production now. Audio-modality only. Hindi works. |
| `gemini-2.5-flash-live` | Standard plugin support | Text/hybrid stitched pipelines. Faster TTS path. |
| `gemini-3.1-flash-live-preview` | Supported with caveats; needs `thinking_config` set to "minimal" | Newer / faster claimed; **known mid-session API gaps — verify before adopting** |

### `RealtimeInputConfig` — what to actually set

(Field names from the underlying `google.genai` SDK that `livekit-plugins-google` re-exports. Verify against the installed version.)

```python
from google.genai import types

types.RealtimeInputConfig(
    automatic_activity_detection=types.AutomaticActivityDetection(
        disabled=False,
        start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
        end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
        prefix_padding_ms=20,         # Google's example default
        silence_duration_ms=200,      # 100 is too tight for phone, 300 too patient
    ),
)
```

For low-latency Indian phone audio:
- `silence_duration_ms=100–250` (Google's default example is 100). 400+ is too patient for phone.
- `prefix_padding_ms=20` (default).
- Sensitivity `HIGH` on both ends — phone audio has clear speech/silence breaks.
- Set `disabled=True` **only if** you have a separately-tuned `MultilingualModel()` driving turn detection. Running both at default settings stacks endpointing windows.

Sources: [Gemini Live API capabilities (Google)](https://ai.google.dev/gemini-api/docs/live-api/capabilities), [LiveKit Gemini plugin](https://docs.livekit.io/agents/models/realtime/plugins/gemini/).

---

## 6. SIP / telephony specifics

### Tested SIP providers

LiveKit's [SIP overview](https://docs.livekit.io/sip/) tests against: **Twilio, Telnyx, Exotel, Plivo, Wavix.**

**Vobiz is not on this list.** It may work, but isn't validated.

### India-native SIP recommendations

For Indian DIDs / outbound:

- **Exotel** and **Plivo** are most India-native. Both have TRAI-compliant Indian numbers and PoPs in India.
- **Twilio** routes Indian traffic via Singapore by default unless you specifically buy local Indian numbers, which has TRAI implications and cost.
- **Vobiz** — unknown. If using Vobiz, measure SIP edge latency separately.

### Region-pinning for telephony

Per [Region pinning](https://docs.livekit.io/telephony/features/region-pinning/):

- Inbound endpoint format: `{sip_subdomain}.{region_name}.sip.livekit.cloud`. For India: `{subdomain}.india.sip.livekit.cloud`.
- Outbound trunks accept `destination_country: "in"` for Indian outbound.

---

## 7. Anti-patterns LiveKit warns against

Pulled directly from LiveKit sources:

- **Stacking VAD layers.** Running LiveKit `MultilingualModel()` plus Gemini's server VAD at default both adds latency and produces inconsistent endpointing. From the [community thread](https://community.livekit.io/t/high-latency-5-8-seconds-with-google-gemini-realtime-plugin-over-sip/238): *"Remove custom turn detection: Use Gemini's built-in turn detection instead."*

- **Heavy system prompts.** From the [latency blog](https://livekit.com/blog/understand-and-improve-agent-latency): *"Context trimming: Reduce prompt and conversation history size."*

- **Synchronous tools in the call path.** Same blog: *"Limit `max_tool_steps`, consolidate external API calls, and use a 'thinking' sound so users aren't waiting in silence."*

- **Region drift.** From [regional checklist](https://livekit.com/field-guides/guide/checklist-for-regional-deployments): *"Even if LiveKit media is pinned to a specific region, external model APIs may process requests elsewhere."*

- **Burstable EC2 / shared CPU.** From [turn-detector docs](https://docs.livekit.io/agents/build/turns/turn-detector/): *"Use compute-optimized instances (AWS c6i/c7i) rather than burstable types to avoid inference timeouts due to CPU credit limits."* The Vultr equivalent is "High Frequency" or "CPU-Optimized" — *not* the standard shared "Cloud Compute".

- **Frontend + agent both running noise cancellation.** From [Noise cancellation docs](https://docs.livekit.io/transport/media/noise-cancellation/): *"When using noise or background voice cancellation in the agent code, do not enable noise cancellation models in the frontend."*

- **`sync_transcription=True` blocking audio out** — implied by the field's existence in [Agent session](https://docs.livekit.io/agents/logic/sessions/).

---

## 8. What LiveKit does NOT promise (be honest)

- **Sub-800ms is not an official SLO.** The closest LiveKit-published figure is "best-case ~415ms" in a workshop tutorial. The latency blog gives no numbers.
- **No published Gemini Live latency benchmark.** All concrete Gemini Live + SIP numbers in this project's research are from a community thread or third-party blog, not LiveKit docs.
- **Sub-second on phone is achievable but not guaranteed.** External benchmarks report 730–800ms first-syllable on cascade pipelines; Gemini Live native-audio is reported faster *on paper* but **community-reported 5–8s on SIP** when default config stacks turn detection.
- **No documented "fast-path" flag.** Latency comes from the sum of every knob in §3 above.

---

## 9. Sources

LiveKit official:

- [Agent deployment regions](https://docs.livekit.io/deploy/admin/regions/agent-deployment/)
- [Agents on LiveKit Cloud (product page)](https://livekit.com/products/agent-cloud-deployment)
- [Region pinning for telephony](https://docs.livekit.io/telephony/features/region-pinning/)
- [Checklist for Regional Deployments](https://livekit.com/field-guides/guide/checklist-for-regional-deployments)
- [Voice AI quickstart](https://docs.livekit.io/agents/start/voice-ai/)
- [Turns / endpointing / interruption](https://docs.livekit.io/agents/build/turns/)
- [Turn detector plugin (MultilingualModel)](https://docs.livekit.io/agents/build/turns/turn-detector/)
- [Improved End-of-Turn Model blog](https://livekit.com/blog/improved-end-of-turn-model-cuts-voice-ai-interruptions-39/)
- [Agent speech and audio (preemptive_generation, pre_connect_audio)](https://docs.livekit.io/agents/build/audio/)
- [RoomInputOptions reference](https://docs.livekit.io/reference/python/v1/livekit/agents/voice/room_io/index.html)
- [Agent session](https://docs.livekit.io/agents/logic/sessions/)
- [Noise & echo cancellation (BVCTelephony)](https://docs.livekit.io/transport/media/noise-cancellation/)
- [Gemini Live API plugin (LiveKit)](https://docs.livekit.io/agents/models/realtime/plugins/gemini/)
- [Gemini Live integration page](https://docs.livekit.io/agents/integrations/realtime/gemini/)
- [LiveKit Inference — Deepgram (Mumbai)](https://docs.livekit.io/agents/models/stt/inference/deepgram/)
- [Understand and Improve Voice Agent Latency (LiveKit blog)](https://livekit.com/blog/understand-and-improve-agent-latency)

Source code:

- [GitHub — agent_session.py](https://github.com/livekit/agents/blob/main/livekit-agents/livekit/agents/voice/agent_session.py)
- [Issue #4423 — Gemini 2.5 native audio + text modality limitation](https://github.com/livekit/agents/issues/4423)
- [Issue #3685 — telephony latency](https://github.com/livekit/agents/issues/3685)
- [Issue #4053 — EU region latency increase](https://github.com/livekit/agents/issues/4053)

Community:

- [High latency (5–8s) Gemini Realtime over SIP](https://community.livekit.io/t/high-latency-5-8-seconds-with-google-gemini-realtime-plugin-over-sip/238)

Workshop / unofficial LiveKit:

- [Voice Agent Foundations workshop tutorial](https://livekit.worksh.app/tutorials/livekit-voice-agent/introduction)

Third-party benchmarks:

- [Cracking the <1-second Voice Loop (CloudX)](https://dev.to/cloudx/cracking-the-1-second-voice-loop-what-we-learned-after-30-stack-benchmarks-427)
- [Cerebrium 500ms latency post](https://cerebrium.ai/blog/deploying-a-global-scale-ai-voice-agent-with-500ms-latency)

Google:

- [Gemini Live API capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities)

---

## 10. See also

- **[LATENCY.md](LATENCY.md)** — this project's tiered action plan, applying the findings above to the actual codebase
- **[CLAUDE.md](CLAUDE.md)** — architectural rules and SLOs that any change must respect
- **[README.md](README.md)** — project overview and deployment notes
