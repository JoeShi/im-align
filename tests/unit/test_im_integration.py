"""IM Integration runner tests: deterministic assertions and seams only.

No network, no real Feishu: the cloner and thread verifier are fakes, git
operations run in a temporary repository, and credential checks use an
explicit env dict.
"""

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from tests.e2e.shared.im_integration import (
    IntegrationSkip,
    RunResult,
    SeedCloner,
    _InvalidCompletionWatcher,
    _write_user_config,
    check_artifacts,
    feishu_credentials,
    git_unchanged_except,
    materialize_seed,
    snapshot_git,
)
from tests.e2e.shared.scenario import load_scenario
from tests.e2e.shared.transports import FakeThreadTransport, ThreadMessage
from tests.e2e.shared.verifier import (
    INVALID_COMPLETION_TITLE,
    FeishuOpenAPIVerifier,
    ThreadVerifier,
    find_approval_card,
    message_contains_card,
)

SCENARIO_DIR = Path(__file__).resolve().parents[1] / "e2e" / "scenarios" / "todo-greenfield"


class InvalidCompletionWatcherTests(unittest.TestCase):
    def test_invalid_completion_card_sets_event(self):
        inner = FakeThreadTransport()
        inner.push(ThreadMessage(message_id="m1", text="", card_title=INVALID_COMPLETION_TITLE))
        event = threading.Event()
        watcher = _InvalidCompletionWatcher(inner, event)

        message = watcher.poll()

        self.assertEqual(message.message_id, "m1")
        self.assertTrue(event.is_set())

    def test_other_messages_do_not_set_event(self):
        inner = FakeThreadTransport()
        inner.push(ThreadMessage(message_id="m1", text="还需要什么？", card_title="🤖 Agent"))
        event = threading.Event()
        watcher = _InvalidCompletionWatcher(inner, event)

        watcher.poll()

        self.assertFalse(event.is_set())

    def test_idle_poll_does_not_set_event(self):
        watcher = _InvalidCompletionWatcher(FakeThreadTransport(), threading.Event())

        self.assertIsNone(watcher.poll())
        self.assertFalse(watcher._event.is_set())

    def test_post_reply_delegates(self):
        inner = FakeThreadTransport()
        watcher = _InvalidCompletionWatcher(inner, threading.Event())

        receipt = watcher.post_reply("固定回复")

        self.assertEqual(inner.replies, ["固定回复"])
        self.assertEqual(receipt.message_id, "fake-reply-1")


class WriteUserConfigTests(unittest.TestCase):
    def test_writes_expected_layout_under_config_home(self):
        import stat

        import yaml

        scenario = load_scenario(SCENARIO_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            _write_user_config(
                Path(tmp),
                {"app_id": "cli_x", "app_secret": "secret"},
                scenario,
                backend="kiro-cli",
            )
            path = Path(tmp) / "im-align" / "config.yaml"
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(data["providers"]["e2e"]["app_id"], "cli_x")
        self.assertEqual(data["defaults"]["agent"]["backend"], "kiro-cli")
        self.assertEqual(data["defaults"]["agent"]["spec_root"], scenario.spec_root)
        self.assertEqual(data["commands"]["e2e-fake"]["backend"], "kiro-cli")
        args = data["commands"]["e2e-fake"]["args"]
        self.assertEqual(args[:2], ["-m", "tests.e2e.shared.fake_acp_agent"])
        self.assertTrue(Path(args[2]).is_absolute(), "transcript path must be absolute")

    def test_writes_turn_timeout_for_backend_smoke(self):
        import yaml

        scenario = load_scenario(SCENARIO_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            _write_user_config(
                Path(tmp),
                {"app_id": "cli_x", "app_secret": "secret"},
                scenario,
                turn_timeout_seconds=scenario.timeout_seconds,
            )
            data = yaml.safe_load(
                (Path(tmp) / "im-align" / "config.yaml").read_text(encoding="utf-8")
            )

        self.assertEqual(
            data["defaults"]["timeouts"]["turn_timeout_seconds"],
            scenario.timeout_seconds,
        )

    def test_omits_turn_timeout_unless_requested(self):
        import yaml

        scenario = load_scenario(SCENARIO_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            _write_user_config(
                Path(tmp),
                {"app_id": "cli_x", "app_secret": "secret"},
                scenario,
            )
            data = yaml.safe_load(
                (Path(tmp) / "im-align" / "config.yaml").read_text(encoding="utf-8")
            )

        self.assertNotIn("timeouts", data["defaults"])


class ReactionVerifierTests(unittest.TestCase):
    def test_message_reaction_is_observed_as_ack(self):
        class StubVerifier(FeishuOpenAPIVerifier):
            def _request(self, method, path, body=None, token=""):
                self.observed = (method, path, token)
                return {
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "reaction_type": {"emoji_type": "OK"},
                                "operator": {"operator_id": "ou_bridge"},
                            }
                        ]
                    },
                }

        verifier = StubVerifier(user_access_token="u-token")

        self.assertTrue(
            verifier.message_reaction_received("om_reply", "ou_bridge")
        )
        self.assertEqual(
            verifier.observed,
            (
                "GET",
                "/open-apis/im/v1/messages/om_reply/reactions?page_size=50",
                "u-token",
            ),
        )


