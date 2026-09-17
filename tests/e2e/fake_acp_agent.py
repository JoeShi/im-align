"""Transcript-driven fake ACP Agent Backend for the IM Integration layer.

A generalization of the inline FAKE_AGENT in tests/unit/test_acp_incident_replay.py:
instead of hardcoded behavior, this subprocess is driven by a transcript.yaml
file and replays it over line-delimited JSON-RPC on stdio. The Bridge runs
unmodified; only its backend command points here.

Usage: python fake_acp_agent.py <transcript.yaml>

Protocol surface mirrors the measured ACP backends (scripts/im_align/acp/client.py):
  - initialize / session/new / session/load / session/set_config_option answers
    follow the inline FAKE_AGENT;
  - session/prompt runs the next transcript steps: send_text chunks,
    write_artifact (repo-relative real-path rule, same as the fs capability),
    emit_marker (standalone [ALIGNMENT_COMPLETE] line), end_turn (finish the
    current prompt and resume remaining steps on the next Turn),
    request_permission
    (blocking, verifies the declared expected decision when given),
    hang_until_cancel (never answers the prompt until session/cancel),
    exit (terminates without answering);
  - after the transcript is exhausted the prompt ends with stopReason end_turn.

Deterministic failures (path escape, decision mismatch) exit non-zero and say
why on stderr; stdout carries protocol traffic only.
"""

import json
import sys
from pathlib import Path

try:
    from .scenario import ScenarioError, load_transcript
except ImportError:  # Running as a plain subprocess: python fake_acp_agent.py ...
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from scenario import ScenarioError, load_transcript

EXIT_TRANSCRIPT = 2  # invalid transcript or refused artifact path
EXIT_DECISION_MISMATCH = 3  # permission outcome differed from the declared decision

COMPLETION_MARKER = "[ALIGNMENT_COMPLETE]"

_stop_reason = "end_turn"
_session_id = "fake-session"
_perm_seq = 0


def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(msg):
    sys.stderr.write(f"fake-agent: {msg}\n")
    sys.stderr.flush()


def send_chunk(text):
    send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": _session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                },
            },
        }
    )


def workspace_path(raw_path):
    """Resolve raw_path against cwd with symlinks resolved; refuse escapes.

    Same rule as AcpClient._workspace_path so negative-path Scenarios exercise
    the repository boundary instead of a weaker fake.
    """
    workspace = Path.cwd().resolve()
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(workspace)
    except ValueError:
        raise ValueError(f"refusing artifact path outside repository: {raw_path}")
    return candidate


_input_queue = None


def start_reader():
    """Start the single stdin reader thread feeding a queue.

    One reader prevents a timed-out read from leaking a blocked thread that
    would consume the next line. EOF is pushed as None.
    """
    import queue
    import threading

    global _input_queue
    _input_queue = queue.Queue()

    def read():
        for line in sys.stdin:
            line = line.strip()
            if line:
                _input_queue.put(json.loads(line))
        _input_queue.put(None)

    threading.Thread(target=read, daemon=True).start()
    return _input_queue


def read_line(timeout_seconds=None):
    """Read one protocol message; return parsed msg, None on EOF/timeout.

    The ACP client answers permission requests from a dispatch thread while we
    block here, so a plain blocking read is sufficient for transcripts.
    """
    try:
        return _input_queue.get(timeout=timeout_seconds)
    except Exception:
        return None


def next_perm_id():
    global _perm_seq
    _perm_seq += 1
    return f"perm-{_perm_seq}"


def wait_permission_response(perm_id, timeout_seconds=120):
    while True:
        msg = read_line(timeout_seconds)
        if msg is None:
            return None
        if msg.get("id") == perm_id and "method" not in msg:
            return msg
        # Unrelated traffic while blocked (e.g. a duplicate cancel); ignore.
        log(f"ignored message while waiting for {perm_id}: {msg}")


