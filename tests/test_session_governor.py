"""Session Governor (Phase 1): prefix-stable compaction + provider prompt-cache
economics.

The core honesty claim under test: Tokan's compaction is *deterministic*, so a
growing conversation's shared prefix serializes byte-identically every turn --
the exact condition a provider's prompt cache needs to hit. The gateway then
measures that reused prefix (the base the provider discounts) and surfaces the
provider's cache ratio, without ever counting either as Tokan's own savings.

Tests cover the pure units (normalizer determinism, SessionTracker LCP), the
pipeline/metrics integration, and the end-to-end wire path against a fake
prompt-caching upstream that simulates OpenAI's prefix cache (cached_tokens =
longest-common-prefix with the previous request).
"""
import json
import re
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

from tokan.cdm import CDMNormalizer, estimate_tokens
from tokan.metrics import SessionMetrics
from tokan.pipeline import OptimizationPipeline
from tokan.proxy.interceptor import ProxyConfig, TokenOptimizerProxy
from tokan.session import SESSION_HEADER, PrefixReuse, SessionTracker


# --------------------------------------------------------------------- utils
def _post(url, payload, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = Request(url, data=json.dumps(payload).encode("utf-8"), headers=h, method="POST")
    with urlopen(req, timeout=5) as resp:
        return resp.status, dict(resp.headers), json.loads(resp.read().decode("utf-8"))


def _get(url):
    with urlopen(url, timeout=5) as resp:
        return resp.status, dict(resp.headers), resp.read().decode("utf-8")


def _tokenize(text):
    """Rough word/punct tokenizer standing in for a real BPE."""
    return re.findall(r"\w+|[^\w\s]", text)


def _make_server(handler_cls):
    import threading

    class _Srv(ThreadingHTTPServer):
        allow_reuse_address = True

    srv = _Srv(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def _run(handler_cls, config=None, pipeline=None, fn=None):
    up, up_thread = _make_server(handler_cls)
    base = f"http://127.0.0.1:{up.server_address[1]}/v1"
    pipe = pipeline or OptimizationPipeline()
    cfg = config(base) if callable(config) else (config or ProxyConfig(upstream_base=base))
    cfg.port = 0
    gw = TokenOptimizerProxy(cfg, pipe).start()
    try:
        return fn(gw, gw.origin)
    finally:
        gw.shutdown()
        up.shutdown()
        up.server_close()


def _cache_upstream():
    """Fake OpenAI upstream that simulates the provider's prompt cache.

    It keeps the previous request's token sequence and reports
    ``cached_tokens`` = length of the longest common prefix, exactly how a
    provider's automatic prefix cache behaves. Each call returns a fresh class
    with its own state (one conversation per test).
    """
    state = {"last": [], "last_body": None, "saw_cache_key": None}

    class _Up(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, status=200):
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            state["last_body"] = body
            if body.get("prompt_cache_key") is not None:
                state["saw_cache_key"] = body["prompt_cache_key"]
            seq = []
            for m in body.get("messages") or []:
                seq.extend(_tokenize(json.dumps(m, sort_keys=True)))
            last = state["last"]
            n = min(len(last), len(seq))
            i = 0
            while i < n and last[i] == seq[i]:
                i += 1
            state["last"] = seq
            prompt_tokens = len(seq)
            self._send({
                "id": "chatcmpl-1", "object": "chat.completion",
                "model": body.get("model", "m"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                             "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": 5,
                    "total_tokens": prompt_tokens + 5,
                    "prompt_tokens_details": {"cached_tokens": i},
                },
            })

    _Up.state = state
    return _Up


def _conv(turns):
    """A growing conversation: system + alternating user/assistant turns."""
    msgs = [{"role": "system", "content": "You are a coding agent. Be terse."}]
    for i in range(1, turns + 1):
        msgs.append({"role": "user", "content": f"Turn {i}: explain the diff in module_{i}."})
        msgs.append({"role": "assistant", "content": f"Module_{i} renames a function."})
    return msgs


# ----------------------------------------------------------------- determinism
class TestPrefixStability(unittest.TestCase):
    def setUp(self):
        self.norm = CDMNormalizer()

    def test_shared_prefix_is_byte_stable(self):
        # The provider caches a *prefix*; that only works if turn N's messages
        # serialize identically inside turn N+1.
        s1 = self.norm.canonical_message_strings(_conv(1))
        s2 = self.norm.canonical_message_strings(_conv(2))
        self.assertEqual(s2[: len(s1)], s1)

    def test_key_order_does_not_change_strings(self):
        a = [{"role": "user", "content": "hi", "name": "x", "tool_call_id": "t1"}]
        b = [{"tool_call_id": "t1", "name": "x", "content": "hi", "role": "user"}]
        self.assertEqual(
            self.norm.canonical_message_strings(a),
            self.norm.canonical_message_strings(b),
        )

    def test_repeated_calls_are_deterministic(self):
        msgs = _conv(3)
        self.assertEqual(
            self.norm.canonical_message_strings(msgs),
            self.norm.canonical_message_strings(msgs),
        )


# --------------------------------------------------------------------- tracker
class TestSessionTracker(unittest.TestCase):
    def test_first_turn_is_new_session_no_reuse(self):
        st = SessionTracker()
        msgs = _conv(1)
        pr = st.observe(msgs, CDMNormalizer().canonical_message_strings(msgs))
        self.assertTrue(pr.is_new_session)
        self.assertEqual(pr.reused_tokens, 0)
        self.assertEqual(pr.reused_messages, 0)
        self.assertEqual(pr.fresh_tokens, pr.total_tokens)

    def test_second_turn_reuses_whole_first_prefix(self):
        st = SessionTracker()
        norm = CDMNormalizer()
        m1 = _conv(1)
        pr1 = st.observe(m1, norm.canonical_message_strings(m1))
        m2 = _conv(2)  # superset: appends one assistant + one user turn
        pr2 = st.observe(m2, norm.canonical_message_strings(m2))
        self.assertFalse(pr2.is_new_session)
        self.assertEqual(pr2.reused_messages, len(norm.canonical_message_strings(m1)))
        self.assertEqual(pr2.reused_tokens, pr1.total_tokens)
        self.assertGreater(pr2.reuse_pct, 0)
        self.assertLess(pr2.fresh_tokens, pr2.total_tokens)

    def test_session_key_stable_across_turns_andDistinctAcrossConvs(self):
        st = SessionTracker()
        norm = CDMNormalizer()
        k1 = st.observe(_conv(1), norm.canonical_message_strings(_conv(1))).session_key
        k2 = st.observe(_conv(2), norm.canonical_message_strings(_conv(2))).session_key
        self.assertEqual(k1, k2)
        other = [{"role": "user", "content": "totally different convo"}]
        k3 = st.observe(other, norm.canonical_message_strings(other)).session_key
        self.assertNotEqual(k1, k3)

    def test_explicit_session_header_wins(self):
        st = SessionTracker()
        norm = CDMNormalizer()
        pr = st.observe(_conv(1), norm.canonical_message_strings(_conv(1)), explicit_key="agent-42")
        self.assertEqual(pr.session_key, "agent-42")

    def test_modified_early_history_breaks_prefix(self):
        st = SessionTracker()
        norm = CDMNormalizer()
        st.observe(_conv(2), norm.canonical_message_strings(_conv(2)))
        tampered = _conv(2)
        tampered[0] = {"role": "system", "content": "A different system prompt."}
        pr = st.observe(tampered, norm.canonical_message_strings(tampered))
        # The system prompt is the very first message, so reuse collapses.
        self.assertEqual(pr.reused_messages, 0)


# -------------------------------------------------------------------- metrics
class TestGovernorMetrics(unittest.TestCase):
    def test_provider_cache_ratio(self):
        m = SessionMetrics()
        m.record_provider_prompt(1000, 600)
        self.assertEqual(m.provider_prompt_tokens, 1000)
        self.assertEqual(m.upstream_cached_tokens, 600)
        self.assertAlmostEqual(m.provider_cache_ratio, 0.6, places=4)

    def test_prefix_reuse_not_counted_as_savings(self):
        m = SessionMetrics()
        m.record(route="llm", original_tokens=1000, optimized_tokens=1000)
        m.record_prefix_reuse(800)
        # Prefix reuse is the provider's discount, not compaction savings.
        self.assertEqual(m.tokens_saved, 0)
        self.assertEqual(m.saved_pct, 0.0)
        self.assertEqual(m.prefix_reused_tokens, 800)


# ------------------------------------------------------------ pipeline wiring
class TestPipelineIntegration(unittest.TestCase):
    def test_prepare_attaches_growing_prefix_reuse(self):
        pipe = OptimizationPipeline()
        r1 = pipe.prepare({"model": "m", "messages": _conv(1)})
        self.assertIsNotNone(r1.prefix_reuse)
        self.assertEqual(r1.prefix_reuse.reused_tokens, 0)  # first turn
        r2 = pipe.prepare({"model": "m", "messages": _conv(2)})
        self.assertGreater(r2.prefix_reuse.reused_tokens, 0)
        self.assertEqual(r2.prefix_reuse.session_key, r1.prefix_reuse.session_key)

    def test_record_llm_books_provider_cache_and_prefix(self):
        pipe = OptimizationPipeline()
        pipe.prepare({"model": "m", "messages": _conv(1)})  # seed the session
        r = pipe.prepare({"model": "m", "messages": _conv(2)})
        self.assertGreater(r.prefix_reuse.reused_tokens, 0)  # reuses turn 1
        response = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 300},
            },
        }
        pipe.record_llm(r, response)
        self.assertEqual(pipe.metrics.provider_prompt_tokens, 500)
        self.assertEqual(pipe.metrics.upstream_cached_tokens, 300)
        self.assertGreater(pipe.metrics.prefix_reused_tokens, 0)
        self.assertAlmostEqual(pipe.metrics.provider_cache_ratio, 0.6, places=4)


