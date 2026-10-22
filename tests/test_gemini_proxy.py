"""Gemini wire passthrough: generateContent + streamGenerateContent SSE.

Gemini clients (Gemini CLI via GOOGLE_GEMINI_BASE_URL, SDKs) speak a
part-based ``contents`` wire, not ``messages``. The gateway must compact
text parts in place, preserve non-text parts and the generationConfig
envelope, relay SSE streams, and serve cache hits in Gemini shape.
"""
import json
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tokan import OptimizationPipeline
from tokan.proxy import ProxyConfig, TokenOptimizerProxy
from tokan.proxy.interceptor import _Handler

BLOATED_TEXT = (
    "Refactor this monolithic file and explain the trade-offs step by step:\n"
    "```python\n" + "\n".join(f"def fn_{i}(): return {i}" for i in range(80)) + "\n```\n"
    "Terminal dump follows: \x1b[0;31merror\x1b[0m   \n\n\n"
)
GEMINI_PAYLOAD = {
    "model": "gemini-2.0-flash",
    "contents": [{"role": "user", "parts": [{"text": BLOATED_TEXT}]}],
    "generationConfig": {"temperature": 0.2},
}
GENERATE_PATH = "/v1beta/models/gemini-2.0-flash:generateContent"
STREAM_PATH = "/v1beta/models/gemini-2.0-flash:streamGenerateContent?alt=sse"


