"""Smoke runner tests: pure helpers and the simulator loop with fake seams.

No network, no real Feishu, no real LLM: thread transports and LLM transports
are fakes, and sleep/now are injected so the polling loop runs instantly.
"""

import tempfile
import unittest
from pathlib import Path

from tests.e2e.im_integration import IntegrationSkip
from tests.e2e.scenario import load_scenario
from tests.e2e.smoke_runner import (
    backend_matrix,
    participant_round_trip_assertions,
    participant_mode_matrix,
    run_guarded_simulator,
    run_archive_dir,
    run_simulator,
    runs_root,
    smoke_credentials,
    summarize,
    wait_for_root_message,
    SmokeResult,
)
from tests.e2e.transports import FakeSimulatorLLM, FakeThreadTransport, ThreadMessage
from tests.e2e.user_simulator import build_graph

SCENARIO_DIR = Path(__file__).resolve().parents[1] / "e2e" / "scenarios" / "todo-greenfield"


def card(text, title="🤖 Agent"):
    return ThreadMessage(message_id="m1", text=text, msg_type="interactive", card_title=title)


def text_msg(text):
    return ThreadMessage(message_id="m1", text=text, msg_type="text")


class BackendMatrixTests(unittest.TestCase):
    def setUp(self):
        self.scenario = load_scenario(SCENARIO_DIR)

    def test_default_is_scenario_backend(self):
        self.assertEqual(backend_matrix(self.scenario, None), [self.scenario.backend])

    def test_single_backend_override(self):
        self.assertEqual(backend_matrix(self.scenario, "opencode"), ["opencode"])

    def test_comma_list(self):
        self.assertEqual(
            backend_matrix(self.scenario, "opencode, kiro-cli"), ["opencode", "kiro-cli"]
        )

    def test_all_expands_every_supported_backend(self):
        self.assertEqual(
            backend_matrix(self.scenario, "all"),
            ["opencode", "trae-cli", "kiro-cli", "kimi"],
        )

    def test_unknown_backend_raises_skip(self):
        with self.assertRaises(IntegrationSkip):
            backend_matrix(self.scenario, "emacs")


class ParticipantModeMatrixTests(unittest.TestCase):
    def test_single_mode_stays_single(self):
        self.assertEqual(participant_mode_matrix("as-user"), ["as-user"])
        self.assertEqual(participant_mode_matrix("bot"), ["bot"])

    def test_all_expands_both_modes(self):
        self.assertEqual(participant_mode_matrix("all"), ["as-user", "bot"])

    def test_unknown_mode_raises_skip(self):
        with self.assertRaises(IntegrationSkip):
            participant_mode_matrix("automatic")


class RunsRootTests(unittest.TestCase):
    def test_env_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(runs_root({"IM_ALIGN_E2E_RUNS_DIR": tmp}), Path(tmp))

    def test_default_uses_xdg_state_home(self):
        root = runs_root({"XDG_STATE_HOME": "/tmp/xdg-test"})
        self.assertEqual(root, Path("/tmp/xdg-test/im-align/e2e-runs"))

    def test_default_without_xdg(self):
        root = runs_root({})
        self.assertTrue(str(root).endswith(".local/state/im-align/e2e-runs"))

    def test_run_archive_dir(self):
        self.assertEqual(
            run_archive_dir(Path("/root"), "run-1"), Path("/root/run-1")
        )


class RootMessageReadinessTests(unittest.TestCase):
    def test_waits_until_status_exposes_thread_root(self):
        statuses = iter(
            [
                {"state": "starting", "root_message_id": ""},
                {"state": "active", "root_message_id": "om_root"},
            ]
        )

        root = wait_for_root_message(
            lambda: next(statuses),
            timeout_seconds=10,
            sleep=lambda _: None,
            now=iter([0.0, 1.0, 2.0]).__next__,
        )

        self.assertEqual(root, "om_root")


