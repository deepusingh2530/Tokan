"""End-to-end tests for the Anthropic Messages wire (Claude Code passthrough)."""
import json
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tokan import OptimizationPipeline
from tokan.proxy import ProxyConfig, TokenOptimizerProxy

ANTHROPIC_BODY = {
    "model": "claude-sonnet-4-5",
    "max_tokens": 1024,
    "system": "You are an expert software engineer.",
    "messages": [
        {"role": "user", "content": "Refactor this monolith and explain trade-offs step by step"}
    ],
}


class FakeAnthropicUpstream(BaseHTTPRequestHandler):
    hits = 0

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        json.loads(self.rfile.read(length))
        FakeAnthropicUpstream.hits += 1
        n = FakeAnthropicUpstream.hits
        body = json.dumps(
            {
                "id": f"msg_fake{n}",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": f"anthropic answer #{n}"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 70, "output_tokens": 12},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AnthropicProxyFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FakeAnthropicUpstream.hits = 0
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeAnthropicUpstream)
        cls.upstream_port = cls.upstream.server_address[1]
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def setUp(self):
        FakeAnthropicUpstream.hits = 0
        self.proxy = TokenOptimizerProxy(
            ProxyConfig(
                upstream_base=f"http://127.0.0.1:{self.upstream_port}",
                anthropic_upstream=f"http://127.0.0.1:{self.upstream_port}",
                port=0,
            ),
            OptimizationPipeline.with_cdn(),
        ).start()

    def tearDown(self):
        self.proxy.shutdown()

    def _post(self, path, payload):
        req = urllib.request.Request(
            f"{self.proxy.origin}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()), dict(resp.headers)


class TestAnthropicWire(AnthropicProxyFixture):
    def test_forwards_then_caches(self):
        body1, headers1 = self._post("/v1/messages", ANTHROPIC_BODY)
        self.assertEqual(FakeAnthropicUpstream.hits, 1)
        self.assertEqual(headers1["X-Optimizer-Route"], "llm")
        self.assertEqual(body1["type"], "message")
        self.assertEqual(body1["content"][0]["text"], "anthropic answer #1")

        body2, headers2 = self._post("/v1/messages", ANTHROPIC_BODY)
        self.assertEqual(FakeAnthropicUpstream.hits, 1)  # cache hit, no upstream
        self.assertEqual(headers2["X-Optimizer-Route"], "cache")
        # Cache stores text; serving reshapes into Anthropic blocks.
        self.assertEqual(body2["type"], "message")
        self.assertEqual(body2["content"][0]["type"], "text")
        self.assertEqual(body2["content"][0]["text"], "anthropic answer #1")
        self.assertIn("usage", body2)

    def test_system_prompt_change_busts_cache(self):
        self._post("/v1/messages", ANTHROPIC_BODY)
        self.assertEqual(FakeAnthropicUpstream.hits, 1)
        changed = dict(ANTHROPIC_BODY, system="You are a terse pirate.")
        _, headers = self._post("/v1/messages", changed)
        self.assertEqual(headers["X-Optimizer-Route"], "llm")
        self.assertEqual(FakeAnthropicUpstream.hits, 2)

    def test_count_tokens_answers_locally(self):
        payload = {"system": "You are helpful.", "messages": [{"role": "user", "content": "hello"}]}
        body, _ = self._post("/v1/messages/count_tokens", payload)
        self.assertIsInstance(body["input_tokens"], int)
        self.assertGreater(body["input_tokens"], 0)
        self.assertEqual(FakeAnthropicUpstream.hits, 0)  # never leaves the machine

    def test_unknown_path_still_404s(self):
        import urllib.error

        req = urllib.request.Request(
            f"{self.proxy.origin}/v1/completions",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 404)

    def test_classify_dispatch(self):
        from tokan.proxy.interceptor import _Handler

        self.assertEqual(_Handler._classify("/v1/chat/completions"), ("openai", "chat"))
        self.assertEqual(_Handler._classify("/v1/messages"), ("anthropic", "messages"))
        self.assertEqual(_Handler._classify("/v1/messages/count_tokens"), ("anthropic", "count_tokens"))
        self.assertEqual(_Handler._classify("/v1/embeddings"), (None, None))


class TestStreamedTextExtraction(unittest.TestCase):
    """Relayed SSE must cache the answer text, never the framing."""

    def test_openai_delta_extraction(self):
        from tokan.proxy.interceptor import _extract_streamed_text

        buffered = (
            'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        self.assertEqual(_extract_streamed_text(buffered, "openai"), "Hello")

    def test_anthropic_delta_extraction(self):
        from tokan.proxy.interceptor import _extract_streamed_text

        buffered = (
            'data: {"type":"message_start","message":{"content":[]}}\n\n'
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi "}}\n\n'
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"there"}}\n\n'
            'data: {"type":"message_stop"}\n\n'
        )
        self.assertEqual(_extract_streamed_text(buffered, "anthropic"), "Hi there")

    def test_framing_never_leaks(self):
        from tokan.proxy.interceptor import _extract_streamed_text

        buffered = 'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
        out = _extract_streamed_text(buffered, "openai")
        self.assertNotIn("data:", out)
        self.assertNotIn("choices", out)


if __name__ == "__main__":
    unittest.main()