class FakeGeminiUpstream(BaseHTTPRequestHandler):
    hits = 0
    last_path = ""
    last_headers = {}
    last_payload = None
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        with FakeGeminiUpstream.lock:
            FakeGeminiUpstream.hits += 1
            n = FakeGeminiUpstream.hits
            FakeGeminiUpstream.last_path = self.path
            FakeGeminiUpstream.last_headers = {k.lower(): v for k, v in self.headers.items()}
            FakeGeminiUpstream.last_payload = payload

        if ":streamGenerateContent" in self.path:
            frames = []
            for text in ("streamed ", "answer"):
                frame = {
                    "candidates": [
                        {
                            "content": {"parts": [{"text": text}], "role": "model"},
                            "finishReason": "STOP",
                        }
                    ]
                }
                frames.append(f"data: {json.dumps(frame)}\n\n")
            body = "".join(frames).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
        else:
            body = json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": f"gemini answer #{n}"}],
                                "role": "model",
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 10},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestGeminiProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FakeGeminiUpstream.hits = 0
        FakeGeminiUpstream.last_payload = None
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeGeminiUpstream)
        cls.upstream_port = cls.upstream.server_address[1]
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def setUp(self):
        FakeGeminiUpstream.hits = 0
        FakeGeminiUpstream.last_path = ""
        FakeGeminiUpstream.last_headers = {}
        FakeGeminiUpstream.last_payload = None
        self._make_proxy()

    def _make_proxy(self, **config_kwargs):
        self.proxy = TokenOptimizerProxy(
            ProxyConfig(
                port=0,
                gemini_upstream=f"http://127.0.0.1:{self.upstream_port}",
                **config_kwargs,
            ),
            OptimizationPipeline.with_cdn(confidence_threshold=0.88),
        ).start()
        self.addCleanup(self.proxy.shutdown)

    def _request(self, path, payload, headers=None):
        req = urllib.request.Request(
            f"{self.proxy.origin}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read(), dict(resp.headers)

    def test_generate_content_forwards_compacted_and_caches(self):
        body1, headers1 = self._request(GENERATE_PATH, GEMINI_PAYLOAD)
        self.assertEqual(headers1["X-Optimizer-Route"], "llm")
        self.assertEqual(FakeGeminiUpstream.hits, 1)
        self.assertEqual(
            FakeGeminiUpstream.last_path, "/v1beta/models/gemini-2.0-flash:generateContent"
        )

        sent = FakeGeminiUpstream.last_payload
        self.assertIn("contents", sent)
        self.assertNotIn("messages", sent)
        # generationConfig envelope survives compaction untouched.
        self.assertEqual(sent["generationConfig"], {"temperature": 0.2})
        text = sent["contents"][0]["parts"][0]["text"]
        self.assertNotIn("\x1b", text)  # ANSI stripped
        self.assertLess(len(text), len(BLOATED_TEXT))  # compacted, not verbatim

        # Answer relayed in Gemini shape, then served from cache upstream-free.
        parsed = json.loads(body1)
        self.assertEqual(parsed["candidates"][0]["content"]["parts"][0]["text"], "gemini answer #1")
        body2, headers2 = self._request(GENERATE_PATH, GEMINI_PAYLOAD)
        self.assertEqual(FakeGeminiUpstream.hits, 1)
        self.assertEqual(headers2["X-Optimizer-Route"], "cache")
        self.assertEqual(
            json.loads(body2)["candidates"][0]["content"]["parts"][0]["text"],
            "gemini answer #1",
        )

    def test_gateway_key_replaces_client_google_key(self):
        self._make_proxy(gemini_api_key="gw-gemini-key")
        self._request(
            GENERATE_PATH,
            GEMINI_PAYLOAD,
            headers={"x-goog-api-key": "client-key", "Authorization": "Bearer oauth"},
        )
        got = FakeGeminiUpstream.last_headers
        self.assertEqual(got.get("x-goog-api-key"), "gw-gemini-key")
        self.assertNotIn("oauth", got.get("authorization", ""))

    def test_client_google_key_forwarded_when_gateway_has_none(self):
        self._request(GENERATE_PATH, GEMINI_PAYLOAD, headers={"x-goog-api-key": "client-key"})
        self.assertEqual(
            FakeGeminiUpstream.last_headers.get("x-goog-api-key"), "client-key"
        )

    def test_nontext_parts_pass_through_verbatim(self):
        payload = {
            "model": "gemini-2.0-flash",
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": "What is in this image? " + "context " * 60},
                        {"inline_data": {"mime_type": "image/png", "data": "iVBORw0KGgo="}},
                    ],
                }
            ],
        }
        self._request(GENERATE_PATH, payload)
        parts = FakeGeminiUpstream.last_payload["contents"][0]["parts"]
        self.assertEqual(
            parts[1], {"inline_data": {"mime_type": "image/png", "data": "iVBORw0KGgo="}}
        )

    def test_stream_relayed_and_answer_cached(self):
        body, _ = self._request(STREAM_PATH, GEMINI_PAYLOAD)
        text = body.decode("utf-8")
        self.assertIn("data: {", text)
        # Both SSE frames arrived, carrying the answer fragments.
        self.assertIn('"text": "streamed "', text)
        self.assertIn('"text": "answer"', text)
        self.assertEqual(FakeGeminiUpstream.hits, 1)
        self.assertIn("streamGenerateContent?alt=sse", FakeGeminiUpstream.last_path)

        # The extracted answer (never the SSE framing) now serves from cache.
        body2, headers2 = self._request(GENERATE_PATH, GEMINI_PAYLOAD)
        self.assertEqual(FakeGeminiUpstream.hits, 1)
        self.assertEqual(headers2["X-Optimizer-Route"], "cache")
        self.assertEqual(
            json.loads(body2)["candidates"][0]["content"]["parts"][0]["text"],
            "streamed answer",
        )

    def test_cache_hit_on_stream_endpoint_serves_local_sse(self):
        self._request(GENERATE_PATH, GEMINI_PAYLOAD)  # prime cache via LLM
        body, _ = self._request(STREAM_PATH, GEMINI_PAYLOAD)
        self.assertEqual(FakeGeminiUpstream.hits, 1)  # served locally
        text = body.decode("utf-8")
        self.assertIn("gemini answer #1", text)
        self.assertTrue(text.startswith("data: {"))

    def test_classify_gemini_paths(self):
        self.assertEqual(
            _Handler._classify(GENERATE_PATH), ("gemini", "generate_content")
        )
        self.assertEqual(
            _Handler._classify(STREAM_PATH), ("gemini", "generate_content_stream")
        )


if __name__ == "__main__":
    unittest.main()
