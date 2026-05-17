"""
Outbound voice agent powered by LiveKit + SIP.

The default stack remains Gemini Live native audio. For lower-latency Indian
telephony, `VOICE_STACK=cascade` switches to a streaming Deepgram STT -> Groq
LLM -> Sarvam TTS pipeline without adding request/response buffering.
"""

import os
import certifi

# Required before importing anything that opens TLS connections.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import asyncio
import json
import logging
import math
import time
import wave
from collections.abc import AsyncIterable
from dataclasses import asdict, is_dataclass
from typing import Any, Optional

import config
from google.genai import types as gtypes
from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, NOT_GIVEN, RoomInputOptions, StopResponse, llm
from livekit.agents.llm import ChatMessage
from livekit.agents.types import APIConnectOptions
from livekit.agents.voice.agent import ModelSettings
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.plugins import (
    deepgram,
    google,
    groq,
    noise_cancellation,
    openai as openai_plugin,
    sarvam,
    silero,
)
from livekit.plugins.turn_detector.multilingual import MultilingualModel
from voice_runtime import (
    ConversationRuntime,
    ConversationState,
    VoiceTimingConfig,
    deterministic_reply_for_user_turn,
    is_call_end_request,
    is_connection_check,
    is_human_transfer_request,
    now_ms as runtime_now_ms,
    voice_text_stream_transform,
)

_LOG_LEVEL = os.getenv("LIVEKIT_LOG_LEVEL", "info").upper()
logging.basicConfig(level=getattr(logging, _LOG_LEVEL, logging.INFO))
logger = logging.getLogger("voice-agent")


# ---------------------------------------------------------------------------
# Transcript log
# Each line is one JSON record so the dashboard can tail it as a stream.
# ---------------------------------------------------------------------------
TRANSCRIPT_LOG = "/tmp/transcripts.jsonl"
METRICS_LOG = os.getenv("VOICE_METRICS_LOG", "/tmp/voice-agent-metrics.jsonl")
RECORDINGS_DIR = os.getenv("VOICE_RECORDINGS_DIR", "/opt/livekit-ai-voice/recordings")
PROMPT_TOKEN_TARGET = int(os.getenv("REALTIME_PROMPT_TOKEN_TARGET", "300"))
LOW_LATENCY_VOICE_RULES = (
    "Low-latency voice rules: one short reply, then one question. "
    "Never say 'How can I assist you today?'"
)
CASCADE_STACK_ALIASES = {
    "cascade",
    "tier3",
    "stt-llm-tts",
    "deepgram-groq-sarvam",
}

PREFETCHED_CONTEXT_KEYS = (
    "lead_context",
    "customer_context",
    "crm_context",
    "order_context",
    "payment_context",
)


