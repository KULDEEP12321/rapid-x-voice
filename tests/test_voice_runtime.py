import unittest

from livekit.agents import stt

from voice_runtime import (
    BargeInController,
    ConversationRuntime,
    ConversationState,
    TranscriptBuffer,
    VoiceTimingConfig,
    is_connection_check,
    deterministic_reply_for_user_turn,
    is_call_end_request,
    is_human_transfer_request,
    is_callback_request,
    is_meaningful_user_text,
    sanitize_voice_response,
)


def _speech_event(event_type, text="", confidence=0.9):
    alternatives = []
    if text:
        alternatives = [
            stt.SpeechData(
                language="en-IN",
                text=text,
                confidence=confidence,
            )
        ]
    return stt.SpeechEvent(type=event_type, alternatives=alternatives)


class TranscriptBufferTests(unittest.TestCase):
    def test_partial_transcript_only_updates_temporary_buffer(self):
        buffer = TranscriptBuffer(VoiceTimingConfig())

        buffer.start_speech(now_ms=0)
        buffer.update_partial("I want adm", now_ms=120, confidence=0.72)

        self.assertEqual(buffer.partial_transcript, "I want adm")
        self.assertEqual(buffer.final_transcript_segments, [])
        self.assertFalse(buffer.ready_to_commit(now_ms=1_000))

    def test_final_transcript_commits_after_grace_period(self):
        config = VoiceTimingConfig(final_transcript_grace_ms=350)
        buffer = TranscriptBuffer(config)

        buffer.start_speech(now_ms=0)
        buffer.add_final("I want admission", now_ms=300, confidence=0.88)
        buffer.mark_speech_final(now_ms=320)

        self.assertFalse(buffer.ready_to_commit(now_ms=600))
        self.assertTrue(buffer.ready_to_commit(now_ms=671))
        self.assertEqual(buffer.commit_if_ready(now_ms=671), "I want admission")

    def test_multiple_final_fragments_merge_without_duplicates(self):
        buffer = TranscriptBuffer(VoiceTimingConfig())

        buffer.start_speech(now_ms=0)
        buffer.add_final("I want admission", now_ms=100, confidence=0.91)
        buffer.add_final("I want admission", now_ms=140, confidence=0.91)
        buffer.add_final("for class five", now_ms=260, confidence=0.87)
        buffer.mark_speech_final(now_ms=300)

        self.assertEqual(buffer.merged_final_text(), "I want admission for class five")

    def test_filler_noise_does_not_trigger_response(self):
        buffer = TranscriptBuffer(VoiceTimingConfig())

        buffer.start_speech(now_ms=0)
        buffer.add_final("uh", now_ms=100, confidence=0.92)
        buffer.mark_speech_final(now_ms=200)

        self.assertFalse(is_meaningful_user_text("uh", min_chars=2))
        self.assertIsNone(buffer.commit_if_ready(now_ms=1_000))


class BargeInTests(unittest.TestCase):
    def test_short_noise_does_not_cancel_assistant_tts(self):
        gate = BargeInController(
            VoiceTimingConfig(
                interruption_min_speech_ms=350,
                interruption_min_chars=3,
                tts_cancel_debounce_ms=250,
            )
        )

        decision = gate.evaluate(
            assistant_speaking=True,
            user_speech_started_at_ms=1_000,
            now_ms=1_120,
            transcript="uh",
            confidence=0.9,
        )

        self.assertFalse(decision.should_interrupt)
        self.assertEqual(decision.reason, "speech_too_short")

    def test_valid_user_barge_in_cancels_tts(self):
        gate = BargeInController(
            VoiceTimingConfig(
                interruption_min_speech_ms=350,
                interruption_min_chars=3,
            )
        )

        decision = gate.evaluate(
            assistant_speaking=True,
            user_speech_started_at_ms=1_000,
            now_ms=1_420,
            transcript="wait please",
            confidence=0.86,
        )

        self.assertTrue(decision.should_interrupt)
        self.assertEqual(decision.reason, "meaningful_transcript")

    def test_connection_check_during_assistant_speech_does_not_cancel_tts(self):
        gate = BargeInController(VoiceTimingConfig(interruption_min_speech_ms=350))

        decision = gate.evaluate(
            assistant_speaking=True,
            user_speech_started_at_ms=1_000,
            now_ms=1_600,
            transcript="Hello?",
            confidence=0.92,
        )

        self.assertFalse(decision.should_interrupt)
        self.assertEqual(decision.reason, "connection_check_backchannel")


