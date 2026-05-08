"""
Outbound voice agent powered by Gemini Live native audio + LiveKit + Twilio SIP.

A single Gemini Live `RealtimeModel` handles STT, reasoning and TTS in one
streaming round-trip — there is no separate Deepgram / OpenAI / Cartesia
pipeline anymore.
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
from dataclasses import asdict, is_dataclass
from typing import Any, Optional

import config
from google.genai import types as gtypes
from livekit import agents, api
from livekit.agents import Agent, AgentSession, NOT_GIVEN, RoomInputOptions, llm
from livekit.agents.llm import ChatMessage
from livekit.plugins import google, noise_cancellation, silero

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voice-agent")


# ---------------------------------------------------------------------------
# Transcript log
# Each line is one JSON record so the dashboard can tail it as a stream.
# ---------------------------------------------------------------------------
TRANSCRIPT_LOG = "/tmp/transcripts.jsonl"
METRICS_LOG = os.getenv("VOICE_METRICS_LOG", "/tmp/voice-agent-metrics.jsonl")
PROMPT_TOKEN_TARGET = int(os.getenv("REALTIME_PROMPT_TOKEN_TARGET", "300"))
LOW_LATENCY_VOICE_RULES = (
    "Low-latency voice rules: reply in one short sentence. "
    "Ask one question at a time. Keep questions under 12 words."
)

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

    sections = [base_prompt.strip(), LOW_LATENCY_VOICE_RULES]
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


def _build_turn_handling():
    return {
        "turn_detection": "realtime_llm",
        "endpointing": {
            "mode": "fixed",
            "min_delay": 0.25,
            "max_delay": 0.5,
        },
        "interruption": {
            "enabled": True,
            "mode": "vad",
            "discard_audio_if_uninterruptible": True,
            "min_duration": 0.06,
            "min_words": 0,
            "resume_false_interruption": False,
            "false_interruption_timeout": 0.5,
        },
        "preemptive_generation": {
            "enabled": True,
            "preemptive_tts": False,
            "max_speech_duration": 8.0,
        },
    }


def _build_realtime_input_config():
    return gtypes.RealtimeInputConfig(
        automatic_activity_detection=gtypes.AutomaticActivityDetection(
            disabled=False,
            start_of_speech_sensitivity=gtypes.StartSensitivity.START_SENSITIVITY_HIGH,
            end_of_speech_sensitivity=gtypes.EndSensitivity.END_SENSITIVITY_HIGH,
            prefix_padding_ms=80,
            silence_duration_ms=300,
        ),
        activity_handling=gtypes.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
        turn_coverage=gtypes.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY,
    )


def _build_vad():
    return silero.VAD.load(
        min_speech_duration=0.025,
        min_silence_duration=0.25,
        prefix_padding_duration=0.1,
        activation_threshold=0.42,
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


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
class TransferFunctions(llm.ToolContext):
    def __init__(
        self,
        ctx: agents.JobContext,
        phone_number: Optional[str] = None,
        lead_context: str = "",
    ):
        super().__init__(tools=[])
        self.ctx = ctx
        self.phone_number = phone_number
        self.lead_context = lead_context.strip()

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
        description="Transfer the call to a human or another phone number."
    )
    async def transfer_call(self, destination: Optional[str] = None):
        started = time.perf_counter()
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


# ---------------------------------------------------------------------------
# Realtime model factory
# ---------------------------------------------------------------------------
def _model_supports_generate_reply(model_name: str) -> bool:
    return "3.1" not in model_name


def _select_realtime_model_name(requires_generate_reply: bool = False) -> str:
    configured = config.GEMINI_LIVE_MODEL
    if requires_generate_reply and not _model_supports_generate_reply(configured):
        fallback = config.GEMINI_PROACTIVE_REPLY_MODEL
        if not _model_supports_generate_reply(fallback):
            raise RuntimeError(
                "Configured Gemini Live model cannot proactively greet via "
                "LiveKit generate_reply, and GEMINI_PROACTIVE_REPLY_MODEL is "
                f"also incompatible: {fallback}"
            )
        logger.warning(
            "Gemini Live model %s cannot use generate_reply via LiveKit; "
            "using proactive reply model %s for outbound greeting support.",
            configured,
            fallback,
        )
        return fallback
    return configured


def _build_realtime_model(voice: Optional[str] = None,
                          temperature: Optional[float] = None,
                          system_prompt: Optional[str] = None,
                          requires_generate_reply: bool = False):
    """Create a Gemini Live realtime model. Voice / temperature / instructions
    can be overridden per-call via room metadata."""
    voice = voice or config.GEMINI_VOICE
    temp = temperature if temperature is not None else config.GEMINI_TEMPERATURE
    model_name = _select_realtime_model_name(
        requires_generate_reply=requires_generate_reply
    )

    if not config.GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) is missing. Set it in .env."
        )

    logger.info(
        "Gemini Live: model=%s voice=%s temp=%.2f",
        model_name, voice, temp,
    )

    model_kwargs: dict[str, Any] = {}
    if "3.1" in model_name:
        model_kwargs["thinking_config"] = gtypes.ThinkingConfig(
            thinking_level=gtypes.ThinkingLevel.MINIMAL,
        )

    return google.realtime.RealtimeModel(
        model=model_name,
        api_key=config.GEMINI_API_KEY,
        voice=voice,
        temperature=temp,
        instructions=system_prompt or config.SYSTEM_PROMPT,
        realtime_input_config=_build_realtime_input_config(),
        # Ask Gemini to also emit text transcripts for both sides of the call.
        input_audio_transcription=gtypes.AudioTranscriptionConfig(),
        output_audio_transcription=gtypes.AudioTranscriptionConfig(),
        **model_kwargs,
    )


class OutboundAssistant(Agent):
    def __init__(self, tools: list, instructions: str, llm: Any = NOT_GIVEN) -> None:
        super().__init__(instructions=instructions, tools=tools, llm=llm)


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

    # Room metadata (set by the Next.js dashboard) — wins over job metadata.
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

    _emit_metric(
        room_name,
        "call_started",
        phone_number=phone_number,
        prompt_tokens_estimate=_approx_token_count(instructions),
        has_prefetched_context=bool(lead_context),
    )

    fnc_ctx = TransferFunctions(ctx, phone_number, lead_context=lead_context)

    greeting_realtime = _build_realtime_model(
        voice=meta.get("voice_id"),
        temperature=meta.get("temperature"),
        system_prompt=instructions,
        requires_generate_reply=True,
    )
    main_realtime = _build_realtime_model(
        voice=meta.get("voice_id"),
        temperature=meta.get("temperature"),
        system_prompt=instructions,
        requires_generate_reply=False,
    )

    session = AgentSession(
        vad=_build_vad(),
        turn_handling=_build_turn_handling(),
        aec_warmup_duration=0.0,
    )

    tools = list(fnc_ctx.function_tools.values())
    greeting_agent = OutboundAssistant(
        tools=tools,
        instructions=instructions,
        llm=greeting_realtime,
    )
    main_agent = OutboundAssistant(
        tools=tools,
        instructions=instructions,
        llm=main_realtime,
    )

    async def _switch_to_main_realtime_agent():
        from_model = getattr(greeting_realtime, "model", "")
        to_model = getattr(main_realtime, "model", "")
        if from_model == to_model:
            return
        started = time.perf_counter()
        session.update_agent(main_agent)
        update_task = getattr(session, "_update_activity_atask", None)
        if update_task:
            await update_task
        _emit_metric(
            room_name,
            "realtime_model_switched",
            from_model=from_model,
            to_model=to_model,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    _emit_transcript(room_name, "system", f"Call started -> {phone_number or 'inbound'}")

    last_user_speech_end: float | None = None
    interruption_started: float | None = None
    agent_state = "initializing"
    user_state = "listening"

    @session.on("user_state_changed")
    def _on_user_state(ev):
        nonlocal last_user_speech_end, interruption_started, user_state
        previous = user_state
        user_state = getattr(ev, "new_state", user_state)
        _emit_metric(
            room_name,
            "user_state_changed",
            old_state=getattr(ev, "old_state", previous),
            new_state=user_state,
        )
        if user_state == "speaking" and agent_state == "speaking":
            interruption_started = time.perf_counter()
            _emit_metric(room_name, "caller_interruption_started")
            try:
                interrupt_future = session.interrupt(force=True)
                _emit_metric(room_name, "agent_interrupt_requested", force=True)

                def _log_interrupt_result(future):
                    try:
                        error = future.exception()
                    except asyncio.CancelledError:
                        return
                    if error:
                        logger.warning("forced interrupt failed: %s", error)
                        _emit_metric(
                            room_name,
                            "agent_interrupt_failed",
                            error=str(error),
                        )

                interrupt_future.add_done_callback(_log_interrupt_result)
            except Exception as e:
                logger.warning("forced interrupt request failed: %s", e)
                _emit_metric(room_name, "agent_interrupt_failed", error=str(e))
        if previous == "speaking" and user_state != "speaking":
            last_user_speech_end = time.perf_counter()
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
        agent=greeting_agent,
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

    def _mark_sip_participant_ready(participant: Any, event_name: str):
        if not _is_target_sip_participant(participant, phone_number):
            return
        _emit_metric(
            room_name,
            event_name,
            participant_identity=getattr(participant, "identity", ""),
        )
        sip_participant_ready.set()

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
            await session.generate_reply(
                instructions=config.INITIAL_GREETING,
                allow_interruptions=True,
            )
            await _switch_to_main_realtime_agent()
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
        await session.generate_reply(
            instructions=config.FALLBACK_GREETING,
            allow_interruptions=True,
        )
        await _switch_to_main_realtime_agent()

    async def _on_shutdown():
        _emit_transcript(room_name, "system", "Call ended")

    ctx.add_shutdown_callback(_on_shutdown)


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller",
        )
    )
