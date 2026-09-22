"""Scenario schema and loader tests; pure stdlib, no network."""

import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

from tests.e2e.shared.scenario import ScenarioError, load_scenario, load_transcript

FULL_SHA = "a" * 40

VALID_SCENARIO = f"""
name: demo
backend: kiro-cli
skill:
  name: grill-with-docs
  source: mattpocock/skills
  ref: v1.2.3
  cli: "1.5.9"
  bundle:
    - grill-with-docs
    - grilling
    - domain-modeling
    - to-spec
requirement: |
  add a TODO list
timeout_seconds: 900
expected_artifacts:
  - path: docs/specs/todo.md
    marker_line: "[ALIGNMENT_COMPLETE]"
llm_dimensions:
  - asked_questions
user_brief: |
  only TODO list scope
"""


class LoadScenarioTests(unittest.TestCase):
    def _write(self, tmp, content, name="scenario.yaml"):
        path = Path(tmp) / name
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        return Path(tmp)

    def test_valid_scenario_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = load_scenario(self._write(tmp, VALID_SCENARIO))
            self.assertEqual(scenario.name, "demo")
            self.assertEqual(scenario.backend, "kiro-cli")
            self.assertEqual(scenario.timeout_seconds, 900)
            self.assertEqual(scenario.expected_artifacts[0].path, "docs/specs/todo.md")
            self.assertEqual(
                scenario.expected_artifacts[0].marker_line, "[ALIGNMENT_COMPLETE]"
            )
            self.assertEqual(scenario.llm_dimensions, ["asked_questions"])
            self.assertIsNone(scenario.seed_repo)
            self.assertFalse(scenario.seed_dir.is_dir())
            self.assertEqual(scenario.skill.name, "grill-with-docs")
            self.assertEqual(scenario.skill.source, "mattpocock/skills")
            self.assertEqual(scenario.skill.ref, "v1.2.3")
            self.assertEqual(scenario.skill.cli, "1.5.9")
            self.assertEqual(
                scenario.skill.bundle,
                ("grill-with-docs", "grilling", "domain-modeling", "to-spec"),
            )

    def test_missing_required_field_rejected(self):
        import yaml

        base = yaml.safe_load(textwrap.dedent(VALID_SCENARIO))
        for field in ("name", "backend", "skill", "requirement", "timeout_seconds",
                      "expected_artifacts", "llm_dimensions", "user_brief"):
            with tempfile.TemporaryDirectory() as tmp:
                data = dict(base)
                del data[field]
                path = Path(tmp) / "scenario.yaml"
                path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
                with self.assertRaises(ScenarioError, msg=field):
                    load_scenario(tmp)

    def test_branch_name_ref_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = VALID_SCENARIO + "seed_repo:\n  url: https://github.com/org/repo\n  ref: main\n"
            self._write(tmp, content)
            with self.assertRaisesRegex(ScenarioError, "full commit SHA"):
                load_scenario(tmp)

    def test_short_sha_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = VALID_SCENARIO + f"seed_repo:\n  url: https://github.com/org/repo\n  ref: {'a' * 7}\n"
            self._write(tmp, content)
            with self.assertRaisesRegex(ScenarioError, "full commit SHA"):
                load_scenario(tmp)

    def test_full_sha_40_and_64_accepted(self):
        for ref in ("a" * 40, "b" * 64):
            with tempfile.TemporaryDirectory() as tmp:
                content = VALID_SCENARIO + f"seed_repo:\n  url: https://github.com/org/repo\n  ref: {ref}\n"
                self._write(tmp, content)
                scenario = load_scenario(tmp)
                self.assertEqual(scenario.seed_repo.ref, ref)

    def test_seed_and_seed_repo_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "seed").mkdir()
            (Path(tmp) / "seed" / "file.txt").write_text("x", encoding="utf-8")
            content = VALID_SCENARIO + (
                "seed_repo:\n  url: https://github.com/org/repo\n  ref: " + FULL_SHA + "\n"
            )
            self._write(tmp, content)
            with self.assertRaisesRegex(ScenarioError, "mutually exclusive"):
                load_scenario(tmp)

    def test_seed_dir_without_seed_repo_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "seed").mkdir()
            self._write(tmp, VALID_SCENARIO)
            scenario = load_scenario(tmp)
            self.assertTrue(scenario.seed_dir.is_dir())
            self.assertIsNone(scenario.seed_repo)

    def test_unsupported_backend_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, VALID_SCENARIO.replace("backend: kiro-cli", "backend: emacs"))
            with self.assertRaisesRegex(ScenarioError, "backend"):
                load_scenario(tmp)

    def test_artifact_path_must_be_repo_relative(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = VALID_SCENARIO.replace(
                "- path: docs/specs/todo.md", "- path: ../escape.md"
            )
            self._write(tmp, content)
            with self.assertRaisesRegex(ScenarioError, "repository-relative"):
                load_scenario(tmp)

    def test_duplicate_dimensions_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = VALID_SCENARIO.replace("  - asked_questions", "  - asked_questions\n  - asked_questions")
            self._write(tmp, content)
            with self.assertRaisesRegex(ScenarioError, "duplicate"):
                load_scenario(tmp)


class SpecRootFieldTests(unittest.TestCase):
    def _write(self, tmp, extra=""):
        path = Path(tmp) / "scenario.yaml"
        path.write_text(textwrap.dedent(VALID_SCENARIO + extra), encoding="utf-8")
        return Path(tmp)

    def test_absent_spec_root_uses_built_in_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = load_scenario(self._write(tmp))
            self.assertEqual(scenario.spec_root, "docs/specs")

    def test_valid_spec_root_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = load_scenario(self._write(tmp, "spec_root: docs/design\n"))
            self.assertEqual(scenario.spec_root, "docs/design")

    def test_dot_prefixed_spec_root_normalizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = load_scenario(self._write(tmp, "spec_root: ./docs//specs\n"))
            self.assertEqual(scenario.spec_root, "docs/specs")

    def test_absolute_spec_root_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "spec_root: /etc\n")
            with self.assertRaisesRegex(ScenarioError, "spec_root"):
                load_scenario(tmp)

    def test_parent_escape_spec_root_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "spec_root: docs/../src\n")
            with self.assertRaisesRegex(ScenarioError, "spec_root"):
                load_scenario(tmp)

    def test_non_string_spec_root_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "spec_root: 123\n")
            with self.assertRaisesRegex(ScenarioError, "spec_root"):
                load_scenario(tmp)

    def test_todo_greenfield_declares_spec_root(self):
        scenario_dir = Path(__file__).resolve().parents[1] / "e2e" / "scenarios" / "todo-greenfield"
        scenario = load_scenario(scenario_dir)
        self.assertEqual(scenario.spec_root, "docs/adr")
        self.assertTrue(scenario.expected_artifacts[0].path.startswith(scenario.spec_root + "/"))


