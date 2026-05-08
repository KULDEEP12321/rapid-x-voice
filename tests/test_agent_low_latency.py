import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import agent
import config


class LowLatencyConfigTests(unittest.TestCase):
    def test_turn_handling_pins_fast_endpointing_and_barge_in(self):
        turn_handling = agent._build_turn_handling()

        self.assertEqual(turn_handling["turn_detection"], "realtime_llm")
        self.assertLessEqual(turn_handling["endpointing"]["min_delay"], 0.25)
        self.assertLessEqual(turn_handling["endpointing"]["max_delay"], 0.5)
        self.assertTrue(turn_handling["interruption"]["enabled"])
        self.assertEqual(turn_handling["interruption"]["mode"], "vad")
        self.assertLessEqual(turn_handling["interruption"]["min_duration"], 0.08)
        self.assertFalse(turn_handling["interruption"]["resume_false_interruption"])
        self.assertLessEqual(
            turn_handling["interruption"]["false_interruption_timeout"],
            0.6,
        )

    def test_gemini_activity_detection_interrupts_on_speech_start(self):
        realtime_config = agent._build_realtime_input_config()
        activity = realtime_config.automatic_activity_detection

        self.assertFalse(activity.disabled)
        self.assertLessEqual(activity.prefix_padding_ms, 100)
        self.assertEqual(activity.silence_duration_ms, 300)
        self.assertEqual(
            realtime_config.activity_handling.value,
            "START_OF_ACTIVITY_INTERRUPTS",
        )

    def test_session_disables_aec_warmup_and_forces_barge_in_stop(self):
        source = Path(agent.__file__).read_text(encoding="utf-8")

        self.assertIn("aec_warmup_duration=0.0", source)
        self.assertIn("session.interrupt(force=True)", source)

    def test_sip_dial_does_not_block_on_answer_before_warm_session(self):
        source = Path(agent.__file__).read_text(encoding="utf-8")

        self.assertIn("wait_until_answered=False", source)
        self.assertNotIn("wait_until_answered=True", source)
        self.assertIn('"participant_connected"', source)

    def test_outbound_greeting_uses_generate_reply_compatible_model(self):
        original_model = config.GEMINI_LIVE_MODEL
        original_fallback = config.GEMINI_PROACTIVE_REPLY_MODEL
        try:
            config.GEMINI_LIVE_MODEL = "gemini-3.1-flash-live-preview"
            config.GEMINI_PROACTIVE_REPLY_MODEL = (
                "gemini-2.5-flash-native-audio-preview-12-2025"
            )

            self.assertEqual(
                agent._select_realtime_model_name(requires_generate_reply=True),
                "gemini-2.5-flash-native-audio-preview-12-2025",
            )
        finally:
            config.GEMINI_LIVE_MODEL = original_model
            config.GEMINI_PROACTIVE_REPLY_MODEL = original_fallback

    def test_normal_turns_use_primary_realtime_model_after_greeting(self):
        original_model = config.GEMINI_LIVE_MODEL
        original_fallback = config.GEMINI_PROACTIVE_REPLY_MODEL
        try:
            config.GEMINI_LIVE_MODEL = "gemini-3.1-flash-live-preview"
            config.GEMINI_PROACTIVE_REPLY_MODEL = (
                "gemini-2.5-flash-native-audio-preview-12-2025"
            )

            self.assertEqual(
                agent._select_realtime_model_name(requires_generate_reply=False),
                "gemini-3.1-flash-live-preview",
            )

            source = Path(agent.__file__).read_text(encoding="utf-8")
            self.assertIn("session.update_agent(main_agent)", source)
            self.assertIn('"realtime_model_switched"', source)
        finally:
            config.GEMINI_LIVE_MODEL = original_model
            config.GEMINI_PROACTIVE_REPLY_MODEL = original_fallback

    def test_outbound_assistant_can_pin_agent_level_realtime_model(self):
        marker_llm = object()
        assistant = agent.OutboundAssistant([], "Be brief.", llm=marker_llm)

        self.assertIs(assistant.llm, marker_llm)

    def test_session_instructions_always_append_low_latency_voice_rules(self):
        instructions = agent._build_session_instructions(
            {
                "system_prompt": "You are a survey caller.",
                "user_prompt": "Ask a CSAT survey.",
            }
        )

        self.assertIn("Low-latency voice rules", instructions)
        self.assertIn("one short sentence", instructions)
        self.assertIn("under 12 words", instructions)

    def test_instructions_append_prefetched_context_not_lookup_instructions(self):
        instructions = agent._build_session_instructions(
            {
                "system_prompt": "You are Priya. Reply in 1-2 sentences.",
                "user_prompt": "Admissions campaign.",
                "lead_context": "Parent asked about Grade 5 fees yesterday.",
            }
        )

        self.assertIn("Admissions campaign.", instructions)
        self.assertIn("Preloaded caller context", instructions)
        self.assertIn("Parent asked about Grade 5 fees yesterday.", instructions)
        self.assertLess(agent._approx_token_count(instructions), 300)

    def test_prefetched_lookup_returns_metadata_without_slow_fetch(self):
        ctx = SimpleNamespace(room=SimpleNamespace(name="room-a"))
        tools = agent.TransferFunctions(
            ctx,
            phone_number="+919876543210",
            lead_context="Parent asked about Grade 5 fees yesterday.",
        )

        self.assertIn("Parent asked", tools._lookup_user_details("+919876543210"))


class MetricsTests(unittest.TestCase):
    def test_emit_metric_writes_json_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = agent.METRICS_LOG
            agent.METRICS_LOG = str(Path(tmp) / "metrics.jsonl")
            try:
                agent._emit_metric(
                    "room-a",
                    "tool_call",
                    tool_name="lookup_user",
                    duration_ms=12.5,
                )
            finally:
                path = Path(agent.METRICS_LOG)
                agent.METRICS_LOG = original

            record = json.loads(path.read_text(encoding="utf-8").strip())

        self.assertEqual(record["room"], "room-a")
        self.assertEqual(record["event"], "tool_call")
        self.assertEqual(record["tool_name"], "lookup_user")
        self.assertEqual(record["duration_ms"], 12.5)


class ConfigTests(unittest.TestCase):
    def test_gemini_api_key_env_name_is_primary(self):
        self.assertEqual(config._env("DOES_NOT_EXIST", default="x"), "x")


if __name__ == "__main__":
    unittest.main()
