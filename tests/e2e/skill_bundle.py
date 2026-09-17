"""Install and stage the complete Skill Bundle required by a Scenario."""

from dataclasses import dataclass
import os
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests" / "e2e" / "skill-bundles.yaml"

AGENT_PATHS = {
    # The skills CLI currently stages OpenCode project skills in the shared
    # .agents/skills compatibility directory (verified by its install output).
    "opencode": Path(".agents/skills"),
    "kiro-cli": Path(".kiro/skills"),
    "trae": Path(".trae/skills"),
    "kimi-cli": Path(".agents/skills"),
}


class SkillBundleError(RuntimeError):
    """The test workspace cannot provide the Scenario's Skill Bundle."""


@dataclass(frozen=True)
class SkillBundle:
    name: str
    source: str
    cli_version: str
    skills: tuple[str, ...]


def load_bundle_for_skill(skill: str, manifest: Path = MANIFEST) -> SkillBundle:
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    for raw in data.get("bundles", []):
        skills = tuple(raw.get("skills", ()))
        if skill in skills:
            return SkillBundle(
                name=str(raw["name"]),
                source=str(raw["source"]),
                cli_version=str(raw.get("cli_version", "1.5.9")),
                skills=skills,
            )
    raise SkillBundleError(f"no Skill Bundle provides Scenario Skill: {skill}")


def _exclude_staged_skills(workspace: Path) -> None:
    exclude = workspace / ".git" / "info" / "exclude"
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    for path in AGENT_PATHS.values():
        pattern = f"/{path.as_posix()}/"
        if pattern not in lines:
            lines.append(pattern)
    exclude.write_text("\n".join(lines) + "\n", encoding="utf-8")


def install_skill_bundle(
    workspace: Path,
    skill: str,
    *,
    manifest: Path = MANIFEST,
    run=subprocess.run,
) -> SkillBundle:
    """Install all bundle skills into every supported Agent Backend path."""
    bundle = load_bundle_for_skill(skill, manifest)
    command = ["npx", "--yes", f"skills@{bundle.cli_version}", "add", bundle.source]
    for name in bundle.skills:
        command.extend(["--skill", name])
    for agent in AGENT_PATHS:
        command.extend(["--agent", agent])
    command.extend(["--copy", "--yes"])
    try:
        run(
            command,
            cwd=workspace,
            check=True,
            timeout=300,
            env={**os.environ, "DISABLE_TELEMETRY": "1"},
        )
    except Exception as e:
        raise SkillBundleError(
            f"cannot install Skill Bundle {bundle.name}: {e}"
        ) from e

    _exclude_staged_skills(workspace)
    missing = []
    for agent_path in AGENT_PATHS.values():
        for name in bundle.skills:
            if not (workspace / agent_path / name / "SKILL.md").is_file():
                missing.append(f"{agent_path / name / 'SKILL.md'}")
    if missing:
        raise SkillBundleError(
            f"Skill Bundle {bundle.name} installed incompletely; missing: "
            + ", ".join(missing)
        )
    return bundle
