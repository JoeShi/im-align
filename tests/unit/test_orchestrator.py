import tempfile
import time
import unittest

from scripts.im_align import state
from scripts.im_align.acp.client import TurnResult
from scripts.im_align.im_providers.base import IncomingMessage
from scripts.im_align.orchestrator import Orchestrator


class FakeProvider:
    def __init__(self, event):
        self._events = [event]
        self.replies = []

    def poll_events(self, timeout):
        if self._events:
            return [self._events.pop(0)]
        time.sleep(0.005)
        return []

    def react_ok(self, message_id):
        pass

    def user_name(self, open_id):
        return "Participant"

    def reply_card(self, root_message_id, card_json):
        self.replies.append((root_message_id, card_json))
        return "om_card"

    def patch_card(self, message_id, card_json):
        pass


class FakeClient:
    def __init__(self):
        self.prompts = []

    def prompt(self, session_id, text):
        self.prompts.append((session_id, text))
        return TurnResult("Continuing alignment.", "end_turn")


class OrchestratorLifecycleTests(unittest.TestCase):
    def test_thread_stop_text_is_normal_alignment_input(self):
        event = IncomingMessage(
            event_id="evt_stop",
            chat_id="oc_test",
            message_id="om_stop",
            root_id="om_root",
            sender_open_id="ou_participant",
            sender_type="user",
            text="stop",
        )
        provider = FakeProvider(event)
        client = FakeClient()
        with tempfile.TemporaryDirectory() as cwd:
            now = time.time()
            record = {
                "state": state.STATE_ACTIVE,
                "root_message_id": "om_root",
                "last_activity_at_ts": now,
                "attempt_started_at_ts": now,
                "started_at_ts": now,
                "cwd": cwd,
                "acp_session_id": "session-1",
                "client": client,
            }
            orchestrator = Orchestrator(
                provider,
                {
                    "timeouts": {"idle_timeout_seconds": 0.05},
                    "agent": {"spec_root": "docs/specs"},
                },
                record,
            )

            outcome = orchestrator.run()

        self.assertEqual(outcome, state.STATE_IDLE_TIMEOUT)
        self.assertEqual(client.prompts, [("session-1", "Participant: stop")])


if __name__ == "__main__":
    unittest.main()
