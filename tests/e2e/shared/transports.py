"""Transport seams for the Backend Smoke layer.

The User Simulator and Run Evaluator run LLM calls and Feishu OpenAPI calls
through these protocols so unit tests can inject fakes and the layer never
needs real credentials to be importable or testable. Real implementations are
configured exclusively through environment variables (docs/e2e-harness-design.md).
"""

from dataclasses import dataclass, field
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from .message_protocol import card_title
from .prompts import (
    SIMULATOR_SYSTEM_PROMPT,
    build_dimension_prompt,
    build_simulator_prompt,
)

log = logging.getLogger("im_align.e2e.transports")


def post_json_with_deadline(
    url: str,
    payload: dict,
    headers: dict,
    total_timeout: float,
    connection_factory=None,
) -> dict:
    """POST JSON and read the full body under a TOTAL deadline.

    urllib's timeout is per socket operation, which cannot bound a request
    whose connection stays alive but never finishes: measured on 2026-09-16,
    the simulator LLM call hung ~15 minutes inside SSL_read on a wedged
    connection and froze the whole Backend Smoke run. Each recv here uses
    only the remaining time, so TimeoutError is raised no matter how slowly
    the server trickles. Only https endpoints are supported.
    """
    import http.client
    import time
    import urllib.parse

    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"only https endpoints are supported: {url}")
    factory = connection_factory or http.client.HTTPSConnection
    connection = factory(parts.hostname, parts.port or 443, timeout=total_timeout)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    deadline = time.monotonic() + total_timeout
    try:
        connection.request(
            "POST",
            path,
            body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
        )
        # Capture the socket before getresponse(): the connection hands it to
        # the response on close-style replies, leaving connection.sock None.
        # The deadline covers the WHOLE exchange; a wedge while reading the
        # response status line (getresponse) is otherwise unbounded (measured
        # 2026-09-16 on the simulator LLM call).
        sock = connection.sock
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"request exceeded total timeout of {total_timeout}s: {url}"
            )
        if sock is not None:
            sock.settimeout(remaining)
        response = connection.getresponse()
        chunks = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"request exceeded total timeout of {total_timeout}s: {url}"
                )
            if sock is not None:
                sock.settimeout(remaining)
            chunk = response.read1(65536)
            if not chunk:
                break
            chunks.append(chunk)
        body = b"".join(chunks)
        if response.status >= 400:
            raise RuntimeError(
                f"endpoint returned HTTP {response.status}: {body[:200]!r}"
            )
        return json.loads(body.decode("utf-8"))
    finally:
        connection.close()


@dataclass
class ThreadMessage:
    """One message observed in the Alignment Thread."""

    message_id: str
    text: str
    msg_type: str = "text"  # text | card
    create_time: float = 0.0
    root_id: str = ""
    sender_open_id: str = ""
    card_title: str = ""


@dataclass(frozen=True)
class ReplyReceipt:
    """Identity of one simulator reply accepted by Feishu/Lark."""

    message_id: str
    create_time: float = 0.0


class ThreadTransport:
    """Feishu OpenAPI polling seam (≈3 s interval); no long connection."""

    def poll(self) -> ThreadMessage | None:
        """Return the next unprocessed message, or None when the Thread is idle."""
        raise NotImplementedError

    def post_reply(self, text: str) -> ReplyReceipt:
        raise NotImplementedError


class SimulatorLLMTransport:
    """LLM seam for the User Simulator; temperature is pinned to 0 by the caller."""

    def compose_answer(self, question: str, user_brief: str) -> str:
        raise NotImplementedError


class EvaluatorLLMTransport:
    """LLM seam for the Run Evaluator; temperature is pinned to 0 by the caller."""

    def judge(self, dimension: str, evidence: str) -> dict:
        """Return the raw judge output for one llm_dimensions field.

        Implementations must return a mapping with at least an "assessment"
        boolean; the evaluator validates the schema and fails closed.
        """
        raise NotImplementedError