# --------------------------------------------------------------- end-to-end
class TestGovernorEndToEnd(unittest.TestCase):
    def test_multi_turn_session_reports_prefix_reuse_and_cache_ratio(self):
        Up = _cache_upstream()

        def fn(gw, base):
            for turns in (1, 2, 3):
                _post(f"{base}/chat/completions",
                      {"model": "m", "messages": _conv(turns)})
            self.assertGreaterEqual(len(gw.receipts), 3)
            turn2, turn3 = gw.receipts[1], gw.receipts[2]
            # Later turns repeat the earlier prefix -> measured reuse + a real
            # provider cache hit reported by the fake upstream.
            self.assertGreater(turn2["prefix_reused"], 0)
            self.assertGreater(turn3["prefix_reused"], 0)
            self.assertGreater(turn2["upstream_cached"], 0)
            self.assertGreater(turn2["provider_cache_ratio"], 0)

            # Dashboard exposes the governor stats and the new column.
            status, _h, body = _get(base + "/dashboard")
            self.assertEqual(status, 200)
            self.assertIn("Prefix reuse", body)
            self.assertIn("Cache ratio", body)
            self.assertIn("Prefix", body)

        _run(Up, fn=fn)

    def test_prefix_reuse_grows_as_conversation_grows(self):
        Up = _cache_upstream()

        def fn(gw, base):
            for turns in (1, 2, 3, 4):
                _post(f"{base}/chat/completions",
                      {"model": "m", "messages": _conv(turns)})
            reuse = [r["prefix_reused"] for r in gw.receipts]
            # Each turn carries the whole prior history, so reuse climbs.
            self.assertEqual(reuse, sorted(reuse))
            self.assertGreater(reuse[-1], reuse[1])

        _run(Up, fn=fn)

    def test_prompt_cache_key_injected_when_enabled(self):
        Up = _cache_upstream()

        def fn(gw, base):
            _post(f"{base}/chat/completions",
                  {"model": "m", "messages": _conv(1)})
            self.assertIsNotNone(Up.state["saw_cache_key"])
            # It equals the session id the gateway derived.
            self.assertEqual(Up.state["saw_cache_key"], gw.receipts[0]["session"])

        _run(Up, config=lambda b: ProxyConfig(upstream_base=b, prompt_cache_key=True), fn=fn)

    def test_prompt_cache_key_off_by_default(self):
        Up = _cache_upstream()

        def fn(gw, base):
            _post(f"{base}/chat/completions",
                  {"model": "m", "messages": _conv(1)})
            self.assertIsNone(Up.state["saw_cache_key"])
            self.assertNotIn("prompt_cache_key", Up.state["last_body"])

        _run(Up, fn=fn)

    def test_explicit_session_header_groups_conversation(self):
        Up = _cache_upstream()

        def fn(gw, base):
            hdr = {SESSION_HEADER: "my-convo"}
            _post(f"{base}/chat/completions",
                  {"model": "m", "messages": _conv(1)}, headers=hdr)
            _post(f"{base}/chat/completions",
                  {"model": "m", "messages": _conv(2)}, headers=hdr)
            self.assertEqual(gw.receipts[0]["session"], "my-convo")
            self.assertEqual(gw.receipts[1]["session"], "my-convo")
            self.assertGreater(gw.receipts[1]["prefix_reused"], 0)

        _run(Up, fn=fn)


if __name__ == "__main__":
    unittest.main()
