"""Minimal ACP (Agent Client Protocol) client.

NDJSON + JSON-RPC 2.0 over stdio, protocol version 1. The implementation
maps directly to measured behavior from legacy/go/internal/acp/acp.go
(ADR-0001):

  - session/prompt blocks until the Turn ends and returns stopReason. Events
    aggregate only agent_message_chunk; agent_thought_chunk, about 95% of the
    stream, and all other types are discarded.
  - Permission semantics match options[].kind
    (allow_once/allow_always/reject_*), never optionId strings. opencode uses
    custom once/always/reject values, while kiro-cli v3 uses
    accept/always-accept/reject/always-reject. kimi-code 0.42.0 does not
    trigger request_permission under host default permission; asking is decided
    by permission in host ~/.kimi-code/config.toml, like opencode.json.
    initialize returns no configOptions, so ACP cannot switch models per Session.
  - Permission requests can block a Turn indefinitely, so they need a timeout
    that returns cancelled. The watchdog may pause while waiting for Approval.
  - After rejection, the Agent may end the Turn silently in opencode tests,
    while kiro-cli emits explanatory text. The client must synthesize an
    "operation rejected" explanation from failed tool_call_update.
  - Implements fs.readTextFile/writeTextFile capability and does not declare
    terminal capability.

The handwritten client uses only the standard library. Its protocol surface is
limited to the bullets above and mirrors the Go implementation for auditing.
The official Python SDK, agent-client-protocol, can be considered later.
"""

import json
import logging
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from .watchdog import TurnWatchdog

log = logging.getLogger("im_align.acp")


class AcpError(Exception):
    pass


@dataclass
class Rejection:
    tool_call_id: str
    title: str
    kind: str
    reason: str


@dataclass
class TurnResult:
    text: str
    stop_reason: str
    rejections: list = field(default_factory=list)

    def display_text(self):
        """Agent output plus rejection notes; the Agent may be silent after rejection."""
        parts = [self.text.rstrip("\n")]
        for rej in self.rejections:
            line = f"⚠️ Operation was rejected: {rej.title}"
            if rej.reason:
                line += f" ({rej.reason})"
            parts.append(line)
        return "\n\n".join(p for p in parts if p)


@dataclass
class PermissionOption:
    option_id: str
    name: str
    kind: str


@dataclass
class PermissionRequest:
    session_id: str
    tool_call_id: str
    title: str
    kind: str
    raw_input: object
    options: list


@dataclass
class PermissionDecision:
    kind: str  # allow_once/allow_always/reject_once/reject_always/cancel


CANCEL = "cancel"


