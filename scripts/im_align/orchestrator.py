"""Session orchestration: IM events -> debounce batching -> ACP Turn -> completion detection.

Behavior matches the interaction model from legacy/go/internal/lark/bot.go:
  - root message opens the Session; Thread replies get emoji ack + debounce;
  - Turn start sends a blue thinking card, and Turn end patches it with output;
  - completion signal is strictly validated in completion.py; Approval uses Provider card callbacks;
  - idle timeout closes the run, and terminal state feeds host Agent resume handoff.
"""

import logging
import threading
import time
from datetime import datetime

from . import cards, state
from .acp.client import AcpClient, PermissionDecision
from .completion import parse_completion
from .config import POLICY_AUTO_ALLOW
from .textutil import chunk_runes

log = logging.getLogger("im_align.orchestrator")

COMPLETION_HINT = """

[SYSTEM CONTRACT] When you decide Alignment is complete, finish with these exact steps:
1. Write the Spec into the current repository; choose the repository-relative path and format, for example SPEC.md, and confirm the file was actually written.
2. On the final line of your reply, output exactly one standalone line: [ALIGNMENT_COMPLETE] <repository-relative path of that file>
3. Do not include [ALIGNMENT_COMPLETE] anywhere else in the reply, including backtick references, code blocks, or narrative mentions.
4. Historical Spec files may already exist in the repository, such as SPEC.md or specs/. They are outputs from previous Alignment Sessions and are unrelated to this Session. Do not treat their existence as completion. This Session's Spec must be actually written or updated by you during this Session."""

ROOT_TEMPLATE = (
    "📋 New Alignment Session\n"
    "skill: {skill}\n"
    "initiator: {initiator}\n"
    "topic: {topic}\n\n"
    "Reply to this message in the Thread to participate in Alignment.\n"
    "💡 Please **mention me** when replying in this Thread; otherwise I cannot receive your message because of Feishu/Lark scope limits."
)

USAGE_TEXT = (
    "This group is handled by im-align Bridge.\n"
    "• A developer starts a Session from the terminal through the im-align Skill.\n"
    "• Reply in the Session Thread and mention me to participate in Alignment.\n"
    "• Mention me with `stop` in the Session Thread to end the Session; initiator only."
)


class StopSession(Exception):
    pass


class IdleTimeout(Exception):
    pass


