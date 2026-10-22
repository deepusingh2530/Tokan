"""Gateway credential/identity passthrough — the plumbing GitHub Copilot needs.

A Copilot client (VS Code with ``overrideProxyUrl``, or Copilot CLI in BYOK
provider mode) arrives with an OAuth token and identity headers the upstream
Copilot API validates. The gateway must relay them untouched, let a
server-side key override them, fill gaps with fallbacks, and pass the
``GET /models`` listing that model pickers call first.
"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tokan import OptimizationPipeline
from tokan.proxy import ProxyConfig, TokenOptimizerProxy
from tokan.proxy.interceptor import (
    COPILOT_FALLBACK_HEADERS,
    COPILOT_UPSTREAMS,
    resolve_upstream_flags,
)


def refactor_payload() -> dict:
    """A payload JeV reliably routes to the LLM (mirrors TestProxyGateway)."""
    return {
        "model": "gpt-4.1",
        "messages": [
            {"role": "system", "content": "You are an expert software engineer. " * 40},
            {
                "role": "user",
                "content": "Refactor this monolithic file and explain the trade-offs step by step:\n"
                "```python\n"
                + "\n".join(f"def fn_{i}(): return {i}" for i in range(80))
                + "\n```",
            },
        ],
    }


class RecordingUpstream(BaseHTTPRequestHandler):
    """OpenAI-shaped upstream that records the headers it received."""

    hits = 0
    last_headers = {}
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def _record(self):
        with RecordingUpstream.lock:
            RecordingUpstream.hits += 1
            RecordingUpstream.last_headers = {
                k.lower(): v for k, v in self.headers.items()
            }

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        self._record()
        body = json.dumps(
            {
                "id": "chatcmpl-recording",
                "object": "chat.completion",
                "model": payload.get("model", "fake-1"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "recorded answer"},
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

    def do_GET(self):
        self._record()
        body = json.dumps(
            {"object": "list", "data": [{"id": "gpt-4o"}, {"id": "gpt-4.1"}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RecordingFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        RecordingUpstream.hits = 0
        RecordingUpstream.last_headers = {}
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), RecordingUpstream)
        cls.upstream_port = cls.upstream.server_address[1]
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()


class TestHeaderPassthrough(RecordingFixture):
    def setUp(self):
        RecordingUpstream.hits = 0
        RecordingUpstream.last_headers = {}

    def _start(self, **config_kwargs):
        config_kwargs.setdefault(
            "upstream_base", f"http://127.0.0.1:{self.upstream_port}"
        )
        config = ProxyConfig(port=0, **config_kwargs)
        self.proxy = TokenOptimizerProxy(
            config, OptimizationPipeline.with_cdn(confidence_threshold=0.88)
        ).start()
        self.addCleanup(self.proxy.shutdown)

    def _post(self, payload, headers=None, path="/chat/completions"):
        req = urllib.request.Request(
            f"{self.proxy.base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()), dict(resp.headers)

    # ------------------------------------------------------------ forwarding
    def test_client_auth_and_copilot_identity_headers_forwarded(self):
        # No gateway key: the client's credentials must reach the upstream.
        self._start()
        _, headers = self._post(
            refactor_payload(),
            headers={
                "Authorization": "Bearer gho_copilot_token",
                "Copilot-Integration-Id": "vscode-chat",
                "Editor-Version": "1.107.0",
                "Editor-Plugin-Version": "copilot-chat/0.32.0",
                "Conversation-ID": "conv-123",
            },
        )
        self.assertEqual(headers.get("X-Optimizer-Route"), "llm")
        got = RecordingUpstream.last_headers
        self.assertEqual(got.get("authorization"), "Bearer gho_copilot_token")
        self.assertEqual(got.get("copilot-integration-id"), "vscode-chat")
        self.assertEqual(got.get("editor-version"), "1.107.0")
        self.assertEqual(got.get("editor-plugin-version"), "copilot-chat/0.32.0")
        self.assertEqual(got.get("conversation-id"), "conv-123")

    def test_gateway_api_key_overrides_client_auth(self):
        # A configured key wins; the client's token must not leak upstream.
        self._start(api_key="gateway-key")
        self._post(
            refactor_payload(),
            headers={"Authorization": "Bearer client-token"},
        )
        got = RecordingUpstream.last_headers
        self.assertEqual(got.get("authorization"), "Bearer gateway-key")

    def test_fallback_headers_fill_gaps_only(self):
        # BYOK clients send no identity headers — fallbacks stand in.
        self._start(fallback_headers=dict(COPILOT_FALLBACK_HEADERS))
        self._post(refactor_payload(), headers={"Editor-Version": "1.107.0"})
        got = RecordingUpstream.last_headers
        self.assertEqual(got.get("editor-version"), "1.107.0")  # real value kept
        self.assertEqual(got.get("copilot-integration-id"), "vscode-chat")  # gap filled

    def test_extra_headers_override_client_values(self):
        self._start(extra_headers={"Copilot-Integration-Id": "server-override"})
        self._post(
            refactor_payload(),
            headers={"Copilot-Integration-Id": "client-value"},
        )
        self.assertEqual(
            RecordingUpstream.last_headers.get("copilot-integration-id"),
            "server-override",
        )

    def test_anthropic_client_key_forwarded_when_gateway_has_none(self):
        # Adapters promise "keep your existing key; the gateway forwards it".
        self._start(anthropic_upstream=f"http://127.0.0.1:{self.upstream_port}")
        req = urllib.request.Request(
            f"{self.proxy.origin}/v1/messages",
            data=json.dumps(
                {
                    "model": "claude-sonnet-4-5",
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": "hello " * 400}],
                }
            ).encode(),
            headers={"Content-Type": "application/json", "x-api-key": "sk-ant-client"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        self.assertEqual(
            RecordingUpstream.last_headers.get("x-api-key"), "sk-ant-client"
        )

    # --------------------------------------------------------------- models
    def test_models_listing_passes_through_with_client_auth(self):
        self._start()
        req = urllib.request.Request(
            f"{self.proxy.base_url}/models",
            headers={"Authorization": "Bearer client-token"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        self.assertEqual([m["id"] for m in data["data"]], ["gpt-4o", "gpt-4.1"])
        got = RecordingUpstream.last_headers
        self.assertEqual(got.get("authorization"), "Bearer client-token")

    def test_models_listing_uses_gateway_key_when_configured(self):
        self._start(api_key="gateway-key")
        req = urllib.request.Request(
            f"{self.proxy.origin}/v1/models", method="GET"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        self.assertEqual(
            RecordingUpstream.last_headers.get("authorization"), "Bearer gateway-key"
        )

    def test_models_upstream_unreachable_is_502_not_crash(self):
        self._start(upstream_base="http://127.0.0.1:1")  # nothing listens here
        req = urllib.request.Request(f"{self.proxy.base_url}/models", method="GET")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 502)


class TestUpstreamFlags(unittest.TestCase):
    def _ns(self, **overrides):
        base = dict(upstream="https://api.openai.com/v1", copilot=None, extra_header=[])
        base.update(overrides)
        return Namespace(**base)

    def test_coplanot_selects_plan_upstream_and_fallbacks(self):
        upstream, extra, fallback = resolve_upstream_flags(
            self._ns(copilot="business", extra_header=["X-Team: platform"])
        )
        self.assertEqual(upstream, COPILOT_UPSTREAMS["business"])
        self.assertEqual(extra, {"X-Team": "platform"})
        self.assertEqual(fallback, COPILOT_FALLBACK_HEADERS)

    def test_coplanot_defaults_to_individual_plan(self):
        # argparse nargs="?" without a value yields the const, "individual".
        upstream, _, _ = resolve_upstream_flags(self._ns(copilot="individual"))
        self.assertEqual(upstream, "https://api.githubcopilot.com")

    def test_no_coplanot_keeps_configured_upstream(self):
        upstream, extra, fallback = resolve_upstream_flags(self._ns())
        self.assertEqual(upstream, "https://api.openai.com/v1")
        self.assertEqual(extra, {})
        self.assertEqual(fallback, {})

    def test_malformed_extra_header_raises(self):
        with self.assertRaises(ValueError):
            resolve_upstream_flags(self._ns(extra_header=["missing-a-colon"]))

    def test_extra_header_preserves_value_colons(self):
        # "Authorization: Bearer a:b:c" — split on the first colon only.
        _, extra, _ = resolve_upstream_flags(
            self._ns(extra_header=["Authorization: Bearer a:b:c"])
        )
        self.assertEqual(extra["Authorization"], "Bearer a:b:c")


if __name__ == "__main__":
    unittest.main()
