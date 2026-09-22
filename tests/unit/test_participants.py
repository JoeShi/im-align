"""Participant Adapter behavior for Backend Smoke and Full e2e."""

import json
import unittest

from tests.e2e.shared.participants import ParticipantConfigError, participant_from_env


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class RecordingUrlopen:
    def __init__(self):
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        return FakeResponse(
            {"code": 0, "data": {"message_id": "om_reply", "create_time": "123000"}}
        )


class BotInfoUrlopen:
    def __call__(self, request, timeout=None):
        if "tenant_access_token/internal" in request.full_url:
            app_id = json.loads(request.data)["app_id"]
            return FakeResponse(
                {
                    "code": 0,
                    "tenant_access_token": f"t-{app_id}",
                    "expire": 3600,
                }
            )
        authorization = request.get_header("Authorization")
        open_id = {
            "Bearer t-cli_bridge": "ou_bridge",
        }[authorization]
        return FakeResponse({"code": 0, "msg": "ok", "bot": {"open_id": open_id}})


class ParticipantAdapterTests(unittest.TestCase):
    IDENTITIES = {
        "E2E_BRIDGE_BOT_OPEN_ID": "ou_bridge",
        "E2E_SIMULATOR_USER_ACCESS_TOKEN": "u-token",
    }

    APP_CREDENTIALS_ONLY = {
        "E2E_BRIDGE_FEISHU_APP_ID": "cli_bridge",
        "E2E_BRIDGE_FEISHU_APP_SECRET": "bridge-secret",
    }

    def test_as_user_identity_uses_user_token(self):
        participant = participant_from_env(self.IDENTITIES)

        self.assertEqual(participant.bridge_bot_open_id, "ou_bridge")

    def test_as_user_reply_mentions_bridge_bot_and_returns_receipt(self):
        http = RecordingUrlopen()
        participant = participant_from_env(self.IDENTITIES)
        thread = participant.open_thread(
            "oc_chat", root_message_id="om_root", urlopen=http
        )

        receipt = thread.post_reply("排序按创建时间")

        request = http.requests[-1]
        body = json.loads(request.data)
        content = json.loads(body["content"])["text"]
        self.assertEqual(request.get_header("Authorization"), "Bearer u-token")
        self.assertTrue(request.full_url.endswith("/messages/om_root/reply"))
        self.assertTrue(body["reply_in_thread"])
        self.assertNotIn("root_id", body)
        self.assertEqual(
            content,
            '<at user_id="ou_bridge"></at> 排序按创建时间',
        )
        self.assertEqual(receipt.message_id, "om_reply")
        self.assertEqual(receipt.create_time, 123.0)

    def test_as_user_resolves_bridge_bot_open_id_from_app_credentials(self):
        participant = participant_from_env(
            dict(self.APP_CREDENTIALS_ONLY, E2E_SIMULATOR_USER_ACCESS_TOKEN="u-token"),
            urlopen=BotInfoUrlopen(),
        )

        self.assertEqual(participant.bridge_bot_open_id, "ou_bridge")

    def test_as_user_verifier_uses_user_identity(self):
        participant = participant_from_env(self.IDENTITIES)

        verifier = participant.open_verifier(root_message_id="om_root")

        self.assertEqual(verifier._user_access_token, "u-token")
        self.assertEqual(verifier._root_message_id, "om_root")

    def test_missing_user_token_is_a_participant_configuration_error(self):
        with self.assertRaisesRegex(ParticipantConfigError, "USER_ACCESS_TOKEN"):
            participant_from_env({"E2E_BRIDGE_BOT_OPEN_ID": "ou_bridge"})

    def test_identity_lookup_failure_is_a_participant_configuration_error(self):
        def unavailable(request, timeout=None):
            raise OSError("network unavailable")

        with self.assertRaisesRegex(ParticipantConfigError, "cannot resolve bot identity"):
            participant_from_env(
                self.APP_CREDENTIALS_ONLY,
                urlopen=unavailable,
            )


if __name__ == "__main__":
    unittest.main()
