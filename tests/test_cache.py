"""Tests for the two-tier (local LRU + remote edge) semantic cache."""
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tokan.cache import CDNConfig, EdgeCache


class TestLocalCache(unittest.TestCase):
    def test_miss_then_hit(self):
        cache = EdgeCache()
        self.assertIsNone(cache.get("k"))
        cache.set("k", "value")
        self.assertEqual(cache.get("k"), "value")
        stats = cache.stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)

    def test_lru_eviction_bounds_memory(self):
        cache = EdgeCache(max_entries=3)
        for i in range(10):
            cache.set(f"k{i}", i)
        self.assertLessEqual(len(cache._local), 3)
        self.assertIsNone(cache.get("k0"))  # oldest evicted

    def test_ttl_expires_entries(self):
        now = [1000.0]
        cache = EdgeCache(CDNConfig(ttl_seconds=60), clock=lambda: now[0])
        cache.set("k", "v")
        self.assertEqual(cache.get("k"), "v")
        now[0] += 61  # past TTL
        self.assertIsNone(cache.get("k"))  # expired, not a zombie hit
        self.assertEqual(cache.stats()["hits"], 1)

    def test_semantic_digest_ignores_punctuation_and_case(self):
        a = EdgeCache.semantic_digest("Refactor the parser!")
        b = EdgeCache.semantic_digest("refactor   the parser")
        self.assertEqual(a, b)

    def test_semantic_digest_differs_on_real_change(self):
        a = EdgeCache.semantic_digest("refactor the parser")
        b = EdgeCache.semantic_digest("delete the parser entirely")
        self.assertNotEqual(a, b)

    def test_thread_safety_smoke(self):
        cache = EdgeCache(max_entries=64)
        errors = []

        def worker(base):
            try:
                for i in range(200):
                    cache.set(f"{base}-{i}", i)
                    cache.get(f"{base}-{i}")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])


class _EdgeHandler(BaseHTTPRequestHandler):
    store = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path in self.store:
            body = self.store[self.path]
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", 0))
        self.store[self.path] = self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class TestRemoteTier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _EdgeHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_remote_roundtrip(self):
        _EdgeHandler.store.clear()
        endpoint = f"http://127.0.0.1:{self.port}"
        writer = EdgeCache(CDNConfig(endpoint=endpoint, namespace="t1"))
        reader = EdgeCache(CDNConfig(endpoint=endpoint, namespace="t1"))
        writer.set("digest1", "cached answer")
        self.assertEqual(reader.get("digest1"), "cached answer")
        self.assertGreaterEqual(reader.stats()["remote_hits"], 1)

    def test_fail_open_on_dead_endpoint(self):
        cfg = CDNConfig(endpoint="http://127.0.0.1:1", timeout_seconds=0.2, fail_open=True)
        cache = EdgeCache(cfg)
        self.assertIsNone(cache.get("anything"))  # must not raise
        cache.set("k", "v")                        # remote PUT fails silently
        self.assertEqual(cache.get("k"), "v")      # local tier still works

    def test_fail_closed_raises(self):
        cfg = CDNConfig(endpoint="http://127.0.0.1:1", timeout_seconds=0.2, fail_open=False)
        cache = EdgeCache(cfg)
        with self.assertRaises(RuntimeError):
            cache.get("anything")


if __name__ == "__main__":
    unittest.main()