class RuntimeStateTests(unittest.TestCase):
    def test_stale_groq_response_is_discarded_after_interruption(self):
        runtime = ConversationRuntime("room-test", VoiceTimingConfig())

        first_assistant_turn = runtime.start_assistant_turn(now_ms=1_000)
        runtime.cancel_assistant_turn(now_ms=1_200, reason="user_barge_in")
        second_assistant_turn = runtime.start_assistant_turn(now_ms=1_300)

        self.assertFalse(runtime.is_active_assistant_turn(first_assistant_turn))
        self.assertTrue(runtime.is_active_assistant_turn(second_assistant_turn))

    def test_user_barge_in_clears_pending_call_end(self):
        runtime = ConversationRuntime("room-test", VoiceTimingConfig())
        runtime.request_call_end("caller_declined")

        runtime.cancel_assistant_turn(now_ms=100, reason="meaningful_transcript")

        self.assertIsNone(runtime.consume_call_end_request())

    def test_non_terminal_user_turn_clears_pending_call_end(self):
        runtime = ConversationRuntime("room-test", VoiceTimingConfig())
        runtime.request_call_end("caller_declined")

        runtime.commit_livekit_user_turn("Can we start the call now?", now_ms=100)

        self.assertIsNone(runtime.consume_call_end_request())

    def test_conversation_memory_keeps_summary_and_recent_turns(self):
        runtime = ConversationRuntime(
            "room-test",
            VoiceTimingConfig(max_recent_turns=4),
        )

        for idx in range(4):
            runtime.memory.record("user", f"user fact {idx}")
            runtime.memory.record("assistant", f"assistant answer {idx}")

        self.assertIn("user fact 0", runtime.memory.summary)
        self.assertIn("assistant answer 0", runtime.memory.summary)
        self.assertLessEqual(len(runtime.memory.recent_turns), 4)
        self.assertIn("user fact 3", runtime.memory.prompt_fragment())

    def test_state_machine_rejects_invalid_transition(self):
        runtime = ConversationRuntime("room-test", VoiceTimingConfig())

        with self.assertRaises(ValueError):
            runtime.transition(ConversationState.ASSISTANT_SPEAKING, event_type="bad")

        runtime.transition(ConversationState.USER_SPEAKING, event_type="speech_started")
        runtime.transition(ConversationState.USER_PAUSED, event_type="speech_ended")
        runtime.transition(ConversationState.THINKING, event_type="turn_committed")
        runtime.transition(ConversationState.ASSISTANT_SPEAKING, event_type="first_audio")

    def test_committed_turn_resets_transcript_stabilizer(self):
        runtime = ConversationRuntime("room-test", VoiceTimingConfig())
        runtime.filter_stt_event(
            _speech_event(stt.SpeechEventType.FINAL_TRANSCRIPT, "What demo is it?"),
            assistant_speaking=False,
            now_ms=100,
        )

        committed = runtime.commit_livekit_user_turn(
            "What demo is it?",
            confidence=0.9,
            now_ms=200,
        )

        self.assertEqual(committed, "What demo is it?")
        self.assertEqual(runtime.transcripts.final_transcript_segments, [])


class VoiceResponseSanitizerTests(unittest.TestCase):
    def test_voice_response_sanitizer_removes_markdown(self):
        text = "**Sure!** Visit https://example.com/a-b and choose `Class 5`:\n- fees\n- timing"

        sanitized = sanitize_voice_response(text)

        self.assertNotIn("**", sanitized)
        self.assertNotIn("`", sanitized)
        self.assertNotIn("- fees", sanitized)
        self.assertIn("example dot com", sanitized)
        self.assertIn("fees", sanitized)

    def test_voice_response_sanitizer_removes_placeholders_and_inline_bullets(self):
        text = (
            "Got it: - We handle [specific service based on their interest]. "
            "- Our clients save [X] hours/week."
        )

        sanitized = sanitize_voice_response(text)

        self.assertNotIn("[", sanitized)
        self.assertNotIn("]", sanitized)
        self.assertNotIn(" - ", sanitized)
        self.assertNotIn("specific service", sanitized)
        self.assertIn("We handle", sanitized)