@dataclass
class FakeThreadTransport(ThreadTransport):
    """Scripted Thread for tests: queued inbound messages, recorded replies."""

    inbox: list = field(default_factory=list)
    replies: list = field(default_factory=list)

    def push(self, message: ThreadMessage) -> None:
        self.inbox.append(message)

    def poll(self) -> ThreadMessage | None:
        if not self.inbox:
            return None
        return self.inbox.pop(0)

    def post_reply(self, text: str) -> ReplyReceipt:
        self.replies.append(text)
        return ReplyReceipt(message_id=f"fake-reply-{len(self.replies)}")


@dataclass
class FakeSimulatorLLM(SimulatorLLMTransport):
    """Scripted simulator LLM: canned answer or a callable(question, brief)."""

    answer: object = "ack"

    def compose_answer(self, question: str, user_brief: str) -> str:
        if callable(self.answer):
            return self.answer(question, user_brief)
        return self.answer


@dataclass
class FakeEvaluatorLLM(EvaluatorLLMTransport):
    """Scripted evaluator LLM: fixed result or a callable(dimension, evidence)."""

    result: object = field(default_factory=lambda: {"assessment": True, "rationale": "ok"})

    def judge(self, dimension: str, evidence: str) -> dict:
        if callable(self.result):
            return self.result(dimension, evidence)
        return self.result


def evaluator_llm_from_env(env) -> EvaluatorLLMTransport | None:
    """Build a real evaluator LLM from the E2E_EVALUATOR_LLM_* env vars.

    Returns None when the configuration is incomplete so callers can skip
    cleanly instead of failing; no provider is hardcoded.
    """
    base_url = env.get("E2E_EVALUATOR_LLM_BASE_URL", "")
    api_key = env.get("E2E_EVALUATOR_LLM_API_KEY", "")
    model = env.get("E2E_EVALUATOR_LLM_MODEL", "")
    if not (base_url and api_key and model):
        return None
    return OpenAICompatibleEvaluator(base_url=base_url, api_key=api_key, model=model)


class OpenAICompatibleEvaluator(EvaluatorLLMTransport):
    """Minimal chat-completions client for CI-injected OpenAI-compatible endpoints."""

    TOTAL_TIMEOUT_SECONDS = 180.0

    def __init__(self, base_url: str, api_key: str, model: str):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    def judge(self, dimension: str, evidence: str) -> dict:
        prompt = build_dimension_prompt(dimension, evidence)
        payload = post_json_with_deadline(
            self._base_url + "/chat/completions",
            {
                "model": self._model,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            },
            headers={"Authorization": "Bearer " + self._api_key},
            total_timeout=self.TOTAL_TIMEOUT_SECONDS,
        )
        content = payload["choices"][0]["message"]["content"]
        return json.loads(content)


def simulator_llm_from_env(env) -> "OpenAICompatibleSimulator | None":
    """Build a real simulator LLM from the E2E_SIMULATOR_LLM_* env vars; None if incomplete."""
    base_url = env.get("E2E_SIMULATOR_LLM_BASE_URL", "")
    api_key = env.get("E2E_SIMULATOR_LLM_API_KEY", "")
    model = env.get("E2E_SIMULATOR_LLM_MODEL", "")
    if not (base_url and api_key and model):
        return None
    return OpenAICompatibleSimulator(base_url=base_url, api_key=api_key, model=model)


class OpenAICompatibleSimulator(SimulatorLLMTransport):
    """Minimal chat-completions client for CI-injected OpenAI-compatible endpoints.

    A reasoning model can legitimately take tens of seconds; the total
    deadline is far above that but well below a hung connection.
    """

    TOTAL_TIMEOUT_SECONDS = 180.0

    def __init__(self, base_url: str, api_key: str, model: str):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    def compose_answer(self, question: str, user_brief: str) -> str:
        payload = post_json_with_deadline(
            self._base_url + "/chat/completions",
            {
                "model": self._model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": SIMULATOR_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": build_simulator_prompt(question, user_brief),
                    },
                ],
            },
            headers={"Authorization": "Bearer " + self._api_key},
            total_timeout=self.TOTAL_TIMEOUT_SECONDS,
        )
        return payload["choices"][0]["message"]["content"]


