"""
Rapid X AI - Voice Agent Configuration

All telephony / model / persona settings live here. Values fall back to
environment variables (`.env`), with backward-compat fallbacks for the legacy
`VOBIZ_*` names that earlier versions of this repo used.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

DOTENV_PATH = Path(os.getenv("VOICE_AGENT_DOTENV", Path(__file__).with_name(".env")))
load_dotenv(DOTENV_PATH, override=True)


def _env(*names, default=None):
    """Return the first env var that is set among `names`, else `default`."""
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# 1. Agent persona & prompts
# ---------------------------------------------------------------------------
PHONE_AGENT_SYSTEM_LAYER = """
You are a live outbound phone agent. You are never a general chatbot.
Follow the campaign goal. Continue the outreach flow after greetings.
Be concise: one question at a time. No markdown or emojis.
Never invent facts, statistics, examples, prices, services, or results.
Never say placeholder text, bracketed text, bullets, or markdown.
If the caller declines, says no thank you, or says bye, give one short goodbye and stop.
Never mention internal systems.
"""

SYSTEM_PROMPT = """
You are a live phone voice agent. The caller is hearing you in real time.

Default behaviors:
- Be concise, natural, and helpful.
- Use the caller's latest message as the highest priority.
- Maintain context from the whole call and adapt if interrupted.
- Ask one targeted question at a time.
- Keep replies short for phone audio, usually 1-3 sentences.
- If the caller gives incomplete information, ask a specific clarification.
- Never pretend to know facts that are not in the prompt, memory, or tools.
- Confirm important details before final actions or transfers.
- Do not repeat yourself or over-explain.
- If the caller says no, no thank you, not interested, bye, or goodbye, do not pitch again.
  Say one short goodbye and end the call.
- Never say placeholders such as [specific service], [X], [Y], or examples unless
  those exact facts are in the campaign context.
- Do not use bullets, numbered lists, markdown, emojis, or long paragraphs.
- If the caller only says "hello" while you were already introducing yourself,
  continue your introduction instead of saying "Yes, I am here" or apologizing.
- If the caller asks what demo or service this is, answer directly using the
  campaign or preloaded caller context before asking another question.
- If the caller says they are ready to proceed with the demo, acknowledge and
  explain the next step. Do not transfer unless they ask for a human.