class ThreadContainerVerifierTests(unittest.TestCase):
    def test_messages_since_uses_thread_container_when_root_bound(self):
        # Measured: the chat-container listing omits thread replies for bot
        # identities; a root-bound verifier must list the thread container.
        observed = []

        class StubVerifier(FeishuOpenAPIVerifier):
            def _request(self, method, path, body=None, token=""):
                observed.append(path)
                if path.startswith("/open-apis/im/v1/messages/om_root"):
                    return {
                        "code": 0,
                        "data": {"items": [{"message_id": "om_root", "thread_id": "omt_9"}]},
                    }
                return {
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "message_id": "om_x",
                                "create_time": "2000",
                                "msg_type": "interactive",
                                "body": {"content": "{}"},
                            },
                            {
                                "message_id": "om_old",
                                "create_time": "1000",
                                "msg_type": "text",
                                "body": {"content": '{"text":"old"}'},
                            },
                        ],
                        "has_more": False,
                    },
                }

        verifier = StubVerifier(user_access_token="u-token", root_message_id="om_root")

        messages = verifier.messages_since("oc_chat", 1.5)

        self.assertEqual([m["message_id"] for m in messages], ["om_x"])
        self.assertTrue(
            any("container_id_type=thread&container_id=omt_9" in p for p in observed)
        )
        self.assertFalse(any("container_id_type=chat" in p for p in observed))

    def test_user_identity_is_required(self):
        with self.assertRaisesRegex(ValueError, "user_access_token"):
            FeishuOpenAPIVerifier()

    def test_user_identity_sends_user_token(self):
        class StubVerifier(FeishuOpenAPIVerifier):
            def _request(self, method, path, body=None, token=""):
                self.observed_token = token
                return {"code": 0, "data": {"items": [], "has_more": False}}

        verifier = StubVerifier(user_access_token="u-token")

        self.assertEqual(verifier.messages_since("oc_chat", 0.0), [])
        self.assertEqual(verifier.observed_token, "u-token")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=30
    ).stdout


class FakeCloner(SeedCloner):
    def __init__(self):
        self.calls = []

    def clone(self, url: str, ref: str, dest: Path):
        self.calls.append((url, ref))
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "cloned.txt").write_text(f"{url}@{ref}\n", encoding="utf-8")


class MaterializeSeedTests(unittest.TestCase):
    def test_seed_tree_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            scenario = load_scenario(SCENARIO_DIR)
            materialize_seed(scenario, workspace)
            self.assertTrue((workspace / ".git").is_dir())
            self.assertEqual(git(workspace, "rev-parse", "--is-inside-work-tree").strip(), "true")
            self.assertEqual(git(workspace, "log", "--format=%s", "-1").strip(), "seed")

    def test_seed_repo_uses_injected_cloner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scenario_dir = root / "sc"
            scenario_dir.mkdir()
            (scenario_dir / "scenario.yaml").write_text(
                (SCENARIO_DIR / "scenario.yaml").read_text(encoding="utf-8")
                + "seed_repo:\n  url: https://github.com/org/repo\n  ref: "
                + "a" * 40
                + "\n",
                encoding="utf-8",
            )
            scenario = load_scenario(scenario_dir)
            cloner = FakeCloner()
            workspace = root / "ws"
            workspace.mkdir()
            materialize_seed(scenario, workspace, cloner=cloner)
            self.assertEqual(cloner.calls, [("https://github.com/org/repo", "a" * 40)])
            self.assertTrue((workspace / "cloned.txt").is_file())