class AcpClient:
    def __init__(
        self,
        argv,
        cwd,
        on_permission=None,
        on_stderr=None,
        approval_timeout=timedelta(minutes=10),
        turn_timeout=timedelta(minutes=5),
    ):
        self._argv = argv
        self._cwd = cwd
        self._on_permission = on_permission
        self._on_stderr = on_stderr
        self._approval_timeout = approval_timeout
        self._turn_timeout = turn_timeout
        self._model = ""

        self._proc = None
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending = {}
        self._dispatch = ThreadPoolExecutor(max_workers=8, thread_name_prefix="acp")
        self._closing = threading.Event()

        self._turn_lock = threading.Lock()
        self._agg_lock = threading.Lock()
        self._in_turn = False
        self._chunks = []
        self._rejections = []
        self._tool_calls = {}
        self._watchdog = None

    def set_model(self, model):
        self._model = model or ""

    # ---- Process and handshake ----

    def start(self):
        log.info("starting Agent Backend subprocess: %s", " ".join(self._argv))
        self._proc = subprocess.Popen(
            self._argv,
            cwd=self._cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        return self._request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": False,
                },
                "clientInfo": {"name": "im-align", "version": "0.2.0"},
            },
            timeout=60,
        )

    def new_session(self):
        resp = self._request(
            "session/new", {"cwd": self._cwd, "mcpServers": []}, timeout=90
        )
        session_id = resp["sessionId"]
        self._apply_model(session_id, resp.get("configOptions") or [])
        return session_id

    def load_session(self, session_id):
        # During resume, history replays as session/update and is discarded while in_turn=False.
        self._request(
            "session/load",
            {"sessionId": session_id, "cwd": self._cwd, "mcpServers": []},
            timeout=120,
        )

    def _fail_pending(self, reason: str):
        """Wake all local pending RPCs when closing or when the Agent process exits."""
        with self._id_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for slot in pending:
            slot["error"] = reason
            slot["event"].set()

    def close(self):
        self._closing.set()
        if self._watchdog:
            self._watchdog.stop()
        # Wake all local pending RPCs so closing does not wait for full Approval timeouts.
        self._fail_pending("ACP client is closed")
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
        self._dispatch.shutdown(wait=False, cancel_futures=True)

    def _apply_model(self, session_id, config_options):
        """opencode switches model per Session through session/set_config_option.

        The measured optionId is model. trae-cli injects model through argv in
        config.backend_argv, so this method does not handle it. When model is
        not explicitly configured, the Agent's own selection is preserved.
        """
        if not self._model:
            return
        if not any(str(o.get("id")) == "model" for o in config_options):
            log.warning("Agent did not provide model configOption; cannot switch to %s", self._model)
            return
        try:
            self._request(
                "session/set_config_option",
                {"sessionId": session_id, "optionId": "model", "value": self._model},
                timeout=15,
            )
            log.info("switched Session model: %s", self._model)
        except Exception as e:  # Model switch failure does not block Alignment.
            log.warning("failed to switch model %s: %s", self._model, e)

    # ---- turn ----

    def prompt(self, session_id, text):
        with self._turn_lock:
            self._begin_turn()
            cancel_event = threading.Event()
            self._watchdog = TurnWatchdog(self._turn_timeout, cancel_event.set)
            try:
                slot = self._send_request(
                    "session/prompt",
                    {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": text}],
                    },
                )
                cancelled_by_watchdog = False
                while True:
                    if slot["event"].wait(timeout=1):
                        break
                    if cancel_event.is_set():
                        cancelled_by_watchdog = True
                        log.warning(
                            "Turn timed out; budget %s excludes Approval wait, sending session/cancel",
                            self._turn_timeout,
                        )
                        self._notify("session/cancel", {"sessionId": session_id})
                        if not slot["event"].wait(timeout=30):
                            raise AcpError("Turn timed out and did not end within 30s after session/cancel")
                        break
                if slot["error"] is not None:
                    raise AcpError(f"acp session/prompt: {slot['error']}")
                stop_reason = (slot["result"] or {}).get("stopReason", "")
                if cancelled_by_watchdog and stop_reason != "cancelled":
                    raise AcpError(
                        f"Turn timed out; budget {self._turn_timeout} excludes Approval wait"
                    )
                return self._end_turn(stop_reason)
            finally:
                self._watchdog.stop()
                self._watchdog = None

    def cancel_session(self, session_id):
        """Ask the Agent to cancel the current Turn for bounded terminal stop."""
        if self._proc and self._proc.poll() is None:
            self._notify("session/cancel", {"sessionId": session_id})

    # ---- JSON-RPC frame IO ----

    def _read_stdout(self):
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("failed to parse protocol line; dropping: %.200s", line)
                    continue
                # Notifications and responses must preserve stdout arrival order;
                # otherwise prompt response may clear _in_turn before prior chunks.
                # Only server requests that may block for human Approval go to the pool.
                if "method" in msg and "id" in msg:
                    self._dispatch.submit(self._on_message, msg)
                else:
                    self._on_message(msg)
        except Exception as e:
            log.debug("stdout read loop ended: %s", e)
        # After the Agent process exits, no response will arrive. Wake pending
        # slots or session/prompt can wait forever; measured with kimi acp on
        # 2026-09-13 when the subprocess died mid-Turn and worker hung until stop.
        self._fail_pending("Agent process exited before responding")

    def _read_stderr(self):
        for line in self._proc.stderr:
            line = line.rstrip("\n")
            if self._on_stderr:
                self._on_stderr(line)
            else:
                log.debug("[agent stderr] %s", line)

    def _on_message(self, msg):
        if "method" in msg and "id" in msg:
            self._on_server_request(msg)
        elif "method" in msg:
            self._on_notification(msg)
        elif "id" in msg:
            with self._id_lock:
                slot = self._pending.pop(msg["id"], None)
            if slot:
                if "error" in msg:
                    slot["error"] = json.dumps(msg["error"], ensure_ascii=False)
                else:
                    slot["result"] = msg.get("result")
                slot["event"].set()

    def _on_notification(self, msg):
        if msg["method"] != "session/update":
            return
        update = (msg.get("params") or {}).get("update") or {}
        kind = update.get("sessionUpdate")
        with self._agg_lock:
            if kind == "agent_message_chunk":
                if self._in_turn:
                    text = (update.get("content") or {}).get("text") or ""
                    self._chunks.append(text)
            elif kind == "tool_call":
                self._tool_calls[str(update.get("toolCallId"))] = {
                    "title": update.get("title", ""),
                    "kind": str(update.get("kind", "")),
                }
            elif kind == "tool_call_update":
                self._track_tool_call_update(update)
            # agent_thought_chunk, about 95% of events, and all other updates are ignored.

    def _track_tool_call_update(self, update):
        tc_id = str(update.get("toolCallId"))
        info = self._tool_calls.setdefault(tc_id, {"title": "", "kind": ""})
        if update.get("title") is not None:
            info["title"] = update["title"]
        if update.get("kind") is not None:
            info["kind"] = str(update["kind"])
        if not self._in_turn or update.get("status") != "failed":
            return
        title = info["title"] or tc_id
        self._rejections.append(
            Rejection(
                tool_call_id=tc_id,
                title=title,
                kind=info["kind"],
                reason=_extract_tool_error(update),
            )
        )

    def _on_server_request(self, msg):
        method = msg["method"]
        params = msg.get("params") or {}
        try:
            if method == "session/request_permission":
                result = self._handle_request_permission(params)
            elif method == "fs/read_text_file":
                result = self._handle_read_text(params)
            elif method == "fs/write_text_file":
                result = self._handle_write_text(params)
            else:
                self._respond_error(msg["id"], -32601, f"method not supported: {method}")
                return
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        except Exception:
            log.exception("failed to handle server request %s", method)
            self._respond_error(msg["id"], 1, "internal error")

    def _handle_request_permission(self, p):
        options = [
            PermissionOption(
                option_id=str(o.get("optionId", "")),
                name=o.get("name", ""),
                kind=str(o.get("kind", "")),
            )
            for o in (p.get("options") or [])
        ]
        if not options:
            return _cancelled_outcome()
        if self._on_permission is None:
            # auto_allow prefers allow_always, then allow_once, matching by kind semantics.
            return _select_by_kinds(options, "allow_always", "allow_once")

        tool_call = p.get("toolCall") or {}
        req = PermissionRequest(
            session_id=str(p.get("sessionId", "")),
            tool_call_id=str(tool_call.get("toolCallId", "")),
            title=tool_call.get("title") or "",
            kind=str(tool_call.get("kind") or ""),
            raw_input=tool_call.get("rawInput"),
            options=options,
        )

        done = threading.Event()
        box = {}

        def run():
            try:
                box["decision"] = self._on_permission(req)
            except Exception as e:
                log.warning("Approval callback failed: %s", e)
                box["decision"] = PermissionDecision(CANCEL)
            finally:
                done.set()

        if self._watchdog:
            self._watchdog.pause()
        threading.Thread(target=run, daemon=True).start()
        deadline = time.monotonic() + self._approval_timeout.total_seconds()
        resolved = False
        while not self._closing.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if done.wait(timeout=min(0.2, remaining)):
                resolved = True
                break
        if self._watchdog:
            self._watchdog.resume()

        if not resolved:
            if self._closing.is_set():
                log.info("Approval %r cancelled because ACP client is closing", req.title)
            else:
                log.warning("Approval %r timed out after %s; returning cancelled", req.title, self._approval_timeout)
            return _cancelled_outcome()
        decision = box.get("decision") or PermissionDecision(CANCEL)
        if decision.kind == CANCEL:
            return _cancelled_outcome()
        return _select_by_kinds(options, decision.kind)

    def _workspace_path(self, raw_path):
        """Constrain ACP file requests to the launch-time repository and resolve symlinks."""
        workspace = Path(self._cwd).resolve()
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        candidate = candidate.resolve(strict=False)
        try:
            candidate.relative_to(workspace)
        except ValueError as e:
            raise AcpError(f"refusing access outside repository: {raw_path}") from e
        return candidate

    def _handle_read_text(self, p):
        path = self._workspace_path(p["path"])
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        line = p.get("line")
        limit = p.get("limit")
        if line is not None or limit is not None:
            lines = content.split("\n")
            start = min(line - 1, len(lines)) if line and line > 1 else 0
            end = len(lines)
            if limit and limit > 0 and start + limit < end:
                end = start + limit
            content = "\n".join(lines[start:end])
        return {"content": content}

    def _handle_write_text(self, p):
        path = self._workspace_path(p["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(p["content"])
        os.chmod(path, 0o644)
        return {}

    # ---- Turn aggregation ----

    def _begin_turn(self):
        with self._agg_lock:
            self._in_turn = True
            self._chunks = []
            self._rejections = []

    def _end_turn(self, stop_reason):
        with self._agg_lock:
            self._in_turn = False
            return TurnResult(
                text="".join(self._chunks),
                stop_reason=stop_reason,
                rejections=list(self._rejections),
            )

    # ---- Low-level RPC ----

    def _send_request(self, method, params):
        with self._id_lock:
            self._next_id += 1
            msg_id = self._next_id
        slot = {"id": msg_id, "event": threading.Event(), "result": None, "error": None}
        self._pending[msg_id] = slot
        self._send(
            {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
        )
        return slot

    def _request(self, method, params, timeout):
        slot = self._send_request(method, params)
        if not slot["event"].wait(timeout=timeout):
            with self._id_lock:
                self._pending.pop(slot["id"], None)
            raise AcpError(f"acp {method} did not respond within {timeout}s")
        if slot["error"] is not None:
            raise AcpError(f"acp {method}: {slot['error']}")
        return slot["result"]

    def _notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _respond_error(self, msg_id, code, message):
        self._send(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": code, "message": message},
            }
        )

    def _send(self, msg):
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        if not self._proc or not self._proc.stdin:
            raise AcpError("backend subprocess is not started")
        with self._write_lock:
            try:
                self._proc.stdin.write(data.decode("utf-8"))
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise AcpError(f"failed to write to backend subprocess: {e}") from e


def _extract_tool_error(update):
    """Extract error text from failed tool_call_update.

    Backends place rejection reasons in different measured locations; see the
    ADR-0003 revalidation list:

    - opencode: rawOutput.error string.
    - kiro-cli v3 engine: rawOutput.message string, for example
      "The user rejected this tool call."
    - kiro-cli v2 engine: text blocks in content[], for example
      "User denied tool execution."

    Probe in that order and return an empty string when none are found.
    """
    raw = update.get("rawOutput")
    if isinstance(raw, dict):
        for key in ("error", "message"):
            if isinstance(raw.get(key), str):
                return raw[key]
    for item in update.get("content") or []:
        if not isinstance(item, dict):
            continue
        inner = item.get("content")
        if isinstance(inner, dict) and isinstance(inner.get("text"), str) and inner["text"]:
            return inner["text"]
        # Support backends that flatten text blocks as {"type": "text", "text": ...}.
        if isinstance(item.get("text"), str) and item["text"]:
            return item["text"]
    return ""


def _select_by_kinds(options, *kinds):
    """Find the first option matching kind semantics; return cancelled on misses."""
    for kind in kinds:
        for opt in options:
            if opt.kind == kind:
                return _selected_outcome(opt.option_id)
    return _cancelled_outcome()


def _selected_outcome(option_id):
    return {"outcome": {"outcome": "selected", "optionId": option_id}}


def _cancelled_outcome():
    return {"outcome": {"outcome": "cancelled"}}