class SkillMappingTests(unittest.TestCase):
    def _write(self, tmp, skill):
        data = yaml.safe_load(textwrap.dedent(VALID_SCENARIO))
        data["skill"] = skill
        (Path(tmp) / "scenario.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
        )
        return Path(tmp)

    def test_legacy_bare_string_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "grill-with-docs")
            with self.assertRaisesRegex(ScenarioError, "ADR-0011"):
                load_scenario(tmp)

    def test_missing_key_rejected(self):
        skill = {
            "name": "grill-with-docs",
            "source": "mattpocock/skills",
            "ref": "v1.2.3",
            "cli": "1.5.9",
            "bundle": ["grill-with-docs", "grilling"],
        }
        for key in ("name", "source", "ref", "cli", "bundle"):
            with tempfile.TemporaryDirectory() as tmp:
                self._write(tmp, {k: v for k, v in skill.items() if k != key})
                with self.assertRaisesRegex(ScenarioError, key, msg=key):
                    load_scenario(tmp)

    def test_empty_bundle_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(
                tmp,
                {
                    "name": "grill-with-docs",
                    "source": "mattpocock/skills",
                    "ref": "v1.2.3",
                    "cli": "1.5.9",
                    "bundle": [],
                },
            )
            with self.assertRaisesRegex(ScenarioError, "bundle"):
                load_scenario(tmp)

    def test_name_must_be_bundle_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(
                tmp,
                {
                    "name": "other-skill",
                    "source": "mattpocock/skills",
                    "ref": "v1.2.3",
                    "cli": "1.5.9",
                    "bundle": ["grill-with-docs"],
                },
            )
            with self.assertRaisesRegex(ScenarioError, "member"):
                load_scenario(tmp)

    def test_non_string_bundle_entry_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(
                tmp,
                {
                    "name": "grill-with-docs",
                    "source": "mattpocock/skills",
                    "ref": "v1.2.3",
                    "cli": "1.5.9",
                    "bundle": ["grill-with-docs", 42],
                },
            )
            with self.assertRaisesRegex(ScenarioError, "bundle\\[1\\]"):
                load_scenario(tmp)


class LoadTranscriptTests(unittest.TestCase):
    def _write(self, tmp, content):
        path = Path(tmp) / "transcript.yaml"
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        return path

    def test_valid_transcript_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                """
                participant_replies:
                  - "fixed user reply"
                steps:
                  - type: send_text
                    text: "hello"
                  - type: end_turn
                  - type: request_permission
                    title: write spec
                    kind: edit
                    options:
                      - optionId: once
                        name: Allow once
                        kind: allow_once
                    decision: allow_once
                  - type: write_artifact
                    path: docs/spec.md
                    content: "spec body"
                  - type: emit_marker
                    path: docs/spec.md
                  - type: hang_until_cancel
                  - type: exit
                    exit_code: 1
                """,
            )
            transcript = load_transcript(path)
            self.assertEqual(
                [s.type for s in transcript.steps],
                ["send_text", "end_turn", "request_permission", "write_artifact",
                 "emit_marker", "hang_until_cancel", "exit"],
            )
            self.assertEqual(transcript.participant_replies, ["fixed user reply"])
            self.assertEqual(transcript.steps[2].decision, "allow_once")
            self.assertEqual(transcript.steps[6].exit_code, 1)

    def test_unknown_step_type_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "steps:\n  - type: teleport\n")
            with self.assertRaisesRegex(ScenarioError, "type"):
                load_transcript(path)

    def test_missing_steps_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "other: 1\n")
            with self.assertRaisesRegex(ScenarioError, "steps"):
                load_transcript(path)

    def test_permission_decision_must_be_valid_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                """
                steps:
                  - type: request_permission
                    title: write spec
                    kind: edit
                    options:
                      - optionId: once
                        name: Allow once
                        kind: allow_once
                    decision: maybe
                """,
            )
            with self.assertRaisesRegex(ScenarioError, "decision"):
                load_transcript(path)


if __name__ == "__main__":
    unittest.main()