class ArtifactAssertionTests(unittest.TestCase):
    def test_artifacts_and_marker_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            scenario = load_scenario(SCENARIO_DIR)
            artifact_path = scenario.expected_artifacts[0].path
            spec = workspace / artifact_path
            spec.parent.mkdir(parents=True)
            spec.write_text("# Spec\n[ALIGNMENT_COMPLETE]\n", encoding="utf-8")
            results = check_artifacts(workspace, scenario)
            self.assertEqual(results, {f"artifact:{artifact_path}": True})

    def test_missing_artifact_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            scenario = load_scenario(SCENARIO_DIR)
            results = check_artifacts(workspace, scenario)
            artifact_path = scenario.expected_artifacts[0].path
            self.assertEqual(results, {f"artifact:{artifact_path}": False})

    def test_marker_line_enforced_only_when_declared(self):
        import dataclasses

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            scenario = load_scenario(SCENARIO_DIR)
            artifact = dataclasses.replace(
                scenario.expected_artifacts[0], marker_line="[ALIGNMENT_COMPLETE]"
            )
            scenario = dataclasses.replace(
                scenario, expected_artifacts=[artifact]
            )
            spec = workspace / artifact.path
            spec.parent.mkdir(parents=True)
            spec.write_text("# Spec without marker\n", encoding="utf-8")
            self.assertEqual(
                check_artifacts(workspace, scenario),
                {f"artifact:{artifact.path}": False},
            )
            spec.write_text("# Spec\n[ALIGNMENT_COMPLETE]\n", encoding="utf-8")
            self.assertEqual(
                check_artifacts(workspace, scenario),
                {f"artifact:{artifact.path}": True},
            )

    def test_glob_path_matches_agent_named_file(self):
        import dataclasses

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            scenario = load_scenario(SCENARIO_DIR)
            artifact = dataclasses.replace(
                scenario.expected_artifacts[0],
                path="docs/specs/*.md",
                marker_line="[ALIGNMENT_COMPLETE]",
            )
            scenario = dataclasses.replace(scenario, expected_artifacts=[artifact])
            spec = workspace / "docs" / "specs" / "0001-todo-list.md"
            spec.parent.mkdir(parents=True)
            # No match yet.
            self.assertEqual(
                check_artifacts(workspace, scenario),
                {"artifact:docs/specs/*.md": False},
            )
            # A matching file without the marker still fails.
            spec.write_text("# Spec without marker\n", encoding="utf-8")
            self.assertEqual(
                check_artifacts(workspace, scenario),
                {"artifact:docs/specs/*.md": False},
            )
            # At least one match satisfying the marker passes.
            spec.write_text("# Spec\n[ALIGNMENT_COMPLETE]\n", encoding="utf-8")
            self.assertEqual(
                check_artifacts(workspace, scenario),
                {"artifact:docs/specs/*.md": True},
            )


class GitAssertionTests(unittest.TestCase):
    def _repo(self, tmp: str) -> Path:
        workspace = Path(tmp)
        subprocess.run(["git", "init", "--quiet"], cwd=workspace, check=True)
        # CI runners have no ambient git identity; the seed commit fails
        # with exit 128 without an explicit one (measured 2026-09-17).
        subprocess.run(
            [
                "git",
                "-c", "user.name=im-align-e2e",
                "-c", "user.email=im-align-e2e@localhost",
                "commit", "--quiet", "--allow-empty", "-m", "seed",
            ],
            cwd=workspace,
            check=True,
        )
        return workspace

    def test_snapshot_and_no_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._repo(tmp)
            before = snapshot_git(workspace)
            after = snapshot_git(workspace)
            self.assertTrue(git_unchanged_except(before, after, ["docs/specs/x.md"]))

    def test_undeclared_change_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._repo(tmp)
            before = snapshot_git(workspace)
            (workspace / "src.py").write_text("new file\n", encoding="utf-8")
            after = snapshot_git(workspace)
            self.assertFalse(
                git_unchanged_except(before, after, ["docs/specs/todo.md"])
            )

    def test_declared_artifact_change_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._repo(tmp)
            before = snapshot_git(workspace)
            spec = workspace / "docs" / "specs" / "todo.md"
            spec.parent.mkdir(parents=True)
            spec.write_text("spec\n", encoding="utf-8")
            after = snapshot_git(workspace)
            self.assertTrue(
                git_unchanged_except(before, after, ["docs/specs/todo.md"])
            )

    def test_glob_allowlist_matches_agent_named_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._repo(tmp)
            before = snapshot_git(workspace)
            spec = workspace / "docs" / "specs" / "0001-todo-list.md"
            spec.parent.mkdir(parents=True)
            spec.write_text("spec\n", encoding="utf-8")
            after = snapshot_git(workspace)
            self.assertTrue(
                git_unchanged_except(before, after, ["docs/specs/*.md"])
            )

    def test_head_move_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = self._repo(tmp)
            before = snapshot_git(workspace)
            (workspace / "x").write_text("x", encoding="utf-8")
            git(workspace, "add", "x")
            # Same ambient-identity constraint as _repo above.
            git(
                workspace,
                "-c", "user.name=im-align-e2e",
                "-c", "user.email=im-align-e2e@localhost",
                "commit", "--quiet", "-m", "extra",
            )
            after = snapshot_git(workspace)
            self.assertFalse(git_unchanged_except(before, after, []))


