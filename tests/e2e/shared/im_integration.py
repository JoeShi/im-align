"""IM Integration layer runner: scripted fake ACP backend against the real Bridge.

Drives one Scenario end to end: materialize a temporary seed workspace, point
the Bridge's backend command at tests.e2e.shared.fake_acp_agent through a
command alias in an isolated XDG_CONFIG_HOME, run start/wait/status, then apply
deterministic assertions (docs/e2e-harness-design.md):

  - expected_artifacts exist with content containing marker_line
    (path may be a glob; passes when at least one match satisfies it);
  - status --json reports done;
  - git status/log are unchanged except for declared artifacts (no branch,
    commit, or push);
  - a fixed user-originated Thread reply is visible, acknowledged, and drives
    the next Turn before completion;
  - the Thread received the completion card and zero Approval cards
    (ADR-0005 happy path; Feishu OpenAPI under the Participant identity).

Real Feishu Bridge credentials and the test user's OAuth access token are read
only from the environment; when any is missing the run is skipped. The user
token remains harness-side and is never passed to Bridge config or state.
"""

import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .harness_support import (
    bridge_worker_env,
    participant_round_trip_assertions,
    run_guarded_actor,
    wait_for_root_message,
)
from .scenario import Scenario, load_scenario, load_transcript
from .verifier import (
    COMPLETION_CARD_TITLE,
    INVALID_COMPLETION_TITLE,
    ThreadVerifier,
    find_approval_card,
    message_contains_card,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
BRIDGE_PATH = REPO_ROOT / "scripts" / "bridge.py"
# Fixed scripts for the IM Integration fake backend live with the layer, not
# with the shared Scenario data: one file per Scenario name.
TRANSCRIPTS_DIR = Path(__file__).resolve().parent.parent / "im_integration" / "transcripts"


def transcript_path_for(scenario: Scenario) -> Path:
    return TRANSCRIPTS_DIR / f"{scenario.name}.yaml"

# Environment variables holding CI secrets for the real Feishu Thread. Never
# hardcode values; a missing variable skips the run.
ENV_APP_ID = "E2E_BRIDGE_FEISHU_APP_ID"
ENV_APP_SECRET = "E2E_BRIDGE_FEISHU_APP_SECRET"
ENV_CHAT_ID = "E2E_BRIDGE_CHAT_ID"


class IntegrationSkip(Exception):
    """The run cannot start (missing credentials); reported, not failed."""


@dataclass
class RunResult:
    scenario: str
    run_id: str = ""
    state: str = ""
    skipped: bool = False
    skip_reason: str = ""
    assertions: dict = field(default_factory=dict)
    log_path: str = ""

    @property
    def passed(self):
        return (
            not self.skipped
            and self.state == "done"
            and bool(self.assertions)
            and all(value is True for value in self.assertions.values())
        )


# ---- Seed materialization (network seam, injectable for tests) ----


class SeedCloner:
    """Clone a public seed_repo into dest, checked out at the pinned full SHA."""

    def clone(self, url: str, ref: str, dest: Path):
        subprocess.run(["git", "clone", "--quiet", url, str(dest)], check=True, timeout=300)
        subprocess.run(["git", "checkout", "--quiet", ref], cwd=dest, check=True, timeout=120)


def materialize_seed(scenario: Scenario, workspace: Path, cloner: SeedCloner | None = None):
    """Copy seed/ or clone seed_repo into workspace; init git when absent."""
    if scenario.seed_repo:
        (cloner or SeedCloner()).clone(scenario.seed_repo.url, scenario.seed_repo.ref, workspace)
    elif scenario.seed_dir.is_dir():
        shutil.copytree(scenario.seed_dir, workspace, dirs_exist_ok=True)
    if not (workspace / ".git").exists():
        subprocess.run(["git", "init", "--quiet"], cwd=workspace, check=True, timeout=60)
        # CI runners have no ambient git identity, so an explicit one is
        # required or the seed commit fails with exit 128 (measured
        # 2026-09-17 on the ubuntu-latest unit job).
        subprocess.run(
            [
                "git",
                "-c", "user.name=im-align-e2e",
                "-c", "user.email=im-align-e2e@localhost",
                "commit", "--quiet", "--allow-empty", "-m", "seed",
            ],
            cwd=workspace,
            check=True,
            timeout=60,
        )


# ---- Deterministic assertions (pure functions, no Feishu/ACP) ----


def check_artifacts(workspace: Path, scenario: Scenario) -> dict:
    results = {}
    for artifact in scenario.expected_artifacts:
        key = f"artifact:{artifact.path}"
        # artifact.path may be a glob (e.g. docs/adr/0001-*.md) because the Spec
        # file name is agent-generated and not known ahead of time. Pass when at
        # least one match exists; when marker_line is set, at least one match
        # must contain it.
        matches = [p for p in sorted(workspace.glob(artifact.path)) if p.is_file()]
        if not matches:
            results[key] = False
            continue
        if not artifact.marker_line:
            results[key] = True
            continue
        results[key] = any(
            artifact.marker_line in p.read_text(encoding="utf-8") for p in matches
        )
    return results


def snapshot_git(workspace: Path) -> dict:
    """Capture HEAD and porcelain status; an empty seed repo has no HEAD yet."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    status = subprocess.run(
        # -uall defeats git's untracked-directory collapse so individual
        # artifact paths can be compared against the declared allow-list.
        ["git", "status", "--porcelain", "-uall"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    return {"head": head, "status": status}


def _porcelain_paths(status: str) -> dict:
    paths = {}
    for line in status.splitlines():
        if not line.strip():
            continue
        paths[line[3:]] = line[:2]
    return paths


def git_unchanged_except(before: dict, after: dict, allowed_paths: list) -> bool:
    """True when HEAD is identical and only declared artifacts changed on disk.

    allowed_paths entries may be globs (e.g. docs/adr/0001-*.md); a repo path is
    allowed when it matches any pattern via fnmatch.
    """
    if before["head"] != after["head"]:
        return False

    def _allowed(path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in allowed_paths)

    before_paths = _porcelain_paths(before["status"])
    after_paths = _porcelain_paths(after["status"])
    if any(not _allowed(p) for p in set(after_paths) - set(before_paths)):
        return False
    for path in set(before_paths) & set(after_paths):
        if before_paths[path] != after_paths[path] and not _allowed(path):
            return False
    return True


# ---- Feishu OpenAPI thread verification lives in verifier.py ----


# ---- Scripted Participant actor ----


_QUESTION_MARKS = ("?", "？", "❓")


def run_scripted_participant(
    thread,
    participant_replies: list,
    timeout_seconds: float,
    session_terminal,
    *,
    poll_interval: float = 3.0,
    sleep=time.sleep,
    now=time.monotonic,
) -> list:
    """Post fixed user replies only after the corresponding Agent question."""
    receipts = []
    reply_index = 0
    deadline = now() + timeout_seconds
    while reply_index < len(participant_replies) and now() < deadline:
        message = thread.poll()
        if message is not None and any(
            marker in (message.text or "") for marker in _QUESTION_MARKS
        ):
            if session_terminal():
                break
            receipt = thread.post_reply(participant_replies[reply_index])
            if not receipt.message_id:
                raise RuntimeError("Participant reply did not return a message_id")
            receipts.append(
                {
                    "message_id": receipt.message_id,
                    "create_time": receipt.create_time,
                }
            )
            reply_index += 1
            continue
        if session_terminal():
            break
        sleep(poll_interval)
    if reply_index != len(participant_replies):
        raise RuntimeError(
            f"scripted Participant posted {reply_index}/{len(participant_replies)} replies"
        )
    return receipts


class _InvalidCompletionWatcher:
    """Transport wrapper that flags the Bridge's invalid-completion card.

    The scripted Participant polls Thread messages anyway, so this is the
    earliest harness-side detection point for a rejected completion marker.
    """

    def __init__(self, inner, event: threading.Event):
        self._inner = inner
        self._event = event

    def poll(self):
        message = self._inner.poll()
        if message is not None and getattr(message, "card_title", "") == INVALID_COMPLETION_TITLE:
            self._event.set()
        return message

    def post_reply(self, text):
        return self._inner.post_reply(text)


# ---- Bridge driving ----


def _repo_chat_id(repo_cwd) -> str:
    """Read im.chat_id from a repository .im-align.yaml; empty when absent."""
    try:
        with open(Path(repo_cwd) / ".im-align.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return ""
    value = (data.get("im") or {}).get("chat_id")
    return value if isinstance(value, str) else ""


def feishu_credentials(env=None, repo_cwd=None) -> dict:
    env = env if env is not None else os.environ
    chat_id = env.get(ENV_CHAT_ID, "")
    if not chat_id and repo_cwd:
        # The launching repository often already declares the group chat;
        # the env var stays the override for CI with a dedicated test group.
        chat_id = _repo_chat_id(repo_cwd)
    creds = {
        "app_id": env.get(ENV_APP_ID, ""),
        "app_secret": env.get(ENV_APP_SECRET, ""),
        "chat_id": chat_id,
    }
    missing = [name for name, value in creds.items() if not value]
    if missing:
        raise IntegrationSkip(
            f"missing Feishu/Lark credentials: {', '.join(missing)}; "
            f"set {ENV_APP_ID}, {ENV_APP_SECRET}, and {ENV_CHAT_ID}"
        )
    return creds


def _write_user_config(
    config_home: Path,
    creds: dict,
    scenario: Scenario,
    backend: str | None = None,
    turn_timeout_seconds: int | None = None,
):
    defaults = {
        "im": {
            "provider": "e2e",
        },
        "agent": {"backend": backend or scenario.backend, "spec_root": scenario.spec_root},
    }
    # Measured 2026-09-16: a real opencode Turn (skill loading plus workspace
    # exploration) exceeded the 300 s product default, was cancelled mid-Turn,
    # and the Session stalled on a question-less partial card until timeout.
    # LLM latency varies, so the harness gives one Turn the whole scenario
    # budget; the runner's wall-clock wait bounds the run overall.
    if turn_timeout_seconds is not None:
        defaults["timeouts"] = {"turn_timeout_seconds": int(turn_timeout_seconds)}
    user_config = {
        "providers": {
            "e2e": {
                "type": "feishu",
                "app_id": creds["app_id"],
                "app_secret": creds["app_secret"],
                "default_chat_id": "",
            }
        },
        "defaults": defaults,
        "commands": {
            "e2e-fake": {
                "backend": backend or scenario.backend,
                "command": sys.executable,
                # The Bridge spawns the backend with cwd set to the temporary
                # workspace, so the fake Agent must be importable as a module:
                # PYTHONPATH below points at the repository root. The
                # transcript path stays absolute for the same reason.
                "args": [
                    "-m", "tests.e2e.shared.fake_acp_agent",
                    str(transcript_path_for(scenario).resolve()),
                ],
            }
        },
    }
    path = config_home / "im-align" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(user_config, f, allow_unicode=True, sort_keys=False)
    os.chmod(path, 0o600)


def _bridge(env: dict, workspace: Path, args: list, timeout: int) -> dict:
    proc = subprocess.run(
        [sys.executable, str(BRIDGE_PATH)] + args,
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"bridge {' '.join(args)} failed (exit={proc.returncode}): {proc.stderr.strip()}"
        )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def run_integration(
    scenario_dir,
    cloner: SeedCloner | None = None,
    verifier: ThreadVerifier | None = None,
    participant=None,
    thread_transport=None,
    env=None,
    poll_interval: float = 3.0,
) -> RunResult:
    """Run one Scenario against the real Bridge; never raises for assertion failures."""
    env = env if env is not None else os.environ
    scenario = load_scenario(scenario_dir)
    transcript_file = transcript_path_for(scenario)
    if not transcript_file.is_file():
        raise RuntimeError(
            f"IM Integration transcript not found: {transcript_file} "
            f"(expected tests/e2e/im_integration/transcripts/<scenario-name>.yaml)"
        )
    transcript = load_transcript(transcript_file)
    if not transcript.participant_replies:
        raise RuntimeError(
            "IM Integration transcript must define participant_replies and end_turn steps"
        )
    result = RunResult(scenario=scenario.name)
    try:
        creds = feishu_credentials(env, repo_cwd=os.getcwd())
        if participant is None:
            # Imported lazily because Participant adapters use the verifier in
            # this package. Runtime import keeps that dependency acyclic.
            from .participants import ParticipantConfigError, participant_from_env
            try:
                participant = participant_from_env(env)
            except ParticipantConfigError as e:
                raise IntegrationSkip(str(e)) from e
    except IntegrationSkip as e:
        result.skipped = True
        result.skip_reason = str(e)
        return result

    run_started = time.time()
    with tempfile.TemporaryDirectory(prefix=f"im-align-e2e-{scenario.name}-") as tmp:
        tmp = Path(tmp)
        workspace = tmp / "workspace"
        workspace.mkdir()
        materialize_seed(scenario, workspace, cloner=cloner)

        before = snapshot_git(workspace)

        config_home = tmp / "config"
        state_home = tmp / "state"
        _write_user_config(config_home, creds, scenario)
        run_env = bridge_worker_env(os.environ, config_home, state_home)
        # The Bridge spawns the fake backend via `python -m
        # tests.e2e.shared.fake_acp_agent` with cwd set to the temporary
        # workspace, so the repository root must be on PYTHONPATH for the
        # module to resolve.
        run_env["PYTHONPATH"] = (
            str(REPO_ROOT)
            + os.pathsep
            + run_env.get("PYTHONPATH", "")
        ).rstrip(os.pathsep)

        start = _bridge(
            run_env,
            workspace,
            [
                "start",
                f"[e2e:{scenario.name}] {scenario.requirement[:80]}",
                "--skill", scenario.skill.name,
                "--backend", scenario.backend,
                "--command-alias", "e2e-fake",
                "--chat", creds["chat_id"],
                "--json",
            ],
            timeout=120,
        )
        result.run_id = start["run_id"]
        result.log_path = start.get("log_path", "")
        root_message_id = start.get("root_message_id", "")
        if not root_message_id:
            try:
                root_message_id = wait_for_root_message(
                    lambda: _bridge(
                        run_env,
                        workspace,
                        ["status", result.run_id, "--json"],
                        timeout=60,
                    ),
                    timeout_seconds=min(180, scenario.timeout_seconds),
                )
            except Exception:
                try:
                    _bridge(
                        run_env,
                        workspace,
                        ["stop", result.run_id, "--json"],
                        timeout=60,
                    )
                except Exception:
                    pass
                raise

        session_state = {"terminal": False}
        thread = thread_transport or participant.open_thread(
            creds["chat_id"],
            root_message_id=root_message_id,
            can_post=lambda: not session_state["terminal"],
        )
        invalid_completion = threading.Event()
        watched_thread = _InvalidCompletionWatcher(thread, invalid_completion)
        actor_outcome = {"state": None, "error": None}

        def actor_work():
            run_guarded_actor(
                actor_outcome,
                lambda: run_scripted_participant(
                    watched_thread,
                    transcript.participant_replies,
                    scenario.timeout_seconds,
                    lambda: session_state["terminal"],
                    poll_interval=poll_interval,
                ),
                lambda: _bridge(
                    run_env,
                    workspace,
                    ["stop", result.run_id, "--json"],
                    timeout=60,
                ),
            )

        actor_thread = threading.Thread(target=actor_work, daemon=True)
        actor_thread.start()
        try:
            # Wait in bounded slices: when the Bridge rejects the completion
            # marker the Session would otherwise sit idle until the idle
            # timeout, burning the whole scenario budget for an outcome that
            # is already determined.
            deadline = time.monotonic() + scenario.timeout_seconds
            while True:
                if invalid_completion.is_set():
                    final = _bridge(
                        run_env,
                        workspace,
                        ["stop", result.run_id, "--json"],
                        timeout=60,
                    )
                    result.skip_reason = (
                        "invalid completion signal detected in Thread; "
                        "Bridge rejected the Agent's completion marker"
                    )
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    final = _bridge(
                        run_env,
                        workspace,
                        ["status", result.run_id, "--json"],
                        timeout=60,
                    )
                    break
                slice_seconds = max(1, int(min(30, remaining)))
                final = _bridge(
                    run_env,
                    workspace,
                    ["wait", result.run_id, "--timeout", str(slice_seconds), "--json"],
                    timeout=slice_seconds + 120,
                )
                if final.get("state") in ("done", "failed", "idle_timeout", "stopped"):
                    break
        finally:
            session_state["terminal"] = True
            actor_thread.join(timeout=30)
            if actor_thread.is_alive():
                actor_outcome["error"] = RuntimeError(
                    "scripted Participant did not stop after Session termination"
                )
                try:
                    _bridge(
                        run_env,
                        workspace,
                        ["stop", result.run_id, "--json"],
                        timeout=60,
                    )
                except Exception as stop_error:
                    actor_outcome["error"].add_note(
                        f"failed to stop Session after actor join timeout: {stop_error}"
                    )
            status = _bridge(run_env, workspace, ["status", "--json"], timeout=60)
            if status["state"] not in ("done", "failed", "idle_timeout", "stopped"):
                _bridge(run_env, workspace, ["stop", result.run_id, "--json"], timeout=60)
        if actor_outcome["error"] is not None:
            error = actor_outcome["error"]
            raise RuntimeError(f"scripted Participant failed: {error}") from error
        result.state = final["state"]

        result.assertions = check_artifacts(workspace, scenario)
        result.assertions["status_done"] = result.state == "done"
        after = snapshot_git(workspace)
        allowed = [a.path for a in scenario.expected_artifacts]
        result.assertions["git_unchanged"] = git_unchanged_except(before, after, allowed)

        check = verifier or participant.open_verifier(root_message_id=root_message_id)
        reply_receipts = actor_outcome["state"] or []
        try:
            since = run_started - 60
            thread_messages = check.messages_since(creds["chat_id"], since)
            result.assertions["completion_card"] = any(
                message_contains_card(message, COMPLETION_CARD_TITLE)
                for message in thread_messages
            )
            result.assertions["zero_approval_cards"] = not find_approval_card(
                thread_messages
            )
            acked_message_ids = {
                receipt["message_id"]
                for receipt in reply_receipts
                if check.message_reaction_received(
                    receipt["message_id"], participant.bridge_bot_open_id,
                    operator_app_id=creds["app_id"],
                )
            }
            result.assertions.update(
                participant_round_trip_assertions(
                    reply_receipts,
                    thread_messages,
                    root_message_id=root_message_id,
                    bridge_bot_open_id=participant.bridge_bot_open_id,
                    bridge_app_id=creds["app_id"],
                    acked_message_ids=acked_message_ids,
                )
            )
        except Exception as e:
            result.assertions.update(
                {
                    "completion_card": False,
                    "zero_approval_cards": False,
                    "participant_replied": False,
                    "participant_reply_acked": False,
                    "participant_drove_turn": False,
                }
            )
            result.skip_reason = f"thread verification error: {e}"
    return result


def main(argv):
    if len(argv) != 2:
        print("usage: python -m tests.e2e.shared.im_integration <scenario_dir>", file=sys.stderr)
        return 2
    result = run_integration(argv[1])
    if result.skipped:
        print(f"SKIP {result.scenario}: {result.skip_reason}")
        return 0
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
