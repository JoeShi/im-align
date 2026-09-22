"""Feishu OpenAPI thread verification under the test-user Participant identity.

Post-run Thread checks for the Feishu-touching layers (docs/e2e-harness-design.md).
All traffic goes over HTTPS polling with the user access token; the long
connection and its machine-level single-session lock stay with the Bridge.

The Bridge app intentionally lacks im:message.group_msg, so the verifier is
always built with the test-user Participant identity, never the Bridge
credentials.
"""

import json
import urllib.request

from .message_protocol import bridge_identity_matches

COMPLETION_CARD_TITLE = "✅ Alignment Complete"

APPROVAL_CARD_TITLE = "🔐 Approval Request"

# The Bridge posts this card when it rejects the Agent's completion marker
# (scripts/im_align/orchestrator.py). The L2 runner watches for it to fail
# fast instead of waiting out the scenario budget.
INVALID_COMPLETION_TITLE = "⚠️ Invalid Completion Signal"


class ThreadVerifier:
    """Post-run checks under the selected Participant identity."""

    def messages_since(self, chat_id: str, since_ts: float) -> list:
        raise NotImplementedError

    def completion_card_received(self, chat_id: str, since_ts: float) -> bool:
        raise NotImplementedError

    def approval_card_received(self, chat_id: str, since_ts: float) -> bool:
        raise NotImplementedError

    def message_reaction_received(
        self, message_id: str, operator_open_id: str, *, operator_app_id: str = ""
    ) -> bool:
        raise NotImplementedError


def message_contains_card(message: dict, marker: str) -> bool:
    """Deterministic card marker check over one im/v1/messages item."""
    body = json.dumps(message.get("body", {}), ensure_ascii=False)
    return marker in body


def find_approval_card(messages: list) -> bool:
    """True when any message carries an Approval card (ADR-0005 happy-path gate).

    Matches the card header title from cards.approval_card plus the
    `"im_align": "approval"` button value, so a renamed title alone cannot
    hide a regression that re-introduces Approval cards.
    """
    for message in messages:
        body = json.dumps(message.get("body", {}), ensure_ascii=False)
        if APPROVAL_CARD_TITLE in body or '"im_align": "approval"' in body:
            return True
    return False


class FeishuOpenAPIVerifier(ThreadVerifier):
    """Poll im/v1/messages over HTTPS under the user identity; keeps the long-connection lock free.

    Measured constraint: the chat-container listing omits thread replies for
    bot identities, so when root_message_id is set the verifier resolves the
    root message's thread_id once and lists the thread container.
    """

    def __init__(
        self,
        domain: str = "feishu",
        user_access_token: str = "",
        root_message_id: str = "",
    ):
        if not user_access_token:
            raise ValueError("user_access_token is required")
        self._user_access_token = user_access_token
        self._root_message_id = root_message_id
        self._thread_id = ""
        self._base = (
            "https://open.feishu.cn" if domain == "feishu" else "https://open.larksuite.com"
        )

    def _request(
        self, method: str, path: str, body: dict | None = None, token: str = ""
    ) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(
            self._base + path,
            data=data,
            method=method,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _token(self) -> str:
        return self._user_access_token

    def _resolve_thread_id(self, token: str) -> str:
        if self._thread_id:
            return self._thread_id
        import urllib.parse

        message_id = urllib.parse.quote(self._root_message_id, safe="")
        resp = self._request("GET", f"/open-apis/im/v1/messages/{message_id}", token=token)
        if resp.get("code") != 0:
            raise RuntimeError(f"resolve thread failed: {resp.get('msg')}")
        items = resp.get("data", {}).get("items", [])
        thread_id = items[0].get("thread_id", "") if items else ""
        if not thread_id:
            raise RuntimeError("root message has no thread_id")
        self._thread_id = thread_id
        return thread_id

    def _container_query(self, chat_id: str, page_token: str, token: str) -> str:
        if self._root_message_id:
            container = (
                "container_id_type=thread"
                f"&container_id={self._resolve_thread_id(token)}"
            )
        else:
            container = f"container_id_type=chat&container_id={chat_id}"
        query = f"/open-apis/im/v1/messages?{container}&sort_type=ByCreateTimeDesc&page_size=50"
        if page_token:
            query += f"&page_token={page_token}"
        return query

    def messages_since(self, chat_id: str, since_ts: float) -> list:
        """Messages at or after since_ts in ascending order; stops paging once older items appear."""
        token = self._token()
        page_token = ""
        found = []
        while True:
            query = self._container_query(chat_id, page_token, token)
            resp = self._request("GET", query, token=token)
            if resp.get("code") != 0:
                raise RuntimeError(f"list messages failed: {resp.get('msg')}")
            crossed = False
            for item in resp.get("data", {}).get("items", []):
                create_time = int(item.get("create_time", "0")) / 1000
                if create_time < since_ts:
                    crossed = True
                else:
                    found.append(item)
            if crossed:
                break
            page_token = resp.get("data", {}).get("page_token", "")
            if not resp.get("data", {}).get("has_more"):
                break
        return found

    def completion_card_received(self, chat_id: str, since_ts: float) -> bool:
        for message in self.messages_since(chat_id, since_ts):
            if message_contains_card(message, COMPLETION_CARD_TITLE):
                return True
        return False

    def approval_card_received(self, chat_id: str, since_ts: float) -> bool:
        return find_approval_card(self.messages_since(chat_id, since_ts))

    def message_reaction_received(
        self, message_id: str, operator_open_id: str, *, operator_app_id: str = ""
    ) -> bool:
        token = self._token()
        path = f"/open-apis/im/v1/messages/{message_id}/reactions?page_size=50"
        resp = self._request("GET", path, token=token)
        if resp.get("code") != 0:
            raise RuntimeError(f"list message reactions failed: {resp.get('msg')}")
        for item in resp.get("data", {}).get("items", []):
            reaction_type = item.get("reaction_type") or {}
            operator = item.get("operator") or {}
            observed_operator = operator.get("operator_id") or item.get("operator_id", "")
            if (
                reaction_type.get("emoji_type") == "OK"
                and bridge_identity_matches(
                    observed_operator, operator.get("operator_type", ""),
                    operator_open_id, operator_app_id,
                )
            ):
                return True
        return False
