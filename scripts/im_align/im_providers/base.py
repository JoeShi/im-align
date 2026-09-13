"""IM Provider abstraction; v1 supports Feishu/Lark, future providers can share it."""

import abc
from dataclasses import dataclass


@dataclass
class IncomingMessage:
    event_id: str
    chat_id: str
    message_id: str
    root_id: str  # Thread root message id; empty for non-thread replies.
    sender_open_id: str
    sender_type: str
    text: str


class Provider(abc.ABC):
    @abc.abstractmethod
    def start(self):
        """Open a long connection in background threads and queue events for poll_events."""

    @abc.abstractmethod
    def stop(self):
        pass

    @abc.abstractmethod
    def poll_events(self, timeout):
        """Return IncomingMessage items received within timeout seconds; empty means timeout."""

    @abc.abstractmethod
    def send_text(self, chat_id, text):
        """Send text to a group and return message_id."""

    @abc.abstractmethod
    def reply_card(self, root_message_id, card_json):
        """Reply with a card in a Thread and return message_id."""

    @abc.abstractmethod
    def patch_card(self, message_id, card_json):
        """Update a previously sent card in place."""

    @abc.abstractmethod
    def react_ok(self, message_id):
        """Add an OK emoji ack; failures are logged by the caller and not raised."""

    @abc.abstractmethod
    def user_name(self, open_id):
        """Resolve open_id to display name; fall back to short open_id on failure."""

    @abc.abstractmethod
    def resolve_open_id(self, email):
        """Resolve email to open_id; raise LookupError when not found."""

    # ---- Approval: Provider receives card button callbacks ----

    @abc.abstractmethod
    def register_approval(self, approval_id, initiator_open_id):
        """Register one pending Approval and return the handle waiting for button clicks."""

    @abc.abstractmethod
    def cancel_approval(self, approval_id):
        pass