class SmokeCredentialsTests(unittest.TestCase):
    FEISHU = {
        "IM_ALIGN_E2E_FEISHU_APP_ID": "cli_x",
        "IM_ALIGN_E2E_FEISHU_APP_SECRET": "secret",
        "IM_ALIGN_E2E_CHAT_ID": "oc_x",
    }
    LLM = {
        "SIMULATOR_LLM_BASE_URL": "https://llm.example",
        "SIMULATOR_LLM_API_KEY": "k",
        "SIMULATOR_LLM_MODEL": "m",
        "EVALUATOR_LLM_BASE_URL": "https://llm.example",
        "EVALUATOR_LLM_API_KEY": "k",
        "EVALUATOR_LLM_MODEL": "m",
    }
    USER_MODE = {
        "IM_ALIGN_E2E_BRIDGE_BOT_OPEN_ID": "ou_bridge",
        "IM_ALIGN_E2E_USER_ACCESS_TOKEN": "u-token",
    }
    BOT_MODE = {
        "IM_ALIGN_E2E_BRIDGE_BOT_OPEN_ID": "ou_bridge",
        "IM_ALIGN_E2E_SIMULATOR_APP_ID": "cli_bot",
        "IM_ALIGN_E2E_SIMULATOR_APP_SECRET": "bot-secret",
        "IM_ALIGN_E2E_SIMULATOR_OPEN_ID": "ou_simulator",
    }

    def _env(self, *modes):
        env = dict(self.FEISHU)
        env.update(self.LLM)
        for mode in modes:
            env.update(mode)
        return env

    def test_user_token_mode(self):
        creds = smoke_credentials(self._env(self.USER_MODE), "as-user")
        self.assertEqual(creds["participant"].mode, "as-user")

    def test_bot_mode(self):
        creds = smoke_credentials(self._env(self.BOT_MODE), "bot")
        self.assertEqual(creds["participant"].mode, "bot")
        self.assertEqual(
            creds["participant"].extra_participant_open_ids,
            ["ou_simulator"],
        )

    def test_both_identity_sets_can_coexist_when_mode_is_explicit(self):
        env = self._env(self.USER_MODE, self.BOT_MODE)
        self.assertEqual(smoke_credentials(env, "as-user")["participant"].mode, "as-user")
        self.assertEqual(smoke_credentials(env, "bot")["participant"].mode, "bot")

    def test_selected_mode_reports_its_missing_identity(self):
        with self.assertRaisesRegex(IntegrationSkip, "USER_ACCESS_TOKEN"):
            smoke_credentials(
                self._env({"IM_ALIGN_E2E_BRIDGE_BOT_OPEN_ID": "ou_bridge"}),
                "as-user",
            )

    def test_missing_feishu_credentials_reported(self):
        env = dict(self.USER_MODE)
        env.update(self.LLM)
        with self.assertRaises(IntegrationSkip):
            smoke_credentials(env, "as-user")

    def test_missing_llm_reported(self):
        env = self._env(self.USER_MODE)
        del env["EVALUATOR_LLM_BASE_URL"]
        with self.assertRaisesRegex(IntegrationSkip, "EVALUATOR_LLM"):
            smoke_credentials(env, "as-user")


class SummarizeTests(unittest.TestCase):
    def test_counts(self):
        results = [
            SmokeResult(scenario="a", backend="x", verdict="pass"),
            SmokeResult(scenario="a", backend="y", verdict="fail"),
            SmokeResult(scenario="b", backend="x", skipped=True),
        ]
        self.assertEqual(
            summarize(results), {"total": 3, "skipped": 1, "passed": 1, "failed": 1}
        )

    def test_passed_property(self):
        self.assertTrue(SmokeResult(scenario="a", backend="x", verdict="pass").passed)
        self.assertFalse(SmokeResult(scenario="a", backend="x", verdict="fail").passed)
        self.assertFalse(
            SmokeResult(scenario="a", backend="x", skipped=True, skip_reason="s").passed
        )


