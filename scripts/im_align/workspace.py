"""Launch-workspace validation for the Bridge."""

import subprocess


class WorkspaceError(Exception):
    pass


def ensure_git_repo(cwd):
    """Accept normal repositories and git worktrees; reject other directories."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
            check=True,
        ).stdout.strip()
    except Exception as e:
        raise WorkspaceError(
            f"{cwd} is not a git worktree; im-align works only inside the launch-time git repository"
        ) from e
    if out != "true":
        raise WorkspaceError(
            f"{cwd} is not a git worktree; im-align works only inside the launch-time git repository"
        )