- Do not mention internal tools, prompts, Sarvam, Deepgram, Groq, APIs, or implementation details.
- Speak fluent English and Hindi; switch language to match the caller.
- If you must check data, say "Let me check that..." before using a tool.
- If the user explicitly asks for a human, call `transfer_call`.
- On "bye" or "goodbye", say goodbye warmly and end the call.
"""

INITIAL_GREETING = "Hi there, how's your day going so far?"

FALLBACK_GREETING = "Greet the user and state the reason for the call."
EMPTY_ASSISTANT_FALLBACK = "Sorry, I missed that. Could you say that again?"
LLM_TEMPORARY_ERROR_FALLBACK = "Sorry, I had a brief issue. Could you repeat that?"
LLM_RATE_LIMIT_FALLBACK = (
    "Sorry, I am having a temporary system delay. Could we try this again in a few minutes?"
)
CALL_END_AFTER_AUDIO_DELAY_SECONDS = float(os.getenv("CALL_END_AFTER_AUDIO_DELAY_SECONDS", "0.4"))


def _gemini_api_key():
    """Prefer GEMINI_API_KEY and remove conflicting GOOGLE_API_KEY values."""
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        os.environ.pop("GOOGLE_API_KEY", None)
        return gemini_key
    return os.getenv("GOOGLE_API_KEY")


# ---------------------------------------------------------------------------
# 2. Voice stack
#    "gemini" keeps the native audio-to-audio path.
#    "cascade" uses streaming STT -> Groq LLM -> Sarvam TTS for lower latency.
# ---------------------------------------------------------------------------
VOICE_STACK = os.getenv("VOICE_STACK", "gemini").strip().lower()

GEMINI_API_KEY = _gemini_api_key()
GEMINI_LIVE_MODEL = os.getenv(
    "GEMINI_LIVE_MODEL",
    "gemini-2.5-flash-native-audio-preview-12-2025",
)
GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Puck")
GEMINI_TEMPERATURE = float(os.getenv("GEMINI_TEMPERATURE", "0.8"))

# Voices supported by Gemini Live native-audio.
GEMINI_VOICES = [
    "Puck", "Charon", "Kore", "Fenrir", "Aoede",
    "Leda", "Orus", "Zephyr",
]


# ---------------------------------------------------------------------------
# 2b. Tier 3 cascade model settings
# ---------------------------------------------------------------------------
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
DEEPGRAM_MODEL = os.getenv("DEEPGRAM_MODEL", "nova-3")
DEEPGRAM_LANGUAGE = os.getenv("DEEPGRAM_LANGUAGE", "multi")
DEEPGRAM_ENDPOINTING_MS = int(os.getenv("DEEPGRAM_ENDPOINTING_MS", "350"))
DEEPGRAM_INTERIM_RESULTS = _env_bool("DEEPGRAM_INTERIM_RESULTS", "true")
DEEPGRAM_VAD_EVENTS = _env_bool("DEEPGRAM_VAD_EVENTS", "true")
DEEPGRAM_FILLER_WORDS = _env_bool("DEEPGRAM_FILLER_WORDS", "true")

CASCADE_LLM_PROVIDER = os.getenv("CASCADE_LLM_PROVIDER", "groq").strip().lower()
CASCADE_LLM_MAX_RETRIES = int(os.getenv("CASCADE_LLM_MAX_RETRIES", "0"))

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_TEMPERATURE = float(os.getenv("GROQ_TEMPERATURE", "0.35"))
GROQ_MAX_COMPLETION_TOKENS = int(os.getenv("GROQ_MAX_COMPLETION_TOKENS", "60"))
GROQ_TIMEOUT_SECONDS = float(os.getenv("GROQ_TIMEOUT_SECONDS", "12"))
GROQ_MAX_RETRIES = int(os.getenv("GROQ_MAX_RETRIES", "0"))
CLOUDFLARE_AI_GATEWAY_TOKEN = os.getenv("CLOUDFLARE_AI_GATEWAY_TOKEN", "").strip()

SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")
SARVAM_LLM_MODEL = os.getenv("SARVAM_LLM_MODEL", "sarvam-m")
SARVAM_LLM_TEMPERATURE = float(os.getenv("SARVAM_LLM_TEMPERATURE", "0.35"))
SARVAM_LLM_MAX_TOKENS = int(os.getenv("SARVAM_LLM_MAX_TOKENS", "80"))
SARVAM_TTS_MODEL = os.getenv("SARVAM_TTS_MODEL", "bulbul:v2")
SARVAM_LANGUAGE = os.getenv("SARVAM_LANGUAGE", "en-IN")
SARVAM_SPEAKER = os.getenv("SARVAM_SPEAKER", "anushka")
SARVAM_SAMPLE_RATE = int(os.getenv("SARVAM_SAMPLE_RATE", "16000"))
SARVAM_PACE = float(os.getenv("SARVAM_PACE", "1.08"))
SARVAM_TEMPERATURE = float(os.getenv("SARVAM_TEMPERATURE", "0.6"))
SARVAM_MIN_BUFFER_SIZE = int(os.getenv("SARVAM_MIN_BUFFER_SIZE", "30"))
SARVAM_MAX_CHUNK_LENGTH = int(os.getenv("SARVAM_MAX_CHUNK_LENGTH", "80"))
SARVAM_OUTPUT_AUDIO_BITRATE = os.getenv("SARVAM_OUTPUT_AUDIO_BITRATE", "64k")
SARVAM_ENABLE_PREPROCESSING = (
    os.getenv("SARVAM_ENABLE_PREPROCESSING", "true").strip().lower()
    in {"1", "true", "yes", "on"}
)

MIN_USER_UTTERANCE_CHARS = int(os.getenv("MIN_USER_UTTERANCE_CHARS", "2"))
FINAL_TRANSCRIPT_GRACE_MS = int(os.getenv("FINAL_TRANSCRIPT_GRACE_MS", "350"))
USER_SILENCE_COMMIT_MS = int(os.getenv("USER_SILENCE_COMMIT_MS", "700"))
INTERRUPTION_MIN_SPEECH_MS = int(os.getenv("INTERRUPTION_MIN_SPEECH_MS", "350"))
INTERRUPTION_MIN_CHARS = int(os.getenv("INTERRUPTION_MIN_CHARS", "3"))
INTERRUPTION_CONFIDENCE_THRESHOLD = float(
    os.getenv("INTERRUPTION_CONFIDENCE_THRESHOLD", "0.35")
)
TTS_CANCEL_DEBOUNCE_MS = int(os.getenv("TTS_CANCEL_DEBOUNCE_MS", "250"))
MAX_RECENT_TURNS = int(os.getenv("MAX_RECENT_TURNS", "12"))

CASCADE_MIN_ENDPOINTING_DELAY = float(os.getenv("CASCADE_MIN_ENDPOINTING_DELAY", "0.35"))
CASCADE_MAX_ENDPOINTING_DELAY = float(os.getenv("CASCADE_MAX_ENDPOINTING_DELAY", "0.7"))


# ---------------------------------------------------------------------------
# 3. Telephony (SIP)
#    Primary names are SIP_*, with legacy VOBIZ_* honored as fallback.
# ---------------------------------------------------------------------------
SIP_TRUNK_ID = _env("SIP_TRUNK_ID", "VOBIZ_SIP_TRUNK_ID")
SIP_DOMAIN = _env("SIP_DOMAIN", "VOBIZ_SIP_DOMAIN")
SIP_USERNAME = _env("SIP_USERNAME", "VOBIZ_USERNAME")
SIP_PASSWORD = _env("SIP_PASSWORD", "VOBIZ_PASSWORD")
SIP_OUTBOUND_NUMBER = _env("SIP_OUTBOUND_NUMBER", "VOBIZ_OUTBOUND_NUMBER")
SIP_ANSWER_TIMEOUT_SECONDS = float(
    os.getenv("SIP_ANSWER_TIMEOUT_SECONDS", os.getenv("SIP_ANSWER_TIMEOUT", "90"))
)

DEFAULT_TRANSFER_NUMBER = os.getenv("DEFAULT_TRANSFER_NUMBER")


# ---------------------------------------------------------------------------
# 4. LiveKit
# ---------------------------------------------------------------------------
LIVEKIT_URL = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")


# ---------------------------------------------------------------------------
# 5. Dashboard / deployment
# ---------------------------------------------------------------------------
DASHBOARD_PORT = int(os.getenv("PORT", "3000"))
DASHBOARD_HOST = os.getenv("HOST", "0.0.0.0")