# ---- Feishu OpenAPI polling transport (user token; no long connection) ----
# The transport and verifier are user-token only: ADR-0009 measured that the
# platform never delivers bot-originated messages to the Bridge event stream,
# so ADR-0010 removed the tenant-token (bot) transport path.


def select_fresh(items: list, seen: dict) -> list:
    """Drop unchanged messages; record the fresh ones in seen.

    A message is fresh when its message_id is unseen or its update_time
    changed. The latter matters because the Bridge patches Turn results into
    the thinking card in place: dedup by message_id alone hid every Agent
    answer from the simulator (measured 2026-09-16). A None marker means
    "suppress re-delivery" (Session root, own replies).
    """
    fresh = []
    for item in items:
        message_id = item.get("message_id")
        if not message_id:
            continue
        update_time = item.get("update_time") or ""
        if message_id in seen and (
            seen[message_id] is None or seen[message_id] == update_time
        ):
            continue
        seen[message_id] = update_time
        fresh.append(item)
    return fresh


def flatten_card_text(raw: str) -> str:
    """Join the human-readable text fields of a card JSON body.

    The Bridge renders Turn output as cards whose body is a JSON tree of
    tagged elements; the simulator LLM must receive the readable text, not
    the wire envelope. Falls back to the raw body when parsing fails.
    """
    try:
        card = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    parts = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("tag") == "text" and isinstance(node.get("text"), str):
                parts.append(node["text"])
            else:
                for value in node.values():
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(card)
    return "".join(parts) or raw


def thread_message_from_api_item(item: dict) -> ThreadMessage:
    """Convert one im/v1/messages item to a ThreadMessage for classification.

    Card bodies are flattened to their readable text (question marks survive
    for the deterministic classifier); text bodies are unwrapped from their
    JSON envelope.
    """
    msg_type = item.get("msg_type", "text")
    raw = (item.get("body") or {}).get("content", "") or ""
    text = raw
    if msg_type == "text":
        try:
            text = json.loads(raw).get("text", raw)
        except (json.JSONDecodeError, AttributeError):
            text = raw
    else:
        text = flatten_card_text(raw)
    try:
        create_time = int(item.get("create_time", "0")) / 1000
    except (TypeError, ValueError):
        create_time = 0.0
    return ThreadMessage(
        message_id=item.get("message_id", ""),
        text=text,
        msg_type=msg_type,
        create_time=create_time,
        root_id=item.get("root_id", ""),
        sender_open_id=(item.get("sender") or {}).get("id", ""),
        card_title=card_title(raw) if msg_type == "interactive" else "",
    )


