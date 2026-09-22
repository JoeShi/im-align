"""OpenAPIThreadTransport: user-token polling over the OpenAPI seams.

Fake urlopen routes by URL so no network is touched.
"""

import io
import unittest
import urllib.error

from tests.e2e.shared.transports import OpenAPIThreadTransport, post_json_with_deadline


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        import json

        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeUrlopen:
    """Routes tenant-token exchanges and records authenticated API calls."""

    def __init__(self):
        self.token_calls = 0
        self.api_calls = []  # (url, authorization_header)

    def __call__(self, req, timeout=None):
        url = req.full_url
        if "tenant_access_token/internal" in url:
            self.token_calls += 1
            return FakeResponse(
                {"code": 0, "tenant_access_token": f"t-{self.token_calls}", "expire": 3600}
            )
        self.api_calls.append((url, req.get_header("Authorization") or ""))
        if "container_id_type=chat" in url:
            return FakeResponse({"code": 0, "data": {"items": [], "has_more": False}})
        return FakeResponse({"code": 0, "data": {"message_id": "om_new"}})


class FakeHttpConnection:
    """Connection double for post_json_with_deadline: scripted read1 chunks."""

    def __init__(self, chunks, delay=0.0, status=200):
        self._chunks = list(chunks)
        self._delay = delay
        self._status = status
        self.sock = self  # settimeout sink
        self.requested_path = None

    def settimeout(self, value):
        self.timeout = value

    def request(self, method, path, body=None, headers=None):
        self.requested_path = path

    def getresponse(self):
        connection = self

        class Response:
            status = connection._status

            def read1(self, n):
                if connection._delay:
                    import time

                    time.sleep(connection._delay)
                if not connection._chunks:
                    return b""
                return connection._chunks.pop(0)

        return Response()

    def close(self):
        pass


class PostJsonWithDeadlineTests(unittest.TestCase):
    def test_complete_body_is_parsed(self):
        connection = FakeHttpConnection(
            [b'{"choices":[{"message":{"content":"{\\"assessment\\":true}"}}]}']
        )
        result = post_json_with_deadline(
            "https://example.com/chat/completions",
            {"model": "m"},
            headers={"Authorization": "Bearer k"},
            total_timeout=5.0,
            connection_factory=lambda host, port, timeout=None: connection,
        )
        self.assertTrue(result["choices"][0]["message"]["content"])
        self.assertEqual(connection.requested_path, "/chat/completions")

    def test_trickling_response_raises_total_timeout(self):
        # urllib's per-operation timeout cannot fire while the server keeps
        # the connection alive; only a cumulative deadline bounds this.
        connection = FakeHttpConnection([b"x"] * 1000, delay=0.05)
        with self.assertRaisesRegex(TimeoutError, "total timeout"):
            post_json_with_deadline(
                "https://example.com/chat/completions",
                {},
                headers={},
                total_timeout=0.3,
                connection_factory=lambda host, port, timeout=None: connection,
            )

    def test_http_error_status_raises(self):
        connection = FakeHttpConnection([b'{"error":"bad key"}'], status=401)
        with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            post_json_with_deadline(
                "https://example.com/chat/completions",
                {},
                headers={},
                total_timeout=5.0,
                connection_factory=lambda host, port, timeout=None: connection,
            )

    def test_non_https_endpoint_rejected(self):
        with self.assertRaisesRegex(ValueError, "https"):
            post_json_with_deadline(
                "http://example.com/x", {}, headers={}, total_timeout=5.0
            )