def _json_safe(value: Any):
    """Best-effort conversion for SDK metric objects and enums."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _emit_metric(room: str, event: str, **fields: Any):
    """Append one structured metrics record to a JSONL log."""
    record = {
        "ts": time.time(),
        "room": room,
        "event": event,
        **{k: _json_safe(v) for k, v in fields.items()},
    }
    try:
        with open(METRICS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("metrics write failed: %s", e)


def _approx_token_count(text: str) -> int:
    """Cheap realtime-prompt estimate; good enough for dashboard guardrails."""
    words = [w for w in text.strip().split() if w]
    return math.ceil(len(words) * 1.3)


def _prefetched_context_from_meta(meta: dict) -> str:
    parts: list[str] = []
    for key in PREFETCHED_CONTEXT_KEYS:
        value = meta.get(key)
        if not value:
            continue
        label = key.replace("_", " ")
        if isinstance(value, str):
            body = value.strip()
        else:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if body:
            parts.append(f"{label}: {body}")
    return "\n".join(parts)


def _build_session_instructions(meta: dict) -> str:
    system_prompt_override = (meta.get("system_prompt") or "").strip()
    base_prompt = system_prompt_override or config.SYSTEM_PROMPT
    user_prompt = (meta.get("user_prompt") or "").strip()
    lead_context = _prefetched_context_from_meta(meta)
    raw_lead_context = meta.get("lead_context")
    raw_lead_text = raw_lead_context.strip() if isinstance(raw_lead_context, str) else ""

    sections = [
        config.PHONE_AGENT_SYSTEM_LAYER.strip(),
        base_prompt.strip(),
        LOW_LATENCY_VOICE_RULES,
    ]
    if user_prompt:
        sections.append(f"Campaign context:\n{user_prompt}")
    if lead_context and raw_lead_text != user_prompt:
        sections.append(f"Preloaded caller context:\n{lead_context}")

    instructions = "\n\n".join(s for s in sections if s)
    token_estimate = _approx_token_count(instructions)
    if token_estimate > PROMPT_TOKEN_TARGET:
        logger.warning(
            "Realtime prompt is long: estimated_tokens=%s target=%s",
            token_estimate,
            PROMPT_TOKEN_TARGET,
        )
    return instructions


def _agent_tools_for_stack(voice_stack: str, tools: list[llm.Tool]) -> list[llm.Tool]:
    if voice_stack == "cascade" and config.CASCADE_LLM_PROVIDER == "sarvam":
        return []
    return tools


def _is_llm_rate_limit_error(exc: BaseException) -> bool:
    message = _exception_chain_text(exc)
    return (
        "429" in message
        and (
            "rate_limit" in message
            or "rate limit" in message
            or "tokens per day" in message
            or "tpd" in message
        )
    )


def _exception_chain_text(exc: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        parts.append(str(current))
        current = current.__cause__ or current.__context__
    return "\n".join(parts).lower()


def _is_sarvam_chat_order_error(exc: BaseException) -> bool:
    message = _exception_chain_text(exc)
    return (
        "400" in message
        and "user and assistant turns must alternate" in message
    )


def _build_voice_timing_config() -> VoiceTimingConfig:
    return VoiceTimingConfig(
        min_user_utterance_chars=config.MIN_USER_UTTERANCE_CHARS,
        final_transcript_grace_ms=config.FINAL_TRANSCRIPT_GRACE_MS,
        user_silence_commit_ms=config.USER_SILENCE_COMMIT_MS,
        interruption_min_speech_ms=config.INTERRUPTION_MIN_SPEECH_MS,
        interruption_min_chars=config.INTERRUPTION_MIN_CHARS,
        interruption_confidence_threshold=config.INTERRUPTION_CONFIDENCE_THRESHOLD,
        tts_cancel_debounce_ms=config.TTS_CANCEL_DEBOUNCE_MS,
        max_recent_turns=config.MAX_RECENT_TURNS,
    )


def _prepend_memory_context(chat_ctx: llm.ChatContext, memory_text: str) -> None:
    memory_text = memory_text.strip()
    if not memory_text:
        return
    memory_message = ChatMessage(
        role="system",
        content=[
            "Compact call memory for continuity. Use this only as context; "
            "the caller's latest message has highest priority.\n"
            f"{memory_text}"
        ],
    )
    insert_at = 0
    while (
        insert_at < len(chat_ctx.items)
        and isinstance(chat_ctx.items[insert_at], ChatMessage)
        and chat_ctx.items[insert_at].role in {"system", "developer"}
    ):
        insert_at += 1
    chat_ctx.items.insert(insert_at, memory_message)


def _merge_message_text(message: ChatMessage, text: str) -> None:
    existing = (message.text_content or "").strip()
    message.content = [f"{existing}\n{text}".strip() if existing else text]


def _sarvam_safe_chat_context(
    chat_ctx: llm.ChatContext,
    *,
    system_instructions: str = "",
    max_messages: int = 8,
) -> llm.ChatContext:
    """Return a Sarvam-compatible chat history: user-first and alternating."""
    safe_ctx = llm.ChatContext.empty()
    instruction_parts: list[str] = []

    def _append_instruction(text: str) -> None:
        text = text.strip()
        if text and text not in instruction_parts:
            instruction_parts.append(text)

    _append_instruction(system_instructions)

    for message in chat_ctx.messages():
        role = message.role
        if role in {"system", "developer"}:
            _append_instruction(message.text_content or "")
            continue
        if role not in ("user", "assistant"):
            continue

        text = (message.text_content or "").strip()
        if not text:
            continue
        if role == "assistant" and message.interrupted:
            continue

        messages = safe_ctx.messages()
        if not messages and role != "user":
            continue
        if messages and messages[-1].role == role:
            _merge_message_text(messages[-1], text)
            continue

        safe_ctx.add_message(
            role=role,
            content=text,
            interrupted=False,
            created_at=message.created_at,
        )

    while safe_ctx.messages() and safe_ctx.messages()[-1].role != "user":
        safe_ctx.items.pop()

    messages = safe_ctx.messages()
    if len(messages) > max_messages:
        messages = messages[-max_messages:]
        if messages and messages[0].role != "user":
            messages = messages[1:]
        safe_ctx = llm.ChatContext(list(messages))

    instruction_text = "\n".join(instruction_parts).strip()
    if instruction_text and safe_ctx.messages():
        first_message = safe_ctx.messages()[0]
        if first_message.role == "user":
            first_message.content = [
                f"Instructions:\n{instruction_text}\n\nCaller: {first_message.text_content}"
            ]

    return safe_ctx


def _role_signature(chat_ctx: llm.ChatContext) -> str:
    return ",".join(message.role for message in chat_ctx.messages())


def _replace_with_sarvam_safe_context(
    chat_ctx: llm.ChatContext,
    *,
    system_instructions: str,
    source: str,
) -> None:
    before_len = len(chat_ctx.items)
    before_roles = _role_signature(chat_ctx)
    safe_ctx = _sarvam_safe_chat_context(
        chat_ctx,
        system_instructions=system_instructions,
    )
    after_roles = _role_signature(safe_ctx)
    if (
        len(safe_ctx.items) != before_len
        or after_roles != before_roles
        or safe_ctx.items != chat_ctx.items
    ):
        logger.info(
            "Sanitized Sarvam chat history at %s: %s -> %s items roles=%s -> %s",
            source,
            before_len,
            len(safe_ctx.items),
            before_roles,
            after_roles,
        )
    chat_ctx.items = safe_ctx.items


def _build_turn_handling():
    return {
        "turn_detection": "realtime_llm",
        "interruption": {
            "enabled": True,
            "mode": "vad",
            "discard_audio_if_uninterruptible": True,
            "min_duration": 0.06,
            "min_words": 0,
            "resume_false_interruption": True,
            "false_interruption_timeout": 1.0,
        },
        "preemptive_generation": {
            "enabled": True,
            "preemptive_tts": True,
            "max_speech_duration": 8.0,
        },
    }


def _build_cascade_turn_handling():
    return {
        "turn_detection": MultilingualModel(),
        "endpointing": {
            "mode": "dynamic",
            "min_delay": config.CASCADE_MIN_ENDPOINTING_DELAY,
            "max_delay": config.CASCADE_MAX_ENDPOINTING_DELAY,
        },
        "interruption": {
            "enabled": True,
            "mode": "adaptive",
            "discard_audio_if_uninterruptible": True,
            "min_duration": config.INTERRUPTION_MIN_SPEECH_MS / 1000,
            "min_words": 1,
            "resume_false_interruption": True,
            "false_interruption_timeout": 1.0,
        },
        "preemptive_generation": {
            "enabled": False,
            "preemptive_tts": False,
            "max_speech_duration": 6.0,
        },
    }


def _build_realtime_input_config():
    return gtypes.RealtimeInputConfig(
        automatic_activity_detection=gtypes.AutomaticActivityDetection(
            disabled=False,
            start_of_speech_sensitivity=gtypes.StartSensitivity.START_SENSITIVITY_HIGH,
            end_of_speech_sensitivity=gtypes.EndSensitivity.END_SENSITIVITY_HIGH,
            prefix_padding_ms=20,
            silence_duration_ms=200,
        ),
        activity_handling=gtypes.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
        turn_coverage=gtypes.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY,
    )


# Loaded once at import; LiveKit's latency guidance: prewarm VAD before
# the first job lands so entrypoint() doesn't pay model-load on cold start.
# Tuned for barge-in only — turn-end is owned by Gemini's server-side VAD.
_VAD = silero.VAD.load(
    min_speech_duration=0.05,
    min_silence_duration=0.10,
    prefix_padding_duration=0.05,
    activation_threshold=0.5,
    sample_rate=16000,
)


def _is_target_sip_participant(participant: Any, phone_number: str | None) -> bool:
    identity = getattr(participant, "identity", "")
    if phone_number and identity == f"sip_{phone_number}":
        return True
    return identity.startswith("sip_")


def _emit_transcript(room: str, role: str, text: str, is_final: bool = True):
    """Append one transcript event to the JSONL log."""
    if not text:
        return
    record = {
        "ts": time.time(),
        "room": room,
        "role": role,        # "user" | "agent" | "system"
        "text": text,
        "is_final": is_final,
    }
    try:
        with open(TRANSCRIPT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("transcript write failed: %s", e)
    logger.info("[%s] %s: %s", role.upper(), room, text[:120])


async def _record_track_to_wav(
    track: rtc.Track,
    file_path: str,
    room_name: str,
    participant_identity: str,
):
    """Stream audio from a remote track to a local WAV file until the track ends."""
    started = time.time()
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    audio_stream = rtc.AudioStream(track)
    wf: Optional[wave.Wave_write] = None
    frame_count = 0
    sample_rate = 0

    try:
        async for event in audio_stream:
            frame = event.frame
            if wf is None:
                wf = wave.open(file_path, "wb")
                wf.setnchannels(frame.num_channels)
                wf.setsampwidth(2)  # LiveKit AudioFrame is 16-bit PCM
                wf.setframerate(frame.sample_rate)
                sample_rate = frame.sample_rate
                _emit_metric(
                    room_name,
                    "recording_started",
                    participant=participant_identity,
                    file=file_path,
                    sample_rate=sample_rate,
                    channels=frame.num_channels,
                )
            wf.writeframes(bytes(frame.data))
            frame_count += 1
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning("recording loop error for %s: %s", participant_identity, e)
    finally:
        if wf is not None:
            try:
                wf.close()
            except Exception:
                pass
        _emit_metric(
            room_name,
            "recording_finished",
            participant=participant_identity,
            file=file_path,
            frames=frame_count,
            duration_s=round(time.time() - started, 2),
        )


async def _end_call_room(ctx: agents.JobContext, room_name: str, reason: str) -> None:
    started = time.perf_counter()
    try:
        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=room_name))
        _emit_metric(
            room_name,
            "call_end_room_deleted",
            reason=reason,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
    except Exception as e:
        logger.warning("failed to end call room %s: %s", room_name, e)
        _emit_metric(
            room_name,
            "call_end_room_delete_failed",
            reason=reason,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            error=str(e),
        )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
class TransferFunctions(llm.ToolContext):
    def __init__(
        self,
        ctx: agents.JobContext,
        phone_number: Optional[str] = None,
        lead_context: str = "",
        runtime: ConversationRuntime | None = None,
    ):
        super().__init__(tools=[])
        self.ctx = ctx
        self.phone_number = phone_number
        self.lead_context = lead_context.strip()
        self.runtime = runtime

    def _lookup_user_details(self, phone: str) -> str:
        if self.lead_context:
            return f"Preloaded caller context for {phone}: {self.lead_context}"

        return (
            "User found: Shreyas Raj. Status: Premium. "
            "Last order: Coffee setup (Delivered)."
        )

    @llm.function_tool(description="Look up user details by phone number.")
    def lookup_user(self, phone: str):
        started = time.perf_counter()
        logger.info("Looking up user: %s", phone)
        try:
            return self._lookup_user_details(phone)
        finally:
            _emit_metric(
                self.ctx.room.name,
                "tool_call",
                tool_name="lookup_user",
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                prefetched=bool(self.lead_context),
            )

    @llm.function_tool(
        description=(
            "ONLY transfer when the caller explicitly asks to speak to a human, "
            "agent, operator, representative, or explicitly requests a transfer. "
            "Do not call this for demo scheduling, 'proceed with demo', consent, "
            "generic help, or abusive language."
        )
    )
    async def transfer_call(self, destination: Optional[str] = None):
        started = time.perf_counter()
        if not self._last_user_requested_transfer():
            _emit_metric(
                self.ctx.room.name,
                "tool_call",
                tool_name="transfer_call",
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                status="blocked_no_user_request",
            )
            return (
                "The caller did not ask for a human transfer. Do not transfer. "
                "Continue naturally: answer their latest question or confirm the demo next step."
            )
        if destination is None:
            destination = config.DEFAULT_TRANSFER_NUMBER
            if not destination:
                _emit_metric(
                    self.ctx.room.name,
                    "tool_call",
                    tool_name="transfer_call",
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    status="missing_destination",
                )
                return "Error: no default transfer number configured."

        # Build a SIP URI if the caller passed a bare number.
        if "@" not in destination:
            clean = destination.replace("tel:", "").replace("sip:", "")
            if config.SIP_DOMAIN:
                destination = f"sip:{clean}@{config.SIP_DOMAIN}"
            elif not destination.startswith(("tel:", "sip:")):
                destination = f"tel:{clean}"
        elif not destination.startswith("sip:"):
            destination = f"sip:{destination}"

        # Identify which participant to transfer.
        identity = None
        if self.phone_number:
            identity = f"sip_{self.phone_number}"
        else:
            for p in self.ctx.room.remote_participants.values():
                identity = p.identity
                break

        if not identity:
            logger.error("No remote participant to transfer.")
            return "Failed to transfer: could not identify the caller."

        try:
            logger.info("Transferring %s -> %s", identity, destination)
            await self.ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=self.ctx.room.name,
                    participant_identity=identity,
                    transfer_to=destination,
                    play_dialtone=False,
                )
            )
            _emit_metric(
                self.ctx.room.name,
                "tool_call",
                tool_name="transfer_call",
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                status="ok",
            )
            return "Transfer initiated successfully."
        except Exception as e:
            logger.exception("Transfer failed")
            _emit_metric(
                self.ctx.room.name,
                "tool_call",
                tool_name="transfer_call",
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                status="error",
                error=str(e),
            )
            return f"Error executing transfer: {e}"

    def _last_user_requested_transfer(self) -> bool:
        if self.runtime is None:
            return True
        for turn in reversed(self.runtime.memory.recent_turns):
            if turn.role == "user":
                return is_human_transfer_request(turn.text)
        return False


# ---------------------------------------------------------------------------
# Realtime model factory
# ---------------------------------------------------------------------------
def _coerce_float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("invalid float value %r; using %.2f", value, default)
        return default


def _select_voice_stack(meta: dict | None = None) -> str:
    requested = str((meta or {}).get("voice_stack") or config.VOICE_STACK).strip().lower()
    if requested in CASCADE_STACK_ALIASES:
        return "cascade"
    if requested in {"gemini", "realtime", "native", "native-audio"}:
        return "gemini"
    logger.warning("unknown VOICE_STACK=%r; falling back to gemini", requested)
    return "gemini"


def _require_env(name: str, value: str | None):
    if not value:
        raise RuntimeError(f"{name} is missing. Set it in .env.")
    return value


def _build_realtime_model(voice: Optional[str] = None,
                          temperature: Optional[float] = None,
                          system_prompt: Optional[str] = None):
    """Create a Gemini Live realtime model. Voice / temperature / instructions
    can be overridden per-call via room metadata."""
    voice = voice or config.GEMINI_VOICE
    temp = _coerce_float(temperature, config.GEMINI_TEMPERATURE)
    model_name = config.GEMINI_LIVE_MODEL

    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) is missing. Set it in .env."
        )

    logger.info(
        "Gemini Live: model=%s voice=%s temp=%.2f",
        model_name, voice, temp,
    )

    return google.realtime.RealtimeModel(
        model=model_name,
        api_key=config.GEMINI_API_KEY,
        voice=voice,
        temperature=temp,
        instructions=system_prompt or config.SYSTEM_PROMPT,
        realtime_input_config=_build_realtime_input_config(),
        input_audio_transcription=gtypes.AudioTranscriptionConfig(),
        output_audio_transcription=gtypes.AudioTranscriptionConfig(),
    )


def _build_cascade_models(temperature: Optional[float] = None):
    """Create streaming STT, LLM, and TTS providers for the Tier 3 path."""
    deepgram_key = _require_env("DEEPGRAM_API_KEY", config.DEEPGRAM_API_KEY)
    sarvam_key = _require_env("SARVAM_API_KEY", config.SARVAM_API_KEY)
    llm_provider = config.CASCADE_LLM_PROVIDER

    logger.info(
        "Cascade stack: stt=deepgram/%s:%s llm=%s tts=sarvam/%s:%s speaker=%s",
        config.DEEPGRAM_MODEL,
        config.DEEPGRAM_LANGUAGE,
        llm_provider,
        config.SARVAM_TTS_MODEL,
        config.SARVAM_LANGUAGE,
        config.SARVAM_SPEAKER or "default",
    )

    if llm_provider == "sarvam":
        llm_model = sarvam.LLM(
            model=config.SARVAM_LLM_MODEL,
            api_key=sarvam_key,
            temperature=_coerce_float(temperature, config.SARVAM_LLM_TEMPERATURE),
            max_tokens=config.SARVAM_LLM_MAX_TOKENS,
            reasoning_effort=None,
        )
    elif llm_provider == "groq":
        groq_key = _require_env("GROQ_API_KEY", config.GROQ_API_KEY)
        groq_kwargs = {
            "model": config.GROQ_MODEL,
            "api_key": groq_key,
            "base_url": config.GROQ_BASE_URL,
            "temperature": _coerce_float(temperature, config.GROQ_TEMPERATURE),
            "max_completion_tokens": config.GROQ_MAX_COMPLETION_TOKENS,
            "parallel_tool_calls": False,
            "timeout": config.GROQ_TIMEOUT_SECONDS,
            "max_retries": config.GROQ_MAX_RETRIES,
        }
        if config.CLOUDFLARE_AI_GATEWAY_TOKEN:
            llm_model = openai_plugin.LLM(
                **groq_kwargs,
                extra_headers={
                    "cf-aig-authorization": (
                        f"Bearer {config.CLOUDFLARE_AI_GATEWAY_TOKEN}"
                    )
                },
            )
        else:
            llm_model = groq.LLM(**groq_kwargs)
    else:
        raise RuntimeError(
            "CASCADE_LLM_PROVIDER must be either 'sarvam' or 'groq'. "
            f"Got {llm_provider!r}."
        )

    tts_model = sarvam.TTS(
        target_language_code=config.SARVAM_LANGUAGE,
        model=config.SARVAM_TTS_MODEL,
        speaker=config.SARVAM_SPEAKER or None,
        speech_sample_rate=config.SARVAM_SAMPLE_RATE,
        pace=config.SARVAM_PACE,
        temperature=config.SARVAM_TEMPERATURE,
        output_audio_bitrate=config.SARVAM_OUTPUT_AUDIO_BITRATE,
        min_buffer_size=config.SARVAM_MIN_BUFFER_SIZE,
        max_chunk_length=config.SARVAM_MAX_CHUNK_LENGTH,
        enable_preprocessing=config.SARVAM_ENABLE_PREPROCESSING,
        api_key=sarvam_key,
    )
    if hasattr(tts_model, "prewarm"):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("Skipping Sarvam TTS prewarm without a running event loop.")
        else:
            tts_model.prewarm()

    return {
        "stt": deepgram.STT(
            model=config.DEEPGRAM_MODEL,
            language=config.DEEPGRAM_LANGUAGE,
            api_key=deepgram_key,
            interim_results=config.DEEPGRAM_INTERIM_RESULTS,
            no_delay=True,
            endpointing_ms=config.DEEPGRAM_ENDPOINTING_MS,
            filler_words=config.DEEPGRAM_FILLER_WORDS,
            smart_format=False,
            vad_events=config.DEEPGRAM_VAD_EVENTS,
        ),
        "llm": llm_model,
        "tts": tts_model,
    }


class OutboundAssistant(Agent):
    def __init__(
        self,
        tools: list,
        instructions: str,
        llm: Any = NOT_GIVEN,
        *,
        sanitize_chat_history: bool = False,
        runtime: ConversationRuntime | None = None,
    ) -> None:
        self._sanitize_chat_history = sanitize_chat_history
        self._runtime = runtime
        self._runtime_instructions = instructions.strip()
        super().__init__(instructions=instructions, tools=tools, llm=llm)

    def stt_node(
        self,
        audio: AsyncIterable[rtc.AudioFrame],
        model_settings: ModelSettings,
    ):
        async def _filtered_stt_events():
            node = Agent.stt_node(self, audio, model_settings)
            if asyncio.iscoroutine(node):
                node = await node
            if not isinstance(node, AsyncIterable):
                return

            async for ev in node:
                if self._runtime is None:
                    yield ev
                    continue
                filtered = self._runtime.filter_stt_event(
                    ev,
                    assistant_speaking=(
                        self._runtime.state == ConversationState.ASSISTANT_SPEAKING
                    ),
                    now_ms=runtime_now_ms(),
                )
                if filtered is not None:
                    yield filtered

        return _filtered_stt_events()

    def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ):
        async def _guarded_llm_stream():
            assistant_turn_id = (
                self._runtime.start_assistant_turn(now_ms=runtime_now_ms())
                if self._runtime
                else None
            )
            started = time.perf_counter()
            first_token_seen = False
            response_parts: list[str] = []
            saw_tool_call = False

            deterministic_reply = self._deterministic_reply_for_latest_user_turn()
            if deterministic_reply:
                if self._runtime:
                    self._runtime.emit(
                        "deterministic_reply",
                        responseText=deterministic_reply,
                    )
                yield deterministic_reply
                return

            node = None
            try:
                if self._sanitize_chat_history:
                    _replace_with_sarvam_safe_context(
                        chat_ctx,
                        system_instructions=self._runtime_instructions,
                        source="llm_node",
                    )

                node = Agent.llm_node(self, chat_ctx, tools, model_settings)
                if asyncio.iscoroutine(node):
                    node = await node

                if isinstance(node, str):
                    if self._runtime and not self._runtime.is_active_assistant_turn(
                        assistant_turn_id
                    ):
                        self._runtime.emit(
                            "stale_response_discarded",
                            staleAssistantTurnId=assistant_turn_id,
                        )
                        return
                    response_parts.append(node)
                    yield node
                    return

                if not isinstance(node, AsyncIterable):
                    return

                async for chunk in node:
                    if self._runtime and not self._runtime.is_active_assistant_turn(
                        assistant_turn_id
                    ):
                        self._runtime.emit(
                            "stale_response_discarded",
                            staleAssistantTurnId=assistant_turn_id,
                        )
                        return
                    if self._runtime and not first_token_seen:
                        first_token_seen = True
                        self._runtime.emit(
                            "groq_first_token",
                            latencyMs=round((time.perf_counter() - started) * 1000, 2),
                        )

                    if isinstance(chunk, str):
                        response_parts.append(chunk)
                    else:
                        delta = getattr(chunk, "delta", None)
                        content = getattr(delta, "content", None) if delta else None
                        tool_calls = getattr(delta, "tool_calls", None) if delta else None
                        if tool_calls:
                            saw_tool_call = True
                        if content:
                            response_parts.append(content)
                    yield chunk

                if (
                    self._runtime
                    and not saw_tool_call
                    and not "".join(response_parts).strip()
                    and self._runtime.is_active_assistant_turn(assistant_turn_id)
                ):
                    fallback = config.EMPTY_ASSISTANT_FALLBACK
                    self._runtime.emit(
                        "empty_assistant_response_fallback",
                        responseText=fallback,
                    )
                    response_parts.append(fallback)
                    yield fallback
            except Exception as exc:
                if _is_llm_rate_limit_error(exc):
                    fallback = config.LLM_RATE_LIMIT_FALLBACK
                    event_type = "llm_rate_limit_fallback"
                elif _is_sarvam_chat_order_error(exc):
                    fallback = config.LLM_TEMPORARY_ERROR_FALLBACK
                    event_type = "llm_chat_order_fallback"
                else:
                    raise
                if self._runtime:
                    self._runtime.emit(
                        event_type,
                        error=str(exc),
                        responseText=fallback,
                    )
                response_parts.append(fallback)
                yield fallback
            finally:
                if self._runtime:
                    self._runtime.emit(
                        "groq_request_finished",
                        latencyMs=round((time.perf_counter() - started) * 1000, 2),
                        responseText="".join(response_parts).strip(),
                    )
                if node is not None and hasattr(node, "aclose"):
                    await node.aclose()

        return _guarded_llm_stream()

    def _deterministic_reply_for_latest_user_turn(self) -> str | None:
        if self._runtime is None:
            return None
        user_turns = [
            turn for turn in self._runtime.memory.recent_turns if turn.role == "user"
        ]
        connection_only_opening = bool(user_turns) and all(
            is_connection_check(turn.text) for turn in user_turns
        )
        for turn in reversed(self._runtime.memory.recent_turns):
            if turn.role == "user":
                if is_call_end_request(turn.text):
                    self._runtime.request_call_end("caller_declined")
                return deterministic_reply_for_user_turn(
                    turn.text,
                    instructions=(
                        self._runtime_instructions if connection_only_opening else ""
                    ),
                )
        return None

    def tts_node(
        self,
        text: AsyncIterable[str],
        model_settings: ModelSettings,
    ):
        async def _guarded_tts_stream():
            assistant_turn_id = (
                self._runtime.active_assistant_turn_id if self._runtime else None
            )
            started = time.perf_counter()
            first_audio_seen = False
            node = Agent.tts_node(self, text, model_settings)
            if asyncio.iscoroutine(node):
                node = await node
            if not isinstance(node, AsyncIterable):
                return

            try:
                async for frame in node:
                    if (
                        self._runtime
                        and assistant_turn_id is not None
                        and not self._runtime.is_active_assistant_turn(assistant_turn_id)
                    ):
                        self._runtime.emit(
                            "stale_tts_audio_discarded",
                            staleAssistantTurnId=assistant_turn_id,
                        )
                        return
                    if self._runtime and not first_audio_seen:
                        first_audio_seen = True
                        self._runtime.mark_assistant_speaking()
                        total_latency_ms = None
                        if self._runtime.last_user_commit_at_ms is not None:
                            total_latency_ms = (
                                runtime_now_ms() - self._runtime.last_user_commit_at_ms
                            )
                        self._runtime.emit(
                            "tts_first_audio",
                            latencyMs=round((time.perf_counter() - started) * 1000, 2),
                            userFinalToAssistantAudioLatencyMs=total_latency_ms,
                        )
                    yield frame
            finally:
                if hasattr(node, "aclose"):
                    await node.aclose()

        return _guarded_tts_stream()

    async def on_user_turn_completed(
        self,
        turn_ctx: llm.ChatContext,
        new_message: ChatMessage,
    ) -> None:
        memory_fragment = self._runtime.memory.prompt_fragment() if self._runtime else ""
        if self._runtime:
            committed = self._runtime.commit_livekit_user_turn(
                new_message.text_content or "",
                confidence=new_message.transcript_confidence,
                now_ms=runtime_now_ms(),
            )
            if not committed:
                raise StopResponse()
            new_message.content = [committed]
            _prepend_memory_context(turn_ctx, memory_fragment)
            turn_ctx.truncate(max_items=config.MAX_RECENT_TURNS + 4)

        if not self._sanitize_chat_history:
            return

        before_len = len(turn_ctx.items)
        before_roles = _role_signature(turn_ctx)
        _replace_with_sarvam_safe_context(
            turn_ctx,
            system_instructions=self._runtime_instructions,
            source="turn_completed",
        )
        if self._runtime:
            self._runtime.emit(
                "sarvam_chat_sanitized",
                source="turn_completed",
                originalItems=before_len,
                sanitizedItems=len(turn_ctx.items),
                originalRoles=before_roles,
                sanitizedRoles=_role_signature(turn_ctx),
            )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
async def entrypoint(ctx: agents.JobContext):
    logger.info("Connecting to room: %s", ctx.room.name)

    phone_number: Optional[str] = None
    meta: dict = {}

    # Job metadata (when dispatched via AgentDispatch).
    try:
        if ctx.job.metadata:
            data = json.loads(ctx.job.metadata)
            phone_number = data.get("phone_number")
            meta.update(data)
    except Exception:
        pass

    # Room metadata (set by the Python dashboard) wins over job metadata.
    try:
        if ctx.room.metadata:
            data = json.loads(ctx.room.metadata)
            if data.get("phone_number"):
                phone_number = data.get("phone_number")
            meta.update(data)
    except Exception:
        logger.warning("No valid JSON metadata on room.")

    room_name = ctx.room.name
    call_started = time.perf_counter()
    instructions = _build_session_instructions(meta)
    lead_context = _prefetched_context_from_meta(meta)
    voice_stack = _select_voice_stack(meta)

    def _emit_runtime_event(event_type: str, payload: dict[str, Any]) -> None:
        _emit_metric(room_name, "voice_runtime", **payload)

    runtime = ConversationRuntime(
        room_name,
        _build_voice_timing_config(),
        log_event=_emit_runtime_event,
    )

    _emit_metric(
        room_name,
        "call_started",
        phone_number=phone_number,
        voice_stack=voice_stack,
        prompt_tokens_estimate=_approx_token_count(instructions),
        has_prefetched_context=bool(lead_context),
    )

    fnc_ctx = TransferFunctions(
        ctx,
        phone_number,
        lead_context=lead_context,
        runtime=runtime,
    )

    tools = list(fnc_ctx.function_tools.values())
    agent_tools = _agent_tools_for_stack(voice_stack, tools)
    if tools and not agent_tools:
        logger.info(
            "Disabling function tools for cascade provider %s; model does not support tool schemas.",
            config.CASCADE_LLM_PROVIDER,
        )
    if voice_stack == "cascade":
        session = AgentSession(
            vad=_VAD,
            turn_handling=_build_cascade_turn_handling(),
            aec_warmup_duration=0.0,
            session_close_transcript_timeout=config.USER_SILENCE_COMMIT_MS / 1000,
            conn_options=SessionConnectOptions(
                llm_conn_options=APIConnectOptions(
                    max_retry=config.CASCADE_LLM_MAX_RETRIES,
                    timeout=config.GROQ_TIMEOUT_SECONDS,
                ),
            ),
            tts_text_transforms=[
                "filter_markdown",
                "filter_emoji",
                voice_text_stream_transform,
            ],
            max_tool_steps=2,
            **_build_cascade_models(temperature=meta.get("temperature")),
        )
        agent = OutboundAssistant(
            tools=agent_tools,
            instructions=instructions,
            sanitize_chat_history=config.CASCADE_LLM_PROVIDER == "sarvam",
            runtime=runtime,
        )
    else:
        realtime = _build_realtime_model(
            voice=meta.get("voice_id"),
            temperature=meta.get("temperature"),
            system_prompt=instructions,
        )
        session = AgentSession(
            vad=_VAD,
            turn_handling=_build_turn_handling(),
            aec_warmup_duration=0.0,
        )
        agent = OutboundAssistant(
            tools=agent_tools,
            instructions=instructions,
            llm=realtime,
            runtime=runtime,
        )

    _emit_transcript(room_name, "system", f"Call started -> {phone_number or 'inbound'}")

    last_user_speech_end: float | None = None
    interruption_started: float | None = None
    agent_state = "initializing"
    user_state = "listening"
    barge_in_task: asyncio.Task | None = None
    call_end_task: asyncio.Task | None = None

    async def _end_call_after_farewell(reason: str) -> None:
        await asyncio.sleep(config.CALL_END_AFTER_AUDIO_DELAY_SECONDS)
        await _end_call_room(ctx, room_name, reason)

    def _schedule_call_end_after_farewell(reason: str) -> None:
        nonlocal call_end_task
        if call_end_task and not call_end_task.done():
            return
        runtime.emit("call_end_scheduled", reason=reason)
        call_end_task = asyncio.create_task(_end_call_after_farewell(reason))

    async def _maybe_interrupt_after_debounce(turn_id: int, started_at_ms: int) -> None:
        await asyncio.sleep(config.INTERRUPTION_MIN_SPEECH_MS / 1000)
        if turn_id != runtime.user_turn_id:
            return
        if user_state != "speaking" or agent_state != "speaking":
            return
        decision = runtime.barge_in.evaluate(
            assistant_speaking=True,
            user_speech_started_at_ms=started_at_ms,
            now_ms=runtime_now_ms(),
            transcript=runtime.last_partial_transcript,
            confidence=runtime.last_partial_confidence,
        )
        runtime.emit(
            "interruption_evaluated",
            interruptionDetected=decision.should_interrupt,
            interruptionReason=decision.reason,
            partialTranscript=runtime.last_partial_transcript,
        )
        if not decision.should_interrupt:
            return

        nonlocal_interruption_started = time.perf_counter()
        runtime.cancel_assistant_turn(
            now_ms=runtime_now_ms(),
            reason=decision.reason,
        )
        _emit_metric(
            room_name,
            "caller_interruption_started",
            reason=decision.reason,
            debounced_ms=config.INTERRUPTION_MIN_SPEECH_MS,
        )
        try:
            interrupt_future = session.interrupt(force=True)
            _emit_metric(room_name, "agent_interrupt_requested", force=True)

            def _log_interrupt_result(future):
                nonlocal interruption_started
                interruption_started = nonlocal_interruption_started
                try:
                    error = future.exception()
                except asyncio.CancelledError:
                    return
                if error:
                    logger.warning("forced interrupt failed: %s", error)
                    runtime.emit(
                        "tts_cancel_failed",
                        interruptionReason=decision.reason,
                        error=str(error),
                    )
                    _emit_metric(room_name, "agent_interrupt_failed", error=str(error))

            interrupt_future.add_done_callback(_log_interrupt_result)
        except Exception as e:
            logger.warning("forced interrupt request failed: %s", e)
            runtime.emit(
                "tts_cancel_failed",
                interruptionReason=decision.reason,
                error=str(e),
            )
            _emit_metric(room_name, "agent_interrupt_failed", error=str(e))

    @session.on("user_state_changed")
    def _on_user_state(ev):
        nonlocal last_user_speech_end, user_state, barge_in_task
        previous = user_state
        user_state = getattr(ev, "new_state", user_state)
        now = time.perf_counter()
        event_now_ms = runtime_now_ms()
        _emit_metric(
            room_name,
            "user_state_changed",
            old_state=getattr(ev, "old_state", previous),
            new_state=user_state,
        )
        try:
            if user_state == "speaking":
                runtime.on_user_speech_started(now_ms=event_now_ms)
            elif previous == "speaking":
                runtime.on_user_speech_ended(now_ms=event_now_ms)
        except ValueError as e:
            logger.warning("voice state transition failed: %s", e)
            runtime.emit("state_transition_error", error=str(e))

        if user_state == "speaking" and agent_state == "speaking":
            if barge_in_task and not barge_in_task.done():
                barge_in_task.cancel()
            barge_in_task = asyncio.create_task(
                _maybe_interrupt_after_debounce(runtime.user_turn_id, event_now_ms)
            )
        if previous == "speaking" and user_state != "speaking":
            if barge_in_task and not barge_in_task.done():
                barge_in_task.cancel()
            last_user_speech_end = now
            _emit_metric(room_name, "caller_speech_ended")

    @session.on("agent_state_changed")
    def _on_agent_state(ev):
        nonlocal agent_state, last_user_speech_end, interruption_started
        previous = agent_state
        agent_state = getattr(ev, "new_state", agent_state)
        now = time.perf_counter()
        _emit_metric(
            room_name,
            "agent_state_changed",
            old_state=getattr(ev, "old_state", previous),
            new_state=agent_state,
        )
        try:
            if agent_state == "thinking":
                if runtime.state in {
                    ConversationState.IDLE,
                    ConversationState.USER_PAUSED,
                    ConversationState.INTERRUPTED,
                }:
                    runtime.transition(ConversationState.THINKING, event_type="agent_thinking")
                else:
                    runtime.emit("agent_thinking")
            elif agent_state == "speaking":
                runtime.mark_assistant_speaking()
            elif previous == "speaking" and agent_state != "speaking":
                runtime.emit("assistant_audio_stopped")
                call_end_reason = runtime.consume_call_end_request()
                if call_end_reason:
                    _schedule_call_end_after_farewell(call_end_reason)
        except ValueError as e:
            logger.warning("voice state transition failed: %s", e)
            runtime.emit("state_transition_error", error=str(e))
        if agent_state == "speaking" and last_user_speech_end is not None:
            _emit_metric(
                room_name,
                "caller_speech_end_to_first_bot_audio",
                duration_ms=round((now - last_user_speech_end) * 1000, 2),
                target_ms="600-900",
            )
            last_user_speech_end = None
        if previous == "speaking" and agent_state != "speaking" and interruption_started:
            _emit_metric(
                room_name,
                "caller_interruption_to_bot_stop",
                duration_ms=round((now - interruption_started) * 1000, 2),
                target_ms="<200",
            )
            interruption_started = None

    @session.on("user_input_transcribed")
    def _on_user_input(ev):
        # Streaming partials + final. We only persist finals to keep the log clean.
        if getattr(ev, "is_final", False):
            _emit_transcript(room_name, "user", ev.transcript or "", is_final=True)

    @session.on("conversation_item_added")
    def _on_item(ev):
        item = getattr(ev, "item", None)
        if isinstance(item, ChatMessage):
            text = item.text_content or ""
            role = item.role  # "user" | "assistant" | "system"
            mapped = "agent" if role == "assistant" else role
            metrics = getattr(item, "metrics", {}) or {}
            if metrics:
                _emit_metric(
                    room_name,
                    "turn_latency",
                    role=mapped,
                    **{
                        k: round(v * 1000, 2) if isinstance(v, (int, float)) else v
                        for k, v in metrics.items()
                    },
                )
            # Skip user echoes — already written by user_input_transcribed.
            if mapped == "user":
                return
            _emit_transcript(room_name, mapped, text)
            if mapped == "agent":
                runtime.finish_assistant_turn(
                    text,
                    interrupted=getattr(item, "interrupted", False),
                )

    @session.on("metrics_collected")
    def _on_metrics(ev):
        metrics = getattr(ev, "metrics", ev)
        payload = _json_safe(metrics)
        metric_type = (
            payload.get("type", type(metrics).__name__)
            if isinstance(payload, dict)
            else type(metrics).__name__
        )
        _emit_metric(room_name, "sdk_metric", metric_type=metric_type, metrics=payload)

    @session.on("session_usage_updated")
    def _on_usage(ev):
        _emit_metric(room_name, "session_usage_updated", usage=getattr(ev, "usage", None))

    session_start = time.perf_counter()
    await session.start(
        room=ctx.room,
        agent=agent,
        room_input_options=RoomInputOptions(
            audio_frame_size_ms=20,
            pre_connect_audio=True,
            noise_cancellation=noise_cancellation.BVCTelephony(),
            close_on_disconnect=True,
        ),
    )
    _emit_metric(
        room_name,
        "session_started",
        duration_ms=round((time.perf_counter() - session_start) * 1000, 2),
    )

    sip_participant_ready = asyncio.Event()
    recording_tasks: list[asyncio.Task] = []

    def _mark_sip_participant_ready(participant: Any, event_name: str):
        if not _is_target_sip_participant(participant, phone_number):
            return
        _emit_metric(
            room_name,
            event_name,
            participant_identity=getattr(participant, "identity", ""),
        )
        sip_participant_ready.set()

    @ctx.room.on("track_subscribed")
    def _on_track_subscribed(
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ):
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        if not _is_target_sip_participant(participant, phone_number):
            return
        file_path = os.path.join(RECORDINGS_DIR, f"{room_name}.wav")
        task = asyncio.create_task(
            _record_track_to_wav(track, file_path, room_name, participant.identity)
        )
        recording_tasks.append(task)

    @ctx.room.on("participant_connected")
    def _on_participant_connected(participant):
        _mark_sip_participant_ready(participant, "sip_participant_connected")

    @ctx.room.on("participant_active")
    def _on_participant_active(participant):
        _mark_sip_participant_ready(participant, "sip_participant_active")

    for participant in ctx.room.remote_participants.values():
        _mark_sip_participant_ready(participant, "sip_participant_already_present")

    # Decide whether the agent itself needs to dial out.
    should_dial = False
    if phone_number:
        already_present = any(
            "sip_" in p.identity for p in ctx.room.remote_participants.values()
        )
        should_dial = not already_present

    if should_dial:
        if not config.SIP_TRUNK_ID:
            logger.error("SIP_TRUNK_ID not configured — cannot dial out.")
            ctx.shutdown()
            return

        logger.info("Dialing %s via trunk %s", phone_number, config.SIP_TRUNK_ID)
        dial_started = time.perf_counter()
        _emit_metric(room_name, "sip_dial_started", phone_number=phone_number)
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=config.SIP_TRUNK_ID,
                    sip_call_to=phone_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=False,
                )
            )
            _emit_metric(
                room_name,
                "sip_invite_created",
                duration_ms=round((time.perf_counter() - dial_started) * 1000, 2),
            )
            await asyncio.wait_for(
                sip_participant_ready.wait(),
                timeout=config.SIP_ANSWER_TIMEOUT_SECONDS,
            )
            logger.info("SIP participant joined.")
            _emit_metric(
                room_name,
                "sip_answered",
                duration_ms=round((time.perf_counter() - dial_started) * 1000, 2),
            )
            await session.say(
                config.INITIAL_GREETING,
                allow_interruptions=True,
                add_to_chat_ctx=True,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "SIP participant did not join within %.1fs",
                config.SIP_ANSWER_TIMEOUT_SECONDS,
            )
            _emit_metric(
                room_name,
                "sip_answer_timeout",
                duration_ms=round((time.perf_counter() - dial_started) * 1000, 2),
                timeout_seconds=config.SIP_ANSWER_TIMEOUT_SECONDS,
            )
            ctx.shutdown()
        except Exception as e:
            logger.exception("Outbound dial failed: %s", e)
            _emit_metric(
                room_name,
                "sip_dial_failed",
                duration_ms=round((time.perf_counter() - dial_started) * 1000, 2),
                error=str(e),
            )
            ctx.shutdown()
    else:
        logger.info("Participant already in room — greeting.")
        _emit_metric(
            room_name,
            "ready_for_existing_participant",
            duration_ms=round((time.perf_counter() - call_started) * 1000, 2),
        )
        await session.say(
            config.INITIAL_GREETING,
            allow_interruptions=True,
            add_to_chat_ctx=True,
        )

    async def _on_shutdown():
        for task in recording_tasks:
            if not task.done():
                task.cancel()
        if recording_tasks:
            await asyncio.gather(*recording_tasks, return_exceptions=True)
        _emit_transcript(room_name, "system", "Call ended")

    ctx.add_shutdown_callback(_on_shutdown)


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller",
        )
    )
