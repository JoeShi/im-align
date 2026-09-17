"""IM Integration layer runner: scripted fake ACP backend against the real Bridge.

Drives one Scenario end to end: materialize a temporary seed workspace, point
the Bridge's backend command at fake_acp_agent.py through a command alias in an
isolated XDG_CONFIG_HOME, run start/wait/status, then apply deterministic
assertions (docs/e2e-harness-design.md):

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
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

try:
    from .harness_support import (
        bridge_worker_env,
        participant_round_trip_assertions,
        run_guarded_actor,
        wait_for_root_message,
    )
    from .scenario import Scenario, load_scenario, load_transcript
    from .message_protocol import bridge_identity_matches
except ImportError:  # Running as a plain script.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from harness_support import (
        bridge_worker_env,
        participant_round_trip_assertions,
        run_guarded_actor,
        wait_for_root_message,
    )
    from scenario import Scenario, load_scenario, load_transcript
    from message_protocol import bridge_identity_matches

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPO_ROOT / "scripts" / "bridge.py"
FAKE_AGENT_PATH = Path(__file__).resolve().parent / "fake_acp_agent.py"

# Environment variables holding CI secrets for the real Feishu Thread. Never
# hardcode values; a missing variable skips the run.
ENV_APP_ID = "IM_ALIGN_E2E_FEISHU_APP_ID"
ENV_APP_SECRET = "IM_ALIGN_E2E_FEISHU_APP_SECRET"
ENV_CHAT_ID = "IM_ALIGN_E2E_CHAT_ID"

COMPLETION_CARD_TITLE = "✅ Alignment Complete"


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


# ---- Feishu OpenAPI thread verification (transport seam, injectable) ----


class ThreadVerifier:
    """Post-run checks under the selected Participant identity."""

    def messages_since(self, chat_id: str, since_ts: float) -> list:
        raise NotImplementedError

    def completion_card_received(self, chat_id: str, since_ts: float) -> bool:
        raise NotImplementedError

    def approval_card_received(self, chat_id: str, since_ts: float) -> bool:
        raise NotImplementedError

    def message_reaction_received(
        self, message_id: str, operator_open_id: str, *, operator_app_id: str = ""
    ) -> bool:
        raise NotImplementedError


APPROVAL_CARD_TITLE = "🔐 Approval Request"


def message_contains_card(message: dict, marker: str) -> bool:
    """Deterministic card marker check over one im/v1/messages item."""
    body = json.dumps(message.get("body", {}), ensure_ascii=False)
    return marker in body


def find_approval_card(messages: list) -> bool:
    """True when any message carries an Approval card (ADR-0005 happy-path gate).

    Matches the card header title from cards.approval_card plus the
    `"im_align": "approval"` button value, so a renamed title alone cannot
    hide a regression that re-introduces Approval cards.
    """
    for message in messages:
        body = json.dumps(message.get("body", {}), ensure_ascii=False)
        if APPROVAL_CARD_TITLE in body or '"im_align": "approval"' in body:
            return True
    return False


class FeishuOpenAPIVerifier(ThreadVerifier):
    """Poll im/v1/messages over HTTPS; keeps the long-connection lock free.

    Two identities, exactly one of which is required (mirroring the
    transports): app_id + app_secret (tenant token), or a user_access_token.
    The Bridge app intentionally lacks im:message.group_msg, so the runner
    builds this with the Participant identity, never the Bridge credentials.

    Measured constraint: the chat-container listing omits thread replies for
    bot identities, so when root_message_id is set the verifier resolves the
    root message's thread_id once and lists the thread container.
    """

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
        domain: str = "feishu",
        user_access_token: str = "",
        root_message_id: str = "",
    ):
        if user_access_token and (app_id or app_secret):
            raise ValueError(
                "user_access_token and app_id/app_secret are mutually exclusive"
            )
        if not user_access_token and not (app_id and app_secret):
            raise ValueError(
                "either user_access_token or app_id + app_secret is required"
            )
        self._app_id = app_id
        self._app_secret = app_secret
        self._user_access_token = user_access_token
        self._root_message_id = root_message_id
        self._thread_id = ""
        self._base = (
            "https://open.feishu.cn" if domain == "feishu" else "https://open.larksuite.com"
        )

    def _request(
        self, method: str, path: str, body: dict | None = None, token: str = ""
    ) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(
            self._base + path,
            data=data,
            method=method,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _tenant_token(self) -> str:
        resp = self._request(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": self._app_id, "app_secret": self._app_secret},
        )
        if resp.get("code") != 0:
            raise RuntimeError(f"tenant_access_token failed: {resp.get('msg')}")
        return resp["tenant_access_token"]

    def _token(self) -> str:
        if self._user_access_token:
            return self._user_access_token
        return self._tenant_token()

    def _resolve_thread_id(self, token: str) -> str:
        if self._thread_id:
            return self._thread_id
        import urllib.parse

        message_id = urllib.parse.quote(self._root_message_id, safe="")
        resp = self._request("GET", f"/open-apis/im/v1/messages/{message_id}", token=token)
        if resp.get("code") != 0:
            raise RuntimeError(f"resolve thread failed: {resp.get('msg')}")
        items = resp.get("data", {}).get("items", [])
        thread_id = items[0].get("thread_id", "") if items else ""
        if not thread_id:
            raise RuntimeError("root message has no thread_id")
        self._thread_id = thread_id
        return thread_id

    def _container_query(self, chat_id: str, page_token: str, token: str) -> str:
        if self._root_message_id:
            container = (
                "container_id_type=thread"
                f"&container_id={self._resolve_thread_id(token)}"
            )
        else:
            container = f"container_id_type=chat&container_id={chat_id}"
        query = f"/open-apis/im/v1/messages?{container}&sort_type=ByCreateTimeDesc&page_size=50"
        if page_token:
            query += f"&page_token={page_token}"
        return query

    def messages_since(self, chat_id: str, since_ts: float) -> list:
        """Messages at or after since_ts in ascending order; stops paging once older items appear."""
        token = self._token()
        page_token = ""
        found = []
        while True:
            query = self._container_query(chat_id, page_token, token)
            resp = self._request("GET", query, token=token)
            if resp.get("code") != 0:
                raise RuntimeError(f"list messages failed: {resp.get('msg')}")
            crossed = False
            for item in resp.get("data", {}).get("items", []):
                create_time = int(item.get("create_time", "0")) / 1000
                if create_time < since_ts:
                    crossed = True
                else:
                    found.append(item)
            if crossed:
                break
            page_token = resp.get("data", {}).get("page_token", "")
            if not resp.get("data", {}).get("has_more"):
                break
        return found

    def completion_card_received(self, chat_id: str, since_ts: float) -> bool:
        for message in self.messages_since(chat_id, since_ts):
            if message_contains_card(message, COMPLETION_CARD_TITLE):
                return True
        return False

    def approval_card_received(self, chat_id: str, since_ts: float) -> bool:
        return find_approval_card(self.messages_since(chat_id, since_ts))

    def message_reaction_received(
        self, message_id: str, operator_open_id: str, *, operator_app_id: str = ""
    ) -> bool:
        token = self._token()
        path = f"/open-apis/im/v1/messages/{message_id}/reactions?page_size=50"
        resp = self._request("GET", path, token=token)
        if resp.get("code") != 0:
            raise RuntimeError(f"list message reactions failed: {resp.get('msg')}")
        for item in resp.get("data", {}).get("items", []):
            reaction_type = item.get("reaction_type") or {}
            operator = item.get("operator") or {}
            observed_operator = operator.get("operator_id") or item.get("operator_id", "")
            if (
                reaction_type.get("emoji_type") == "OK"
                and bridge_identity_matches(
                    observed_operator, operator.get("operator_type", ""),
                    operator_open_id, operator_app_id,
                )
            ):
                return True
        return False


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
    extra_participant_open_ids=(),
    turn_timeout_seconds: int | None = None,
):
    defaults = {
        "im": {
            "provider": "e2e",
            "extra_participant_open_ids": list(extra_participant_open_ids),
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
                # Absolute paths: the Bridge spawns the backend with cwd set to
                # the temporary workspace, so relative paths would not resolve.
                "args": [str(FAKE_AGENT_PATH), str(Path(scenario.transcript_path()).resolve())],
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
    transcript = load_transcript(scenario.transcript_path())
    if not transcript.participant_replies:
        raise RuntimeError(
            "IM Integration transcript must define participant_replies and end_turn steps"
        )
    result = RunResult(scenario=scenario.name)
    try:
        creds = feishu_credentials(env, repo_cwd=os.getcwd())
        if participant is None:
            # Imported lazily because Participant adapters use the verifier in
            # this module. Runtime import keeps that dependency acyclic.
            try:
                from .participants import ParticipantConfigError, participant_from_env
            except ImportError:
                from participants import ParticipantConfigError, participant_from_env
            try:
                participant = participant_from_env("as-user", env)
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

        start = _bridge(
            run_env,
            workspace,
            [
                "start",
                f"[e2e:{scenario.name}] {scenario.requirement[:80]}",
                "--skill", scenario.skill,
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
        actor_outcome = {"state": None, "error": None}

        def actor_work():
            run_guarded_actor(
                actor_outcome,
                lambda: run_scripted_participant(
                    thread,
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
            final = _bridge(
                run_env,
                workspace,
                ["wait", result.run_id, "--timeout", str(scenario.timeout_seconds), "--json"],
                timeout=scenario.timeout_seconds + 180,
            )
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
        print("usage: python -m tests.e2e.im_integration <scenario_dir>", file=sys.stderr)
        return 2
    result = run_integration(argv[1])
    if result.skipped:
        print(f"SKIP {result.scenario}: {result.skip_reason}")
        return 0
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
