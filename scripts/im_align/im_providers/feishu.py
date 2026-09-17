"""Feishu/Lark Provider: lark-oapi long-connection message IO.

All interactions correspond to legacy/go/internal/lark: long connections
(WebSocket) receives im.message.receive_v1; cards use Card 1.0 JSON.
"""

import json
import logging
import queue
import threading
import time

import lark_oapi as lark
from lark_oapi.api.contact.v3 import GetUserRequest
from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequestBody,
    CreateMessageReactionRequest,
    CreateMessageRequestBody,
    CreateMessageRequest,
    Emoji,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from .base import IncomingMessage, Provider

log = logging.getLogger("im_align.feishu")

_DOMAINS = {
    "feishu": lark.FEISHU_DOMAIN,
    "larksuite": lark.LARK_DOMAIN,
}


class FeishuProvider(Provider):
    def __init__(self, app_id, app_secret, domain="feishu", extra_participants=()):
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = _DOMAINS[domain]
        # ADR-0006: bot messages are discarded by default because group bot
        # traffic is mostly unrelated chatter. An entry here is an explicitly
        # configured allowlist (typically a dedicated second CI app playing the
        # customer); an allowlisted bot drives Turns like a human participant.
        self._extra_participants = frozenset(extra_participants)
        self.client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .domain(self._domain)
            .build()
        )
        self._events = queue.Queue()
        self._ws = None
        self._names = {}
        self._seen = set()
        self._seen_lock = threading.Lock()
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self._start_error = None

    # ---- Long connection ----

    def start(self):
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )
        self._ws = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=handler,
            domain=self._domain,
            auto_reconnect=True,
            log_level=lark.LogLevel.WARNING,
        )
        thread = threading.Thread(target=self._run_ws, daemon=True)
        thread.start()
        # lark-oapi 1.7.3 has no public ready callback. With the pinned version,
        # client creation is the readiness signal. Auth/network failures must
        # leave the run failed instead of creating an active Session that never
        # receives Thread events.
        deadline = time.time() + 20
        while time.time() < deadline:
            if self._stopping.is_set():
                raise RuntimeError("Feishu/Lark long-connection startup was cancelled")
            if getattr(self._ws, "_conn", None) is not None:
                self._ready.set()
                return
            if self._start_error is not None:
                raise RuntimeError(f"Feishu/Lark long-connection startup failed: {self._start_error}")
            if not thread.is_alive():
                raise RuntimeError("Feishu/Lark long-connection thread exited early")
            time.sleep(0.1)
        raise RuntimeError("Feishu/Lark long connection was not ready within 20 seconds")

    def _run_ws(self):
        while not self._stopping.is_set():
            try:
                log.info("starting Feishu/Lark long connection (app_id=%s)...", self._app_id)
                self._ws.start()
            except Exception as e:
                self._start_error = e
                log.error("Feishu/Lark long connection error: %s", e)
            self._stopping.wait(3)

    def stop(self):
        self._stopping.set()
        # lark-oapi ws client has no explicit close; process exit disconnects it.

    def poll_events(self, timeout):
        out = []
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return out
            try:
                out.append(self._events.get(timeout=min(remaining, 1.0)))
            except queue.Empty:
                if out:
                    return out

    # ---- Message receive ----

    def _on_message(self, data) -> None:
        try:
            event = data.event
            msg = event.message
            sender = event.sender
            event_id = getattr(data.header, "event_id", "") if data.header else ""
            if self._dup(event_id):
                return
            sender_type = getattr(sender, "sender_type", "")
            sender_open_id = sender.sender_id.open_id if sender.sender_id else ""
            if sender_type == "bot" and sender_open_id not in self._extra_participants:
                return
            text = self._extract_text(msg)
            self._events.put(
                IncomingMessage(
                    event_id=event_id,
                    chat_id=msg.chat_id or "",
                    message_id=msg.message_id or "",
                    root_id=msg.root_id or "",
                    sender_open_id=sender_open_id,
                    sender_type=sender_type,
                    text=text,
                )
            )
        except Exception:
            log.exception("failed to handle message event")

    def _dup(self, event_id):
        if not event_id:
            return False
        with self._seen_lock:
            if event_id in self._seen:
                return True
            if len(self._seen) > 1000:
                self._seen.clear()
            self._seen.add(event_id)
            return False

    @staticmethod
    def _extract_text(msg):
        if msg.message_type != "text":
            return ""
        try:
            content = json.loads(msg.content)
        except (TypeError, json.JSONDecodeError):
            return ""
        text = content.get("text", "")
        for mention in msg.mentions or []:
            key = getattr(mention, "key", None)
            if key:
                text = text.replace(key, "")
        return text.strip()

    # ---- Send ----

    def send_text(self, chat_id, text):
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            raise RuntimeError(f"send message: code={resp.code} msg={resp.msg}")
        return resp.data.message_id

    def reply_card(self, root_message_id, card_json):
        req = (
            ReplyMessageRequest.builder()
            .message_id(root_message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(card_json)
                .reply_in_thread(True)
                .build()
            )
            .build()
        )
        resp = self.client.im.v1.message.reply(req)
        if not resp.success():
            raise RuntimeError(f"reply message: code={resp.code} msg={resp.msg}")
        return resp.data.message_id

    def patch_card(self, message_id, card_json):
        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder().content(card_json).build()
            )
            .build()
        )
        resp = self.client.im.v1.message.patch(req)
        if not resp.success():
            raise RuntimeError(f"patch message: code={resp.code} msg={resp.msg}")

    def react_ok(self, message_id):
        req = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type("OK").build())
                .build()
            )
            .build()
        )
        resp = self.client.im.v1.message_reaction.create(req)
        if not resp.success():
            raise RuntimeError(f"add reaction: code={resp.code} msg={resp.msg}")

    # ---- Contacts ----

    def user_name(self, open_id):
        if not open_id:
            return "unknown"
        if open_id in self._names:
            return self._names[open_id]
        name = open_id[:8] + "…" if len(open_id) > 8 else open_id
        try:
            req = GetUserRequest.builder().user_id(open_id).user_id_type("open_id").build()
            resp = self.client.contact.v3.user.get(req)
            if resp.success() and resp.data and resp.data.user and resp.data.user.name:
                name = resp.data.user.name
            else:
                log.warning("failed to resolve display name open_id=%s code=%s msg=%s", open_id, resp.code, resp.msg)
        except Exception as e:
            log.warning("display-name resolution error open_id=%s: %s", open_id, e)
        self._names[open_id] = name
        return name
