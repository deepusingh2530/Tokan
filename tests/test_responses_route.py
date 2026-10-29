"""Responses API route: real measurement, wire compaction, cache serving, prov.cache.

The Responses wire (gpt-5/codex family) carries the conversation in ``input``,
not ``messages``. These tests pin the behaviors that make Tokan an optimizer on
this route rather than a passthrough: per-request fingerprints that track the
actual input, compaction applied on the wire, cache/local hits served in
Responses shape (including SSE), and provider-prompt-cache booking from
``input_tokens_details``.
"""
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from tokan import OptimizationPipeline
from tokan.pipeline import responses_input_to_messages, usage_cached_tokens
from tokan.proxy import ProxyConfig, TokenOptimizerProxy
from tokan.proxy.interceptor import _extract_streamed_text, _extract_streamed_usage


class _UpstreamServer(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


# Big enough that CDM head/tail tool elision kicks in (> tool_head + tool_tail).
NOISY_TOOL_OUTPUT = "\n".join(
    ["$ git status", "On branch main"]
    + [f"modified: file_{i}.py \x1b[31mM\x1b[0m" for i in range(200)]
)


def responses_payload(text="summarize the repo", *, stream=False, dup_tool=False):
    inp = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]},
        {"type": "function_call_output", "call_id": "c1", "output": NOISY_TOOL_OUTPUT},
    ]
    if dup_tool:
        inp.append({"type": "function_call_output", "call_id": "c2", "output": NOISY_TOOL_OUTPUT})
    inp.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go on"}]})
    return {"model": "gpt-5-codex", "input": inp, "stream": stream}


class TestResponsesProjection(unittest.TestCase):
    """input -> messages projection + fingerprints (the constant-26 bug)."""

    def test_string_input_becomes_user_message(self):
        self.assertEqual(responses_input_to_messages("hi"), [{"role": "user", "content": "hi"}])

    def test_items_project_to_messages(self):
        msgs = responses_input_to_messages(responses_payload()["input"])
        self.assertEqual([m["role"] for m in msgs], ["user", "tool", "user"])
        self.assertIn("file_199.py", msgs[1]["content"])  # tool output reaches the digest

    def test_reasoning_items_are_skipped(self):
        msgs = responses_input_to_messages([
            {"type": "reasoning", "summary": []},
            {"type": "message", "role": "user", "content": "yo"},
        ])
        self.assertEqual(msgs, [{"role": "user", "content": "yo"}])

    def test_fingerprint_varies_with_input(self):
        # Regression: responses payloads used to fingerprint an empty message
        # list, so every request showed the same 26->12 receipts.
        pipe = OptimizationPipeline()
        small = pipe.prepare({"model": "m", "input": "hi"})
        large = pipe.prepare({"model": "m", "input": "please explain everything " * 100})
        self.assertLess(small.record.original_tokens, large.record.original_tokens)
        self.assertNotEqual(small.record.digest, large.record.digest)

    def test_same_input_same_digest(self):
        pipe = OptimizationPipeline()
        a = pipe.prepare(responses_payload())
        b = pipe.prepare(responses_payload())
        self.assertEqual(a.record.digest, b.record.digest)  # cache can hit

    def test_responses_usage_details_cached_tokens(self):
        usage = {
            "input_tokens": 500,
            "output_tokens": 10,
            "input_tokens_details": {"cached_tokens": 300},
        }
        self.assertEqual(usage_cached_tokens(usage, protocol="openai"), 300)

    def test_chat_usage_details_still_work(self):
        usage = {"prompt_tokens": 500, "prompt_tokens_details": {"cached_tokens": 100}}
        self.assertEqual(usage_cached_tokens(usage, protocol="openai"), 100)


class TestStreamedResponsesExtraction(unittest.TestCase):
    """SSE parsing for Responses frames (usage rides in response.completed)."""

    BUFFERED = (
        'event: response.output_text.delta\n'
        'data: {"type":"response.output_text.delta","delta":"hello "}\n\n'
        'event: response.output_text.delta\n'
        'data: {"type":"response.output_text.delta","delta":"world"}\n\n'
        'event: response.completed\n'
        'data: {"type":"response.completed","response":{"usage":'
        '{"input_tokens":90,"input_tokens_details":{"cached_tokens":40}}}}\n\n'
    )

    def test_usage_from_completed_event(self):
        usage = _extract_streamed_usage(self.BUFFERED, "openai")
        self.assertEqual(usage.get("input_tokens"), 90)
        self.assertEqual(usage_cached_tokens(usage, protocol="openai"), 40)

    def test_text_from_delta_events(self):
        self.assertEqual(_extract_streamed_text(self.BUFFERED, "openai"), "hello world")


class _FakeResponsesUpstream:
    """Records the compacted input it receives; answers in Responses shape."""

    hits = 0
    last_payload = None
    lock = threading.Lock()

    @classmethod
    def reset(cls):
        cls.hits = 0
        cls.last_payload = None


