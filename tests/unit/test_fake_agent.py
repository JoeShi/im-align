"""Fake ACP backend tests: raw JSON-RPC protocol order and AcpClient integration.

Spawns fake_acp_agent.py as a real subprocess and feeds it line-delimited
JSON-RPC, mirroring the style of tests/unit/test_acp_incident_replay.py but with
transcript-driven behavior. No network, no Feishu.
"""

import json
import queue
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path

from scripts.im_align.acp.client import AcpClient, PermissionDecision
from tests.e2e.fake_acp_agent import EXIT_DECISION_MISMATCH, EXIT_TRANSCRIPT
from tests.e2e.scenario import load_transcript

FAKE_AGENT = Path(__file__).resolve().parents[1] / "e2e" / "fake_acp_agent.py"


class AgentProcess:
    """Line-delimited JSON-RPC peer for the fake agent subprocess.

    A reader thread feeds a queue because select() on a buffered TextIOWrapper
    misses data already pulled into the userspace buffer.
    """

    def __init__(self, transcript: Path, cwd: Path):
        self.proc = subprocess.Popen(
            [sys.executable, str(FAKE_AGENT), str(transcript)],
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.stdout_queue = queue.Queue()
        self.stderr_lines = []
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._next_id = 0

    def _pump(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                self.stdout_queue.put(json.loads(line))
        self.stdout_queue.put(None)

    def _drain_stderr(self):
        for line in self.proc.stderr:
            self.stderr_lines.append(line)

    def request(self, method: str, params: dict, timeout: float = 5.0) -> dict:
        self._next_id += 1
        msg_id = self._next_id
        self.send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        while True:
            msg = self.read_msg(timeout)
            if msg is None:
                raise AssertionError(
                    f"no response to {method}; stderr={''.join(self.stderr_lines)!r}"
                )
            if msg.get("id") == msg_id and "method" not in msg:
                return msg

    def send(self, msg: dict):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def read_msg(self, timeout: float = 5.0) -> dict | None:
        try:
            return self.stdout_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def wait(self, timeout: float = 5.0) -> int:
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise AssertionError(
                f"agent did not exit; stderr={''.join(self.stderr_lines)!r}"
            ) from None
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                pipe.close()
            except Exception:
                pass
        return code


def write_transcript(tmp: Path, steps: str) -> Path:
    path = tmp / "transcript.yaml"
    path.write_text(textwrap.dedent(steps), encoding="utf-8")
    load_transcript(path)  # fail fast on invalid fixtures
    return path


class FakeAgentProtocolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.workspace = tmp / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _agent(self, steps: str) -> AgentProcess:
        transcript = write_transcript(Path(self._tmp.name), steps)
        return AgentProcess(transcript, self.workspace)

    def test_handshake_answers_like_inline_fake_agent(self):
        agent = self._agent("steps:\n  - type: send_text\n    text: ok\n")
        reply = agent.request(
            "initialize",
            {"protocolVersion": 1, "clientCapabilities": {"fs": {}}, "clientInfo": {}},
        )
        self.assertEqual(
            reply["result"],
            {"protocolVersion": 1, "capabilities": {"prompt": True}, "configOptions": []},
        )
        reply = agent.request("session/new", {"cwd": str(self.workspace), "mcpServers": []})
        self.assertEqual(reply["result"], {"sessionId": "fake-session"})
        agent.proc.stdin.close()
        self.assertEqual(agent.wait(), 0)

    def test_happy_path_steps_in_order(self):
        agent = self._agent(
            """
            steps:
              - type: send_text
                text: "working on it\\n"
              - type: write_artifact
                path: docs/specs/todo.md
                content: "# Spec\\nbody\\n"
              - type: emit_marker
                path: docs/specs/todo.md
            """
        )
        agent.request("initialize", {})
        agent.request("session/new", {})
        agent.send(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "session/prompt",
                "params": {"sessionId": "fake-session", "prompt": []},
            }
        )
        chunk = agent.read_msg()
        self.assertEqual(chunk["method"], "session/update")
        update = chunk["params"]["update"]
        self.assertEqual(update["sessionUpdate"], "agent_message_chunk")
        self.assertEqual(update["content"]["text"], "working on it\n")
        marker = agent.read_msg()
        self.assertIn(
            "\n[ALIGNMENT_COMPLETE] docs/specs/todo.md\n",
            marker["params"]["update"]["content"]["text"],
        )
        done = agent.read_msg()
        self.assertEqual(done, {"jsonrpc": "2.0", "id": 9, "result": {"stopReason": "end_turn"}})
        self.assertEqual(
            (self.workspace / "docs" / "specs" / "todo.md").read_text(encoding="utf-8"),
            "# Spec\nbody\n",
        )
        agent.proc.stdin.close()
        self.assertEqual(agent.wait(), 0)

    def test_permission_request_verifies_declared_decision(self):
        agent = self._agent(
            """
            steps:
              - type: request_permission
                title: write spec
                kind: edit
                options:
                  - optionId: once
                    name: Allow once
                    kind: allow_once
                decision: allow_once
            """
        )
        agent.request("initialize", {})
        agent.request("session/new", {})
        agent.send(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "session/prompt",
                "params": {"sessionId": "fake-session", "prompt": []},
            }
        )
        req = agent.read_msg()
        self.assertEqual(req["method"], "session/request_permission")
        self.assertEqual(req["params"]["toolCall"]["title"], "write spec")
        agent.send(
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": "once"}},
            }
        )
        done = agent.read_msg()
        self.assertEqual(done["result"], {"stopReason": "end_turn"})
        agent.proc.stdin.close()
        self.assertEqual(agent.wait(), 0)

    def test_permission_decision_mismatch_exits_nonzero(self):
        agent = self._agent(
            """
            steps:
              - type: request_permission
                title: write spec
                kind: edit
                options:
                  - optionId: once
                    name: Allow once
                    kind: allow_once
                decision: allow_once
            """
        )
        agent.request("initialize", {})
        agent.request("session/new", {})
        agent.send(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "session/prompt",
                "params": {"sessionId": "fake-session", "prompt": []},
            }
        )
        req = agent.read_msg()
        agent.send(
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"outcome": {"outcome": "cancelled"}},
            }
        )
        agent.proc.stdin.close()
        self.assertEqual(agent.wait(), EXIT_DECISION_MISMATCH)

    def test_hang_until_cancel_waits_for_session_cancel(self):
        agent = self._agent(
            """
            steps:
              - type: hang_until_cancel
            """
        )
        agent.request("initialize", {})
        agent.request("session/new", {})
        agent.send(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "session/prompt",
                "params": {"sessionId": "fake-session", "prompt": []},
            }
        )
        self.assertIsNone(agent.read_msg(timeout=0.5))
        agent.send(
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "fake-session"}}
        )
        done = agent.read_msg()
        self.assertEqual(done, {"jsonrpc": "2.0", "id": 7, "result": {"stopReason": "cancelled"}})
        agent.proc.stdin.close()
        self.assertEqual(agent.wait(), 0)

    def test_exit_step_terminates_without_answering(self):
        agent = self._agent(
            """
            steps:
              - type: exit
                exit_code: 7
            """
        )
        agent.request("initialize", {})
        agent.request("session/new", {})
        agent.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {"sessionId": "fake-session", "prompt": []},
            }
        )
        self.assertEqual(agent.wait(), 7)

    def test_symlink_escape_rejected(self):
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (self.workspace / "link").symlink_to(outside, target_is_directory=True)
        agent = self._agent(
            """
            steps:
              - type: write_artifact
                path: link/evil.md
                content: "nope"
            """
        )
        agent.request("initialize", {})
        agent.request("session/new", {})
        agent.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {"sessionId": "fake-session", "prompt": []},
            }
        )
        agent.proc.stdin.close()
        self.assertEqual(agent.wait(), EXIT_TRANSCRIPT)
        self.assertFalse((outside / "evil.md").exists())

    def test_end_turn_resumes_remaining_steps_on_later_prompt(self):
        agent = self._agent(
            """
            participant_replies:
              - fixed reply
            steps:
              - type: send_text
                text: first turn
              - type: end_turn
              - type: send_text
                text: second turn
            """
        )
        agent.request("initialize", {})
        agent.request("session/new", {})
        for prompt_id, expected_text in ((41, "first turn"), (42, "second turn"), (43, None)):
            agent.send(
                {
                    "jsonrpc": "2.0",
                    "id": prompt_id,
                    "method": "session/prompt",
                    "params": {"sessionId": "fake-session", "prompt": []},
                }
            )
            if expected_text is not None:
                chunk = agent.read_msg()
                self.assertEqual(chunk["params"]["update"]["content"]["text"], expected_text)
            done = agent.read_msg()
            self.assertEqual(done["result"], {"stopReason": "end_turn"})
        agent.proc.stdin.close()
        self.assertEqual(agent.wait(), 0)


