"""Incident replay for Issue #13 at the AcpClient seam.

A fake Agent Backend (line-delimited JSON-RPC over stdio) replays the live
failure timeline of run-1789439169-73ee6b at small scale: prompt -> permission
request -> approval decision -> agent never answers the prompt. The invariant:
client.prompt() must still terminate with a cancelled Turn within the remaining
working budget plus grace, no matter what the agent does after the Approval.
"""

import json
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import timedelta
from pathlib import Path

from scripts.im_align.acp.client import AcpClient, PermissionDecision

FAKE_AGENT = textwrap.dedent(
    """
    import json
    import sys
    import threading

    pending_prompt_id = None

    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\\n")
        sys.stdout.flush()

    def ask_permission():
        # Give the client a moment to settle into the Turn before requesting.
        threading.Timer(0.1, lambda: send({
            "jsonrpc": "2.0",
            "id": "perm-1",
            "method": "session/request_permission",
            "params": {
                "sessionId": "fake-session",
                "toolCall": {"toolCallId": "tc-1", "title": "fake tool", "kind": "bash"},
                "options": [{"optionId": "once", "name": "Allow once", "kind": "allow_once"}],
            },
        })).start()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method", "")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": 1,
                "capabilities": {"prompt": True},
                "configOptions": [],
            }})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": "fake-session"}})
        elif method == "session/prompt":
            pending_prompt_id = msg["id"]
            ask_permission()
            # After the permission decision the agent hangs and never answers
            # the prompt, exactly like the live incident.
        elif method == "session/cancel":
            if pending_prompt_id is not None:
                send({"jsonrpc": "2.0", "id": pending_prompt_id,
                      "result": {"stopReason": "cancelled"}})
        # Responses to session/request_permission carry an id and no method;
        # the client consumes them, so the agent ignores them here.
    """
)


class ApprovalThenHangTests(unittest.TestCase):
    def test_prompt_terminates_when_agent_hangs_after_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = Path(tmp) / "fake_agent.py"
            agent.write_text(FAKE_AGENT, encoding="utf-8")
            client = AcpClient(
                [sys.executable, str(agent)],
                cwd=tmp,
                on_permission=lambda req: PermissionDecision("allow_once"),
                turn_timeout=timedelta(seconds=0.8),
                approval_timeout=timedelta(seconds=5),
            )
            try:
                client.start()
                session_id = client.new_session()
                start = time.monotonic()
                turn = client.prompt(session_id, "hello")
                elapsed = time.monotonic() - start
            finally:
                client.close()
        self.assertEqual(turn.stop_reason, "cancelled")
        # 0.8s budget + 0.1s pre-permission work + 1s poll granularity + grace.
        self.assertLess(elapsed, 4.0, f"prompt hung for {elapsed:.1f}s after approval resume")


if __name__ == "__main__":
    unittest.main()