class UserTokenTransportTests(unittest.TestCase):
    def make_transport(self, urlopen=None):
        return OpenAPIThreadTransport(
            "u-token",
            "oc_chat",
            urlopen=urlopen,
        )

    def test_missing_identity_raises(self):
        with self.assertRaisesRegex(ValueError, "user_access_token"):
            OpenAPIThreadTransport("", "oc_chat")

    def test_http_error_surfaces_feishu_error_code_and_message(self):
        class PermissionDeniedFake(FakeUrlopen):
            def __call__(self, req, timeout=None):
                raise urllib.error.HTTPError(
                    req.full_url,
                    400,
                    "Bad Request",
                    {},
                    io.BytesIO(
                        b'{"code":230027,"msg":"need scope: im:message.group_msg"}'
                    ),
                )

        transport = self.make_transport(urlopen=PermissionDeniedFake())

        with self.assertRaisesRegex(
            RuntimeError,
            r"HTTP 400.*230027.*im:message\.group_msg",
        ):
            transport.poll()

    def test_poll_sends_user_token(self):
        fake = FakeUrlopen()
        transport = self.make_transport(urlopen=fake)
        transport.poll()
        self.assertEqual(fake.token_calls, 0)
        _, auth = fake.api_calls[-1]
        self.assertEqual(auth, "Bearer u-token")

    def test_poll_returns_only_messages_from_bound_thread(self):
        class MessagesFake(FakeUrlopen):
            def __call__(self, req, timeout=None):
                self.api_calls.append(
                    (req.full_url, req.get_header("Authorization") or "")
                )
                if "/messages/om_root" in req.full_url:
                    return FakeResponse(
                        {
                            "code": 0,
                            "data": {"items": [{"message_id": "om_root", "thread_id": "omt_1"}]},
                        }
                    )
                if "container_id_type=thread" in req.full_url:
                    return FakeResponse(
                        {
                            "code": 0,
                            "data": {
                                "items": [
                                    {
                                        "message_id": "om_expected",
                                        "root_id": "om_root",
                                        "msg_type": "text",
                                        "body": {"content": '{"text":"expected"}'},
                                    },
                                ]
                            },
                        }
                    )
                raise AssertionError("chat-container listing must not be used: " + req.full_url)

        transport = OpenAPIThreadTransport(
            "u-token",
            "oc_chat",
            root_message_id="om_root",
            urlopen=MessagesFake(),
        )

        self.assertEqual(transport.poll().message_id, "om_expected")
        self.assertIsNone(transport.poll())

    def test_poll_reads_thread_container_via_root_thread_id(self):
        # Measured: the chat-container listing omits thread replies entirely
        # for bot identities, so polling it leaves the simulator blind. The
        # transport must resolve the root message's thread_id and list the
        # thread container, oldest first.
        class ThreadFake(FakeUrlopen):
            def __call__(self, req, timeout=None):
                url = req.full_url
                if "tenant_access_token/internal" in url:
                    return super().__call__(req, timeout)
                self.api_calls.append((url, req.get_header("Authorization") or ""))
                if "/messages/om_root" in url:
                    return FakeResponse(
                        {
                            "code": 0,
                            "data": {
                                "items": [
                                    {"message_id": "om_root", "thread_id": "omt_1"},
                                ]
                            },
                        }
                    )
                if "container_id_type=thread" in url:
                    return FakeResponse(
                        {
                            "code": 0,
                            "data": {
                                "items": [
                                    {
                                        "message_id": "om_root",
                                        "msg_type": "text",
                                        "body": {"content": '{"text":"root"}'},
                                    },
                                    {
                                        "message_id": "om_agent1",
                                        "root_id": "om_root",
                                        "msg_type": "interactive",
                                        "body": {"content": '{"title":"A"}'},
                                    },
                                    {
                                        "message_id": "om_agent2",
                                        "root_id": "om_root",
                                        "msg_type": "interactive",
                                        "body": {"content": '{"title":"B"}'},
                                    },
                                ]
                            },
                        }
                    )
                raise AssertionError("chat-container listing must not be used: " + url)

        fake = ThreadFake()
        transport = OpenAPIThreadTransport(
            "u-token",
            "oc_chat",
            root_message_id="om_root",
            urlopen=fake,
        )

        first = transport.poll()
        second = transport.poll()
        # chronological order, root pre-marked seen, one message per poll
        self.assertEqual([first.message_id, second.message_id], ["om_agent1", "om_agent2"])
        self.assertIsNone(transport.poll())
        self.assertTrue(
            any("container_id_type=thread&container_id=omt_1" in url for url, _ in fake.api_calls)
        )

    def test_patched_card_is_redelivered_with_new_content(self):
        # The Bridge patches Turn results into the thinking card in place.
        # A message_id-only dedup hid every Agent answer from the simulator
        # (measured 2026-09-16); update_time changes must re-deliver.
        class PatchingFake(FakeUrlopen):
            def __init__(self):
                super().__init__()
                self.patched = False

            def __call__(self, req, timeout=None):
                if "tenant_access_token/internal" in req.full_url:
                    return super().__call__(req, timeout)
                if "/messages/om_root" in req.full_url:
                    return FakeResponse(
                        {
                            "code": 0,
                            "data": {"items": [{"message_id": "om_root", "thread_id": "omt_1"}]},
                        }
                    )
                if "container_id_type=thread" in req.full_url:
                    if not self.patched:
                        return FakeResponse(
                            {
                                "code": 0,
                                "data": {
                                    "items": [
                                        {
                                            "message_id": "om_root",
                                            "msg_type": "text",
                                            "update_time": "1000",
                                            "body": {"content": '{"text":"root"}'},
                                        },
                                        {
                                            "message_id": "om_card",
                                            "root_id": "om_root",
                                            "msg_type": "interactive",
                                            "update_time": "1000",
                                            "body": {"content": '{"title":"⏳ Agent Is Thinking"}'},
                                        },
                                    ]
                                },
                            }
                        )
                    return FakeResponse(
                        {
                            "code": 0,
                            "data": {
                                "items": [
                                    {
                                        "message_id": "om_root",
                                        "msg_type": "text",
                                        "update_time": "1000",
                                        "body": {"content": '{"text":"root"}'},
                                    },
                                    {
                                        "message_id": "om_card",
                                        "root_id": "om_root",
                                        "msg_type": "interactive",
                                        "update_time": "2000",
                                        "body": {"content": '{"title":"🤖 Agent","elements":[[{"tag":"text","text":"❓ 优先级？"}]]}'},
                                    },
                                ]
                            },
                        }
                    )
                raise AssertionError("chat-container listing must not be used: " + req.full_url)

        fake = PatchingFake()
        transport = OpenAPIThreadTransport(
            "u-token",
            "oc_chat",
            root_message_id="om_root",
            urlopen=fake,
        )

        first = transport.poll()
        self.assertEqual(first.message_id, "om_card")
        self.assertIn("⏳", first.text)
        fake.patched = True

        second = transport.poll()
        self.assertEqual(second.message_id, "om_card")
        self.assertIn("优先级", second.text)

    def test_session_root_message_is_never_polled(self):
        # The Session root is the Bridge's announcement, not a question; the
        # simulator must never see it (it once answered the topic before the
        # Agent's first Turn output existed).
        class RootFake(FakeUrlopen):
            def __call__(self, req, timeout=None):
                if "tenant_access_token/internal" in req.full_url:
                    return super().__call__(req, timeout)
                self.api_calls.append((req.full_url, req.get_header("Authorization") or ""))
                if "/messages/om_root" in req.full_url:
                    return FakeResponse(
                        {
                            "code": 0,
                            "data": {"items": [{"message_id": "om_root", "thread_id": "omt_1"}]},
                        }
                    )
                if "container_id_type=thread" in req.full_url:
                    return FakeResponse(
                        {
                            "code": 0,
                            "data": {
                                "items": [
                                    {
                                        "message_id": "om_root",
                                        "msg_type": "text",
                                        "body": {"content": '{"text":"📋 New Alignment Session"}'},
                                    },
                                ]
                            },
                        }
                    )
                raise AssertionError("chat-container listing must not be used: " + req.full_url)

        transport = OpenAPIThreadTransport(
            "u-token",
            "oc_chat",
            root_message_id="om_root",
            urlopen=RootFake(),
        )

        self.assertIsNone(transport.poll())
        self.assertIsNone(transport.poll())

    def test_posted_reply_is_not_polled_back_as_a_question(self):
        class ReplyEchoFake(FakeUrlopen):
            def __call__(self, req, timeout=None):
                self.api_calls.append(
                    (req.full_url, req.get_header("Authorization") or "")
                )
                if req.method == "POST":
                    return FakeResponse(
                        {"code": 0, "data": {"message_id": "om_reply", "create_time": "1000"}}
                    )
                if "/messages/om_root" in req.full_url:
                    return FakeResponse(
                        {
                            "code": 0,
                            "data": {"items": [{"message_id": "om_root", "thread_id": "omt_1"}]},
                        }
                    )
                if "container_id_type=thread" in req.full_url:
                    return FakeResponse(
                        {
                            "code": 0,
                            "data": {
                                "items": [
                                    {
                                        "message_id": "om_root",
                                        "msg_type": "text",
                                        "body": {"content": '{"text":"root"}'},
                                    },
                                    {
                                        "message_id": "om_reply",
                                        "root_id": "om_root",
                                        "msg_type": "text",
                                        "body": {"content": '{"text":"my answer"}'},
                                    },
                                    {
                                        "message_id": "om_agent",
                                        "root_id": "om_root",
                                        "msg_type": "text",
                                        "body": {"content": '{"text":"next question"}'},
                                    },
                                ]
                            },
                        }
                    )
                raise AssertionError("chat-container listing must not be used: " + req.full_url)

        transport = OpenAPIThreadTransport(
            "u-token",
            "oc_chat",
            root_message_id="om_root",
            urlopen=ReplyEchoFake(),
        )

        transport.post_reply("my answer")

        self.assertEqual(transport.poll().message_id, "om_agent")
        self.assertIsNone(transport.poll())


if __name__ == "__main__":
    unittest.main()
