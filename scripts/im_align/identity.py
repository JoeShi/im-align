"""Session Initiator identity resolution chain, ADR-0004.

Order:
1. cached initiator from repository-level .im-align.yaml, including open_id;
2. lark-cli using the same Feishu/Lark app as the Bridge, via ``whoami --as user``;
3. ``git config user.email`` from the target repository;
4. interactive terminal email prompt.

After an email is obtained, the IM Provider contact API resolves it to open_id;
resolution failure rejects startup. Feishu/Lark open_id values are app-scoped,
so lark-cli open_id values issued by other apps cannot be compared directly
with card callback operator.open_id. Known risk, see ADR-0004: local users can
forge local email. The hard check happens on Approval card callbacks, where
Feishu/Lark verifies the clicker's open_id; the root message displays the
initiator for group visibility.
"""

import json
import os
import shutil
import subprocess
import sys

import yaml

from .config import repo_config_path


class IdentityError(Exception):
    pass


def read_repo_initiator(cwd):
    path = repo_config_path(cwd)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return None
    value = data.get("initiator")
    if isinstance(value, dict) and value.get("open_id"):
        return dict(value)
    if isinstance(value, str) and value:
        return {"email": value, "open_id": ""}
    return None


def write_repo_initiator(cwd, email, open_id, name=""):
    path = repo_config_path(cwd)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}
    initiator = {"email": email, "open_id": open_id}
    if name:
        initiator["name"] = name
    data["initiator"] = initiator
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def lark_cli_identity(expected_app_id=None):
    """Return identity from same-app lark-cli when available; otherwise fall back silently.

    open_id is app-scoped. Even for the same person, open_id from another app
    cannot be compared with operator.open_id from Bridge card callbacks.
    """
    if not shutil.which("lark-cli"):
        return None
    try:
        out = subprocess.run(
            ["lark-cli", "whoami", "--as", "user"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout
        data = json.loads(out)
    except Exception:
        return None
    if not data.get("available"):
        return None
    if expected_app_id and data.get("appId") != expected_app_id:
        return None
    behalf = data.get("onBehalfOf") or {}
    open_id = behalf.get("openId", "")
    if not open_id:
        return None
    return {"email": "", "open_id": open_id, "name": behalf.get("userName", "")}


def git_email(cwd):
    try:
        out = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
            check=True,
        ).stdout.strip()
        return out or None
    except Exception:
        return None


def prompt_email():
    if not sys.stdin.isatty():
        raise IdentityError(
            "cannot determine Session Initiator: no reusable same-app lark-cli identity, "
            "git user.email is empty, and stdin is not interactive. Configure initiator "
            "in .im-align.yaml or pass an email with --initiator"
        )
    email = input("Enter the Session Initiator's Feishu/Lark email: ").strip()
    if not email:
        raise IdentityError("email was not provided; cannot start")
    return email


def resolve_claim(cwd, cli_initiator=None, expected_lark_app_id=None):
    """Return claimed identity {'email','open_id','name'}; not yet verified."""
    if cli_initiator:
        if cli_initiator.startswith("ou_"):
            return {"email": "", "open_id": cli_initiator, "name": ""}
        return {"email": cli_initiator, "open_id": "", "name": ""}

    cached = read_repo_initiator(cwd)
    if cached and cached.get("open_id"):
        cached.setdefault("email", "")
        cached.setdefault("name", "")
        return cached

    via_cli = lark_cli_identity(expected_lark_app_id)
    if via_cli:
        return via_cli

    email = git_email(cwd) or prompt_email()
    return {"email": email, "open_id": "", "name": ""}


def ensure_git_repo(cwd):
    """Accept normal repositories and git worktrees; reject bare or outside paths."""
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
        raise IdentityError(
            f"{cwd} is not a git worktree; im-align works only inside the launch-time git repository"
        ) from e
    if out != "true":
        raise IdentityError(
            f"{cwd} is not a git worktree; im-align works only inside the launch-time git repository"
        )
