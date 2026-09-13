"""Feishu/Lark Provider: lark-oapi long-connection IO + Approval card callbacks.

All interactions correspond to legacy/go/internal/lark: long connections
(WebSocket) receive im.message.receive_v1 and card.action.trigger; cards use
Card 1.0 JSON; button values carry kind semantics and never match by optionId.
"""

import json
import logging
import queue
import threading
import time

import lark_oapi as lark
from lark_oapi.api.contact.v3 import (
    BatchGetIdUserRequest,
    BatchGetIdUserRequestBody,
    GetUserRequest,
)
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


class ApprovalHandle:
    """One pending Approval waiting for a click."""

    def __init__(self, approval_id, initiator_open_id):
        self.approval_id = approval_id
        self.initiator_open_id = initiator_open_id
        self.event = threading.Event()
        self.kind = ""
        self.operator_open_id = ""
        self.message_id = ""  # Approval card message id, used for in-place updates.


class FeishuProvider(Provider):
    def __init__(self, app_id, app_secret, domain="feishu"):
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = _DOMAINS[domain]
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
        self._approvals = {}
        self._approvals_lock = threading.Lock()
        self._approval_seq = 0
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self._start_error = None

    # ---- Long connection ----

    def start(self):
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .register_p2_card_action_trigger(self._on_card_action)
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
            if getattr(sender, "sender_type", "") == "bot":
                return
            text = self._extract_text(msg)
            self._events.put(
                IncomingMessage(
                    event_id=event_id,
                    chat_id=msg.chat_id or "",
                    message_id=msg.message_id or "",
                    root_id=msg.root_id or "",
                    sender_open_id=(sender.sender_id.open_id if sender.sender_id else ""),
                    sender_type=getattr(sender, "sender_type", ""),
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

    # ---- Card callbacks for Approval ----

    def _on_card_action(self, data):
        action = data.event.action
        value = getattr(action, "value", None) or {}
        if value.get("im_align") != "approval":
            return P2CardActionTriggerResponse()
        approval_id = value.get("req_id", "")
        kind = value.get("kind", "")
        operator = getattr(data.event.operator, "open_id", "") or ""
        message_id = getattr(data.event.context, "open_message_id", "") or ""
        log.info("received Approval callback req=%s kind=%s", approval_id, kind)

        with self._approvals_lock:
            handle = self._approvals.get(approval_id)
        if not handle:
            return self._toast("this Approval was already processed or has timed out")
        if operator != handle.initiator_open_id:
            log.warning(
                "Approval clicker is not the Session Initiator req=%s operator=%s expected=%s",
                approval_id,
                operator,
                handle.initiator_open_id,
            )
            return self._toast("only the Session Initiator can approve")
        if not handle.event.is_set():
            handle.kind = kind
            handle.operator_open_id = operator
            handle.message_id = message_id
            handle.event.set()
        return self._toast("recorded")

    @staticmethod
    def _toast(content):
        return P2CardActionTriggerResponse({"toast": {"type": "info", "content": content}})

    def register_approval(self, approval_id, initiator_open_id):
        handle = ApprovalHandle(approval_id, initiator_open_id)
        with self._approvals_lock:
            self._approvals[approval_id] = handle
        return handle

    def cancel_approval(self, approval_id):
        with self._approvals_lock:
            self._approvals.pop(approval_id, None)

    def next_approval_id(self):
        with self._approvals_lock:
            self._approval_seq += 1
            return f"perm-{self._approval_seq}"

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

    def resolve_open_id(self, email):
        req = (
            BatchGetIdUserRequest.builder()
            .user_id_type("open_id")
            .request_body(
                BatchGetIdUserRequestBody.builder()
                .emails([email])
                .include_resigned(False)
                .build()
            )
            .build()
        )
        resp = self.client.contact.v3.user.batch_get_id(req)
        if not resp.success():
            raise LookupError(f"contact batch_get_id: code={resp.code} msg={resp.msg}")
        for item in (resp.data.user_list or []):
            if item.email == email and item.user_id:
                return item.user_id
        raise LookupError(f"email {email} was not found in Feishu/Lark contacts")


# Keep the card callback response import at the bottom so the main flow stays readable.
from lark_oapi.event.callback.model.p2_card_action_trigger import (  # noqa: E402
    P2CardActionTriggerResponse,
)
