#!/usr/bin/env python3
"""Bridge CLI entry bundled with the im-align Skill.

The host Agent calls only the stable subcommands in this file and does not parse
runtime logs. setup writes credentials to user-level config. start/resume place
the long connection and ACP Session in an independent background process.
"""

import argparse
import getpass
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from im_align import cards, config as cfgmod  # noqa: E402
from im_align import identity, state  # noqa: E402
from im_align.acp.client import AcpClient  # noqa: E402
from im_align.im_providers.feishu import FeishuProvider  # noqa: E402
from im_align.orchestrator import Orchestrator  # noqa: E402

log = logging.getLogger("im_align.bridge")
SCRIPT_PATH = str(Path(__file__).resolve())
SKILL_ROOT = str(Path(__file__).resolve().parent.parent)


def build_parser():
    p = argparse.ArgumentParser(prog="bridge.py", description="im-align Bridge")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="interactively configure Feishu/Lark credentials")

    s = sub.add_parser("start", help="start an Alignment Session and return run_id immediately")
    s.add_argument("topic", help="Alignment topic")
    s.add_argument("--skill", default=None, help="Alignment Skill; default is grill-with-docs")
    s.add_argument("--provider", default=None, help="configured IM provider key")
    s.add_argument("--chat", default=None, help="Feishu/Lark group chat_id (oc_...)")
    s.add_argument("--backend", default=None, choices=["opencode", "trae-cli", "kiro-cli", "kimi"])
    s.add_argument("--model", default=None, help="model identifier; omit to keep the Agent's current selection")
    s.add_argument("--command-alias", default=None, help="configured trusted Agent Backend command alias")
    s.add_argument("--approval", default=None, choices=[cfgmod.POLICY_CALLBACK, cfgmod.POLICY_AUTO_ALLOW], help="temporary Approval mode override")
    s.add_argument("--acknowledge-auto-allow", action="store_true", help="confirm the risk of using --approval auto_allow")
    s.add_argument("--initiator", default=None, help="initiator email or open_id")
    s.add_argument("--foreground", action="store_true", help="run in foreground for debugging")
    s.add_argument("--json", action="store_true", help="emit stable JSON")

    w = sub.add_parser("wait", help="bounded wait for terminal run state")
    w.add_argument("run_id", nargs="?", default=None)
    w.add_argument("--timeout", type=int, default=600)
    w.add_argument("--json", action="store_true")

    st = sub.add_parser("status", help="show run status")
    st.add_argument("run_id", nargs="?", default=None)
    st.add_argument("--json", action="store_true")

    r = sub.add_parser("resume", help="resume a run in idle_timeout/failed/stopped state")
    r.add_argument("run_id", nargs="?", default=None)
    r.add_argument("--foreground", action="store_true")
    r.add_argument("--json", action="store_true")

    x = sub.add_parser("stop", help="stop a running run")
    x.add_argument("run_id", nargs="?", default=None)
    x.add_argument("--json", action="store_true")

    worker = sub.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("run_id")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("IM_ALIGN_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return globals()[f"cmd_{args.command}"](args)
    except (cfgmod.ConfigError, identity.IdentityError, RuntimeError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Cancelled", file=sys.stderr)
        return 130
    except Exception:
        log.exception("unhandled exception")
        return 1


# ---- setup ----


def _read_existing_setup(path):
    import yaml

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise cfgmod.ConfigError(f"configuration file {path} must be a mapping at the top level")
    return data


def _prompt(label, current="", secret=False):
    suffix = f" [{current}]" if current and not secret else ""
    value = getpass.getpass(f"{label}{suffix}: ") if secret else input(f"{label}{suffix}: ")
    return value.strip() or current


def cmd_setup(args):
    import yaml

    path = Path(cfgmod.user_config_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.exists():
        os.chmod(path, 0o600)
    existing = _read_existing_setup(path)
    existing_providers = existing.get("providers") or {}
    old_defaults = existing.get("defaults") or {}
    old_im_defaults = old_defaults.get("im") or {}
    old_agent_defaults = old_defaults.get("agent") or {}
    default_provider_key = old_im_defaults.get("provider")
    if not default_provider_key and len(existing_providers) == 1:
        default_provider_key = next(iter(existing_providers))
    provider_key = _prompt("provider key", default_provider_key or "feishu")
    old_provider = existing_providers.get(provider_key) or {}
    old_type = old_provider.get("type")
    if not old_type:
        old_type = "lark" if old_provider.get("domain") == "larksuite" else "feishu"
    old_chat_id = old_provider.get("default_chat_id", "")

    print("Feishu/Lark custom app configuration; see references/feishu-setup.md")
    app_id = _prompt("app_id (cli_...)", old_provider.get("app_id", ""))
    app_secret = _prompt("app_secret (leave blank to keep current value)", old_provider.get("app_secret", ""), secret=True)
    provider_type = _prompt("type feishu/lark", old_type)
    chat_id = _prompt("default group chat_id (oc_..., optional)", old_chat_id)
    backend = _prompt("default Agent Backend opencode/trae-cli/kiro-cli/kimi", old_agent_defaults.get("backend", old_defaults.get("backend", "opencode")))
    domain = cfgmod.PROVIDER_TYPE_DOMAINS.get(provider_type, "")

    providers = dict(existing_providers)
    providers[provider_key] = {
        "type": provider_type,
        "app_id": app_id,
        "app_secret": app_secret,
        "default_chat_id": chat_id,
    }
    defaults = {"im": {"provider": provider_key}, "agent": {"backend": backend}}
    data = {"providers": providers, "defaults": defaults}
    commands = existing.get("commands") or {}
    if commands:
        cfgmod._read_commands(commands)
        data["commands"] = commands

    # Fully validate the candidate before atomic replacement; invalid input must not corrupt existing config.
    candidate = dict(cfgmod.DEFAULTS)
    candidate.update(defaults)
    candidate.update(
        {
            "provider": provider_key,
            "chat_id": chat_id or "oc_setup_validation",
            "backend": backend,
            "provider_type": provider_type,
            "feishu_app_id": app_id,
            "feishu_app_secret": app_secret,
            "feishu_domain": domain,
        }
    )
    cfgmod.validate(candidate)
    cfgmod.backend_argv(
        candidate["backend"],
        candidate.get("model"),
        candidate.get("command"),
        candidate.get("args"),
    )

    fd, tmp_name = tempfile.mkstemp(prefix="config.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise

    print("Validating credentials...")
    provider = FeishuProvider(app_id, app_secret, domain)
    email = input("Use your Feishu/Lark email for a connectivity check; leave blank to skip: ").strip()
    if email:
        try:
            open_id = provider.resolve_open_id(email)
            print(f"Credentials are valid; resolved open_id: {open_id}")
        except Exception as e:
            print(f"Warning: connectivity check failed; configuration was written to {path}: {e}")
    print(f"Done: {path} (mode 0600)")
    return 0


# ---- record helpers ----


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _overrides(args):
    return {
        "skill": getattr(args, "skill", None),
        "provider": getattr(args, "provider", None),
        "chat_id": getattr(args, "chat", None),
        "backend": getattr(args, "backend", None),
        "model": getattr(args, "model", None),
        "command_alias": getattr(args, "command_alias", None),
        "permission": getattr(args, "approval", None),
        "acknowledge_auto_allow": getattr(args, "acknowledge_auto_allow", False),
    }


def _record_overrides(record):
    return {
        "skill": record["skill"],
        "provider": record.get("provider", ""),
        "chat_id": record["chat_id"],
        "backend": record["backend"],
        "model": record.get("model", ""),
        "command_alias": record.get("command_alias", ""),
        "permission": record["permission"],
        "acknowledge_auto_allow": record["permission"] == cfgmod.POLICY_AUTO_ALLOW,
    }


def _prepare_record(args):
    cwd = os.path.abspath(os.getcwd())
    identity.ensure_git_repo(cwd)
    config = cfgmod.load(cwd, _overrides(args))
    agent = config["agent"]
    argv = cfgmod.backend_argv(
        agent["backend"],
        agent.get("model"),
        agent.get("command"),
        agent.get("args"),
    )
    if not shutil.which(argv[0]):
        raise RuntimeError(f"Agent Backend command {argv[0]!r} was not found; install it or fix configuration")

    claim = identity.resolve_claim(
        cwd, args.initiator, expected_lark_app_id=config["feishu_app_id"]
    )
    provider = FeishuProvider(
        config["feishu_app_id"], config["feishu_app_secret"], config["feishu_domain"]
    )
    if not claim.get("open_id"):
        if not claim.get("email"):
            raise identity.IdentityError("initiator has neither email nor open_id")
        try:
            claim["open_id"] = provider.resolve_open_id(claim["email"])
        except Exception as e:
            raise identity.IdentityError(f"failed to resolve initiator email to Feishu/Lark open_id: {e}") from e
    if not claim.get("name"):
        claim["name"] = provider.user_name(claim["open_id"])

    now = time.time()
    return {
        "run_id": state.new_run_id(),
        "state": state.STATE_STARTING,
        "topic": args.topic.strip(),
        "provider": config["im"]["provider"],
        "skill": agent["skill"],
        "chat_id": config["im"]["chat_id"],
        "backend": agent["backend"],
        "model": agent.get("model", ""),
        "command_alias": agent.get("command_alias", ""),
        "agent_argv": argv,
        "permission": config["approval"]["mode"],
        "cwd": cwd,
        "initiator_email": claim.get("email", ""),
        "initiator_open_id": claim["open_id"],
        "initiator_name": claim.get("name", ""),
        "created_at": _now_iso(),
        "started_at_ts": now,
        "attempt_started_at_ts": now,
        "last_activity_at_ts": now,
        "root_message_id": "",
        "acp_session_id": "",
        "spec_path": "",
        "fatal": "",
        "pid": 0,
    }


def _mark_stale(record):
    record = dict(record)
    record["state"] = state.STATE_FAILED
    record["fatal"] = "Bridge process exited without writing terminal state"
    record["ended_at"] = _now_iso()
    record["pid"] = 0
    state.save_active(record)
    state.append_history(record)


def _reserve(record):
    with state.single_session_lock():
        previous = state.load_active()
        if previous and previous.get("state") not in state.TERMINAL_STATES:
            pid = int(previous.get("pid") or 0)
            age = time.time() - float(previous.get("attempt_started_at_ts") or 0)
            if state.process_alive(pid) or age < 30:
                raise RuntimeError(
                    f"an Alignment Session is already running ({previous.get('run_id', '?')}); wait or stop it first"
                )
            _mark_stale(previous)
        state.save_active(record)


def _spawn_worker(record):
    log_path = state.log_path(record["run_id"])
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    stream = open(log_path, "a", encoding="utf-8")
    os.chmod(log_path, 0o600)
    try:
        proc = subprocess.Popen(
            [sys.executable, SCRIPT_PATH, "_worker", record["run_id"]],
            cwd=record["cwd"],
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        stream.close()
        failed = dict(record)
        failed.update({"state": state.STATE_FAILED, "fatal": "failed to start Bridge background process", "ended_at": _now_iso()})
        state.save_active(failed)
        state.append_history(failed)
        raise
    stream.close()

    # The worker must claim its PID within a bounded window. The window needs to
    # cover local Python cold start; measured with venv interpreter + lark_oapi
    # import at 4.5-7s twice on 2026-09-12, and 9.3s while Microsoft Defender
    # scanned venv files on 2026-09-13. Extreme single reads can hang for minutes.
    # Wait here only for process state ownership, not Feishu/Lark or ACP handshake.
    deadline = time.time() + 60.0
    current = record
    while time.time() < deadline:
        time.sleep(0.05)
        current = state.load_active() or record
        if current.get("state") in state.TERMINAL_STATES:
            return current
        exit_code = proc.poll()
        if exit_code is not None:
            failed = dict(current)
            failed.update(
                {
                    "state": state.STATE_FAILED,
                    "fatal": f"Bridge worker exited during startup (exit={exit_code})",
                    "ended_at": _now_iso(),
                    "pid": 0,
                }
            )
            state.save_active(failed)
            state.append_history(failed)
            return failed
        if current.get("pid") == proc.pid:
            return current

    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)
    failed = dict(state.load_active() or record)
    failed.update(
        {
            "state": state.STATE_FAILED,
            "fatal": "Bridge worker did not claim state within 60 seconds",
            "ended_at": _now_iso(),
            "pid": 0,
        }
    )
    state.save_active(failed)
    state.append_history(failed)
    return failed


def _record_view(record, wait_timed_out=False):
    session_id = record.get("acp_session_id", "")
    backend = record.get("backend", "")
    if session_id and backend == cfgmod.BACKEND_OPENCODE:
        backend_resume = shlex.join(["opencode", "-s", session_id])
    elif session_id and backend == cfgmod.BACKEND_TRAE_CLI:
        backend_resume = shlex.join(["traecli", "resume", session_id])
    else:
        # kiro-cli and kimi do not yet have a measured ACP sessionId -> native
        # resume mapping. For kimi, the native -S id namespace is unverified.
        # Do not invent one; leave empty and let Bridge resume reuse ACP session/load.
        backend_resume = ""
    return {
        "run_id": record.get("run_id", ""),
        "state": record.get("state", ""),
        "topic": record.get("topic", ""),
        "skill": record.get("skill", ""),
        "backend": backend,
        "model": record.get("model", ""),
        "cwd": record.get("cwd", ""),
        "initiator": record.get("initiator_name", ""),
        "root_message_id": record.get("root_message_id", ""),
        "acp_session_id": session_id,
        "spec_path": record.get("spec_path", ""),
        "error": record.get("fatal", ""),
        "pid": record.get("pid", 0),
        "log_path": state.log_path(record.get("run_id", "")),
        "wait_timed_out": wait_timed_out,
        "bridge_resume_command": shlex.join(
            [
                "uv",
                "run",
                "--project",
                SKILL_ROOT,
                "python",
                SCRIPT_PATH,
                "resume",
                record.get("run_id", ""),
                "--json",
            ]
        ),
        "backend_resume_command": backend_resume,
    }


def _emit(record, as_json=False, wait_timed_out=False):
    view = _record_view(record, wait_timed_out)
    if as_json:
        print(json.dumps(view, ensure_ascii=False))
    else:
        print(f"run: {view['run_id']}\nstate: {view['state']}")
        if view["spec_path"]:
            print(f"Spec: {view['spec_path']}")
        if view["error"]:
            print(f"Error: {view['error']}")
        if view["backend_resume_command"]:
            print(f"Continue with Agent Backend: {view['backend_resume_command']}")
    return view


def _get_record(run_id=None):
    record = state.load_run(run_id)
    if not record:
        suffix = f" {run_id}" if run_id else ""
        raise RuntimeError(f"run not found{suffix}")
    pid = int(record.get("pid") or 0)
    if record.get("state") not in state.TERMINAL_STATES and pid > 0 and not state.process_alive(pid):
        _mark_stale(record)
        record = state.load_run(record.get("run_id")) or record
    return record


# ---- lifecycle commands ----


def cmd_start(args):
    if not args.topic.strip():
        raise ValueError("Alignment topic cannot be empty")
    record = _prepare_record(args)
    _reserve(record)
    if args.foreground:
        _execute_record(record["run_id"], resume=False)
        record = _get_record(record["run_id"])
    else:
        record = _spawn_worker(record)
    _emit(record, args.json)
    return 0 if record.get("state") != state.STATE_FAILED else 1


def cmd_status(args):
    _emit(_get_record(args.run_id), args.json)
    return 0


def cmd_wait(args):
    if args.timeout <= 0:
        raise ValueError("--timeout must be a positive integer")
    record = _get_record(args.run_id)
    run_id = record["run_id"]
    deadline = time.time() + args.timeout
    while record.get("state") not in state.TERMINAL_STATES and time.time() < deadline:
        time.sleep(1)
        record = _get_record(run_id)
    timed_out = record.get("state") not in state.TERMINAL_STATES
    _emit(record, args.json, wait_timed_out=timed_out)
    return 0


def cmd_resume(args):
    old = _get_record(args.run_id)
    if old.get("state") not in {state.STATE_FAILED, state.STATE_IDLE_TIMEOUT, state.STATE_STOPPED}:
        raise RuntimeError(
            f"run {old.get('run_id')} is in state {old.get('state')}; only failed/idle_timeout/stopped can be resumed"
        )
    if not old.get("acp_session_id") or not old.get("root_message_id"):
        raise RuntimeError("this run has not established an ACP session or Feishu/Lark Thread and cannot be resumed")
    identity.ensure_git_repo(old["cwd"])
    record = dict(old)
    record.update(
        {
            "state": state.STATE_STARTING,
            "fatal": "",
            "pid": 0,
            "attempt_started_at_ts": time.time(),
            "last_activity_at_ts": time.time(),
            "resumed_at": _now_iso(),
        }
    )
    _reserve(record)
    if args.foreground:
        _execute_record(record["run_id"], resume=True)
        record = _get_record(record["run_id"])
    else:
        record = _spawn_worker(record)
    _emit(record, args.json)
    return 0 if record.get("state") != state.STATE_FAILED else 1


def cmd_stop(args):
    record = _get_record(args.run_id)
    if record.get("state") in state.TERMINAL_STATES:
        _emit(record, args.json)
        return 0
    pid = int(record.get("pid") or 0)
    if not state.process_alive(pid):
        _mark_stale(record)
        record = _get_record(record["run_id"])
        _emit(record, args.json)
        return 1
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 15
    while time.time() < deadline:
        time.sleep(0.25)
        record = _get_record(record["run_id"])
        if record.get("state") in state.TERMINAL_STATES:
            break
    _emit(record, args.json)
    if record.get("state") not in state.TERMINAL_STATES:
        print("Error: Bridge stop was not confirmed within 15 seconds", file=sys.stderr)
        return 1
    return 0


def cmd__worker(args):
    return _execute_record(args.run_id, resume=None)


def _execute_record(run_id, resume=None):
    with state.single_session_lock():
        record = _get_record(run_id)
        if record.get("run_id") != run_id:
            raise RuntimeError(f"active run changed; refusing to start {run_id}")
        if resume is None:
            resume = bool(record.get("acp_session_id") and record.get("root_message_id"))

        provider = None
        client = None
        orchestrator = None
        stop_before_ready = False

        def request_stop(signum, frame):
            nonlocal stop_before_ready
            stop_before_ready = True
            if orchestrator:
                orchestrator.request_stop()
            if record.get("state") == state.STATE_STARTING:
                if provider:
                    provider.stop()
                if client:
                    client.close()

        old_term = signal.signal(signal.SIGTERM, request_stop)
        old_int = signal.signal(signal.SIGINT, request_stop)
        try:
            record["pid"] = os.getpid()
            state.save_active(record)
            config = cfgmod.load(record["cwd"], _record_overrides(record))
            provider = FeishuProvider(
                config["feishu_app_id"],
                config["feishu_app_secret"],
                config["feishu_domain"],
            )
            orchestrator = Orchestrator(provider, config, record)
            on_permission = None if config["approval"]["mode"] == cfgmod.POLICY_AUTO_ALLOW else orchestrator.on_permission
            client = AcpClient(
                record["agent_argv"],
                record["cwd"],
                on_permission=on_permission,
                approval_timeout=timedelta(seconds=config["timeouts"]["approval_timeout_seconds"]),
                turn_timeout=timedelta(seconds=config["timeouts"]["turn_timeout_seconds"]),
            )
            client.set_model(record.get("model", ""))
            provider.start()
            client.start()
            if resume:
                client.load_session(record["acp_session_id"])
                record["client"] = client
            else:
                session_id = client.new_session()
                orchestrator.run_first_turn(client, session_id)
            if stop_before_ready:
                orchestrator.request_stop()
            if record.get("state") not in state.TERMINAL_STATES:
                record["state"] = state.STATE_ACTIVE
            state.save_active(record)

            outcome = orchestrator.run()
            record["state"] = state.STATE_FAILED if record.get("fatal") else outcome
            if provider and record.get("root_message_id"):
                try:
                    if record["state"] == state.STATE_IDLE_TIMEOUT:
                        provider.reply_card(
                            record["root_message_id"],
                            cards.simple_card(
                                "orange",
                                "⏸️ Session Paused Due To Inactivity",
                                "No new replies arrived for a long time, so the Bridge exited. The developer can resume the original Thread and Agent session from the terminal.",
                            ),
                        )
                    elif record["state"] == state.STATE_STOPPED:
                        provider.reply_card(
                            record["root_message_id"],
                            cards.simple_card(
                                "grey",
                                "⏹️ Session Stopped",
                                "The Session was stopped. The developer can resume it from the terminal if needed.",
                            ),
                        )
                except Exception:
                    log.exception("failed to send terminal-state notification")
        except Exception as e:
            if stop_before_ready:
                log.info("Bridge received stop request during startup: %s", e)
                record["state"] = state.STATE_STOPPED
                record["fatal"] = ""
            else:
                log.exception("Bridge run failed")
                record["state"] = state.STATE_FAILED
                record["fatal"] = str(e)
            if provider and record.get("root_message_id") and not stop_before_ready:
                try:
                    provider.reply_card(
                        record["root_message_id"],
                        cards.simple_card("red", "❌ Bridge Failed", f"Error: {e}"),
                    )
                except Exception:
                    log.exception("failed to send failure notification")
        finally:
            record.pop("client", None)
            record["pid"] = 0
            record["ended_at"] = _now_iso()
            state.save_active(record)
            state.append_history(record)
            if client:
                client.close()
            if provider:
                provider.stop()
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
    return 0 if record.get("state") != state.STATE_FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
