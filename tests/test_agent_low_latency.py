import json
import asyncio
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
        self.assertEqual(turn_handling["interruption"]["min_words"], 0)
        self.assertTrue(turn_handling["interruption"]["resume_false_interruption"])
        self.assertGreaterEqual(
            turn_handling["interruption"]["false_interruption_timeout"],
            1.0,
        )
        self.assertTrue(turn_handling["preemptive_generation"]["enabled"])
        self.assertTrue(turn_handling["preemptive_generation"]["preemptive_tts"])

    def test_cascade_turn_handling_uses_streaming_endpointing(self):
        detector = object()
        with patch.object(agent, "MultilingualModel", return_value=detector):
            turn_handling = agent._build_cascade_turn_handling()

        self.assertIn("endpointing", turn_handling)
        self.assertIs(turn_handling["turn_detection"], detector)
        self.assertGreaterEqual(turn_handling["endpointing"]["min_delay"], 0.3)
        self.assertLessEqual(turn_handling["endpointing"]["max_delay"], 0.9)
        self.assertEqual(turn_handling["interruption"]["mode"], "adaptive")
        self.assertGreaterEqual(turn_handling["interruption"]["min_duration"], 0.35)
        self.assertEqual(turn_handling["interruption"]["min_words"], 1)
        self.assertTrue(turn_handling["interruption"]["resume_false_interruption"])
        self.assertFalse(turn_handling["preemptive_generation"]["enabled"])
        self.assertFalse(turn_handling["preemptive_generation"]["preemptive_tts"])

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

    def test_session_disables_aec_warmup_and_debounces_barge_in_stop(self):
        source = Path(agent.__file__).read_text(encoding="utf-8")

        self.assertIn("aec_warmup_duration=0.0", source)
        self.assertIn("_maybe_interrupt_after_debounce", source)
        self.assertIn("INTERRUPTION_MIN_SPEECH_MS", source)
        self.assertIn("session.interrupt(force=True)", source)

    def test_cascade_llm_retries_are_disabled_to_avoid_dead_air(self):
        self.assertEqual(config.CASCADE_LLM_MAX_RETRIES, 0)
        source = Path(agent.__file__).read_text(encoding="utf-8")

        self.assertIn("SessionConnectOptions", source)
        self.assertIn("llm_conn_options=APIConnectOptions", source)
        self.assertIn("max_retry=config.CASCADE_LLM_MAX_RETRIES", source)

    def test_sip_dial_does_not_block_on_answer_before_warm_session(self):
        source = Path(agent.__file__).read_text(encoding="utf-8")

        self.assertIn("wait_until_answered=False", source)
        self.assertNotIn("wait_until_answered=True", source)
        self.assertIn('"participant_connected"', source)

    def test_outbound_opening_uses_fixed_text_not_llm_generation(self):
        self.assertEqual(config.INITIAL_GREETING, "Hi there, how's your day going so far?")
        source = Path(agent.__file__).read_text(encoding="utf-8")

        self.assertIn("session.say(", source)
        self.assertIn("config.INITIAL_GREETING", source)
        self.assertNotIn("instructions=config.INITIAL_GREETING", source)

    def test_outbound_assistant_uses_deterministic_busy_reply_before_groq(self):
        runtime = agent.ConversationRuntime("room-a", agent._build_voice_timing_config())
        runtime.memory.record("user", "Yes. You caught made a bad call later.")
        assistant = agent.OutboundAssistant([], "Be brief.", runtime=runtime)

        self.assertEqual(
            assistant._deterministic_reply_for_latest_user_turn(),
            "No worries at all. What time today would be better for a quick call?",
        )

    def test_outbound_assistant_continues_cagos_opener_on_initial_hello(self):
        runtime = agent.ConversationRuntime("room-a", agent._build_voice_timing_config())
        runtime.memory.record("assistant", config.INITIAL_GREETING)
        runtime.memory.record("user", "Hello?")
        assistant = agent.OutboundAssistant(
            [],
            "You are Ava calling for CAGOS. Book a meeting with CA firms.",
            runtime=runtime,
        )

        self.assertEqual(
            assistant._deterministic_reply_for_latest_user_turn(),
            (
                "Thanks. I'm Ava calling from CAGOS. We help CA firms handle more "
                "clients without hiring. Can I take 20 seconds?"
            ),
        )

    def test_outbound_assistant_ends_after_polite_refusal(self):
        runtime = agent.ConversationRuntime("room-a", agent._build_voice_timing_config())
        runtime.memory.record("user", "No. Thank you. Bye bye.")
        assistant = agent.OutboundAssistant([], "Be brief.", runtime=runtime)

        self.assertEqual(
            assistant._deterministic_reply_for_latest_user_turn(),
            "Understood. Have a great day!",
        )
        self.assertEqual(runtime.consume_call_end_request(), "caller_declined")

    def test_agent_deletes_room_after_terminal_farewell_audio(self):
        source = Path(agent.__file__).read_text(encoding="utf-8")

        self.assertIn("consume_call_end_request", source)
        self.assertIn("assistant_audio_stopped", source)
        self.assertIn("delete_room(api.DeleteRoomRequest(room=room_name))", source)

    def test_llm_rate_limit_error_yields_voice_fallback(self):
        async def broken_stream():
            raise RuntimeError(
                "Error code: 429 - rate_limit_exceeded on tokens per day (TPD)"
            )
            yield "never"

        async def collect_chunks():
            runtime = agent.ConversationRuntime("room-a", agent._build_voice_timing_config())
            assistant = agent.OutboundAssistant([], "Be brief.", runtime=runtime)
            with patch.object(agent.Agent, "llm_node", return_value=broken_stream()):
                return [
                    chunk
                    async for chunk in assistant.llm_node(
                        agent.llm.ChatContext.empty(),
                        [],
                        SimpleNamespace(),
                    )
                ]

        self.assertEqual(
            asyncio.run(collect_chunks()),
            [config.LLM_RATE_LIMIT_FALLBACK],
        )

    def test_sarvam_chat_order_error_yields_voice_fallback(self):
        async def broken_stream():
            raise RuntimeError(
                "Error code: 400 - User and assistant turns must alternate, "
                "starting with a user message!"
            )
            yield "never"

        async def collect_chunks():
            runtime = agent.ConversationRuntime("room-a", agent._build_voice_timing_config())
            assistant = agent.OutboundAssistant(
                [],
                "Be brief.",
                sanitize_chat_history=True,
                runtime=runtime,
            )
            chat_ctx = agent.llm.ChatContext.empty()
            chat_ctx.add_message(role="user", content="Hello?")
            with patch.object(agent.Agent, "llm_node", return_value=broken_stream()):
                return [
                    chunk
                    async for chunk in assistant.llm_node(
                        chat_ctx,
                        [],
                        SimpleNamespace(),
                    )
                ]

        self.assertEqual(
            asyncio.run(collect_chunks()),
            [config.LLM_TEMPORARY_ERROR_FALLBACK],
        )

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

    def test_cascade_provider_is_explicitly_supported(self):
        self.assertIn(config.CASCADE_LLM_PROVIDER, {"groq", "sarvam"})

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
            "DEEPGRAM_ENDPOINTING_MS": config.DEEPGRAM_ENDPOINTING_MS,
            "DEEPGRAM_VAD_EVENTS": config.DEEPGRAM_VAD_EVENTS,
            "DEEPGRAM_FILLER_WORDS": config.DEEPGRAM_FILLER_WORDS,
            "GROQ_MODEL": config.GROQ_MODEL,
            "GROQ_BASE_URL": config.GROQ_BASE_URL,
            "GROQ_TIMEOUT_SECONDS": config.GROQ_TIMEOUT_SECONDS,
            "GROQ_MAX_RETRIES": config.GROQ_MAX_RETRIES,
            "CLOUDFLARE_AI_GATEWAY_TOKEN": config.CLOUDFLARE_AI_GATEWAY_TOKEN,
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
            config.DEEPGRAM_ENDPOINTING_MS = 350
            config.DEEPGRAM_VAD_EVENTS = True
            config.DEEPGRAM_FILLER_WORDS = True
            config.GROQ_MODEL = "llama-3.3-70b-versatile"
            config.GROQ_BASE_URL = "https://gateway.example.com/groq/openai/v1"
            config.GROQ_TIMEOUT_SECONDS = 12
            config.GROQ_MAX_RETRIES = 1
            config.CLOUDFLARE_AI_GATEWAY_TOKEN = ""
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
        self.assertEqual(stt.call_args.kwargs["endpointing_ms"], 350)
        self.assertTrue(stt.call_args.kwargs["vad_events"])
        self.assertTrue(stt.call_args.kwargs["filler_words"])
        self.assertEqual(llm.call_args.kwargs["model"], "llama-3.3-70b-versatile")
        self.assertEqual(
            llm.call_args.kwargs["base_url"],
            "https://gateway.example.com/groq/openai/v1",
        )
        self.assertEqual(llm.call_args.kwargs["timeout"], 12)
        self.assertEqual(llm.call_args.kwargs["max_retries"], 1)
        self.assertEqual(llm.call_args.kwargs["temperature"], 0.2)
        self.assertEqual(tts.call_args.kwargs["model"], "bulbul:v2")
        self.assertEqual(tts.call_args.kwargs["target_language_code"], "en-IN")

    def test_voice_runtime_timing_defaults_are_production_safe(self):
        timing = agent._build_voice_timing_config()

        self.assertEqual(timing.final_transcript_grace_ms, 350)
        self.assertEqual(timing.user_silence_commit_ms, 700)
        self.assertEqual(timing.interruption_min_speech_ms, 350)
        self.assertEqual(timing.tts_cancel_debounce_ms, 250)
        self.assertEqual(timing.max_recent_turns, 12)

    def test_build_cascade_models_adds_cloudflare_gateway_auth_header(self):
        originals = {
            "CASCADE_LLM_PROVIDER": config.CASCADE_LLM_PROVIDER,
            "DEEPGRAM_API_KEY": config.DEEPGRAM_API_KEY,
            "GROQ_API_KEY": config.GROQ_API_KEY,
            "SARVAM_API_KEY": config.SARVAM_API_KEY,
            "GROQ_BASE_URL": config.GROQ_BASE_URL,
            "GROQ_MODEL": config.GROQ_MODEL,
            "CLOUDFLARE_AI_GATEWAY_TOKEN": config.CLOUDFLARE_AI_GATEWAY_TOKEN,
        }
        try:
            config.CASCADE_LLM_PROVIDER = "groq"
            config.DEEPGRAM_API_KEY = "dg-test"
            config.GROQ_API_KEY = "groq-test"
            config.SARVAM_API_KEY = "sarvam-test"
            config.GROQ_BASE_URL = "https://gateway.ai.cloudflare.com/v1/acct/vagos/compat"
            config.GROQ_MODEL = "groq/llama-3.3-70b-versatile"
            config.CLOUDFLARE_AI_GATEWAY_TOKEN = "cf-token"

            with (
                patch.object(agent.deepgram, "STT", return_value="stt"),
                patch.object(agent.openai_plugin, "LLM", return_value="llm") as llm,
                patch.object(agent.sarvam, "TTS", return_value="tts"),
            ):
                models = agent._build_cascade_models()
        finally:
            for key, value in originals.items():
                setattr(config, key, value)

        self.assertEqual(models["llm"], "llm")
        self.assertEqual(llm.call_args.kwargs["api_key"], "groq-test")
        self.assertEqual(
            llm.call_args.kwargs["extra_headers"],
            {"cf-aig-authorization": "Bearer cf-token"},
        )

    def test_sarvam_history_normalizer_drops_fragments_and_alternates_roles(self):
        chat_ctx = agent.llm.ChatContext.empty()
        chat_ctx.add_message(role="system", content="Speak briefly.")
        chat_ctx.add_message(role="assistant", content="Hello, I am calling from Rapid X.")
        chat_ctx.add_message(role="user", content="hello")
        chat_ctx.add_message(role="assistant", content="Sure")
        chat_ctx.add_message(role="assistant", content="I can help.")
        chat_ctx.add_message(role="user", content="fees")
        chat_ctx.add_message(role="user", content="grade five")
        chat_ctx.add_message(role="assistant", content="")
        chat_ctx.add_message(role="assistant", content="interrupted fragment", interrupted=True)
        chat_ctx.add_message(role="user", content="are you there?")

        safe_ctx = agent._sarvam_safe_chat_context(chat_ctx)
        messages = safe_ctx.messages()

        self.assertEqual([m.role for m in messages], ["user", "assistant", "user"])
        self.assertIn("Speak briefly.", messages[0].text_content)
        self.assertIn("Caller: hello", messages[0].text_content)
        self.assertEqual(messages[1].text_content, "Sure\nI can help.")
        self.assertEqual(messages[2].text_content, "fees\ngrade five\nare you there?")
        self.assertFalse(any(m.interrupted for m in messages))

    def test_sarvam_history_normalizer_injects_agent_instructions(self):
        chat_ctx = agent.llm.ChatContext.empty()
        chat_ctx.add_message(role="assistant", content=config.INITIAL_GREETING)
        chat_ctx.add_message(role="user", content="Hello?")

        safe_ctx = agent._sarvam_safe_chat_context(
            chat_ctx,
            system_instructions=(
                "You are Ava calling for CAGOS. "
                "Never say How can I assist you today."
            ),
        )
        messages = safe_ctx.messages()

        self.assertEqual([m.role for m in messages], ["user"])
        self.assertIn("You are Ava calling for CAGOS.", messages[0].text_content)
        self.assertIn("Never say How can I assist you today.", messages[0].text_content)
        self.assertIn("Caller: Hello?", messages[0].text_content)

    def test_sarvam_outbound_assistant_preserves_runtime_instructions(self):
        async def complete_turn():
            runtime = agent.ConversationRuntime(
                "room-a",
                agent._build_voice_timing_config(),
            )
            assistant = agent.OutboundAssistant(
                [],
                "You are Ava calling for CAGOS. Never say How can I assist you today.",
                sanitize_chat_history=True,
                runtime=runtime,
            )
            chat_ctx = agent.llm.ChatContext.empty()
            chat_ctx.add_message(role="assistant", content=config.INITIAL_GREETING)
            new_message = chat_ctx.add_message(role="user", content="Hello?")

            await assistant.on_user_turn_completed(chat_ctx, new_message)
            return chat_ctx.messages()[0].text_content

        first_message = asyncio.run(complete_turn())

        self.assertIn("You are Ava calling for CAGOS.", first_message)
        self.assertIn("Never say How can I assist you today.", first_message)
        self.assertIn("Caller: Hello?", first_message)

    def test_sarvam_llm_node_sanitizes_context_at_provider_boundary(self):
        captured_roles = []

        async def fake_provider_stream():
            yield "ok"

        def capture_llm_node(_assistant, chat_ctx, _tools, _model_settings):
            captured_roles.extend(m.role for m in chat_ctx.messages())
            return fake_provider_stream()

        async def collect():
            runtime = agent.ConversationRuntime(
                "room-a",
                agent._build_voice_timing_config(),
            )
            assistant = agent.OutboundAssistant(
                [],
                "You are Ava calling for CAGOS.",
                sanitize_chat_history=True,
                runtime=runtime,
            )
            chat_ctx = agent.llm.ChatContext.empty()
            chat_ctx.add_message(role="assistant", content=config.INITIAL_GREETING)
            chat_ctx.add_message(role="user", content="What is this about?")

            with patch.object(agent.Agent, "llm_node", side_effect=capture_llm_node):
                return [
                    chunk
                    async for chunk in assistant.llm_node(chat_ctx, [], SimpleNamespace())
                ]

        self.assertEqual(asyncio.run(collect()), ["ok"])
        self.assertEqual(captured_roles, ["user"])

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
        self.assertIn("one short reply", instructions)
        self.assertIn("Never say", instructions)

    def test_session_instructions_always_include_phone_agent_system_layer(self):
        instructions = agent._build_session_instructions(
            {
                "system_prompt": "You are a survey caller.",
                "user_prompt": "Ask a CSAT survey.",
            }
        )

        self.assertIn("You are a live outbound phone agent.", instructions)
        self.assertIn("never a general chatbot", instructions)
        self.assertIn("No markdown or emojis", instructions)
        self.assertIn("You are a survey caller.", instructions)
        self.assertIn("Ask a CSAT survey.", instructions)

    def test_cagos_prompt_stays_under_realtime_budget_with_system_layer(self):
        cagos_prompt = (
            "You are Ava calling for CAGOS, Chartered Accountancy Growth Operating System. "
            "Goal: book a 5-minute meeting with Chartered Accountants or CA firm owners. "
            "Value: CAGOS helps CA firms increase revenue by handling more clients without hiring, "
            "using automation for routine follow-ups and admin work. Confirm you are speaking with "
            "the CA, owner, or operations person. Ask permission for 20 seconds. Ask one qualifier: "
            "how many active clients they handle, or whether they want to grow without adding headcount. "
            "If interested, offer tomorrow only between 3pm and 7pm IST, such as 3:30, 5:00, or 6:30. "
            "Capture name, firm, role, time, and phone or email. Confirm details before ending. "
            "If not interested, ask if they already use automation, then politely end. "
            "Do not invent pricing, integrations, logos, or case studies."
        )

        instructions = agent._build_session_instructions({"system_prompt": cagos_prompt})

        self.assertLessEqual(agent._approx_token_count(instructions), 300)

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

    def test_prefetched_lookup_returns_metadata_without_slow_fetch(self):
        ctx = SimpleNamespace(room=SimpleNamespace(name="room-a"))
        tools = agent.TransferFunctions(
            ctx,
            phone_number="+919876543210",
            lead_context="Parent asked about Grade 5 fees yesterday.",
        )

        self.assertIn("Parent asked", tools._lookup_user_details("+919876543210"))

    def test_transfer_tool_blocks_demo_proceed_false_positive(self):
        runtime = agent.ConversationRuntime("room-a", agent._build_voice_timing_config())
        runtime.memory.record("user", "Yeah. It's okay. Can we proceed with the demo?")
        api_mock = SimpleNamespace(
            sip=SimpleNamespace(
                transfer_sip_participant=lambda request: (_ for _ in ()).throw(
                    AssertionError("transfer should not execute")
                )
            )
        )
        ctx = SimpleNamespace(
            room=SimpleNamespace(name="room-a", remote_participants={}),
            api=api_mock,
        )
        tools = agent.TransferFunctions(
            ctx,
            phone_number="+919876543210",
            lead_context="",
            runtime=runtime,
        )

        result = asyncio.run(tools.transfer_call())

        self.assertIn("did not ask for a human transfer", result)


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
