"""Skill Bundle installation is deterministic and Backend-specific."""

import tempfile
import unittest
from pathlib import Path

from tests.e2e.skill_bundle import AGENT_PATHS, install_skill_bundle, load_bundle_for_skill


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests" / "e2e" / "skill-bundles.yaml"


class SkillBundleTests(unittest.TestCase):
    def test_entry_skill_resolves_complete_grill_bundle(self):
        bundle = load_bundle_for_skill("grill-with-docs", MANIFEST)
        self.assertEqual(bundle.name, "grill-me")
        # grill-with-docs only forwards to grilling + domain-modeling; without
        # them in the closure the Agent cannot load its own entry Skill.
        self.assertEqual(
            bundle.skills,
            ("grill-me", "grill-with-docs", "grilling", "domain-modeling", "to-spec"),
        )

    def test_install_stages_all_skills_for_all_backends(self):
        skills = load_bundle_for_skill("grill-with-docs", MANIFEST).skills
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            workspace = Path(kwargs["cwd"])
            for agent_path in AGENT_PATHS.values():
                for skill in skills:
                    path = workspace / agent_path / skill
                    path.mkdir(parents=True, exist_ok=True)
                    (path / "SKILL.md").write_text("---\nname: x\ndescription: x\n---\n")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".git" / "info").mkdir(parents=True)
            install_skill_bundle(workspace, "grill-with-docs", manifest=MANIFEST, run=fake_run)

            self.assertEqual(len(calls), 1)
            command = calls[0][0]
            self.assertIn("--skill", command)
            for skill in skills:
                self.assertIn(skill, command)
            self.assertIn("--agent", command)
            self.assertTrue((workspace / ".git" / "info" / "exclude").exists())


if __name__ == "__main__":
    unittest.main()
