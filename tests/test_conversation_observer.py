"""Observe-only conversation memory, end to end through the gateway.

Parity contract: with ``--conversation-db`` the wire behavior (response body,
status, ``X-Optimizer-*`` headers, receipt fields other than the added ``rid``)
is identical to running without it. The store records; it never serves.
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tokan.memory import ConversationStore, extract_state
from tokan.proxy import ProxyConfig, TokenOptimizerProxy


class _Upstream(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


class _EchoUpstream(BaseHTTPRequestHandler):
    """OpenAI-shaped upstream; answers deterministically per hit count."""

    hits = 0
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        with _EchoUpstream.lock:
            _EchoUpstream.hits += 1
            n = _EchoUpstream.hits
        body = json.dumps(
            {
                "id": f"chatcmpl-{n}",
                "object": "chat.completion",
                "model": payload.get("model", "fake-1"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": f"answer #{n}"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _chat(origin, messages, session="conv-e2e", model="gpt-4o"):
    req = urllib.request.Request(
        f"{origin}/v1/chat/completions",
        data=json.dumps({"model": model, "messages": messages}).encode(),
        headers={"Content-Type": "application/json", "X-Optimizer-Session": session},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, dict(resp.headers), json.loads(resp.read())


def _wait_messages(db_path, conversation, minimum, timeout=5.0):
    """The observer runs just after the response is flushed, so reading the
    store immediately can race it — poll until ``minimum`` message events are
    visible (or fail)."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        store = ConversationStore(db_path)
        try:
            n = sum(1 for e in store.events(conversation) if e.kind == "message")
        finally:
            store.close()
        if n >= minimum:
            return
        time.sleep(0.05)
    raise AssertionError(f"expected >={minimum} message events within {timeout}s")


class TestConversationObserver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _EchoUpstream.hits = 0
        cls.upstream = _Upstream(("127.0.0.1", 0), _EchoUpstream)
        cls.upstream_port = cls.upstream.server_address[1]
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def setUp(self):
        _EchoUpstream.hits = 0

    def _start_gateway(self, **overrides):
        cfg = ProxyConfig(
            upstream_base=f"http://127.0.0.1:{self.upstream_port}",
            port=0,  # ephemeral — never fight over a fixed port
            **overrides,
        )
        proxy = TokenOptimizerProxy(cfg).start()
        self.addCleanup(proxy.shutdown)
        return proxy

    def test_two_turn_conversation_is_recorded(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        proxy = self._start_gateway(conversation_db=os.path.join(tmp.name, "conv.db"))
        origin = f"http://127.0.0.1:{proxy.port}"

        turn1 = [{"role": "user", "content": "Fix the flaky test in tests/test_cache.py"}]
        status1, headers1, body1 = _chat(origin, turn1)
        _wait_messages(os.path.join(tmp.name, "conv.db"), "conv-e2e", 2)
        turn2 = [
            {"role": "user", "content": "Fix the flaky test in tests/test_cache.py"},
            {"role": "assistant", "content": body1["choices"][0]["message"]["content"]},
            {"role": "user", "content": "Now also update docs/USAGE.md — never touch CI files"},
        ]
        status2, headers2, body2 = _chat(origin, turn2)

        self.assertEqual((status1, status2), (200, 200))
        # Both turns are genuinely different payloads -> both went upstream.
        self.assertEqual(body1["choices"][0]["message"]["content"], "answer #1")
        self.assertEqual(body2["choices"][0]["message"]["content"], "answer #2")
        self.assertEqual(headers1["X-Optimizer-Route"], "llm")

        _wait_messages(os.path.join(tmp.name, "conv.db"), "conv-e2e", 4)
        store = ConversationStore(os.path.join(tmp.name, "conv.db"))
        events = store.events("conv-e2e")
        messages = [e for e in events if e.kind == "message"]
        manifests = [e for e in events if e.kind == "manifest"]
        # Turn 1: user question + assistant answer. Turn 2: only the NEW user
        # message (resent history is deduped) + new answer + manifests.
        self.assertEqual(
            [(e.source, e.content.get("text", "")) for e in messages],
            [
                ("user", "Fix the flaky test in tests/test_cache.py"),
                ("assistant", "answer #1"),
                ("user", "Now also update docs/USAGE.md — never touch CI files"),
                ("assistant", "answer #2"),
            ],
        )
        self.assertEqual(len(manifests), 2)
        self.assertEqual(manifests[-1].content["route"], "llm")
        # Structured state resolves to the latest objective + constraint.
        state = extract_state(events)
        self.assertIn("docs/USAGE.md", state.objective)
        self.assertTrue(any("never touch CI files" in c for c in state.constraints))

    def test_parity_with_store_disabled(self):
        # Identical requests through a store-less gateway: no rid, same shape.
        proxy = self._start_gateway()
        origin = f"http://127.0.0.1:{proxy.port}"
        messages = [{"role": "user", "content": "parity check payload"}]
        _, headers, body = _chat(origin, messages, session="parity")
        self.assertNotIn("X-Optimizer-Session-Id", headers)
        receipt_rows = json.loads(
            urllib.request.urlopen(f"{origin}/receipts", timeout=5).read()
        )["receipts"]
        self.assertNotIn("rid", receipt_rows[-1])

    def test_rid_present_only_with_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        proxy = self._start_gateway(conversation_db=os.path.join(tmp.name, "conv.db"))
        origin = f"http://127.0.0.1:{proxy.port}"
        _chat(origin, [{"role": "user", "content": "rid please"}], session="rid-conv")
        receipt_rows = json.loads(
            urllib.request.urlopen(f"{origin}/receipts", timeout=5).read()
        )["receipts"]
        self.assertIn("rid", receipt_rows[-1])

    def test_verbatim_resend_records_no_duplicate(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        proxy = self._start_gateway(conversation_db=os.path.join(tmp.name, "conv.db"))
        origin = f"http://127.0.0.1:{proxy.port}"
        messages = [{"role": "user", "content": "idempotent ask"}]
        _chat(origin, messages, session="resend-conv")
        _wait_messages(os.path.join(tmp.name, "conv.db"), "resend-conv", 2)
        _chat(origin, messages, session="resend-conv")  # verbatim retry
        _wait_messages(os.path.join(tmp.name, "conv.db"), "resend-conv", 2)
        store = ConversationStore(os.path.join(tmp.name, "conv.db"))
        user_msgs = [
            e for e in store.events("resend-conv")
            if e.kind == "message" and e.source == "user"
        ]
        self.assertEqual(len(user_msgs), 1)

    def test_store_failure_fails_open(self):
        # A conversation_db path that cannot be created must not take the
        # gateway down — it serves on with the observer disabled.
        bad_dir = tempfile.TemporaryDirectory()
        self.addCleanup(bad_dir.cleanup)
        blocked = os.path.join(bad_dir.name, "no", "such", "dir", "x.db")
        proxy = TokenOptimizerProxy(
            ProxyConfig(
                upstream_base=f"http://127.0.0.1:{self.upstream_port}",
                port=0,
                conversation_db=blocked,
            )
        )
        # Construction succeeds; the store is None because SQLite cannot open.
        self.assertIsNone(proxy.store)
        proxy.start()
        self.addCleanup(proxy.shutdown)
        origin = f"http://127.0.0.1:{proxy.port}"
        status, _, body = _chat(origin, [{"role": "user", "content": "still alive"}], session="fail-open")
        self.assertEqual(status, 200)
        self.assertIn("answer", body["choices"][0]["message"]["content"])


if __name__ == "__main__":
    unittest.main()
