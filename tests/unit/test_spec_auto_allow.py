"""ADR-0005: deterministic permissions allow only safe reads and Spec edits.

Covers two seams:
  - config.normalize_spec_root validation/normalization, wired through
    user defaults, repository .im-align.yaml, and CLI overrides;
  - PermissionPolicy, which allows every resolved target of an edit only when
    it is inside agent.spec_root and rejects every ambiguous or unsafe action.
"""

import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from scripts.im_align import config
from scripts.im_align.acp.client import AcpClient, AcpError, PermissionRequest
from scripts.im_align.completion import parse_completion
from scripts.im_align.orchestrator import completion_hint
from scripts.im_align.permission_policy import (
    PermissionPolicy,
    _extract_target_path_candidates,
)


@contextmanager
def isolated_config_home():
    old_xdg = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as root:
        os.environ["XDG_CONFIG_HOME"] = root
        try:
            yield Path(root)
        finally:
            if old_xdg is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_xdg


def write_yaml(path, data, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    if mode is not None:
        os.chmod(path, mode)


def write_user_config(data):
    write_yaml(Path(config.user_config_path()), data, mode=0o600)


def user_config(defaults=None):
    return {
        "providers": {
            "feishu": {
                "type": "feishu",
                "app_id": "cli_test",
                "app_secret": "secret",
                "default_chat_id": "oc_user",
            }
        },
        "defaults": defaults or {},
    }


class NormalizeSpecRootTests(unittest.TestCase):
    def test_empty_blank_and_root_are_rejected(self):
        for bad in ("", "   ", ".", "./"):
            with self.assertRaises(config.ConfigError, msg=bad):
                config.normalize_spec_root(bad)

    def test_dot_prefix_and_empty_segments_normalize(self):
        self.assertEqual(config.normalize_spec_root("./docs/specs"), "docs/specs")
        self.assertEqual(config.normalize_spec_root("docs//specs"), "docs/specs")
        self.assertEqual(config.normalize_spec_root("docs/./specs"), "docs/specs")

    def test_absolute_path_rejected(self):
        with self.assertRaisesRegex(config.ConfigError, "repository-relative"):
            config.normalize_spec_root("/etc")

    def test_parent_escape_rejected(self):
        for bad in ("../x.md", "docs/../x.md", "a/b/../../x.md"):
            with self.assertRaisesRegex(config.ConfigError, "repository-relative", msg=bad):
                config.normalize_spec_root(bad)

    def test_non_string_rejected(self):
        with self.assertRaisesRegex(config.ConfigError, "must be a string"):
            config.normalize_spec_root(123)


class SpecRootConfigTests(unittest.TestCase):
    def test_repo_level_spec_root_loads_and_normalizes(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            write_user_config(user_config())
            write_yaml(
                Path(cwd) / ".im-align.yaml",
                {"agent": {"spec_root": "./docs/specs"}},
            )

            loaded = config.load(cwd)

            self.assertEqual(loaded["agent"]["spec_root"], "docs/specs")

    def test_user_defaults_spec_root_loads(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            write_user_config(user_config({"agent": {"spec_root": "design/specs"}}))

            loaded = config.load(cwd)

            self.assertEqual(loaded["agent"]["spec_root"], "design/specs")

    def test_repo_level_overrides_user_default(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            write_user_config(user_config({"agent": {"spec_root": "user/specs"}}))
            write_yaml(Path(cwd) / ".im-align.yaml", {"agent": {"spec_root": "repo/specs"}})

            loaded = config.load(cwd)

            self.assertEqual(loaded["agent"]["spec_root"], "repo/specs")

    def test_unset_spec_root_uses_skill_default(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            write_user_config(user_config())

            loaded = config.load(cwd)

            self.assertEqual(loaded["agent"]["spec_root"], "docs/adr")

    def test_unset_spec_root_keeps_built_in_default_for_unmapped_skill(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            write_user_config(user_config({"agent": {"skill": "custom-skill"}}))

            loaded = config.load(cwd)

            self.assertEqual(loaded["agent"]["spec_root"], "docs/specs")

    def test_invalid_spec_root_rejected_at_load_time(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            write_user_config(user_config())
            write_yaml(Path(cwd) / ".im-align.yaml", {"agent": {"spec_root": "../src"}})

            with self.assertRaisesRegex(config.ConfigError, "repository-relative"):
                config.load(cwd)

    def test_cli_grouped_override_is_supported(self):
        with isolated_config_home(), tempfile.TemporaryDirectory() as cwd:
            write_user_config(user_config())

            loaded = config.load(cwd, {"agent": {"spec_root": "cli/specs"}})

            self.assertEqual(loaded["agent"]["spec_root"], "cli/specs")


def make_request(raw_input, tool_call_id="tc-1", title="write file", kind="edit"):
    return PermissionRequest(
        session_id="s-1",
        tool_call_id=tool_call_id,
        title=title,
        kind=kind,
        raw_input=raw_input,
        options=[],
    )


class PermissionPolicyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make_policy(self, spec_root="docs/specs"):
        return PermissionPolicy(self.cwd, spec_root)

    def test_write_inside_spec_root_is_auto_allowed_without_cards(self):
        policy = self.make_policy()
        req = make_request({"filePath": "docs/specs/todo.md"})

        decision = policy.decide(req)

        self.assertEqual(decision.kind, "allow_once")

    def test_key_name_variants_hit(self):
        variants = [
            {"path": "docs/specs/todo.md"},
            {"file_path": "docs/specs/todo.md"},
            {"filePath": "docs/specs/todo.md"},
            {"target": "docs/specs/todo.md"},
            {"uri": "docs/specs/todo.md"},
            {"filePathUri": "docs/specs/todo.md"},
            {"outer": {"file_path": "docs/specs/todo.md"}},
        ]
        for raw_input in variants:
            with self.subTest(raw_input=raw_input):
                policy = self.make_policy()
                decision = policy.decide(make_request(raw_input))
                self.assertEqual(decision.kind, "allow_once")

    def test_bare_string_raw_input_hits(self):
        policy = self.make_policy()

        decision = policy.decide(make_request("docs/specs/todo.md"))

        self.assertEqual(decision.kind, "allow_once")

    def test_dot_prefixed_candidate_still_hits(self):
        policy = self.make_policy()

        decision = policy.decide(make_request({"path": "./docs/specs/todo.md"}))

        self.assertEqual(decision.kind, "allow_once")

    def test_absolute_candidate_inside_repo_hits(self):
        policy = self.make_policy()

        decision = policy.decide(
            make_request({"path": str(self.cwd / "docs" / "specs" / "todo.md")})
        )

        self.assertEqual(decision.kind, "allow_once")

    def test_nested_files_inside_spec_root_are_auto_allowed(self):
        policy = self.make_policy()

        decision = policy.decide(
            make_request({"path": "docs/specs/api/endpoints.yaml"})
        )

        self.assertEqual(decision.kind, "allow_once")

    def test_known_write_outside_spec_root_is_cancelled_without_card(self):
        policy = self.make_policy()

        decision = policy.decide(make_request({"filePath": "src/main.py"}))

        self.assertEqual(decision.kind, "cancel")

    def test_out_of_repo_candidate_is_cancelled(self):
        policy = self.make_policy()

        decision = policy.decide(
            make_request({"path": "../../etc/passwd", "note": "unrelated"})
        )

        self.assertEqual(decision.kind, "cancel")

    def test_candidate_without_path_keys_falls_through(self):
        policy = self.make_policy()

        decision = policy.decide(make_request({"command": "rm -rf /"}))

        self.assertEqual(decision.kind, "cancel")

    def test_execute_is_rejected(self):
        policy = self.make_policy()

        decision = policy.decide(
            make_request({"path": "docs/specs/todo.md"}, kind="execute")
        )

        self.assertEqual(decision.kind, "cancel")

    def test_read_only_kinds_are_allowed_once(self):
        policy = self.make_policy()
        for kind in ("read", "search", "fetch", "think"):
            with self.subTest(kind=kind):
                self.assertEqual(
                    policy.decide(make_request({}, kind=kind)).kind,
                    "allow_once",
                )

    def test_unknown_kind_is_rejected(self):
        policy = self.make_policy()
        self.assertEqual(policy.decide(make_request({}, kind="other")).kind, "cancel")

    def test_mixed_candidates_are_cancelled(self):
        policy = self.make_policy()

        decision = policy.decide(
            make_request({"path": "docs/specs/todo.md", "target": "src/main.py"})
        )

        self.assertEqual(decision.kind, "cancel")

    def test_symlink_alias_of_spec_dir_matches_real_path(self):
        real_dir = self.cwd / "real"
        real_dir.mkdir()
        (self.cwd / "alias").symlink_to(real_dir, target_is_directory=True)
        policy = self.make_policy(spec_root="alias")

        decision = policy.decide(make_request({"path": "real/spec.md"}))

        self.assertEqual(decision.kind, "allow_once")

    def test_symlink_escape_is_cancelled(self):
        with tempfile.TemporaryDirectory() as outside_tmp:
            outside_dir = Path(outside_tmp)
            (self.cwd / "linkout").symlink_to(outside_dir, target_is_directory=True)
            policy = self.make_policy(spec_root="docs/specs")

            decision = policy.decide(make_request({"path": "linkout/spec.md"}))

            self.assertEqual(decision.kind, "cancel")

    def test_existing_directory_target_is_rejected(self):
        target = self.cwd / "docs/specs/subdir"
        target.mkdir(parents=True)
        policy = self.make_policy()

        decision = policy.decide(make_request({"path": "docs/specs/subdir"}))

        self.assertEqual(decision.kind, "cancel")


class AcpPermissionMappingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def request(path="docs/specs/spec.md", options=None):
        return {
            "sessionId": "s-1",
            "toolCall": {
                "toolCallId": "tc-1",
                "title": "write spec",
                "kind": "edit",
                "rawInput": {"path": path},
            },
            "options": options
            or [
                {"optionId": "backend-once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "backend-always", "name": "Allow always", "kind": "allow_always"},
            ],
        }

    def test_policy_selects_backend_option_by_kind(self):
        policy = PermissionPolicy(self.cwd, "docs/specs")
        client = AcpClient(
            [], str(self.cwd), "docs/specs", decide_permission=policy.decide
        )

        result = client._handle_request_permission(self.request())

        self.assertEqual(
            result,
            {"outcome": {"outcome": "selected", "optionId": "backend-once"}},
        )

    def test_missing_policy_fails_closed(self):
        client = AcpClient([], str(self.cwd), "docs/specs")

        result = client._handle_request_permission(self.request())

        self.assertEqual(result, {"outcome": {"outcome": "cancelled"}})

    def test_missing_allow_once_option_does_not_widen_grant(self):
        policy = PermissionPolicy(self.cwd, "docs/specs")
        client = AcpClient(
            [], str(self.cwd), "docs/specs", decide_permission=policy.decide
        )

        result = client._handle_request_permission(
            self.request(
                options=[
                    {
                        "optionId": "backend-always",
                        "name": "Allow always",
                        "kind": "allow_always",
                    }
                ]
            )
        )

        self.assertEqual(result, {"outcome": {"outcome": "cancelled"}})


class CompletionHintTests(unittest.TestCase):
    def test_uses_resolved_spec_root(self):
        hint = completion_hint("design/agent-output")

        self.assertIn("Write all Spec files under `design/agent-output/`", hint)
        self.assertIn("Do not write outside `design/agent-output/`", hint)
        self.assertNotIn("`docs/specs/`", hint)


class CompletionSpecRootTests(unittest.TestCase):
    def test_primary_spec_must_be_inside_configured_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            inside = repo / "docs/specs/primary.md"
            inside.parent.mkdir(parents=True)
            inside.write_text("# Spec\n", encoding="utf-8")
            started = datetime.now() - timedelta(seconds=1)

            self.assertEqual(
                parse_completion(
                    "[ALIGNMENT_COMPLETE] docs/specs/primary.md",
                    repo,
                    started,
                    "docs/specs",
                ),
                ("docs/specs/primary.md", True, True),
            )

    def test_primary_spec_outside_configured_root_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            outside = repo / "src/design.md"
            outside.parent.mkdir(parents=True)
            outside.write_text("# Not allowed\n", encoding="utf-8")

            self.assertEqual(
                parse_completion(
                    "[ALIGNMENT_COMPLETE] src/design.md",
                    repo,
                    datetime.now() - timedelta(seconds=1),
                    "docs/specs",
                ),
                ("src/design.md", True, False),
            )


class AcpFsWriteRootTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)
        self.client = AcpClient([], str(self.cwd), "docs/specs")

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_inside_root_succeeds(self):
        self.client._handle_write_text(
            {"path": "docs/specs/api.md", "content": "# API\n"}
        )

        self.assertEqual(
            (self.cwd / "docs/specs/api.md").read_text(encoding="utf-8"),
            "# API\n",
        )

    def test_write_outside_root_is_refused(self):
        with self.assertRaisesRegex(AcpError, "outside Spec Root"):
            self.client._handle_write_text(
                {"path": "src/main.py", "content": "changed\n"}
            )
        self.assertFalse((self.cwd / "src/main.py").exists())


class ExtractTargetPathCandidatesTests(unittest.TestCase):
    def test_bare_string(self):
        self.assertEqual(_extract_target_path_candidates("a/b.md"), ["a/b.md"])

    def test_common_keys_case_insensitive(self):
        raw = {"Path": "a.md", "FILEPATH": "b.md", "other": "ignored", "empty": ""}
        self.assertEqual(
            _extract_target_path_candidates(raw), ["a.md", "b.md"]
        )

    def test_one_level_nested(self):
        raw = {"tool": {"file_path": "nested.md", "deeper": {"path": "too-deep.md"}}}
        self.assertEqual(_extract_target_path_candidates(raw), ["nested.md"])

    def test_non_string_and_none_yield_nothing(self):
        self.assertEqual(_extract_target_path_candidates(None), [])
        self.assertEqual(_extract_target_path_candidates(42), [])
        self.assertEqual(_extract_target_path_candidates(["a.md"]), [])


if __name__ == "__main__":
    unittest.main()
