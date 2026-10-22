"""End-to-end tests: pipeline, SDK client, and the transparent gateway proxy."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tokan import OptimizedAgentClient, OptimizationPipeline
from tokan.proxy import ProxyConfig, TokenOptimizerProxy

# A big, deliberately bloated payload that would normally waste tokens.
BLOATED_MESSAGES = [
    {"role": "system", "content": "You are an expert software engineer. " * 40},
    {
        "role": "user",
        "content": "Refactor this monolithic file and explain the trade-offs step by step:\n"
        "```python\n" + "\n".join(f"def fn_{i}(): return {i}" for i in range(80)) + "\n```",
    },
]


class FakeUpstream(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible upstream that counts its hits."""

    hits = 0
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        with FakeUpstream.lock:
            FakeUpstream.hits += 1
            n = FakeUpstream.hits
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


class UpstreamFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FakeUpstream.hits = 0
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        cls.upstream_port = cls.upstream.server_address[1]
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()


class TestPipeline(UpstreamFixture):
    def setUp(self):
        FakeUpstream.hits = 0
        self.pipeline = OptimizationPipeline.with_cdn(confidence_threshold=0.88)

    def test_prepare_flags_llm_route_on_refactor(self):
        result = self.pipeline.prepare({"messages": BLOATED_MESSAGES, "model": "gpt-4o"})
        self.assertEqual(result.route, "llm")
        self.assertGreater(result.record.original_tokens, result.record.canonical_tokens)

    def test_cache_hit_after_record_llm(self):
        payload = {"messages": BLOATED_MESSAGES, "model": "gpt-4o"}
        first = self.pipeline.prepare(payload)
        self.assertEqual(first.route, "llm")
        self.pipeline.record_llm(
            first,
            {"choices": [{"message": {"content": "do the refactor"}}], "usage": {"prompt_tokens": 90, "completion_tokens": 20}},
        )
        second = self.pipeline.prepare(payload)
        self.assertEqual(second.route, "cache")
        self.assertTrue(second.can_serve_locally)

    def test_system_prompt_noise_does_not_defeat_cache(self):
        noisy = [dict(m) for m in BLOATED_MESSAGES]
        noisy[0]["content"] = noisy[0]["content"].replace("engineer.", "engineer.\n\n")
        clean_first = self.pipeline.prepare({"messages": BLOATED_MESSAGES, "model": "gpt-4o"})
        self.pipeline.record_llm(clean_first, {"choices": [{"message": {"content": "ok"}}], "usage": {}})
        noisy_result = self.pipeline.prepare({"messages": noisy, "model": "gpt-4o"})
        self.assertEqual(noisy_result.route, "cache")

    def test_local_never_fires_without_executor_answer(self):
        # Simple yes/no heuristic could look "local", but no executor answered.
        result = self.pipeline.prepare({"messages": [{"role": "user", "content": "is this ok?"}]})
        self.assertFalse(result.can_serve_locally and result.local_answer is None)


class TestSDKClient(UpstreamFixture):
    def test_client_uses_injected_transport_and_caches(self):
        calls = []

        def transport(payload, api_key, timeout):
            calls.append(payload)
            return {
                "choices": [{"message": {"role": "assistant", "content": "refactored"}}],
                "usage": {"prompt_tokens": 80, "completion_tokens": 25, "total_tokens": 105},
            }

        client = OptimizedAgentClient(
            upstream_provider="openai",
            api_key="test-key",
            transport=transport,
            cdn_namespace="sdk-test",
        )
        first = client.chat_completion(BLOATED_MESSAGES, model="gpt-4o")
        self.assertEqual(first.route, "llm")
        self.assertEqual(len(calls), 1)
        # Forwarded messages were compacted, not raw.
        self.assertNotEqual(calls[0]["messages"], BLOATED_MESSAGES)

        second = client.chat_completion(BLOATED_MESSAGES, model="gpt-4o")
        self.assertEqual(second.route, "cache")
        self.assertEqual(len(calls), 1)  # no second upstream call
        self.assertIn("Session Token Savings", client.session_report())
        report = client.metrics()
        self.assertGreater(report["session"]["saved_pct"], 0)

    def test_sdk_without_transport_still_serves_cache_before_network(self):
        client = OptimizedAgentClient(api_key="unused", cdn_namespace="sdk-no-net")

        def no_network(*args, **kwargs):
            raise AssertionError("network should not be reached")

        client._transport = no_network
        # Prime with a fabricated LLM record, then verify cache short-circuit.
        result = client.pipeline.prepare({"messages": BLOATED_MESSAGES, "model": "m"})
        client.pipeline.record_llm(result, {"choices": [{"message": {"content": "cached"}}], "usage": {}})
        resp = client.chat_completion(BLOATED_MESSAGES, model="m")
        self.assertEqual(resp.route, "cache")
        self.assertEqual(resp.content, "cached")


class TestProxyGateway(UpstreamFixture):
    def setUp(self):
        FakeUpstream.hits = 0
        self.proxy = TokenOptimizerProxy(
            ProxyConfig(
                upstream_base=f"http://127.0.0.1:{self.upstream_port}",
                api_key="test-key",
                port=0,  # ephemeral
            ),
            OptimizationPipeline.with_cdn(confidence_threshold=0.88),
        ).start()

    def tearDown(self):
        self.proxy.shutdown()

    def _post(self, payload):
        req = urllib.request.Request(
            f"{self.proxy.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()), dict(resp.headers)

    def test_forwards_then_caches_transparently(self):
        payload = {"model": "gpt-4o", "messages": BLOATED_MESSAGES}
        body1, headers1 = self._post(payload)
        self.assertEqual(FakeUpstream.hits, 1)
        self.assertEqual(headers1.get("X-Optimizer-Route"), "llm")
        self.assertEqual(body1["choices"][0]["message"]["content"], "answer #1")

        body2, headers2 = self._post(payload)
        self.assertEqual(FakeUpstream.hits, 1)  # served from edge cache
        self.assertEqual(headers2.get("X-Optimizer-Route"), "cache")

    def test_healthz_and_metrics(self):
        with urllib.request.urlopen(f"{self.proxy.origin}/healthz", timeout=5) as resp:
            self.assertTrue(json.loads(resp.read())["ok"])
        with urllib.request.urlopen(f"{self.proxy.origin}/metrics", timeout=5) as resp:
            data = json.loads(resp.read())
        self.assertIn("session", data)
        self.assertIn("cache", data)

    def test_unknown_path_404s(self):
        req = urllib.request.Request(
            f"{self.proxy.base_url}/embeddings",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