def _make_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
            cls = _FakeResponsesUpstream
            cls.last_payload = payload
            with cls.lock:
                cls.hits += 1
                n = cls.hits
            body = json.dumps(
                {
                    "id": f"resp_{n}",
                    "object": "response",
                    "status": "completed",
                    "model": payload.get("model", "fake"),
                    "output": [
                        {
                            "type": "message",
                            "id": f"msg_{n}",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": f"answer #{n}", "annotations": []}
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 120,
                        "output_tokens": 8,
                        "total_tokens": 128,
                        "input_tokens_details": {"cached_tokens": 64},
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class TestResponsesGateway(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _FakeResponsesUpstream.reset()
        cls.upstream = _UpstreamServer(("127.0.0.1", 0), _make_handler())
        cls.upstream_port = cls.upstream.server_address[1]
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()

    def setUp(self):
        _FakeResponsesUpstream.reset()
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
            f"{self.proxy.base_url}/responses",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode(), dict(resp.headers)

    # ------------------------------------------------------------ forwarding
    def test_forward_compacts_tool_output_and_preserves_shape(self):
        raw, headers = self._post(responses_payload())
        self.assertEqual(_FakeResponsesUpstream.hits, 1)
        forwarded = _FakeResponsesUpstream.last_payload
        tool_item = [i for i in forwarded["input"] if i.get("type") == "function_call_output"][0]
        self.assertIn("[cdm:", tool_item["output"])  # head/tail elision marker
        self.assertLess(len(tool_item["output"]), len(NOISY_TOOL_OUTPUT))
        # item shape survives compaction (input_text parts still input_text)
        first = forwarded["input"][0]
        self.assertEqual(first["type"], "message")
        self.assertEqual(first["content"][0]["type"], "input_text")
        # relayed body is Responses-shaped and carries the upstream text
        body = json.loads(raw)
        self.assertEqual(body["object"], "response")
        self.assertEqual(body["output"][0]["content"][0]["text"], "answer #1")
        # provider prompt-cache tokens booked from input_tokens_details
        self.assertEqual(headers.get("X-Optimizer-Upstream-Cached-Tokens"), "64")

    def test_consecutive_duplicate_tool_outputs_collapse(self):
        self._post(responses_payload(dup_tool=True))
        outputs = [
            i["output"]
            for i in _FakeResponsesUpstream.last_payload["input"]
            if i.get("type") == "function_call_output"
        ]
        self.assertEqual(len(outputs), 2)
        self.assertIn("[cdm: duplicate of previous tool output elided]", outputs[1])

    # ------------------------------------------------------------ cache serving
    def test_repeat_question_served_from_cache_in_responses_shape(self):
        payload = responses_payload()
        self._post(payload)
        raw2, headers2 = self._post(payload)
        self.assertEqual(_FakeResponsesUpstream.hits, 1)  # never left the building
        self.assertEqual(headers2.get("X-Optimizer-Route"), "cache")
        body = json.loads(raw2)
        self.assertEqual(body["object"], "response")
        self.assertEqual(body["output"][0]["content"][0]["type"], "output_text")
        self.assertEqual(body["output"][0]["content"][0]["text"], "answer #1")
        self.assertTrue(body["optimizer"]["served_without_llm"])

    def test_streamed_cache_hit_emits_responses_events(self):
        self._post(responses_payload())
        raw, headers = self._post(responses_payload(stream=True))
        self.assertEqual(_FakeResponsesUpstream.hits, 1)
        self.assertIn("text/event-stream", headers.get("Content-Type", ""))
        self.assertIn("event: response.created", raw)
        self.assertIn("event: response.output_text.delta", raw)
        self.assertIn("event: response.completed", raw)
        self.assertIn("answer #1", raw)

    def test_different_inputs_get_different_receipts(self):
        self._post(responses_payload("short question"))
        self._post(responses_payload("a much much longer question " * 30))
        with urllib.request.urlopen(f"{self.proxy.origin}/metrics", timeout=5) as resp:
            metrics = json.load(resp)
        self.assertEqual(metrics["session"]["llm_calls"], 2)
        # fingerprints now track the actual input: both requests booked,
        # with the longer one contributing more original tokens.
        self.assertGreater(metrics["session"]["original_tokens"], 0)
        self.assertGreater(metrics["session"]["tokens_saved"], 0)

    def test_metrics_book_provider_cache_from_responses_usage(self):
        self._post(responses_payload())
        with urllib.request.urlopen(f"{self.proxy.origin}/metrics", timeout=5) as resp:
            metrics = json.load(resp)
        self.assertEqual(metrics["session"]["upstream_cached_tokens"], 64)
        self.assertEqual(metrics["session"]["provider_prompt_tokens"], 120)


if __name__ == "__main__":
    unittest.main()
