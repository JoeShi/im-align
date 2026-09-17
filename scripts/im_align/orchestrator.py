"""Session orchestration: IM events -> ACP Turn -> completion detection.

Behavior matches the interaction model from legacy/go/internal/lark/bot.go:
  - root message opens the Session; Thread replies get emoji ack + queued Turn;
  - Turn start sends a blue thinking card, and Turn end patches it with output;
  - completion signal is strictly validated in completion.py;
  - idle timeout closes the run, and terminal state feeds host Agent resume handoff.
"""

import logging
import threading
import time
from datetime import datetime

from . import cards, state
from .completion import parse_completion
from .textutil import chunk_runes

log = logging.getLogger("im_align.orchestrator")

def completion_hint(spec_root):
    """Build the completion contract from the resolved Session configuration."""
    return f"""

[SYSTEM CONTRACT] When you decide Alignment is complete, finish with these exact steps:
1. Write all Spec files under `{spec_root}/`.
2. You may choose filenames, subdirectories, formats, and file count. Do not write outside `{spec_root}/`.
3. On the final line of your reply, output exactly one standalone line naming the primary Spec file: [ALIGNMENT_COMPLETE] <repository-relative path under {spec_root}/>
4. Do not include [ALIGNMENT_COMPLETE] anywhere else in the reply, including backtick references, code blocks, or narrative mentions.
5. Historical Spec files may already exist under the Spec Root. They are outputs from previous Alignment Sessions and are unrelated to this Session. Do not treat their existence as completion. This Session's Spec must include at least one file actually written or updated by you during this Session."""

ROOT_TEMPLATE = (
    "📋 New Alignment Session\n"
    "skill: {skill}\n"
    "topic: {topic}\n\n"
    "Reply to this message in the Thread to participate in Alignment.\n"
    "💡 Please **mention me** when replying in this Thread; otherwise I cannot receive your message because of Feishu/Lark scope limits."
)

USAGE_TEXT = (
    "This group is handled by im-align Bridge.\n"
    "• A developer starts a Session from the terminal through the im-align Skill.\n"
    "• Reply in the Session Thread and mention me to participate in Alignment.\n"
    "• The developer can stop the Session from the terminal."
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
        self._idle = cfg["timeouts"]["idle_timeout_seconds"]
        self._pending = []
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

    # ---- Main loop ----

    def run(self):
        """Block until terminal Session state and return the state string."""
        while True:
            events = self.provider.poll_events(timeout=1)
            now = time.time()
            for ev in events:
                self._handle_event(ev)
            if self._stop_requested:
                if self._turn_busy.is_set() and (
                    self._stop_deadline is None or now < self._stop_deadline
                ):
                    continue
                return state.STATE_DONE if self.record["state"] == state.STATE_DONE else state.STATE_STOPPED
            if self._pending and not self._turn_busy.is_set():
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
            if not self._turn_busy.is_set():
                self._flush()
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
        prompt_text = "\n".join(lines)
        self._turn_busy.set()

        thinking_id = ""
        try:
            thinking_id = self.provider.reply_card(
                self.record["root_message_id"],
                cards.simple_card(
                    "blue",
                    "⏳ Agent Is Thinking",
                    f"Received {len(lines)} replies; starting one Agent Turn. Estimated Turn time: 1.5 to 2.5 minutes...",
                ),
            )
        except Exception as e:
            log.warning("failed to send thinking card: %s", e)

        def work():
            try:
                turn = self.record["client"].prompt(self.record["acp_session_id"], prompt_text)
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
        root_text = ROOT_TEMPLATE.format(
            skill=self.record["skill"],
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

        prompt = (
            f"/{self.record['skill']} {self.record['topic']}"
            + completion_hint(self.cfg["agent"]["spec_root"])
        )
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
            text,
            self.record["cwd"],
            session_start,
            self.cfg["agent"]["spec_root"],
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
                        "it is outside the configured Spec Root, does not exist in the repository, "
                        "or was not written by this Session "
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
