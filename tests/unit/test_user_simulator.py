"""User Simulator graph tests with fake transports; no LLM, no Feishu."""

import unittest

from tests.e2e.shared.transports import (
    FakeSimulatorLLM,
    FakeThreadTransport,
    ThreadMessage,
)
from tests.e2e.shared.user_simulator import (
    MAX_TOLERATED_ERRORS,
    build_graph,
    classify_message,
    run_simulator_turn,
    simulator_loop,
    transcript_export,
)

BRIEF = "only TODO list scope"


def card(text, title="🤖 Agent"):
    return ThreadMessage(message_id="m1", text=text, msg_type="card", card_title=title)


def text_msg(text):
    return ThreadMessage(message_id="m1", text=text, msg_type="text")


class ClassifyMessageTests(unittest.TestCase):
    def test_question_is_default_for_text(self):
        self.assertEqual(classify_message(text_msg("优先级用什么排序？")), "question")

    def test_card_question_marks_classify_as_question(self):
        # The Bridge delivers every Turn output as a card, so real Agent
        # questions only ever arrive as cards.
        self.assertEqual(classify_message(card("❓ **Q1** - 优先级？")), "question")
        self.assertEqual(classify_message(card("should we use JSON?")), "question")

    def test_progress_is_default_for_nonmatching_card(self):
        self.assertEqual(classify_message(card("Agent is thinking...")), "progress")

    def test_complete_card(self):
        self.assertEqual(classify_message(card("✅ Alignment Complete", title="✅ Alignment Complete")), "complete_card")

    def test_error(self):
        self.assertEqual(classify_message(card("❌ Error: boom", title="❌ Error")), "error")

    def test_approval_card_detected_before_complete(self):
        self.assertEqual(classify_message(card("批准此操作？ Allow once", title="🔐 Approval Request")), "approval_card")


class SimulatorGraphTests(unittest.TestCase):
    def _run(self, messages, llm_answer="ack"):
        thread = FakeThreadTransport(inbox=list(messages))
        graph = build_graph(thread, FakeSimulatorLLM(answer=llm_answer))
        return simulator_loop(graph, BRIEF), thread

    def test_idle_thread_ends_turn_without_terminal(self):
        state, thread = self._run([])
        self.assertEqual(state["terminal"], "")
        self.assertEqual(thread.replies, [])

    def test_question_gets_brief_bounded_llm_reply(self):
        captured = {}

        def answer(question, brief):
            captured["question"] = question
            captured["brief"] = brief
            return "可以，只用高/中/低三档。"

        state, thread = self._run([text_msg("排序规则？")], llm_answer=answer)
        self.assertEqual(captured, {"question": "排序规则？", "brief": BRIEF})
        self.assertEqual(thread.replies, ["可以，只用高/中/低三档。"])
        self.assertEqual(state["replies"], ["可以，只用高/中/低三档。"])
        self.assertEqual(
            state["reply_receipts"],
            [{"message_id": "fake-reply-1", "create_time": 0.0}],
        )

    def test_complete_card_records_and_terminates(self):
        state, thread = self._run([card("✅ Alignment Complete", title="✅ Alignment Complete")])
        self.assertEqual(state["terminal"], "complete")
        self.assertEqual(state["events"][0]["type"], "complete_card")
        self.assertEqual(thread.replies, [])

    def test_errors_tolerated_then_escalated(self):
        messages = [card("❌ Error: boom", title="❌ Error")] * MAX_TOLERATED_ERRORS
        state, _ = self._run(messages)
        self.assertEqual(state["error_count"], MAX_TOLERATED_ERRORS)
        self.assertEqual(state["terminal"], "escalated")

    def test_errors_below_threshold_do_not_escalate(self):
        messages = [card("❌ Error: boom", title="❌ Error")] * (MAX_TOLERATED_ERRORS - 1)
        state, _ = self._run(messages)
        self.assertEqual(state["terminal"], "")
        self.assertEqual(state["error_count"], MAX_TOLERATED_ERRORS - 1)

    def test_approval_card_is_deterministic_failure(self):
        state, _ = self._run([card("请批准：Allow once", title="🔐 Approval Request")])
        self.assertEqual(state["terminal"], "approval_card_detected")

    def test_question_waits_for_quiet_period_before_reply(self):
        # The Bridge posts one Turn's output as several sequential messages;
        # the reply must not interleave with chunks still being posted.
        thread = FakeThreadTransport(inbox=[card("❓ 优先级用什么排序？")])
        graph = build_graph(thread, FakeSimulatorLLM(answer="ack"))
        state = {
            "message": None,
            "message_type": "",
            "user_brief": BRIEF,
            "answer": "",
            "replies": [],
            "reply_receipts": [],
            "events": [],
            "error_count": 0,
            "terminal": "",
        }
        state = run_simulator_turn(graph, state)
        self.assertEqual(thread.replies, [])  # staged only
        state = run_simulator_turn(graph, state)
        self.assertEqual(thread.replies, [])  # one quiet poll is not enough
        state = run_simulator_turn(graph, state)
        self.assertEqual(thread.replies, ["ack"])
        self.assertEqual(state["pending_question"], "")

    def test_latest_question_wins_after_quiet_period(self):
        captured = {}

        def answer(question, brief):
            captured["question"] = question
            return "ack"

        thread = FakeThreadTransport(
            inbox=[card("❓ 第一问？"), card("❓ 第二问？")]
        )
        graph = build_graph(thread, FakeSimulatorLLM(answer=answer))
        state = simulator_loop(graph, BRIEF)
        self.assertEqual(captured["question"], "❓ 第二问？")
        self.assertEqual(state["replies"], ["ack"])

    def test_transcript_export_is_json(self):
        state, _ = self._run(
            [text_msg("q1"), card("progress..."), card("✅ Alignment Complete", title="✅ Alignment Complete")]
        )
        exported = transcript_export(state)
        self.assertIn('"complete_card"', exported)
        self.assertIn('"terminal": "complete"', exported)


if __name__ == "__main__":
    unittest.main()