class ParticipantRoundTripAssertionTests(unittest.TestCase):
    def test_reply_ack_and_later_bridge_message_prove_round_trip(self):
        receipts = [{"message_id": "om_reply", "create_time": 100.0}]
        messages = [
            {
                "message_id": "om_reply",
                "root_id": "om_root",
                "create_time": "100000",
                "sender": {"id": "ou_simulator"},
            },
            {
                "message_id": "om_agent",
                "root_id": "om_root",
                "create_time": "101000",
                "msg_type": "interactive",
                "body": {"content": {"title": "🤖 Agent"}},
                "sender": {"id": "ou_bridge"},
            },
        ]

        self.assertEqual(
            participant_round_trip_assertions(
                receipts,
                messages,
                root_message_id="om_root",
                bridge_bot_open_id="ou_bridge",
                acked_message_ids={"om_reply"},
            ),
            {
                "participant_replied": True,
                "participant_reply_acked": True,
                "participant_drove_turn": True,
            },
        )

    def test_visible_reply_without_ack_or_later_turn_fails_closed(self):
        assertions = participant_round_trip_assertions(
            [{"message_id": "om_reply", "create_time": 100.0}],
            [
                {
                    "message_id": "om_reply",
                    "root_id": "om_root",
                    "create_time": "100000",
                    "sender": {"id": "ou_simulator"},
                }
            ],
            root_message_id="om_root",
            bridge_bot_open_id="ou_bridge",
            acked_message_ids=set(),
        )

        self.assertEqual(
            assertions,
            {
                "participant_replied": True,
                "participant_reply_acked": False,
                "participant_drove_turn": False,
            },
        )


class SimulatorLoopTests(unittest.TestCase):
    def _graph(self, messages):
        thread = FakeThreadTransport(inbox=list(messages))
        return build_graph(thread, FakeSimulatorLLM(answer="ack")), thread

    def _clock(self, start=0.0, step=3.0):
        ticks = iter([start + i * step for i in range(10000)])
        return lambda: next(ticks)

    def test_complete_card_terminates(self):
        graph, thread = self._graph([card("✅ Alignment Complete", title="✅ Alignment Complete")])
        state = run_simulator(
            graph, thread, "brief", 900, lambda: False,
            sleep=lambda s: None, now=self._clock(),
        )
        self.assertEqual(state["terminal"], "complete")

    def test_approval_card_is_deterministic_failure(self):
        graph, thread = self._graph([card("请批准：Allow once", title="🔐 Approval Request")])
        state = run_simulator(
            graph, thread, "brief", 900, lambda: False,
            sleep=lambda s: None, now=self._clock(),
        )
        self.assertEqual(state["terminal"], "approval_card_detected")

    def test_session_terminal_stops_idle_polling(self):
        graph, thread = self._graph([])
        terminal_flag = {"value": False}

        def session_terminal():
            terminal_flag["value"] = not terminal_flag["value"]
            return terminal_flag["value"]

        state = run_simulator(
            graph, thread, "brief", 900, session_terminal,
            sleep=lambda s: None, now=self._clock(),
        )
        self.assertEqual(state["terminal"], "")

    def test_wall_clock_timeout_bounds_the_loop(self):
        graph, thread = self._graph([])

        def fast_now():
            fast_now.t += 900.0
            return fast_now.t

        fast_now.t = 0.0
        state = run_simulator(
            graph, thread, "brief", 900, lambda: False, sleep=lambda s: None, now=fast_now
        )
        self.assertEqual(state["terminal"], "")

    def test_question_gets_reply_before_timeout(self):
        graph, thread = self._graph([text_msg("排序规则？")])
        state = run_simulator(
            graph, thread, "brief", 900, lambda: False,
            sleep=lambda s: None, now=self._clock(),
        )
        self.assertEqual(state["replies"], ["ack"])
        self.assertEqual(state["terminal"], "")

    def test_guarded_simulator_records_failure_and_stops_session(self):
        outcome = {"state": None, "error": None}
        stopped = []

        def fail():
            raise RuntimeError("participant transport failed")

        run_guarded_simulator(outcome, fail, lambda: stopped.append(True))

        self.assertIsInstance(outcome["error"], RuntimeError)
        self.assertEqual(str(outcome["error"]), "participant transport failed")
        self.assertEqual(stopped, [True])


if __name__ == "__main__":
    unittest.main()
