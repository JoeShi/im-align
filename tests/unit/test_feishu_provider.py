"""FeishuProvider bot-message filter: bot senders are discarded unconditionally.

Constructs the real FeishuProvider (client creation is offline, matching the
construction smoke in tests/unit/test_config.py) and feeds synthetic
im.message.receive_v1 events into _on_message. No network, no WebSocket.
"""

import json
import unittest
from types import SimpleNamespace

from scripts.im_align.im_providers.feishu import FeishuProvider


def make_message_event(sender_type="user", open_id="ou_human", event_id="evt-1", text="hello"):
    sender = SimpleNamespace(
        sender_type=sender_type,
        sender_id=SimpleNamespace(open_id=open_id) if open_id else None,
    )
    message = SimpleNamespace(
        chat_id="oc_chat",
        message_id="om_msg",
        root_id="",
        message_type="text",
        content=json.dumps({"text": text}),
        mentions=[],
    )
    event = SimpleNamespace(message=message, sender=sender)
    header = SimpleNamespace(event_id=event_id)
    return SimpleNamespace(event=event, header=header)


class BotMessageFilterTests(unittest.TestCase):
    def make_provider(self):
        return FeishuProvider("cli_test", "secret")

    def test_user_message_is_queued(self):
        provider = self.make_provider()
        provider._on_message(make_message_event(sender_type="user", open_id="ou_human"))

        events = provider.poll_events(timeout=0.1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].sender_open_id, "ou_human")
        self.assertEqual(events[0].text, "hello")

    def test_bot_message_is_discarded_unconditionally(self):
        provider = self.make_provider()
        provider._on_message(make_message_event(sender_type="bot", open_id="ou_other_bot"))

        self.assertEqual(provider.poll_events(timeout=0.1), [])

    def test_duplicate_event_id_dedup_still_applies(self):
        provider = self.make_provider()
        event = make_message_event(event_id="evt-dup")
        provider._on_message(event)
        provider._on_message(event)

        self.assertEqual(len(provider.poll_events(timeout=0.1)), 1)


if __name__ == "__main__":
    unittest.main()