class STTEventFilterTests(unittest.TestCase):
    def test_deepgram_partials_do_not_become_committed_turns(self):
        runtime = ConversationRuntime("room-test", VoiceTimingConfig())

        partial = runtime.filter_stt_event(
            _speech_event(stt.SpeechEventType.INTERIM_TRANSCRIPT, "I am loo"),
            assistant_speaking=False,
            now_ms=100,
        )

        self.assertIs(partial.type, stt.SpeechEventType.INTERIM_TRANSCRIPT)
        self.assertEqual(runtime.transcripts.partial_transcript, "I am loo")
        self.assertEqual(runtime.transcripts.final_transcript_segments, [])

    def test_deepgram_repeated_final_fragment_is_suppressed(self):
        runtime = ConversationRuntime("room-test", VoiceTimingConfig())

        first = runtime.filter_stt_event(
            _speech_event(stt.SpeechEventType.FINAL_TRANSCRIPT, "I need fees"),
            assistant_speaking=False,
            now_ms=100,
        )
        duplicate = runtime.filter_stt_event(
            _speech_event(stt.SpeechEventType.FINAL_TRANSCRIPT, "I need fees"),
            assistant_speaking=False,
            now_ms=150,
        )

        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        self.assertEqual(runtime.transcripts.merged_final_text(), "I need fees")

    def test_deepgram_overlapping_final_fragment_emits_only_new_delta(self):
        runtime = ConversationRuntime("room-test", VoiceTimingConfig())

        first = runtime.filter_stt_event(
            _speech_event(stt.SpeechEventType.FINAL_TRANSCRIPT, "I need fees"),
            assistant_speaking=False,
            now_ms=100,
        )
        second = runtime.filter_stt_event(
            _speech_event(stt.SpeechEventType.FINAL_TRANSCRIPT, "I need fees for class five"),
            assistant_speaking=False,
            now_ms=180,
        )

        self.assertEqual(first.alternatives[0].text, "I need fees")
        self.assertEqual(second.alternatives[0].text, "for class five")
        self.assertEqual(runtime.transcripts.merged_final_text(), "I need fees for class five")

    def test_connection_check_final_is_suppressed_while_assistant_speaks(self):
        runtime = ConversationRuntime("room-test", VoiceTimingConfig())

        final = runtime.filter_stt_event(
            _speech_event(stt.SpeechEventType.FINAL_TRANSCRIPT, "Hello?"),
            assistant_speaking=True,
            now_ms=100,
        )

        self.assertIsNone(final)
        self.assertEqual(runtime.transcripts.final_transcript_segments, [])

    def test_connection_check_can_commit_when_assistant_is_silent(self):
        runtime = ConversationRuntime("room-test", VoiceTimingConfig())

        final = runtime.filter_stt_event(
            _speech_event(stt.SpeechEventType.FINAL_TRANSCRIPT, "Hello?"),
            assistant_speaking=False,
            now_ms=100,
        )

        self.assertIsNotNone(final)
        self.assertTrue(is_connection_check("Hello?"))


class IntentGuardTests(unittest.TestCase):
    def test_demo_proceed_is_not_human_transfer_request(self):
        self.assertFalse(is_human_transfer_request("Yeah, can we proceed with the demo?"))
        self.assertFalse(is_human_transfer_request("Can we start the demo now?"))

    def test_explicit_human_request_allows_transfer(self):
        self.assertTrue(is_human_transfer_request("Please transfer me to a human."))
        self.assertTrue(is_human_transfer_request("I want to talk to an agent."))
        self.assertTrue(is_human_transfer_request("Human please."))

    def test_human_question_does_not_trigger_transfer(self):
        self.assertFalse(is_human_transfer_request("Are you a human?"))

    def test_busy_callback_request_gets_deterministic_reply(self):
        text = "Yes. You caught me at a bad time, call later."

        self.assertTrue(is_callback_request(text))
        self.assertEqual(
            deterministic_reply_for_user_turn(text),
            "No worries at all. What time today would be better for a quick call?",
        )

    def test_normal_greeting_response_does_not_trigger_callback_reply(self):
        self.assertFalse(is_callback_request("I am doing well, please go ahead."))
        self.assertIsNone(
            deterministic_reply_for_user_turn("I am doing well, please go ahead.")
        )

    def test_polite_refusal_gets_terminal_reply(self):
        text = "No. Thank you. Bye bye."

        self.assertTrue(is_call_end_request(text))
        self.assertEqual(
            deterministic_reply_for_user_turn(text),
            "Understood. Have a great day!",
        )

    def test_start_call_now_is_not_call_end_request(self):
        self.assertFalse(is_call_end_request("Can we start the call now?"))


if __name__ == "__main__":
    unittest.main()