class FakeAgentAcpClientTests(unittest.TestCase):
    """Drive the fake agent through the real AcpClient, like the incident replay."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.workspace = tmp / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _client(self, transcript_steps: str, decide_permission=None) -> AcpClient:
        transcript = write_transcript(Path(self._tmp.name), transcript_steps)
        return AcpClient(
            [sys.executable, str(FAKE_AGENT), str(transcript)],
            cwd=str(self.workspace),
            spec_root="docs/specs",
            decide_permission=decide_permission,
            turn_timeout=timedelta(seconds=2),
        )

    def test_prompt_aggregates_marker_and_artifact(self):
        client = self._client(
            """
            steps:
              - type: send_text
                text: "based on our discussion:\n"
              - type: write_artifact
                path: docs/specs/todo.md
                content: "# TODO Spec\n"
              - type: emit_marker
                path: docs/specs/todo.md
            """
        )
        try:
            client.start()
            session_id = client.new_session()
            turn = client.prompt(session_id, "align please")
        finally:
            client.close()
        self.assertEqual(turn.stop_reason, "end_turn")
        self.assertIn("[ALIGNMENT_COMPLETE] docs/specs/todo.md", turn.text)
        self.assertTrue((self.workspace / "docs" / "specs" / "todo.md").is_file())

    def test_prompt_cancels_when_agent_hangs(self):
        client = self._client("steps:\n  - type: hang_until_cancel\n")
        try:
            client.start()
            session_id = client.new_session()
            start = time.monotonic()
            turn = client.prompt(session_id, "hang now")
            elapsed = time.monotonic() - start
        finally:
            client.close()
        self.assertEqual(turn.stop_reason, "cancelled")
        self.assertLess(elapsed, 10.0, f"prompt hung for {elapsed:.1f}s waiting for cancel")

    def test_permission_roundtrip_matches_callback_decision(self):
        client = self._client(
            """
            steps:
              - type: request_permission
                title: write spec
                kind: edit
                options:
                  - optionId: once
                    name: Allow once
                    kind: allow_once
                  - optionId: always
                    name: Allow always
                    kind: allow_always
                decision: allow_once
            """,
            decide_permission=lambda req: PermissionDecision("allow_once"),
        )
        try:
            client.start()
            session_id = client.new_session()
            turn = client.prompt(session_id, "write the spec")
        finally:
            client.close()
        self.assertEqual(turn.stop_reason, "end_turn")


if __name__ == "__main__":
    unittest.main()
