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


# ---------------------------------------------------------------------------
# 1. Agent persona & prompts
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """
You are an outbound voice assistant. The user did not initiate this call —
you are calling them.

Default behaviors (override these via the dashboard "System Prompt" field):
- Open by introducing who you are and why you are calling.
- Speak fluent English and Hindi; switch language to match the user.
- Keep replies to 1-2 sentences.
- Ask one question at a time.
- If you must check data, say "Let me check that..." before using a tool.
- If the user explicitly asks for a human, call `transfer_call`.
- On "bye" / "goodbye", say goodbye warmly and end the call.

This is a generic fallback. Set a real persona per call from the dashboard.
"""

INITIAL_GREETING = (
    "The user has picked up the call. Introduce yourself and state the "
    "reason for the call immediately, per your persona instructions."
)

FALLBACK_GREETING = "Greet the user and state the reason for the call."


def _gemini_api_key():
    """Prefer GEMINI_API_KEY and remove conflicting GOOGLE_API_KEY values."""
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        os.environ.pop("GOOGLE_API_KEY", None)
        return gemini_key
    return os.getenv("GOOGLE_API_KEY")


# ---------------------------------------------------------------------------
# 2. Realtime model (Gemini Live native audio)
#    Single round-trip: STT + LLM + TTS handled by Gemini Live.
# ---------------------------------------------------------------------------
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
