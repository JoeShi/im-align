"""Participant Adapter behavior for Backend Smoke and Full e2e."""

import json
import unittest

from tests.e2e.participants import ParticipantConfigError, participant_from_env


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


class BotRecordingUrlopen(RecordingUrlopen):
    def __call__(self, request, timeout=None):
        if "tenant_access_token/internal" in request.full_url:
            return FakeResponse(
                {"code": 0, "tenant_access_token": "t-bot", "expire": 3600}
            )
        return super().__call__(request, timeout)


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
            "Bearer t-cli_bot": "ou_simulator",
        }[authorization]
        return FakeResponse({"code": 0, "msg": "ok", "bot": {"open_id": open_id}})


class ParticipantAdapterTests(unittest.TestCase):
    ALL_IDENTITIES = {
        "IM_ALIGN_E2E_BRIDGE_BOT_OPEN_ID": "ou_bridge",
        "IM_ALIGN_E2E_USER_ACCESS_TOKEN": "u-token",
        "IM_ALIGN_E2E_SIMULATOR_APP_ID": "cli_bot",
        "IM_ALIGN_E2E_SIMULATOR_APP_SECRET": "bot-secret",
        "IM_ALIGN_E2E_SIMULATOR_OPEN_ID": "ou_simulator",
    }

    APP_CREDENTIALS_ONLY = {
        "IM_ALIGN_E2E_FEISHU_APP_ID": "cli_bridge",
        "IM_ALIGN_E2E_FEISHU_APP_SECRET": "bridge-secret",
        "IM_ALIGN_E2E_SIMULATOR_APP_ID": "cli_bot",
        "IM_ALIGN_E2E_SIMULATOR_APP_SECRET": "bot-secret",
    }

    def test_explicit_as_user_mode_ignores_available_bot_credentials(self):
        participant = participant_from_env("as-user", self.ALL_IDENTITIES)

        self.assertEqual(participant.mode, "as-user")
        self.assertEqual(participant.extra_participant_open_ids, [])
        self.assertEqual(participant.bridge_bot_open_id, "ou_bridge")

    def test_explicit_bot_mode_contributes_only_simulator_to_allowlist(self):
        participant = participant_from_env("bot", self.ALL_IDENTITIES)

        self.assertEqual(participant.mode, "bot")
        self.assertEqual(
            participant.extra_participant_open_ids,
            ["ou_simulator"],
        )
        self.assertEqual(participant.bridge_bot_open_id, "ou_bridge")

    def test_as_user_reply_mentions_bridge_bot_and_returns_receipt(self):
        http = RecordingUrlopen()
        participant = participant_from_env("as-user", self.ALL_IDENTITIES)
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

    def test_bot_reply_uses_app_identity_behind_same_interface(self):
        http = BotRecordingUrlopen()
        participant = participant_from_env("bot", self.ALL_IDENTITIES)
        thread = participant.open_thread(
            "oc_chat", root_message_id="om_root", urlopen=http, clock=lambda: 10.0
        )

        receipt = thread.post_reply("保留已完成项目")

        request = http.requests[-1]
        body = json.loads(request.data)
        content = json.loads(body["content"])["text"]
        self.assertEqual(request.get_header("Authorization"), "Bearer t-bot")
        self.assertTrue(request.full_url.endswith("/messages/om_root/reply"))
        self.assertTrue(body["reply_in_thread"])
        self.assertEqual(
            content,
            '<at user_id="ou_bridge"></at> 保留已完成项目',
        )
        self.assertEqual(receipt.message_id, "om_reply")

    def test_bot_mode_resolves_both_open_ids_from_app_credentials(self):
        participant = participant_from_env(
            "bot",
            self.APP_CREDENTIALS_ONLY,
            urlopen=BotInfoUrlopen(),
        )

        self.assertEqual(participant.bridge_bot_open_id, "ou_bridge")
        self.assertEqual(
            participant.extra_participant_open_ids,
            ["ou_simulator"],
        )

    def test_as_user_verifier_uses_user_identity(self):
        participant = participant_from_env("as-user", self.ALL_IDENTITIES)

        verifier = participant.open_verifier(root_message_id="om_root")

        self.assertEqual(verifier._user_access_token, "u-token")
        self.assertEqual(verifier._root_message_id, "om_root")

    def test_bot_verifier_uses_simulator_app_never_bridge_credentials(self):
        participant = participant_from_env("bot", self.ALL_IDENTITIES)

        verifier = participant.open_verifier(root_message_id="om_root")

        self.assertEqual(verifier._app_id, "cli_bot")
        self.assertEqual(verifier._app_secret, "bot-secret")
        self.assertEqual(verifier._root_message_id, "om_root")

    def test_identity_lookup_failure_is_a_participant_configuration_error(self):
        def unavailable(request, timeout=None):
            raise OSError("network unavailable")

        with self.assertRaisesRegex(ParticipantConfigError, "cannot resolve bot identity"):
            participant_from_env(
                "bot",
                self.APP_CREDENTIALS_ONLY,
                urlopen=unavailable,
            )


if __name__ == "__main__":
    unittest.main()
