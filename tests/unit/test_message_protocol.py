"""Measured card and app-identity shapes from the real L3 Thread."""

import json
import unittest
from unittest.mock import Mock

from tests.e2e.harness_support import participant_round_trip_assertions
from tests.e2e.im_integration import FeishuOpenAPIVerifier
from tests.e2e.transports import thread_message_from_api_item
from tests.e2e.user_simulator import classify_message


class MessageProtocolTests(unittest.TestCase):
    def test_agent_prose_is_not_a_control_card(self):
        for text in ("您批准的 ADR。下一步？", "✅ Storage confirmed. Default priority?",
                     "Should failed tasks remain visible?", "Does this need approval?"):
            with self.subTest(text=text):
                message = thread_message_from_api_item({
                    "msg_type": "interactive", "body": {"content": json.dumps({
                        "title": "🤖 Agent", "elements": [[{"tag": "text", "text": text}]],
                    })},
                })
                self.assertEqual(classify_message(message), "question")

    def test_control_title_survives_api_flattening(self):
        for title, expected in (("✅ Alignment Complete", "complete_card"),
                                ("❌ Error", "error"), ("🔐 Approval Request", "approval_card")):
            for card in ({"title": title}, {"header": {"title": {"content": title}}}):
                card["elements"] = [[{"tag": "text", "text": "Details without control keywords"}]]
                with self.subTest(card=card):
                    message = thread_message_from_api_item({
                        "msg_type": "interactive", "body": {"content": json.dumps(card)},
                    })
                    self.assertEqual(classify_message(message), expected)

    def test_app_reaction_identity_is_acknowledged(self):
        verifier = FeishuOpenAPIVerifier(user_access_token="test-token")
        verifier._request = Mock(return_value={"code": 0, "data": {"items": [{
            "operator": {"operator_id": "cli_bridge", "operator_type": "app"},
            "reaction_type": {"emoji_type": "OK"},
        }]}})
        self.assertTrue(verifier.message_reaction_received("reply", "ou_bridge", operator_app_id="cli_bridge"))
        self.assertFalse(verifier.message_reaction_received("reply", "ou_bridge", operator_app_id="cli_other"))

    def test_app_sender_drives_turn_but_stopped_card_does_not(self):
        for title, expected in (("🤖 Agent", True), ("⏳ Agent Is Thinking", True),
                                ("⏹️ Session Stopped", False)):
            with self.subTest(title=title):
                assertions = participant_round_trip_assertions(
                    [{"message_id": "reply", "create_time": 1.0}],
                    [{"message_id": "reply", "root_id": "root"},
                     {"message_id": "agent", "root_id": "root", "create_time": "2000",
                      "msg_type": "interactive", "sender": {"id": "cli_bridge", "id_type": "app_id", "sender_type": "app"},
                      "body": {"content": json.dumps({"title": title})}}],
                    root_message_id="root", bridge_bot_open_id="ou_bridge",
                    bridge_app_id="cli_bridge", acked_message_ids={"reply"},
                )
                self.assertEqual(assertions["participant_drove_turn"], expected)