class OpenAPIThreadTransport(ThreadTransport):
    """Poll the Thread over OpenAPI under the test-user identity.

    Polling keeps the machine-level single-session lock free for the Bridge
    (docs/e2e-harness-design.md). Each poll() fetches recent messages, drops
    unchanged ones (a message is re-delivered when its update_time changes,
    because the Bridge patches Turn results into the thinking card in place),
    and returns one queued message. urlopen and clock are injectable for
    tests.

    Measured constraint: the chat-container listing omits thread replies for
    bot identities, so a bound transport resolves the root message's
    thread_id once and then lists the thread container; the thread container
    rejects message ids, so the root lookup is mandatory.
    """

    def __init__(
        self,
        user_access_token: str = "",
        chat_id: str = "",
        root_message_id: str = "",
        domain: str = "feishu",
        mention_open_id: str = "",
        can_post=None,
        urlopen=None,
    ):
        if not user_access_token:
            raise ValueError("user_access_token is required")
        self._user_token = user_access_token
        self._mention_open_id = mention_open_id
        self._can_post = can_post or (lambda: True)
        self._urlopen = urlopen or urllib.request.urlopen
        self._chat_id = chat_id
        self._root_message_id = root_message_id
        self._thread_id = ""
        self._base = (
            "https://open.feishu.cn" if domain == "feishu" else "https://open.larksuite.com"
        )
        self._seen = {}
        if root_message_id:
            # The Session root is the Bridge's announcement, not a question.
            # Pre-marking it seen prevents the simulator from answering the
            # topic before the Agent's first Turn output exists. None means
            # suppress re-delivery even if the message is later patched.
            self._seen[root_message_id] = None
        self._queue = []

    def _token(self) -> str:
        return self._user_token

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self._base + path,
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": "Bearer " + self._token(),
            },
        )
        try:
            with self._urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            code = payload.get("code", "unknown")
            message = payload.get("msg") or raw or e.reason
            raise RuntimeError(
                f"Feishu API request failed (HTTP {e.code}, code {code}): {message}"
            ) from e

    def _fetch_recent(self) -> list:
        if not self._root_message_id:
            return self._fetch_chat()
        return self._fetch_thread()

    def _fetch_chat(self) -> list:
        query = (
            f"/open-apis/im/v1/messages?container_id_type=chat&container_id={self._chat_id}"
            f"&sort_type=ByCreateTimeDesc&page_size=50"
        )
        resp = self._request("GET", query)
        if resp.get("code") != 0:
            raise RuntimeError(f"list messages failed: {resp.get('msg')}")
        # Descending feed; the queue expects chronological order.
        return list(reversed(resp.get("data", {}).get("items", [])))

    def _resolve_thread_id(self) -> str:
        if self._thread_id:
            return self._thread_id
        message_id = urllib.parse.quote(self._root_message_id, safe="")
        resp = self._request("GET", f"/open-apis/im/v1/messages/{message_id}")
        if resp.get("code") != 0:
            raise RuntimeError(f"resolve thread failed: {resp.get('msg')}")
        items = resp.get("data", {}).get("items", [])
        thread_id = items[0].get("thread_id", "") if items else ""
        if not thread_id:
            raise RuntimeError("root message has no thread_id")
        self._thread_id = thread_id
        return thread_id

    def _fetch_thread(self) -> list:
        thread_id = self._resolve_thread_id()
        query = (
            f"/open-apis/im/v1/messages?container_id_type=thread&container_id={thread_id}"
            f"&sort_type=ByCreateTimeAsc&page_size=50"
        )
        resp = self._request("GET", query)
        if resp.get("code") != 0:
            raise RuntimeError(f"list thread messages failed: {resp.get('msg')}")
        return resp.get("data", {}).get("items", [])

    def poll(self) -> ThreadMessage | None:
        if not self._queue:
            items = select_fresh(self._fetch_recent(), self._seen)
            if items:
                log.getChild("transport").info(
                    "poll fetched %d new message(s): %s",
                    len(items),
                    [item.get("message_id", "")[:16] for item in items],
                )
            self._queue.extend(thread_message_from_api_item(item) for item in items)
        if not self._queue:
            return None
        return self._queue.pop(0)

    def post_reply(self, text: str) -> ReplyReceipt:
        if not self._can_post():
            raise RuntimeError("refusing Participant reply after Session termination")
        message_text = text
        if self._mention_open_id:
            message_text = f'<at user_id="{self._mention_open_id}"></at> {text}'
        if self._root_message_id:
            body = {
                "msg_type": "text",
                "content": json.dumps({"text": message_text}, ensure_ascii=False),
                "reply_in_thread": True,
            }
            message_id = urllib.parse.quote(self._root_message_id, safe="")
            path = f"/open-apis/im/v1/messages/{message_id}/reply"
        else:
            body = {
                "receive_id": self._chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": message_text}, ensure_ascii=False),
            }
            path = "/open-apis/im/v1/messages?receive_id_type=chat"
        resp = self._request("POST", path, body)
        if resp.get("code") != 0:
            raise RuntimeError(f"post reply failed: {resp.get('msg')}")
        data = resp.get("data") or {}
        try:
            create_time = int(data.get("create_time", "0")) / 1000
        except (TypeError, ValueError):
            create_time = 0.0
        message_id = data.get("message_id", "")
        if message_id:
            # The next polling window includes our own reply. Mark it seen now
            # so the User Simulator never classifies its answer as a question.
            self._seen[message_id] = None
        return ReplyReceipt(
            message_id=message_id,
            create_time=create_time,
        )
