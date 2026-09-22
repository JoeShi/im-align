"""Install and stage the complete Skill Bundle declared by a Scenario (ADR-0011)."""

import os
import subprocess
from pathlib import Path

from .scenario import SkillMapping


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
    skill: SkillMapping,
    *,
    run=subprocess.run,
) -> None:
    """Install all bundle skills into every supported Agent Backend path."""
    if skill.name not in skill.bundle:
        raise SkillBundleError(
            f"entry Skill {skill.name!r} is not a member of the declared bundle"
        )
    command = ["npx", "--yes", f"skills@{skill.cli}", "add", skill.source]
    for name in skill.bundle:
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
            f"cannot install Skill Bundle {skill.name}: {e}"
        ) from e

    _exclude_staged_skills(workspace)
    missing = []
    for agent_path in AGENT_PATHS.values():
        for name in skill.bundle:
            if not (workspace / agent_path / name / "SKILL.md").is_file():
                missing.append(f"{agent_path / name / 'SKILL.md'}")
    if missing:
        raise SkillBundleError(
            f"Skill Bundle {skill.name} installed incompletely; missing: "
            + ", ".join(missing)
        )