class CredentialTests(unittest.TestCase):
    FULL_ENV = {
        "E2E_BRIDGE_FEISHU_APP_ID": "cli_x",
        "E2E_BRIDGE_FEISHU_APP_SECRET": "secret",
        "E2E_BRIDGE_CHAT_ID": "oc_x",
    }

    def test_missing_credentials_skip(self):
        with self.assertRaisesRegex(IntegrationSkip, "E2E_BRIDGE_FEISHU_APP_ID"):
            feishu_credentials({})

    def test_credentials_read_from_env(self):
        creds = feishu_credentials(dict(self.FULL_ENV))
        self.assertEqual(creds["chat_id"], "oc_x")
        self.assertNotIn("initiator_open_id", creds)

    def _env_without_chat_id(self):
        return {k: v for k, v in self.FULL_ENV.items() if k != "E2E_BRIDGE_CHAT_ID"}

    def test_chat_id_falls_back_to_repo_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(Path(tmp) / ".im-align.yaml", "w", encoding="utf-8") as f:
                f.write("im:\n  chat_id: oc_repo\n")
            creds = feishu_credentials(self._env_without_chat_id(), repo_cwd=tmp)
        self.assertEqual(creds["chat_id"], "oc_repo")

    def test_env_chat_id_beats_repo_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(Path(tmp) / ".im-align.yaml", "w", encoding="utf-8") as f:
                f.write("im:\n  chat_id: oc_repo\n")
            creds = feishu_credentials(dict(self.FULL_ENV), repo_cwd=tmp)
        self.assertEqual(creds["chat_id"], "oc_x")

    def test_repo_fallback_absent_config_yields_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(IntegrationSkip, "chat_id"):
                feishu_credentials(self._env_without_chat_id(), repo_cwd=tmp)


class ApprovalCardDetectionTests(unittest.TestCase):
    """Zero-Approval-card gate over fake im/v1/messages streams."""

    def _card_message(self, card_json: dict):
        return {"message_id": "om_x", "body": {"content": card_json}}

    def test_approval_card_detected_by_title(self):
        message = self._card_message(
            {"header": {"title": {"content": "🔐 Approval Request"}}}
        )
        self.assertTrue(find_approval_card([message]))

    def test_approval_card_detected_by_button_value(self):
        message = self._card_message(
            {"elements": [{"actions": [{"value": {"im_align": "approval"}}]}]}
        )
        self.assertTrue(find_approval_card([message]))

    def test_completion_card_is_not_an_approval_card(self):
        message = self._card_message(
            {"header": {"title": {"content": "✅ Alignment Complete"}}}
        )
        self.assertFalse(find_approval_card([message]))

    def test_text_message_is_not_an_approval_card(self):
        message = {"message_id": "om_x", "body": {"content": "please approve this idea"}}
        self.assertFalse(find_approval_card([message]))

    def test_empty_stream_passes(self):
        self.assertFalse(find_approval_card([]))

    def test_message_contains_card_marker(self):
        message = self._card_message({"header": {"title": {"content": "✅ Alignment Complete"}}})
        self.assertTrue(message_contains_card(message, "✅ Alignment Complete"))
        self.assertFalse(message_contains_card(message, "🔐 Approval Request"))


class FakeVerifier(ThreadVerifier):
    def __init__(self, completion=True, approval=False):
        self._completion = completion
        self._approval = approval

    def completion_card_received(self, chat_id: str, since_ts: float) -> bool:
        return self._completion

    def approval_card_received(self, chat_id: str, since_ts: float) -> bool:
        return self._approval


class ZeroApprovalAssertionTests(unittest.TestCase):
    """The runner's zero_approval_cards assertion against fake verifiers."""

    def _assertion(self, verifier) -> bool:
        # Mirrors the runner wiring: pass only when no Approval card arrived.
        return not verifier.approval_card_received("oc_x", 0.0)

    def test_happy_path_without_approval_cards_passes(self):
        self.assertTrue(self._assertion(FakeVerifier(completion=True, approval=False)))

    def test_approval_card_fails_the_run(self):
        self.assertFalse(self._assertion(FakeVerifier(completion=True, approval=True)))


class RunResultTests(unittest.TestCase):
    def test_passed_requires_done_state_and_all_assertions(self):
        result = RunResult(scenario="demo", state="done", assertions={"a": True})
        self.assertTrue(result.passed)
        result.assertions["a"] = False
        self.assertFalse(result.passed)
        result.assertions["a"] = True
        result.state = "failed"
        self.assertFalse(result.passed)
        result.skipped = True
        self.assertFalse(result.passed)

    def test_none_assertions_fail_closed(self):
        result = RunResult(
            scenario="demo",
            state="done",
            assertions={"a": True, "completion_card": None, "zero_approval_cards": None},
        )
        self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()