class Orchestrator:
    def __init__(self, provider, cfg, record):
        """record is the mutable active.json mapping; caller persists terminal state."""
        self.provider = provider
        self.cfg = cfg
        self.record = record
        self._debounce = cfg["debounce_seconds"]
        self._idle = cfg["idle_timeout_seconds"]
        self._pending = []
        self._debounce_deadline = 0.0
        self._turn_busy = threading.Event()
        self._stop_requested = False
        self._stop_deadline = None

    def request_stop(self):
        """Cancel the active Turn and end the main loop after at most 10 seconds."""
        self._stop_requested = True
        if self._stop_deadline is None:
            self._stop_deadline = time.time() + 10
        client = self.record.get("client")
        session_id = self.record.get("acp_session_id")
        if client and session_id:
            try:
                client.cancel_session(session_id)
            except Exception:
                log.exception("failed to cancel current Turn")

    # ---- Approval callback, called by the ACP thread ----

    def on_permission(self, req):
        approval_id = self.provider.next_approval_id()
        handle = self.provider.register_approval(
            approval_id, self.record["initiator_open_id"]
        )
        card = cards.approval_card(
            approval_id, req, self.cfg["approval_timeout_seconds"]
        )
        try:
            card_message_id = self.provider.reply_card(self.record["root_message_id"], card)
        except Exception as e:
            log.warning("failed to send Approval card: %s; auto-cancelling", e)
            self.provider.cancel_approval(approval_id)
            return PermissionDecision("cancel")
        log.info("Approval card sent req=%s title=%r", approval_id, req.title)

        ok = handle.event.wait(timeout=self.cfg["approval_timeout_seconds"] - 5)
        self.provider.cancel_approval(approval_id)
        if not ok:
            self.provider.reply_card(
                self.record["root_message_id"],
                cards.simple_card(
                    "grey",
                    "⏰ Approval Timeout",
                    f"Approval request {req.title!r} timed out and the operation was cancelled.",
                ),
            )
            return PermissionDecision("cancel")

        # Patch the Approval card and leave an audit record in the Thread.
        verb = {
            "allow_once": "✅ Approved for this time",
            "allow_always": "✅ Approved for this Session without asking again",
            "reject_once": "🚫 Rejected for this time",
            "reject_always": "🚫 Rejected always",
        }.get(handle.kind, "Processed: " + handle.kind)
        if handle.message_id or card_message_id:
            try:
                self.provider.patch_card(
                    handle.message_id or card_message_id,
                    cards.simple_card(
                        "grey",
                        "Approval Processed",
                        f"**Operation**: {req.title}\n**Decision**: {verb}",
                    ),
                )
            except Exception as e:
                log.warning("failed to update Approval card: %s", e)
        try:
            self.provider.reply_card(
                self.record["root_message_id"],
                cards.simple_card(
                    "grey",
                    "Approval Record",
                    f"**Operation**: {req.title}\n**Decision**: {verb}",
                ),
            )
        except Exception as e:
            log.warning("failed to send Approval record: %s", e)
        return PermissionDecision(handle.kind)

    # ---- Main loop ----

    def run(self):
        """Block until terminal Session state and return the state string."""
        while True:
            events = self.provider.poll_events(timeout=1)
            now = time.time()
            for ev in events:
                self._handle_event(ev)
                self._debounce_deadline = now + self._debounce
            if self._stop_requested:
                if self._turn_busy.is_set() and (
                    self._stop_deadline is None or now < self._stop_deadline
                ):
                    continue
                return state.STATE_DONE if self.record["state"] == state.STATE_DONE else state.STATE_STOPPED
            if self._pending and not self._turn_busy.is_set() and now >= self._debounce_deadline:
                self._flush()
            if self._check_idle(now):
                return state.STATE_IDLE_TIMEOUT

    def _check_idle(self, now):
        # Do not count idle time during a Turn, with pending input, or before the first Turn.
        if self._turn_busy.is_set() or self._pending or not self.record.get("root_message_id"):
            return False
        return now - self.record["last_activity_at_ts"] > self._idle

    def _handle_event(self, ev):
        if ev.root_id and ev.root_id == self.record["root_message_id"]:
            tokens = ev.text.split()
            if tokens and tokens[0] == "stop":
                if ev.sender_open_id != self.record["initiator_open_id"]:
                    self.provider.reply_card(
                        ev.root_id,
                        cards.simple_card("orange", "Not Authorized", "Only the Session Initiator can stop the Session."),
                    )
                    return
                self.request_stop()
                return
            if not ev.text:
                return
            if self.record["state"] != state.STATE_ACTIVE:
                self.provider.reply_card(
                    ev.root_id,
                    cards.simple_card(
                        "orange",
                        "Cannot Reply",
                        f"This Session has ended with state {self.record['state']}; replies are no longer accepted.",
                    ),
                )
                return
            try:
                self.provider.react_ok(ev.message_id)
            except Exception as e:
                log.warning("emoji ack failed message=%s: %s", ev.message_id, e)
            name = self.provider.user_name(ev.sender_open_id)
            self._pending.append(f"{name}: {ev.text}")
            self._touch()
            return
        if not ev.root_id:
            # Bot mentions outside this Session get usage help.
            try:
                self.provider.send_text(ev.chat_id, USAGE_TEXT)
            except Exception as e:
                log.warning("failed to send usage help: %s", e)

    def _touch(self):
        self.record["last_activity_at_ts"] = time.time()

    def _flush(self):
        lines = self._pending
        self._pending = []
        batch = "\n".join(lines)
        self._turn_busy.set()

        thinking_id = ""
        try:
            thinking_id = self.provider.reply_card(
                self.record["root_message_id"],
                cards.simple_card(
                    "blue",
                    "⏳ Agent Is Thinking",
                    f"Received {len(lines)} replies; batching for {self._debounce}s before one Agent Turn. Estimated Turn time: 1.5 to 2.5 minutes...",
                ),
            )
        except Exception as e:
            log.warning("failed to send thinking card: %s", e)

        def work():
            try:
                turn = self.record["client"].prompt(self.record["acp_session_id"], batch)
                self._finish_turn(thinking_id, turn)
            except Exception as e:
                if self._stop_requested:
                    log.info("Turn ended because the Session was stopped: %s", e)
                else:
                    log.exception("Turn failed")
                    self._fail_in_thread(thinking_id, e)
                    self.record["fatal"] = str(e)
                    self._stop_requested = True
            finally:
                self._turn_busy.clear()
                self._touch()

        threading.Thread(target=work, daemon=True).start()

    def run_first_turn(self, client, session_id):
        """Send root message and first `/<skill> <topic>` Turn after ACP is ready."""
        self.record["acp_session_id"] = session_id
        self.record["client"] = client
        initiator = self.record.get("initiator_name") or self.provider.user_name(
            self.record["initiator_open_id"]
        )
        self.record["initiator_name"] = initiator
        root_text = ROOT_TEMPLATE.format(
            skill=self.record["skill"],
            initiator=initiator,
            topic=self.record["topic"],
        )
        root_id = self.provider.send_text(self.record["chat_id"], root_text)
        self.record["root_message_id"] = root_id
        thinking_id = ""
        try:
            thinking_id = self.provider.reply_card(
                root_id,
                cards.simple_card(
                    "blue", "⏳ Creating Session", "Starting the Agent; the first Turn usually takes 1.5 to 2.5 minutes..."
                ),
            )
        except Exception as e:
            log.warning("failed to send thinking card: %s", e)

        prompt = f"/{self.record['skill']} {self.record['topic']}" + COMPLETION_HINT
        self._turn_busy.set()

        def work():
            try:
                turn = client.prompt(session_id, prompt)
                self._finish_turn(thinking_id, turn)
            except Exception as e:
                if self._stop_requested:
                    log.info("first Turn ended because the Session was stopped: %s", e)
                else:
                    log.exception("first Turn failed")
                    self._fail_in_thread(thinking_id, e)
                    self.record["fatal"] = str(e)
                    self._stop_requested = True
            finally:
                self._turn_busy.clear()
                self._touch()

        threading.Thread(target=work, daemon=True).start()

    def _finish_turn(self, thinking_id, turn):
        text = turn.display_text()
        if not text:
            text = f"(Agent produced no text in this Turn, stopReason={turn.stop_reason})"
        chunks = chunk_runes(text)
        try:
            if thinking_id:
                try:
                    self.provider.patch_card(thinking_id, cards.result_card(chunks[0]))
                except Exception as e:
                    log.warning("failed to patch thinking card; sending a new message instead: %s", e)
                    self.provider.reply_card(
                        self.record["root_message_id"], cards.result_card(chunks[0])
                    )
            else:
                self.provider.reply_card(
                    self.record["root_message_id"], cards.result_card(chunks[0])
                )
            for extra in chunks[1:]:
                self.provider.reply_card(
                    self.record["root_message_id"], cards.result_card(extra)
                )
        except Exception:
            log.exception("failed to send Turn result")

        session_start = datetime.fromtimestamp(
            self.record.get("attempt_started_at_ts", self.record["started_at_ts"])
        )
        spec_rel, marker_found, file_valid = parse_completion(
            text, self.record["cwd"], session_start
        )
        if marker_found and file_valid:
            self.record["spec_path"] = spec_rel
            self.record["state"] = state.STATE_DONE
            self._stop_requested = True
            try:
                self.provider.reply_card(
                    self.record["root_message_id"],
                    cards.simple_card(
                        "green",
                        "✅ Alignment Complete",
                        f"The Agent declared Alignment complete. Spec was written to the repository: `{spec_rel}`\nSession ended.",
                    ),
                )
            except Exception:
                # Spec terminal state is determined by file validation; notification failure cannot undo done.
                log.exception("failed to send Alignment completion notification")
            log.info("Alignment complete spec=%s", spec_rel)
        elif marker_found and not file_valid:
            try:
                self.provider.reply_card(
                    self.record["root_message_id"],
                    cards.simple_card(
                        "orange",
                        "⚠️ Invalid Completion Signal",
                        f"The Agent declared completion, but Spec file `{spec_rel}` is invalid: "
                        "it does not exist in the repository, or was not written by this Session "
                        "and may be a historical artifact. The Session continues; ask the Agent "
                        "to write this Session's Spec before declaring completion.",
                    ),
                )
            except Exception:
                log.exception("failed to send invalid completion notification")

    def _fail_in_thread(self, thinking_id, err):
        card = cards.simple_card("red", "❌ Error", "Error: " + str(err))
        if thinking_id:
            try:
                self.provider.patch_card(thinking_id, card)
                return
            except Exception:
                pass
        try:
            self.provider.reply_card(self.record["root_message_id"], card)
        except Exception:
            log.exception("failed to send failure notification")
