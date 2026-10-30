"""v0.3.0 features: persistent cache, receipts + dashboard, diff compaction,
upstream prompt-cache surfacing, and model fallback.

Each test drives a real stdlib gateway against a scripted fake upstream — the
same wire path the A/B harness exercises — so the assertions hold on what an
agent actually sees, not on a mocked internal.
"""
import json
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

from tokan.cache import CDNConfig, EdgeCache
from tokan.cache.sqlite_store import SqliteStore
from tokan.cdm import CDMNormalizer, estimate_tokens
from tokan.pipeline import (
    OptimizationPipeline,
    upstream_cached_tokens,
    usage_cached_tokens,
)
from tokan.proxy.interceptor import (
    ProxyConfig,
    TokenOptimizerProxy,
    _is_model_rejection,
    _extract_streamed_usage,
)


# --------------------------------------------------------------------- utils
def _post(url, payload):
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=5) as resp:
        return resp.status, dict(resp.headers), json.loads(resp.read().decode("utf-8"))


def _get(url):
    with urlopen(url, timeout=5) as resp:
        return resp.status, dict(resp.headers), resp.read().decode("utf-8")


class _ChatUpstream(BaseHTTPRequestHandler):
    """OpenAI-shaped upstream; subclass tweaks the reply."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        self.handle_chat(body)

    def _send(self, obj, status=200):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def handle_chat(self, body):  # default: plain answer
        self._send({
            "id": "chatcmpl-1", "object": "chat.completion",
            "model": body.get("model", "m"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        })


def _make_server(handler_cls):
    """Bind an ephemeral port with address reuse, then serve in a thread.

    ``allow_reuse_address`` + explicit ``server_close()`` avoids the
    TIME_WAIT "Address already in use" collisions when many tests bind
    ephemeral ports back-to-back.
    """
    import threading

    class _Srv(ThreadingHTTPServer):
        allow_reuse_address = True

    srv = _Srv(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def _run(upstream_cls, config=None, pipeline=None, fn=None):
    """Spin a fake upstream + gateway, run fn(gateway, upstream_base), teardown.

    ``config`` may be a ``ProxyConfig`` or a callable ``base -> ProxyConfig``
    (needed when the config must embed the ephemeral upstream port).
    """
    up, up_thread = _make_server(upstream_cls)
    base = f"http://127.0.0.1:{up.server_address[1]}/v1"
    pipe = pipeline or OptimizationPipeline()
    if callable(config):
        cfg = config(base)
    elif config is not None:
        cfg = config
    else:
        cfg = ProxyConfig(upstream_base=base)
    # Port 0 = ephemeral, so parallel tests never collide or steal the
    # user's live gateway on :4040.
    cfg.port = 0
    gw = TokenOptimizerProxy(cfg, pipe).start()
    try:
        # Tests talk to the *gateway*, not the upstream: base is the gateway
        # origin (e.g. http://127.0.0.1:54321). Paths like /chat/completions
        # are appended to it by each test.
        return fn(gw, gw.origin)
    finally:
        gw.shutdown()
        up.shutdown()
        up.server_close()
        up_thread.join(timeout=2)


# --------------------------------------------------------------- 1) sqlite
class TestSqliteStore(unittest.TestCase):
    def test_survives_restart(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "cache.db")
        s1 = SqliteStore(path)
        s1["k"] = {"value": "hello", "expires": 1e18}
        s1.close()

        s2 = SqliteStore(path)  # a "restart"
        self.assertEqual(s2["k"]["value"], "hello")
        s2.close()

    def test_ttl_expires_on_read(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "cache.db")
        store = SqliteStore(path, ttl_seconds=0)  # immediate expiry
        store["k"] = {"value": "v", "expires": 1e18}
        with self.assertRaises(KeyError):
            _ = store["k"]
        store.close()

    def test_corrupt_row_misses_instead_of_raising(self):
        import os, sqlite3, tempfile
        path = os.path.join(tempfile.mkdtemp(), "cache.db")
        store = SqliteStore(path)
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO cache (key, value, expires) VALUES (?, ?, ?)",
                ("bad", "not-json{", 1e18),
            )
        with self.assertRaises(KeyError):
            _ = store["bad"]
        self.assertNotIn("bad", store)
        store.close()

    def test_edge_cache_persists_across_instances(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "cache.db")

        c1 = EdgeCache(store=SqliteStore(path))
        c1.set("digest", "the answer")
        self.assertEqual(c1.get("digest"), "the answer")

        # "restart": fresh EdgeCache + fresh store over the same file
        c2 = EdgeCache(store=SqliteStore(path))
        self.assertEqual(c2.get("digest"), "the answer")
        self.assertEqual(c2.stats()["hits"], 1)


# ------------------------------------------------- 2) receipts + dashboard
class TestReceiptsAndDashboard(unittest.TestCase):
    def test_receipts_recorded_for_llm_route(self):
        def fn(gw, base):
            _post(f"{base}/chat/completions",
                  {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(len(gw.receipts), 1)
            r = gw.receipts[0]
            self.assertEqual(r["route"], "llm")
            self.assertEqual(r["status"], 200)
            self.assertEqual(r["model"], "m")
            self.assertIn("original_tokens", r)
            self.assertIn("ms", r)
            # Latency split: total >= upstream, overhead = total - upstream >= 0.
            # For an LLM route the upstream wait must be > 0 (we called out).
            self.assertGreater(r["upstream_ms"], 0)
            self.assertGreaterEqual(r["ms"], r["upstream_ms"])
            self.assertAlmostEqual(
                r["overhead_ms"], r["ms"] - r["upstream_ms"], places=1)
        _run(_ChatUpstream, fn=fn)

    def test_receipts_record_cache_route_with_zero_sent(self):
        pipe = OptimizationPipeline()
        # prime the cache so the second identical request is a hit
        result = pipe.prepare(
            {"model": "m", "messages": [{"role": "user", "content": "same"}]}
        )
        pipe.record_llm(result, {"choices": [{"message": {"content": "cached"}}], "usage": {}})

        def fn(gw, base):
            _post(f"{base}/chat/completions",
                  {"model": "m", "messages": [{"role": "user", "content": "same"}]})
            r = gw.receipts[-1]
            self.assertEqual(r["route"], "cache")
            self.assertEqual(r["sent_tokens"], 0)   # nothing left the machine
            # Cache hits never leave the machine: all latency is Tokan's own.
            self.assertEqual(r["upstream_ms"], 0)
            self.assertAlmostEqual(r["overhead_ms"], r["ms"], places=1)
        _run(_ChatUpstream, pipeline=pipe, fn=fn)

    def test_dashboard_serves_html_with_stats(self):
        def fn(gw, base):
            _post(f"{base}/chat/completions",
                  {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
            status, headers, body = _get(base + "/dashboard")
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers.get("Content-Type", ""))
            self.assertIn("Tokan", body)
            self.assertIn("Requests", body)
            self.assertIn("llm", body)  # the receipt row
            # Latency split is rendered so a slow generation isn't misread as
            # gateway overhead.
            self.assertIn("tokan / upstream", body)
            self.assertIn("tokan", body)
        _run(_ChatUpstream, fn=fn)


# ------------------------------------------- 3) upstream prompt-cache relay
class _CacheUpstream(_ChatUpstream):
    def handle_chat(self, body):
        self._send({
            "id": "c", "object": "chat.completion", "model": body.get("model", "m"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                "prompt_tokens_details": {"cached_tokens": 64},
            },
        })


class TestUpstreamPromptCache(unittest.TestCase):
    def test_openai_cached_tokens_header_and_metrics(self):
        def fn(gw, base):
            _post(f"{base}/chat/completions",
                  {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(gw.pipeline.metrics.upstream_cached_tokens, 64)
        _run(_CacheUpstream, fn=fn)

    def test_extractor_shapes(self):
        # OpenAI
        self.assertEqual(
            upstream_cached_tokens({"usage": {"prompt_tokens_details": {"cached_tokens": 5}}}), 5)
        # Anthropic
        self.assertEqual(
            upstream_cached_tokens({"usage": {"cache_read_input_tokens": 7}}, protocol="anthropic"), 7)
        # Gemini
        self.assertEqual(
            upstream_cached_tokens({"usageMetadata": {"cachedContentTokenCount": 9}}, protocol="gemini"), 9)
        # absent -> 0
        self.assertEqual(upstream_cached_tokens({"usage": {}}), 0)

    def test_usage_cached_tokens_from_streamed_usage(self):
        self.assertEqual(
            usage_cached_tokens({"prompt_tokens_details": {"cached_tokens": 3}}), 3)
        self.assertEqual(
            usage_cached_tokens({"cache_read_input_tokens": 4}, protocol="anthropic"), 4)


# ------------------------------------------------------------ 4) fallback
class _GatedUpstream(_ChatUpstream):
    """Rejects 'gated-model' the way Copilot does; answers anything else."""

    def handle_chat(self, body):
        if body.get("model") == "gated-model":
            self._send({"error": {"message": "model_not_supported", "code": "model_not_supported"}},
                       status=400)
        else:
            super().handle_chat(body)


class TestModelFallback(unittest.TestCase):
    def test_falls_back_and_discloses(self):
        def fn(gw, base):
            status, headers, body = _post(
                f"{base}/chat/completions",
                {"model": "gated-model", "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("X-Optimizer-Fallback"), "gated-model -> gpt-4o-mini")
            self.assertEqual(gw.pipeline.metrics.fallbacks, 1)
        _run(_GatedUpstream,
             config=lambda base: ProxyConfig(upstream_base=base, fallback_model="gpt-4o-mini"),
             fn=fn)

    def test_no_fallback_when_model_supported(self):
        def fn(gw, base):
            status, headers, _ = _post(
                f"{base}/chat/completions",
                {"model": "ok-model", "messages": [{"role": "user", "content": "hi"}]})
            self.assertEqual(status, 200)
            self.assertIsNone(headers.get("X-Optimizer-Fallback"))
            self.assertEqual(gw.pipeline.metrics.fallbacks, 0)
        _run(_GatedUpstream,
             config=lambda base: ProxyConfig(upstream_base=base, fallback_model="gpt-4o-mini"),
             fn=fn)

    def test_detector_positive_and_negative(self):
        self.assertTrue(_is_model_rejection(400, '{"error":{"code":"model_not_supported"}}'))
        self.assertTrue(_is_model_rejection(404, '{"error":{"code":"model_not_found"}}'))
        # payload-shape errors must NOT trigger a fallback retry
        self.assertFalse(_is_model_rejection(400, '{"error":{"message":"messages is required"}}'))
        self.assertFalse(_is_model_rejection(500, "model_not_supported"))  # wrong status

    def test_streamed_usage_extractor(self):
        buf = (
            'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":9,"prompt_tokens_details":{"cached_tokens":2}}}\n\n'
            "data: [DONE]\n\n"
        )
        usage = _extract_streamed_usage(buf, "openai")
        self.assertEqual(usage.get("prompt_tokens"), 9)
        self.assertEqual(usage_cached_tokens(usage), 2)


# --------------------------------------------------- 5) diff compaction
class TestDiffCompaction(unittest.TestCase):
    def setUp(self):
        self.n = CDMNormalizer()

    def _diff(self, n_ctx):
        return "\n".join(
            ["diff --git a/f.py b/f.py", "@@ -1,%d +1,2 @@ def main():" % (n_ctx + 2)]
            + [" ctx%d" % i for i in range(n_ctx)]
            + ["-removed line", "+added line"]
        )

    def test_elides_long_context_keeps_changes(self):
        out = self.n._compact_diff(self._diff(12))
        self.assertIn("-removed line", out)
        self.assertIn("+added line", out)
        self.assertIn("@@ -1,14", out)
        self.assertIn("unchanged diff lines elided", out)
        # head + tail context survive
        self.assertIn(" ctx0", out)
        self.assertIn(" ctx11", out)

    def test_short_diff_untouched(self):
        d = self._diff(2)
        self.assertEqual(self.n._compact_diff(d), d)

    def test_saves_tokens(self):
        d = self._diff(40)
        out = self.n._compact_diff(d)
        self.assertLess(estimate_tokens(out), estimate_tokens(d))

    def test_non_diff_text_untouched(self):
        prose = "some prose\n- a bullet\n+ another bullet\nno hunks here"
        self.assertEqual(self.n._compact_diff(prose), prose)

    def test_deterministic(self):
        d = self._diff(15)
        self.assertEqual(self.n._compact_diff(d), self.n._compact_diff(d))

    def test_tidy_uses_diff_compaction(self):
        out = self.n.compact_text(self._diff(20))
        self.assertIn("unchanged diff lines elided", out)
        self.assertIn("+added line", out)


class TestSessionMetricsThreadSafety(unittest.TestCase):
    """M2: the threaded gateway books metrics from many handler threads."""

    def test_concurrent_records_are_not_lost(self):
        import threading

        from tokan.metrics import SessionMetrics

        metrics = SessionMetrics()
        threads = [
            threading.Thread(
                target=lambda: [
                    (
                        metrics.record("llm", 100, 80),
                        metrics.record("cache", 50, 0),
                        metrics.record_provider_prompt(100, 40),
                        metrics.record_fallback(),
                    )
                    for _ in range(250)
                ],
            )
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snapshot = metrics.as_dict()
        # 8 threads x 250 iterations x 2 record() calls each.
        self.assertEqual(snapshot["requests"], 4000)
        self.assertEqual(snapshot["llm_calls"], 2000)
        self.assertEqual(snapshot["cache_hits"], 2000)
        self.assertEqual(snapshot["fallbacks"], 2000)
        # 8 threads x 250 x 100 prompt tokens — exact under the lock.
        self.assertEqual(snapshot["provider_prompt_tokens"], 200000)
        self.assertEqual(snapshot["upstream_cached_tokens"], 80000)

    def test_concurrent_edge_cache_counters(self):
        import threading

        cache = EdgeCache(CDNConfig(), max_entries=64)

        def hammer():
            for i in range(200):
                cache.set(f"k{i % 32}", i)
                cache.get(f"k{i % 32}")
                cache.get("missing-key")

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stats = cache.stats()
        # Every operation accounted: gets = hits + misses, exactly.
        self.assertEqual(stats["hits"] + stats["misses"], 3200)


if __name__ == "__main__":
    unittest.main()
