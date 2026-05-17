"""Production guardrails for the realtime voice pipeline.

This module is intentionally provider-light. LiveKit still owns audio
streaming, Deepgram still streams STT, Groq still streams LLM output, and
Sarvam still streams TTS. The classes here make the turn lifecycle explicit so
the agent does not answer on unstable text or cancel speech on tiny noises.
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urlparse

from livekit.agents import stt


class ConversationState(str, Enum):
    IDLE = "IDLE"
    USER_SPEAKING = "USER_SPEAKING"
    USER_PAUSED = "USER_PAUSED"
    THINKING = "THINKING"
    ASSISTANT_SPEAKING = "ASSISTANT_SPEAKING"
    INTERRUPTED = "INTERRUPTED"
    ERROR_RECOVERY = "ERROR_RECOVERY"


_VALID_TRANSITIONS: dict[ConversationState, set[ConversationState]] = {
    ConversationState.IDLE: {
        ConversationState.USER_SPEAKING,
        ConversationState.THINKING,
        ConversationState.ERROR_RECOVERY,
    },
    ConversationState.USER_SPEAKING: {
        ConversationState.USER_PAUSED,
        ConversationState.INTERRUPTED,
        ConversationState.ERROR_RECOVERY,
    },
    ConversationState.USER_PAUSED: {
        ConversationState.USER_SPEAKING,
        ConversationState.THINKING,
        ConversationState.IDLE,
        ConversationState.ERROR_RECOVERY,
    },
    ConversationState.THINKING: {
        ConversationState.ASSISTANT_SPEAKING,
        ConversationState.USER_SPEAKING,
        ConversationState.INTERRUPTED,
        ConversationState.IDLE,
        ConversationState.ERROR_RECOVERY,
    },
    ConversationState.ASSISTANT_SPEAKING: {
        ConversationState.IDLE,
        ConversationState.INTERRUPTED,
        ConversationState.USER_SPEAKING,
        ConversationState.ERROR_RECOVERY,
    },
    ConversationState.INTERRUPTED: {
        ConversationState.USER_SPEAKING,
        ConversationState.USER_PAUSED,
        ConversationState.THINKING,
        ConversationState.IDLE,
        ConversationState.ERROR_RECOVERY,
    },
    ConversationState.ERROR_RECOVERY: {
        ConversationState.IDLE,
        ConversationState.USER_SPEAKING,
    },
}


@dataclass(frozen=True)
class VoiceTimingConfig:
    min_user_utterance_chars: int = 2
    final_transcript_grace_ms: int = 350
    user_silence_commit_ms: int = 700
    interruption_min_speech_ms: int = 350
    interruption_min_chars: int = 3
    interruption_confidence_threshold: float = 0.35
    tts_cancel_debounce_ms: int = 250
    max_recent_turns: int = 12


FILLER_WORDS = {
    "ah",
    "aah",
    "eh",
    "er",
    "erm",
    "hmm",
    "hm",
    "mm",
    "mmm",
    "mhm",
    "uh",
    "umm",
    "um",
    "yeah",
}

CONNECTION_CHECK_PHRASES = {
    "alo",
    "aló",
    "hello",
    "hello hello",
    "hello are you there",
    "are you there",
    "you there",
    "can you hear me",
    "can u hear me",
    "do you hear me",
}

HUMAN_TRANSFER_ROLE_WORDS = {
    "adviser",
    "advisor",
    "agent",
    "counsellor",
    "counselor",
    "executive",
    "human",
    "manager",
    "operator",
    "person",
    "representative",
    "staff",
    "supervisor",
    "team",
}

HUMAN_TRANSFER_ACTION_WORDS = {
    "call",
    "connect",
    "pass",
    "put",
    "speak",
    "talk",
    "transfer",
}

HUMAN_TRANSFER_REQUEST_WORDS = {
    "need",
    "please",
    "want",
}

HUMAN_TRANSFER_DIRECT_PATTERNS = (
    re.compile(r"\btransfer\s+(?:me|my call|this call|the call)\b"),
    re.compile(r"\bput\s+me\s+through\b"),
)

CALLBACK_REQUEST_PHRASES = {
    "bad time",
    "busy",
    "call back",
    "call later",
    "call me later",
    "not a good time",
    "not good time",
    "talk later",
}

CALLBACK_TIME_WORDS = {
    "later",
    "minute",
    "minutes",
    "hour",
    "hours",
    "today",
    "tomorrow",
}

CALL_END_PHRASES = {
    "bye",
    "bye bye",
    "goodbye",
    "no thank you",
    "no thanks",
    "not interested",
    "stop calling",
    "don't call",
    "do not call",
}

CALL_CONTINUE_PHRASES = {
    "can start the call",
    "can we start",
    "start the call",
    "start call",
    "proceed",
    "go ahead",
}


def now_ms() -> int:
    return int(time.monotonic() * 1000)


def normalize_transcript(text: str) -> str:
    text = (text or "").replace("\u200b", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"([,.!?;:]){2,}", r"\1", text)
    return text.strip()


def _word_key(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.lower())


def is_meaningful_user_text(text: str, *, min_chars: int) -> bool:
    normalized = normalize_transcript(text)
    alnum = re.sub(r"[^0-9A-Za-z\u0900-\u097F]+", "", normalized)
    if len(alnum) < min_chars:
        return False
    words = _word_key(normalized)
    if not words:
        return False
    return not all(word in FILLER_WORDS for word in words)


def is_connection_check(text: str) -> bool:
    normalized = normalize_transcript(text).lower()
    normalized = re.sub(r"[^0-9a-z\u0900-\u097Fáéíóúüñ ]+", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if normalized in CONNECTION_CHECK_PHRASES:
        return True
    words = normalized.split()
    return bool(words) and len(words) <= 3 and all(word in {"hello", "alo", "aló"} for word in words)


def is_human_transfer_request(text: str) -> bool:
    normalized = normalize_transcript(text).lower()
    if not normalized:
        return False
    if any(pattern.search(normalized) for pattern in HUMAN_TRANSFER_DIRECT_PATTERNS):
        return True

    words = set(_word_key(normalized))
    if not words:
        return False

    has_human_role = bool(words & HUMAN_TRANSFER_ROLE_WORDS)
    has_transfer_action = bool(words & HUMAN_TRANSFER_ACTION_WORDS)
    has_human_request_word = bool(words & HUMAN_TRANSFER_REQUEST_WORDS)
    asks_for_someone = "someone" in words and bool(words & {"talk", "speak", "connect"})
    asks_about_identity = normalized.startswith(("are you", "is this", "am i"))
    return (
        has_human_role
        and (has_transfer_action or (has_human_request_word and not asks_about_identity))
    ) or asks_for_someone


def is_callback_request(text: str) -> bool:
    normalized = normalize_transcript(text).lower()
    if not normalized:
        return False
    if any(phrase in normalized for phrase in CALLBACK_REQUEST_PHRASES):
        return True

    words = set(_word_key(normalized))
    if "call" in words and words & CALLBACK_TIME_WORDS:
        return True
    return "bad" in words and "call" in words and "later" in words


def is_call_end_request(text: str) -> bool:
    normalized = normalize_transcript(text).lower()
    if not normalized:
        return False

    compact = re.sub(r"[^0-9a-z\u0900-\u097F' ]+", " ", normalized)
    compact = re.sub(r"\s+", " ", compact).strip()
    if any(phrase in compact for phrase in CALL_CONTINUE_PHRASES):
        return False
    if any(phrase in compact for phrase in CALL_END_PHRASES):
        return True

    words = set(_word_key(compact))
    polite_no_words = {"no", "nah", "nope", "thank", "thanks", "you"}
    if words and words <= polite_no_words and "no" in words:
        return True
    return False


def _connection_check_campaign_reply(instructions: str) -> str | None:
    normalized = instructions.lower()
    if (
        "cagos" in normalized
        or "chartered accountant" in normalized
        or "chartered accountancy" in normalized
        or "ca firm" in normalized
    ):
        return (
            "Thanks. I'm Ava calling from CAGOS. We help CA firms handle more "
            "clients without hiring. Can I take 20 seconds?"
        )
    if "protech planner" in normalized or ("demo website" in normalized and "cpa" in normalized):
        return (
            "Thanks. I'm Ava from Protech Planner. We made a free demo website "
            "for your firm. May I send you the link on WhatsApp or email?"
        )
    return None


def deterministic_reply_for_user_turn(text: str, *, instructions: str = "") -> str | None:
    if is_call_end_request(text):
        return "Understood. Have a great day!"
    if is_callback_request(text):
        return "No worries at all. What time today would be better for a quick call?"
    if instructions and is_connection_check(text):
        return _connection_check_campaign_reply(instructions)
    return None


def merge_transcript_segments(segments: list[str]) -> str:
    merged_words: list[str] = []
    for segment in segments:
        words = segment.split()
        if not words:
            continue
        if not merged_words:
            merged_words.extend(words)
            continue

        max_overlap = min(len(merged_words), len(words))
        overlap = 0
        for size in range(max_overlap, 0, -1):
            left = [w.lower() for w in merged_words[-size:]]
            right = [w.lower() for w in words[:size]]
            if left == right:
                overlap = size
                break
        merged_words.extend(words[overlap:])
    return normalize_transcript(" ".join(merged_words))


@dataclass
class TranscriptBuffer:
    config: VoiceTimingConfig
    partial_transcript: str = ""
    final_transcript_segments: list[str] = field(default_factory=list)
    committed_user_turns: list[str] = field(default_factory=list)
    current_utterance_started_at_ms: int | None = None
    last_speech_at_ms: int | None = None
    last_transcript_at_ms: int | None = None
    speech_final_seen: bool = False
    final_confidences: list[float] = field(default_factory=list)

    def start_speech(self, *, now_ms: int) -> None:
        if self.current_utterance_started_at_ms is None:
            self.current_utterance_started_at_ms = now_ms
        self.last_speech_at_ms = now_ms
        self.speech_final_seen = False

    def update_partial(self, text: str, *, now_ms: int, confidence: float = 0.0) -> None:
        del confidence
        self.partial_transcript = normalize_transcript(text)
        self.last_transcript_at_ms = now_ms

    def add_final(self, text: str, *, now_ms: int, confidence: float = 0.0) -> str | None:
        normalized = normalize_transcript(text)
        self.last_transcript_at_ms = now_ms
        self.partial_transcript = ""
        if confidence and confidence < self.config.interruption_confidence_threshold:
            return None
        if not is_meaningful_user_text(
            normalized,
            min_chars=self.config.min_user_utterance_chars,
        ):
            return None

        before = self.merged_final_text()
        candidate_segments = [*self.final_transcript_segments, normalized]
        after = merge_transcript_segments(candidate_segments)
        if after == before:
            return None

        self.final_transcript_segments = candidate_segments
        self.final_confidences.append(confidence)
        if before and after.lower().startswith(before.lower()):
            return normalize_transcript(after[len(before):])
        return normalized

    def mark_speech_final(self, *, now_ms: int) -> None:
        self.speech_final_seen = True
        self.last_speech_at_ms = now_ms

    def merged_final_text(self) -> str:
        return merge_transcript_segments(self.final_transcript_segments)

    def ready_to_commit(self, *, now_ms: int) -> bool:
        if not self.final_transcript_segments:
            return False
        if self.speech_final_seen and self.last_transcript_at_ms is not None:
            if now_ms - self.last_transcript_at_ms >= self.config.final_transcript_grace_ms:
                return True
        if self.last_speech_at_ms is not None:
            return now_ms - self.last_speech_at_ms >= self.config.user_silence_commit_ms
        return False

    def commit_if_ready(self, *, now_ms: int) -> str | None:
        if not self.ready_to_commit(now_ms=now_ms):
            return None
        text = self.merged_final_text()
        self.reset_current()
        if not is_meaningful_user_text(text, min_chars=self.config.min_user_utterance_chars):
            return None
        self.committed_user_turns.append(text)
        return text

    def reset_current(self) -> None:
        self.partial_transcript = ""
        self.final_transcript_segments = []
        self.current_utterance_started_at_ms = None
        self.last_speech_at_ms = None
        self.last_transcript_at_ms = None
        self.speech_final_seen = False
        self.final_confidences = []


@dataclass(frozen=True)
class BargeInDecision:
    should_interrupt: bool
    reason: str


@dataclass
class BargeInController:
    config: VoiceTimingConfig
    last_cancelled_at_ms: int | None = None

    def evaluate(
        self,
        *,
        assistant_speaking: bool,
        user_speech_started_at_ms: int | None,
        now_ms: int,
        transcript: str = "",
        confidence: float | None = None,
    ) -> BargeInDecision:
        if not assistant_speaking:
            return BargeInDecision(False, "assistant_not_speaking")
        if user_speech_started_at_ms is None:
            return BargeInDecision(False, "no_user_speech_start")
        duration_ms = now_ms - user_speech_started_at_ms
        if duration_ms < self.config.interruption_min_speech_ms:
            return BargeInDecision(False, "speech_too_short")
        if (
            self.last_cancelled_at_ms is not None
            and now_ms - self.last_cancelled_at_ms < self.config.tts_cancel_debounce_ms
        ):
            return BargeInDecision(False, "cancel_debounce")
        if (
            confidence is not None
            and confidence > 0
            and confidence < self.config.interruption_confidence_threshold
        ):
            return BargeInDecision(False, "low_confidence")

        normalized = normalize_transcript(transcript)
        if normalized:
            if is_connection_check(normalized):
                return BargeInDecision(False, "connection_check_backchannel")
            if not is_meaningful_user_text(
                normalized,
                min_chars=self.config.interruption_min_chars,
            ):
                return BargeInDecision(False, "filler_or_noise")
            return BargeInDecision(True, "meaningful_transcript")

        return BargeInDecision(True, "sustained_vad")

    def mark_cancelled(self, *, now_ms: int) -> None:
        self.last_cancelled_at_ms = now_ms


@dataclass
class MemoryTurn:
    role: str
    text: str
    interrupted: bool = False


@dataclass
class ConversationMemory:
    max_recent_turns: int
    summary: str = ""
    recent_turns: list[MemoryTurn] = field(default_factory=list)

    def record(self, role: str, text: str, *, interrupted: bool = False) -> None:
        normalized = normalize_transcript(text)
        if not normalized:
            return
        self.recent_turns.append(MemoryTurn(role=role, text=normalized, interrupted=interrupted))
        self._compact_if_needed()

    def _compact_if_needed(self) -> None:
        overflow = max(0, len(self.recent_turns) - self.max_recent_turns)
        if overflow <= 0:
            return
        older = self.recent_turns[:overflow]
        self.recent_turns = self.recent_turns[overflow:]
        older_text = "; ".join(f"{turn.role}: {turn.text}" for turn in older)
        if not older_text:
            return
        if self.summary:
            self.summary = normalize_transcript(f"{self.summary}; {older_text}")
        else:
            self.summary = older_text

    def prompt_fragment(self) -> str:
        parts: list[str] = []
        if self.summary:
            parts.append(f"Older call summary: {self.summary}")
        if self.recent_turns:
            recent = "; ".join(f"{turn.role}: {turn.text}" for turn in self.recent_turns)
            parts.append(f"Recent call context: {recent}")
        return "\n".join(parts)


class ConversationRuntime:
    def __init__(
        self,
        session_id: str,
        config: VoiceTimingConfig,
        *,
        log_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self.config = config
        self.state = ConversationState.IDLE
        self.transcripts = TranscriptBuffer(config)
        self.barge_in = BargeInController(config)
        self.memory = ConversationMemory(config.max_recent_turns)
        self.user_turn_id = 0
        self.assistant_turn_id = 0
        self.request_id = 0
        self.active_assistant_turn_id: int | None = None
        self.user_speech_started_at_ms: int | None = None
        self.last_user_commit_at_ms: int | None = None
        self.last_partial_transcript = ""
        self.last_partial_confidence: float | None = None
        self.pending_call_end_reason: str | None = None
        self.log_event = log_event

    def emit(self, event_type: str, **fields: Any) -> None:
        if self.log_event is None:
            return
        payload = {
            "sessionId": self.session_id,
            "state": self.state.value,
            "eventType": event_type,
            "userTurnId": self.user_turn_id,
            "assistantTurnId": self.assistant_turn_id,
            **fields,
        }
        self.log_event(event_type, payload)

    def transition(self, new_state: ConversationState, *, event_type: str) -> None:
        if new_state == self.state:
            self.emit(event_type, state=self.state.value)
            return
        if new_state not in _VALID_TRANSITIONS[self.state]:
            raise ValueError(
                f"invalid conversation transition {self.state.value} -> {new_state.value}"
            )
        old_state = self.state
        self.state = new_state
        self.emit(event_type, oldState=old_state.value, newState=new_state.value)

    def on_user_speech_started(self, *, now_ms: int) -> None:
        if self.state != ConversationState.USER_SPEAKING:
            self.user_turn_id += 1
            if self.state == ConversationState.ASSISTANT_SPEAKING:
                self.transition(ConversationState.USER_SPEAKING, event_type="user_speech_started")
            elif self.state == ConversationState.INTERRUPTED:
                self.transition(ConversationState.USER_SPEAKING, event_type="user_speech_started")
            elif self.state in {ConversationState.THINKING, ConversationState.IDLE}:
                self.transition(ConversationState.USER_SPEAKING, event_type="user_speech_started")
            elif self.state == ConversationState.USER_PAUSED:
                self.transition(ConversationState.USER_SPEAKING, event_type="user_resumed")
        self.user_speech_started_at_ms = now_ms
        self.transcripts.start_speech(now_ms=now_ms)

    def on_user_speech_ended(self, *, now_ms: int) -> None:
        self.transcripts.last_speech_at_ms = now_ms
        if self.state == ConversationState.USER_SPEAKING:
            self.transition(ConversationState.USER_PAUSED, event_type="user_speech_ended")

    def commit_livekit_user_turn(
        self,
        text: str,
        *,
        confidence: float | None = None,
        now_ms: int | None = None,
    ) -> str | None:
        committed_at_ms = now_ms
        normalized = normalize_transcript(text)
        if (
            confidence is not None
            and confidence > 0
            and confidence < self.config.interruption_confidence_threshold
        ):
            self.emit(
                "user_turn_ignored",
                reason="low_confidence",
                finalTranscript=normalized,
                confidence=confidence,
            )
            return None
        if not is_meaningful_user_text(
            normalized,
            min_chars=self.config.min_user_utterance_chars,
        ):
            self.emit("user_turn_ignored", reason="filler_or_noise", finalTranscript=normalized)
            return None
        self.transcripts.reset_current()
        self.last_user_commit_at_ms = committed_at_ms
        self.memory.record("user", normalized)
        if self.pending_call_end_reason and not is_call_end_request(normalized):
            self.pending_call_end_reason = None
            self.emit("call_end_cancelled", reason="new_user_turn")
        if self.state in {
            ConversationState.USER_SPEAKING,
            ConversationState.USER_PAUSED,
            ConversationState.INTERRUPTED,
            ConversationState.IDLE,
        }:
            self.transition(ConversationState.THINKING, event_type="turn_committed")
        self.emit("committed_user_input", committedUserInput=normalized)
        return normalized

    def start_assistant_turn(self, *, now_ms: int | None = None) -> int:
        del now_ms
        self.assistant_turn_id += 1
        self.request_id += 1
        self.active_assistant_turn_id = self.assistant_turn_id
        if self.state in {
            ConversationState.IDLE,
            ConversationState.USER_PAUSED,
            ConversationState.INTERRUPTED,
        }:
            self.transition(ConversationState.THINKING, event_type="groq_request_started")
        else:
            self.emit("groq_request_started")
        return self.assistant_turn_id

    def mark_assistant_speaking(self) -> None:
        if self.state == ConversationState.THINKING:
            self.transition(ConversationState.ASSISTANT_SPEAKING, event_type="tts_first_audio")
        else:
            self.emit("tts_first_audio")

    def finish_assistant_turn(self, text: str, *, interrupted: bool = False) -> None:
        self.memory.record("assistant", text, interrupted=interrupted)
        if self.state in {
            ConversationState.ASSISTANT_SPEAKING,
            ConversationState.THINKING,
            ConversationState.INTERRUPTED,
        }:
            self.transition(ConversationState.IDLE, event_type="assistant_turn_finished")
        self.active_assistant_turn_id = None

    def request_call_end(self, reason: str) -> None:
        self.pending_call_end_reason = reason
        self.emit("call_end_requested", reason=reason)

    def consume_call_end_request(self) -> str | None:
        reason = self.pending_call_end_reason
        self.pending_call_end_reason = None
        return reason

    def cancel_assistant_turn(self, *, now_ms: int, reason: str) -> None:
        cancelled_turn = self.active_assistant_turn_id
        self.assistant_turn_id += 1
        self.active_assistant_turn_id = None
        if self.pending_call_end_reason:
            self.pending_call_end_reason = None
            self.emit("call_end_cancelled", reason="assistant_interrupted")
        self.barge_in.mark_cancelled(now_ms=now_ms)
        if self.state in {
            ConversationState.ASSISTANT_SPEAKING,
            ConversationState.THINKING,
            ConversationState.USER_SPEAKING,
        }:
            self.transition(ConversationState.INTERRUPTED, event_type="assistant_interrupted")
        self.emit(
            "cancelled_turn",
            cancelledAssistantTurnId=cancelled_turn,
            interruptionReason=reason,
        )

    def is_active_assistant_turn(self, assistant_turn_id: int | None) -> bool:
        return (
            assistant_turn_id is not None
            and self.active_assistant_turn_id == assistant_turn_id
            and assistant_turn_id == self.assistant_turn_id
        )

    def filter_stt_event(
        self,
        ev: stt.SpeechEvent,
        *,
        assistant_speaking: bool,
        now_ms: int,
    ) -> stt.SpeechEvent | None:
        if ev.type == stt.SpeechEventType.START_OF_SPEECH:
            self.on_user_speech_started(now_ms=now_ms)
            self.emit("stt_speech_started")
            return ev

        if ev.type == stt.SpeechEventType.END_OF_SPEECH:
            self.transcripts.mark_speech_final(now_ms=now_ms)
            self.on_user_speech_ended(now_ms=now_ms)
            self.emit("stt_speech_final")
            return ev

        if not ev.alternatives:
            return ev

        alt = ev.alternatives[0]
        transcript = normalize_transcript(alt.text)
        confidence = alt.confidence
        if ev.type == stt.SpeechEventType.INTERIM_TRANSCRIPT:
            self.transcripts.update_partial(
                transcript,
                now_ms=now_ms,
                confidence=confidence,
            )
            self.last_partial_transcript = transcript
            self.last_partial_confidence = confidence
            self.emit(
                "stt_partial",
                partialTranscript=transcript,
                confidence=confidence,
            )
            if assistant_speaking:
                if is_connection_check(transcript):
                    self.emit(
                        "stt_partial_suppressed",
                        reason="connection_check_backchannel",
                        partialTranscript=transcript,
                        confidence=confidence,
                    )
                    return None
                elapsed = (
                    now_ms - self.user_speech_started_at_ms
                    if self.user_speech_started_at_ms is not None
                    else 0
                )
                if elapsed < self.config.interruption_min_speech_ms:
                    return None
            return ev

        if ev.type == stt.SpeechEventType.FINAL_TRANSCRIPT:
            if assistant_speaking and is_connection_check(transcript):
                self.emit(
                    "stt_final_suppressed",
                    reason="connection_check_backchannel",
                    finalTranscript=transcript,
                    confidence=confidence,
                )
                return None
            added = self.transcripts.add_final(
                transcript,
                now_ms=now_ms,
                confidence=confidence,
            )
            if not added:
                self.emit(
                    "stt_final_suppressed",
                    finalTranscript=transcript,
                    confidence=confidence,
                )
                return None
            alt.text = added
            self.emit(
                "stt_final",
                finalTranscript=self.transcripts.merged_final_text(),
                emittedTranscript=alt.text,
                confidence=confidence,
            )
            return ev

        return ev


def sanitize_voice_response(text: str) -> str:
    text = text or ""
    text = re.sub(r"https?://[^\s)]+", lambda match: _speakable_url(match.group(0)), text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    text = re.sub(r"\[[^\]]+\]", "", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "", text)
    text = re.sub(r"(?m)^\s*\d+[.)]\s+", "", text)
    text = re.sub(r"\s+[-*]\s+", " ", text)
    text = text.replace("&", " and ")
    text = text.replace("@", " at ")
    text = text.replace("#", "")
    text = re.sub(r"\s*/\s*", " slash ", text)
    text = re.sub(r"\s+", " ", text)
    return normalize_transcript(text)


def _speakable_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc or parsed.path.split("/")[0]
    host = host.replace("www.", "")
    return host.replace(".", " dot ")


def voice_text_stream_transform(text: AsyncIterable[str]) -> AsyncIterable[str]:
    async def _transform() -> AsyncIterable[str]:
        buffer = ""
        async for chunk in text:
            buffer += chunk
            if len(buffer) < 120 and not re.search(r"[.!?\n]$", buffer):
                continue
            cleaned = sanitize_voice_response(buffer)
            if cleaned:
                yield cleaned + " "
            buffer = ""
        cleaned = sanitize_voice_response(buffer)
        if cleaned:
            yield cleaned

    return _transform()
