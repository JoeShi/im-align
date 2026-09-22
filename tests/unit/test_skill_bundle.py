"""Skill Bundle installation is deterministic and Backend-specific."""

import tempfile
import unittest
from pathlib import Path

from tests.e2e.shared.scenario import SkillMapping
from tests.e2e.shared.skill_bundle import (
    AGENT_PATHS,
    SkillBundleError,
    install_skill_bundle,
)


SKILL = SkillMapping(
    name="grill-with-docs",
    source="mattpocock/skills",
    ref="v1.2.3",
    cli="1.5.9",
    bundle=("grill-with-docs", "grilling", "domain-modeling", "to-spec"),
)


class InstallSkillBundleTests(unittest.TestCase):
    def _fake_run(self, calls, skills):
        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            workspace = Path(kwargs["cwd"])
            for agent_path in AGENT_PATHS.values():
                for skill in skills:
                    path = workspace / agent_path / skill
                    path.mkdir(parents=True, exist_ok=True)
                    (path / "SKILL.md").write_text("---\nname: x\ndescription: x\n---\n")

        return fake_run

    def test_install_stages_all_skills_for_all_backends(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".git" / "info").mkdir(parents=True)
            install_skill_bundle(workspace, SKILL, run=self._fake_run(calls, SKILL.bundle))

            self.assertEqual(len(calls), 1)
            command, kwargs = calls[0]
            self.assertEqual(
                command,
                [
                    "npx", "--yes", "skills@1.5.9", "add", "mattpocock/skills",
                    *(arg for name in SKILL.bundle for arg in ("--skill", name)),
                    *(arg for agent in AGENT_PATHS for arg in ("--agent", agent)),
                    "--copy", "--yes",
                ],
            )
            self.assertEqual(kwargs["cwd"], workspace)
            self.assertTrue((workspace / ".git" / "info" / "exclude").exists())

    def test_missing_skill_md_fails_incomplete(self):
        def fake_run(command, **kwargs):
            workspace = Path(kwargs["cwd"])
            for agent_path in AGENT_PATHS.values():
                for skill in SKILL.bundle:
                    path = workspace / agent_path / skill
                    path.mkdir(parents=True, exist_ok=True)
                    (path / "SKILL.md").write_text("---\nname: x\ndescription: x\n---\n")
            # Simulate a partial install: one backend misses one SKILL.md.
            (workspace / AGENT_PATHS["opencode"] / "to-spec" / "SKILL.md").unlink()

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".git" / "info").mkdir(parents=True)
            with self.assertRaisesRegex(SkillBundleError, "incompletely"):
                install_skill_bundle(workspace, SKILL, run=fake_run)

    def test_entry_skill_must_be_bundle_member(self):
        outsider = SkillMapping(
            name="other-skill",
            source="mattpocock/skills",
            ref="v1.2.3",
            cli="1.5.9",
            bundle=("grill-with-docs",),
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SkillBundleError, "member"):
                install_skill_bundle(Path(tmp), outsider, run=lambda *a, **k: None)

    def test_install_failure_wrapped(self):
        def failing_run(command, **kwargs):
            raise RuntimeError("npx crashed")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SkillBundleError, "cannot install"):
                install_skill_bundle(Path(tmp), SKILL, run=failing_run)


if __name__ == "__main__":
    unittest.main()