def outcome_kind(response, options):
    """Map a permission response to a normalized PermissionDecision.kind."""
    if response is None:
        return "cancel"
    outcome = ((response.get("result") or {}).get("outcome") or {})
    if outcome.get("outcome") != "selected":
        return "cancel"
    option_id = outcome.get("optionId")
    for opt in options:
        if opt.get("optionId") == option_id:
            return opt.get("kind", "")
    return "cancel"


def run_step(step):
    """Run one transcript step and return a control action when needed."""
    global _stop_reason
    if step.type == "send_text":
        send_chunk(step.text)
    elif step.type == "end_turn":
        return "end_turn"
    elif step.type == "write_artifact":
        target = workspace_path(step.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(step.content, encoding="utf-8")
    elif step.type == "emit_marker":
        send_chunk(f"\n{COMPLETION_MARKER} {step.path}\n")
    elif step.type == "request_permission":
        perm_id = next_perm_id()
        send(
            {
                "jsonrpc": "2.0",
                "id": perm_id,
                "method": "session/request_permission",
                "params": {
                    "sessionId": _session_id,
                    "toolCall": {
                        "toolCallId": f"tc-{_perm_seq}",
                        "title": step.title,
                        "kind": step.kind,
                    },
                    "options": step.options,
                },
            }
        )
        response = wait_permission_response(perm_id)
        kind = outcome_kind(response, step.options)
        if response is None:
            log(f"permission request {perm_id} never received a response; treating as cancel")
        if step.decision and kind != step.decision:
            log(
                f"expected permission decision {step.decision!r} but got {kind!r} "
                f"for {perm_id}"
            )
            sys.exit(EXIT_DECISION_MISMATCH)
    elif step.type == "hang_until_cancel":
        _stop_reason = "cancelled"
        # Never answer the pending prompt; wait for session/cancel.
        while True:
            msg = read_line()
            if msg is None:
                log("stdin closed while hanging; exiting")
                sys.exit(0)
            if msg.get("method") == "session/cancel":
                return "hang"
    elif step.type == "exit":
        sys.exit(step.exit_code)
    return None


def handle_prompt(msg, steps_iter):
    global _stop_reason
    prompt_id = msg["id"]
    _stop_reason = "end_turn"
    for step in steps_iter:
        action = run_step(step)
        if action == "end_turn":
            break
        if action == "hang":
            # run_step consumed session/cancel while hanging; answer the prompt
            # exactly like the inline FAKE_AGENT does.
            send({"jsonrpc": "2.0", "id": prompt_id, "result": {"stopReason": "cancelled"}})
            return
    send({"jsonrpc": "2.0", "id": prompt_id, "result": {"stopReason": _stop_reason}})


def main(argv):
    if len(argv) != 2:
        log("usage: fake_acp_agent.py <transcript.yaml>")
        return EXIT_TRANSCRIPT
    try:
        transcript = load_transcript(argv[1])
    except (ScenarioError, OSError) as e:
        log(f"cannot load transcript: {e}")
        return EXIT_TRANSCRIPT

    steps_iter = iter(transcript.steps)
    pending_prompt_id = None
    start_reader()

    # The reader thread feeds the queue; drain it here so permission responses
    # blocked in run_step and session/cancel during hang_until_cancel arrive.
    while True:
        msg = read_line()
        if msg is None:
            break
        method = msg.get("method", "")
        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {
                        "protocolVersion": 1,
                        "capabilities": {"prompt": True},
                        "configOptions": [],
                    },
                }
            )
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": _session_id}})
        elif method == "session/load":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": _session_id}})
        elif method == "session/set_config_option":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        elif method == "session/prompt":
            pending_prompt_id = msg["id"]
            try:
                handle_prompt(msg, steps_iter)
            except ValueError as e:
                log(str(e))
                return EXIT_TRANSCRIPT
            pending_prompt_id = None
        elif method == "session/cancel":
            if pending_prompt_id is not None:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": pending_prompt_id,
                        "result": {"stopReason": "cancelled"},
                    }
                )
                pending_prompt_id = None
        # Responses to session/request_permission carry an id and no method;
        # they are consumed inside run_step, so the main loop ignores them.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
