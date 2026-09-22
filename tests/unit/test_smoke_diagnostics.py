"""Exercise failure archives through the runner without external services."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from tests.e2e.shared.smoke_runner import main, run_smoke
from tests.e2e.shared.transports import FakeEvaluatorLLM, FakeSimulatorLLM, FakeThreadTransport
from tests.unit import test_smoke_runner as fixtures

SCENARIO_DIR = fixtures.SCENARIO_DIR


class FailureArchiveTests(unittest.TestCase):
    def test_completed_run_and_verifier_failure_have_consistent_archives(self):
        for verification_error in (False, True):
            with self.subTest(verification_error=verification_error), tempfile.TemporaryDirectory() as tmp:
                env = {
                    **fixtures.SmokeCredentialsTests.FEISHU,
                    **fixtures.SmokeCredentialsTests.LLM,
                    **fixtures.SmokeCredentialsTests.USER_MODE,
                    "E2E_RUNS_DIR": tmp,
                }
                verifier = Mock()
                verifier.messages_since.return_value = [
                    {"message_id": "reply", "root_id": "root", "create_time": "1000"},
                    {"message_id": "agent", "root_id": "root", "create_time": "2000",
                     "msg_type": "interactive", "sender": {"id": "ou_bridge"},
                     "body": {"content": {"title": "🤖 Agent"}}},
                ]
                verifier.completion_card_received.return_value = True
                verifier.approval_card_received.return_value = False
                verifier.message_reaction_received.return_value = True
                if verification_error:
                    verifier.message_reaction_received.side_effect = RuntimeError("reaction API denied")

                def bridge(run_env, workspace, args, timeout):
                    if args[0] == "wait":
                        spec = workspace / "docs/adr/todo.md"
                        spec.parent.mkdir(parents=True)
                        spec.write_text("TODO Spec")
                    return {"run_id": "run-test", "root_message_id": "root", "state": "done"}

                with patch("tests.e2e.shared.smoke_runner.install_skill_bundle"), patch(
                    "tests.e2e.shared.smoke_runner._bridge", side_effect=bridge,
                ), patch("tests.e2e.shared.smoke_runner.run_simulator", return_value={
                    "reply_receipts": [{"message_id": "reply", "create_time": 1.0}],
                }):
                    result = run_smoke(
                        SCENARIO_DIR, env=env, verifier=verifier,
                        thread_transport=FakeThreadTransport(),
                        simulator_llm=FakeSimulatorLLM(), evaluator_llm=FakeEvaluatorLLM(),
                    )
                archive = Path(result.run_dir)
                expected = "fail" if verification_error else "pass"
                self.assertEqual(result.verdict, expected)
                self.assertEqual(json.loads((archive / "rubric.json").read_text())["verdict"], expected)
                self.assertEqual(json.loads((archive / "result.json").read_text())["verdict"], expected)
                self.assertEqual((archive / "specs/todo.md").read_text(), "TODO Spec")
                if verification_error:
                    self.assertEqual(result.failure_cause, "harness_error")
                    self.assertIn("reaction API denied", (archive / "errors.json").read_text())
                    transcript = json.loads((archive / "transcript.json").read_text())
                    self.assertEqual(len(transcript["thread_messages"]), 2)

    def test_start_failure_preserves_evidence_and_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **fixtures.SmokeCredentialsTests.FEISHU, **fixtures.SmokeCredentialsTests.LLM,
                **fixtures.SmokeCredentialsTests.USER_MODE, "E2E_RUNS_DIR": tmp,
                "E2E_SIMULATOR_LLM_API_KEY": "private-api-key",
            }
            workspace_seen = []

            def bridge(run_env, workspace, args, timeout):
                if args[0] == "start":
                    workspace_seen.append(workspace)
                    logs = Path(run_env["XDG_STATE_HOME"]) / "im-align" / "logs"
                    logs.mkdir(parents=True)
                    (logs / "worker.log").write_text("original failure private-api-key")
                    spec = workspace / "docs" / "adr"
                    spec.mkdir(parents=True)
                    (spec / "draft.md").write_text("partial Spec private-api-key")
                    (spec / "outside.md").symlink_to(Path(tmp) / "outside.txt")
                    (Path(tmp) / "outside.txt").write_text("DO NOT ARCHIVE")
                    raise RuntimeError("startup failure private-api-key")
                if args[0] == "status":
                    raise RuntimeError("secondary cleanup failure")
                self.fail(f"unexpected Bridge call {args}")

            with patch("tests.e2e.shared.smoke_runner.install_skill_bundle"), patch(
                "tests.e2e.shared.smoke_runner._bridge", side_effect=bridge,
            ):
                result = run_smoke(SCENARIO_DIR, env=env)
            self.assertEqual(result.verdict, "fail")
            self.assertEqual(result.failure_cause, "harness_error")
            self.assertFalse(workspace_seen[0].exists())
            archive = Path(result.run_dir)
            self.assertIn("partial Spec", (archive / "specs/draft.md").read_text())
            self.assertTrue((archive / "bridge/logs/worker.log").exists())
            self.assertFalse((archive / "specs/outside.md").exists())
            errors = json.loads((archive / "errors.json").read_text())
            self.assertEqual([e["phase"] for e in errors], ["run", "cleanup"])
            self.assertIn("startup failure", errors[0]["error"])
            contents = "".join(p.read_text() for p in archive.rglob("*") if p.is_file())
            self.assertNotIn("private-api-key", contents)
            self.assertNotIn("DO NOT ARCHIVE", contents)
            self.assertFalse(list(archive.rglob("config.yaml")))

    def test_simulator_failure_keeps_partial_events_and_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **fixtures.SmokeCredentialsTests.FEISHU, **fixtures.SmokeCredentialsTests.LLM,
                **fixtures.SmokeCredentialsTests.USER_MODE, "E2E_RUNS_DIR": tmp,
            }
            verifier = Mock()
            verifier.messages_since.return_value = [{"message_id": "question"}]

            def simulator(*args, **kwargs):
                kwargs["on_state"]({"events": [{"type": "question", "text": "Storage?"}]})
                raise RuntimeError("simulator endpoint unavailable")

            def bridge(run_env, workspace, args, timeout):
                return {"run_id": "run-test", "root_message_id": "root", "state": "stopped"}

            with patch("tests.e2e.shared.smoke_runner.install_skill_bundle"), patch(
                "tests.e2e.shared.smoke_runner._bridge", side_effect=bridge,
            ), patch("tests.e2e.shared.smoke_runner.run_simulator", side_effect=simulator):
                result = run_smoke(
                    SCENARIO_DIR, env=env, verifier=verifier,
                    thread_transport=FakeThreadTransport(),
                    simulator_llm=FakeSimulatorLLM(), evaluator_llm=FakeEvaluatorLLM(),
                )
            archive = Path(result.run_dir)
            self.assertEqual(result.verdict, "fail")
            transcript = json.loads((archive / "transcript.json").read_text())
            self.assertEqual(transcript["simulator"]["events"][0]["text"], "Storage?")
            self.assertEqual(transcript["thread_messages"], [{"message_id": "question"}])
            self.assertIn("simulator endpoint unavailable", (archive / "errors.json").read_text())


class StrictSmokeTests(unittest.TestCase):
    def test_missing_credentials_fail_strict_but_remain_optional_locally(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"E2E_RUNS_DIR": tmp}, clear=True,
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(main([str(SCENARIO_DIR), "--strict"]), 1)
            self.assertEqual(main([str(SCENARIO_DIR)]), 0)
            results = list(Path(tmp).glob("*/result.json"))
            self.assertEqual(len(results), 2)
            self.assertTrue(all(json.loads(p.read_text())["skipped"] for p in results))

    def test_empty_matrix_fails_strict(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main([str(SCENARIO_DIR), "--backends", ",", "--strict"]), 1)
