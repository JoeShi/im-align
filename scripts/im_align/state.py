"""Session state persistence and machine-level single-session lock.

State stores only JSON-serializable data; runtime objects such as the ACP client
are filtered out. Multiple WebSocket connections for the same Feishu/Lark app
randomly shard events, so v1 allows only one Bridge worker to hold the lock on a
machine.
"""

import contextlib
import fcntl
import json
import os
import secrets
import time

from .config import state_dir

STATE_STARTING = "starting"
STATE_ACTIVE = "active"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_IDLE_TIMEOUT = "idle_timeout"
STATE_STOPPED = "stopped"
TERMINAL_STATES = {STATE_DONE, STATE_FAILED, STATE_IDLE_TIMEOUT, STATE_STOPPED}


def new_run_id():
    return f"run-{int(time.time())}-{secrets.token_hex(3)}"


def ensure_state_dir():
    path = state_dir()
    os.makedirs(os.path.join(path, "logs"), mode=0o700, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return path


def active_path():
    return os.path.join(state_dir(), "active.json")


def history_path():
    return os.path.join(state_dir(), "history.jsonl")


def lock_path():
    return os.path.join(state_dir(), "lock")


def log_path(run_id):
    return os.path.join(state_dir(), "logs", f"{run_id}.log")


def _json_record(data):
    """Remove runtime objects so temporary Orchestrator clients do not pollute state."""
    clean = {}
    for key, value in data.items():
        if key.startswith("_") or key == "client":
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        clean[key] = value
    return clean


def load_active():
    try:
        with open(active_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(f"failed to read state file {active_path()}: {e}") from e


def load_run(run_id=None):
    active = load_active()
    if active and (not run_id or active.get("run_id") == run_id):
        return active
    if not run_id:
        return None
    try:
        with open(history_path(), "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("run_id") == run_id:
            return item
    return None


def save_active(data):
    ensure_state_dir()
    tmp = active_path() + f".{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_json_record(data), f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, active_path())


def append_history(data):
    ensure_state_dir()
    path = history_path()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(_json_record(data), ensure_ascii=False) + "\n")
        f.flush()
    os.chmod(path, 0o600)


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@contextlib.contextmanager
def single_session_lock():
    """Hold the machine-level lock non-blockingly until the context exits."""
    ensure_state_dir()
    f = open(lock_path(), "a+")
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            active = load_active()
            hint = active.get("run_id", "?") if active else "unknown"
            raise RuntimeError(
                f"an Alignment Session is already running ({hint}); wait, stop it, or let it finish first"
            ) from e
        yield
    finally:
        with contextlib.suppress(Exception):
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()
