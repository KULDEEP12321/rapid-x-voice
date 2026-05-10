import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent
import config


class LowLatencyConfigTests(unittest.TestCase):
    def test_turn_handling_lets_gemini_own_endpointing(self):
        turn_handling = agent._build_turn_handling()

        self.assertEqual(turn_handling["turn_detection"], "realtime_llm")
        self.assertNotIn("endpointing", turn_handling)
        self.assertTrue(turn_handling["interruption"]["enabled"])
        self.assertEqual(turn_handling["interruption"]["mode"], "vad")
        self.assertLessEqual(turn_handling["interruption"]["min_duration"], 0.08)
        self.assertFalse(turn_handling["interruption"]["resume_false_interruption"])
        self.assertLessEqual(
            turn_handling["interruption"]["false_interruption_timeout"],
            0.6,
        )
        self.assertTrue(turn_handling["preemptive_generation"]["enabled"])
        self.assertTrue(turn_handling["preemptive_generation"]["preemptive_tts"])

    def test_cascade_turn_handling_uses_streaming_endpointing(self):
        detector = object()
        with patch.object(agent, "MultilingualModel", return_value=detector):
            turn_handling = agent._build_cascade_turn_handling()

        self.assertIn("endpointing", turn_handling)
        self.assertIs(turn_handling["turn_detection"], detector)
        self.assertLessEqual(turn_handling["endpointing"]["min_delay"], 0.25)
        self.assertLessEqual(turn_handling["endpointing"]["max_delay"], 0.8)
        self.assertEqual(turn_handling["interruption"]["mode"], "adaptive")
        self.assertTrue(turn_handling["preemptive_generation"]["preemptive_tts"])

    def test_gemini_activity_detection_interrupts_on_speech_start(self):
        realtime_config = agent._build_realtime_input_config()
        activity = realtime_config.automatic_activity_detection

        self.assertFalse(activity.disabled)
        self.assertEqual(activity.prefix_padding_ms, 20)
        self.assertEqual(activity.silence_duration_ms, 200)
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

    def test_default_model_is_generate_reply_compatible(self):
        self.assertEqual(
            config.GEMINI_LIVE_MODEL,
            "gemini-2.5-flash-native-audio-preview-12-2025",
        )

    def test_single_realtime_model_path_has_no_mid_call_agent_switch(self):
        original_model = config.GEMINI_LIVE_MODEL
        try:
            config.GEMINI_LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"

            source = Path(agent.__file__).read_text(encoding="utf-8")
            self.assertNotIn("_select_realtime_model_name", source)
            self.assertNotIn("session.update_agent(main_agent)", source)
            self.assertNotIn('"realtime_model_switched"', source)
            self.assertNotIn("main_realtime", source)
            self.assertNotIn("greeting_realtime", source)
        finally:
            config.GEMINI_LIVE_MODEL = original_model

    def test_outbound_assistant_can_pin_agent_level_realtime_model(self):
        marker_llm = object()
        assistant = agent.OutboundAssistant([], "Be brief.", llm=marker_llm)

        self.assertIs(assistant.llm, marker_llm)

    def test_sarvam_cascade_disables_tool_schemas(self):
        original_provider = config.CASCADE_LLM_PROVIDER
        try:
            config.CASCADE_LLM_PROVIDER = "sarvam"
            self.assertEqual(agent._agent_tools_for_stack("cascade", ["tool"]), [])

            config.CASCADE_LLM_PROVIDER = "groq"
            self.assertEqual(agent._agent_tools_for_stack("cascade", ["tool"]), ["tool"])
            self.assertEqual(agent._agent_tools_for_stack("gemini", ["tool"]), ["tool"])
        finally:
            config.CASCADE_LLM_PROVIDER = original_provider

    def test_voice_stack_selects_cascade_aliases(self):
        self.assertEqual(agent._select_voice_stack({"voice_stack": "cascade"}), "cascade")
        self.assertEqual(agent._select_voice_stack({"voice_stack": "tier3"}), "cascade")
        self.assertEqual(agent._select_voice_stack({"voice_stack": "gemini"}), "gemini")

    def test_build_cascade_models_wires_provider_settings(self):
        originals = {
            "CASCADE_LLM_PROVIDER": config.CASCADE_LLM_PROVIDER,
            "DEEPGRAM_API_KEY": config.DEEPGRAM_API_KEY,
            "GROQ_API_KEY": config.GROQ_API_KEY,
            "SARVAM_API_KEY": config.SARVAM_API_KEY,
            "DEEPGRAM_MODEL": config.DEEPGRAM_MODEL,
            "DEEPGRAM_LANGUAGE": config.DEEPGRAM_LANGUAGE,
            "GROQ_MODEL": config.GROQ_MODEL,
            "SARVAM_TTS_MODEL": config.SARVAM_TTS_MODEL,
            "SARVAM_LANGUAGE": config.SARVAM_LANGUAGE,
        }
        try:
            config.CASCADE_LLM_PROVIDER = "groq"
            config.DEEPGRAM_API_KEY = "dg-test"
            config.GROQ_API_KEY = "groq-test"
            config.SARVAM_API_KEY = "sarvam-test"
            config.DEEPGRAM_MODEL = "nova-3"
            config.DEEPGRAM_LANGUAGE = "multi"
            config.GROQ_MODEL = "llama-3.3-70b-versatile"
            config.SARVAM_TTS_MODEL = "bulbul:v2"
            config.SARVAM_LANGUAGE = "en-IN"

            with (
                patch.object(agent.deepgram, "STT", return_value="stt") as stt,
                patch.object(agent.groq, "LLM", return_value="llm") as llm,
                patch.object(agent.sarvam, "TTS", return_value="tts") as tts,
            ):
                models = agent._build_cascade_models(temperature="0.2")
        finally:
            for key, value in originals.items():
                setattr(config, key, value)

        self.assertEqual(models, {"stt": "stt", "llm": "llm", "tts": "tts"})
        self.assertEqual(stt.call_args.kwargs["model"], "nova-3")
        self.assertEqual(stt.call_args.kwargs["language"], "multi")
        self.assertTrue(stt.call_args.kwargs["no_delay"])
        self.assertEqual(llm.call_args.kwargs["model"], "llama-3.3-70b-versatile")
        self.assertEqual(llm.call_args.kwargs["temperature"], 0.2)
        self.assertEqual(tts.call_args.kwargs["model"], "bulbul:v2")
        self.assertEqual(tts.call_args.kwargs["target_language_code"], "en-IN")

    def test_build_cascade_models_can_use_sarvam_llm(self):
        originals = {
            "CASCADE_LLM_PROVIDER": config.CASCADE_LLM_PROVIDER,
            "DEEPGRAM_API_KEY": config.DEEPGRAM_API_KEY,
            "SARVAM_API_KEY": config.SARVAM_API_KEY,
            "SARVAM_LLM_MODEL": config.SARVAM_LLM_MODEL,
            "SARVAM_LLM_MAX_TOKENS": config.SARVAM_LLM_MAX_TOKENS,
        }
        try:
            config.CASCADE_LLM_PROVIDER = "sarvam"
            config.DEEPGRAM_API_KEY = "dg-test"
            config.SARVAM_API_KEY = "sarvam-test"
            config.SARVAM_LLM_MODEL = "sarvam-m"
            config.SARVAM_LLM_MAX_TOKENS = 64

            with (
                patch.object(agent.deepgram, "STT", return_value="stt"),
                patch.object(agent.sarvam, "LLM", return_value="sarvam-llm") as llm,
                patch.object(agent.sarvam, "TTS", return_value="tts"),
            ):
                models = agent._build_cascade_models(temperature="0.25")
        finally:
            for key, value in originals.items():
                setattr(config, key, value)

        self.assertEqual(models["llm"], "sarvam-llm")
        self.assertEqual(llm.call_args.kwargs["model"], "sarvam-m")
        self.assertEqual(llm.call_args.kwargs["temperature"], 0.25)
        self.assertEqual(llm.call_args.kwargs["max_tokens"], 64)
        self.assertIsNone(llm.call_args.kwargs["reasoning_effort"])

    def test_build_cascade_models_requires_provider_keys(self):
        original = config.DEEPGRAM_API_KEY
        try:
            config.DEEPGRAM_API_KEY = ""
            with self.assertRaisesRegex(RuntimeError, "DEEPGRAM_API_KEY"):
                agent._build_cascade_models()
        finally:
            config.DEEPGRAM_API_KEY = original

    def test_vad_is_prewarmed_at_module_import(self):
        source = Path(agent.__file__).read_text(encoding="utf-8")

        self.assertIn("_VAD = silero.VAD.load(", source)
        self.assertIn("vad=_VAD", source)

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
