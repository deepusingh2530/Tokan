"""Tests for the JeV System-1 routing engine."""
import unittest

from tokan.jev import JevRouter


class TestJevRouter(unittest.TestCase):
    def setUp(self):
        self.router = JevRouter(confidence_threshold=0.88)

    def test_executor_answer_routes_local(self):
        d = self.router.evaluate(
            [{"role": "user", "content": "what's on line 42 of context: ..."}],
            deterministic_answer="the symbol Foo",
        )
        self.assertEqual(d.route, "local")
        self.assertGreaterEqual(d.confidence, 0.88)
        self.assertIn("local_executor", d.signals)

    def test_refactor_request_routes_llm(self):
        d = self.router.evaluate(
            [
                {"role": "system", "content": "You are an expert software engineer."},
                {
                    "role": "user",
                    "content": "Refactor this massive monolithic file into services:\n```python\n"
                    "def a():\n    pass\n```\nExplain the trade-offs step by step.",
                },
            ]
        )
        self.assertEqual(d.route, "llm")
        self.assertGreater(d.estimated_llm_tokens, 0)

    def test_no_executor_answer_never_serves_local_from_heuristics_alone(self):
        """Router may suggest local, but without an executor answer the
        pipeline downgrades it — assert the signal path is present."""
        d = self.router.evaluate([{"role": "user", "content": "is it ok?"}])
        # heuristic-only: confidence may be high, but no executor evidence
        self.assertNotIn("local_executor", d.signals)

    def test_cache_hit_short_circuits(self):
        d = self.router.evaluate([{"role": "user", "content": "refactor everything"}], cache_hit=True)
        self.assertEqual(d.route, "cache")
        self.assertEqual(d.confidence, 1.0)

    def test_code_and_paths_lean_llm(self):
        d = self.router.evaluate(
            [
                {
                    "role": "user",
                    "content": "check src/core/parser.py and src/core/lexer.py, "
                    "```py\nx=1\n```",
                }
            ]
        )
        self.assertEqual(d.route, "llm")

    def test_decision_serializes(self):
        d = self.router.evaluate([{"role": "user", "content": "list all routes"}])
        as_dict = d.as_dict()
        self.assertIn("route", as_dict)
        self.assertIn("confidence", as_dict)

    def test_routing_stays_fast_on_pathological_2mb_blob(self):
        # Regression: the file-path regex nested-quantifier backtracked
        # exponentially (~19s on this payload), so one pasted log dump could
        # stall the gateway for seconds. Routing must stay cheap.
        import time

        blob = ("x" * 5000 + "\n") * 420  # ~2MB of long slash-free lines
        t0 = time.perf_counter()
        d = self.router.evaluate(
            [{"role": "user", "content": "Refactor this dump:\n" + blob}]
        )
        elapsed = time.perf_counter() - t0
        self.assertEqual(d.route, "llm")
        # ~25ms locally; keep margin for slow CI runners. The pre-fix
        # exponential regex took ~19s on this payload, so 0.5s catches it.
        self.assertLess(elapsed, 0.5, f"routing took {elapsed:.2f}s")

    def test_file_paths_still_signal_llm_within_cap(self):
        # Capping the scan blob must not blind the router: paths near the
        # top still tip a borderline local-looking query to the LLM.
        neutral = self.router.evaluate(
            [{"role": "user", "content": "list all routes"}]
        )
        with_paths = self.router.evaluate(
            [
                {
                    "role": "user",
                    "content": "src/tokan/cli.py tests/test_mcp.py docs/a.md\n"
                    "list all routes",
                }
            ]
        )
        self.assertEqual(neutral.route, "local")
        self.assertEqual(with_paths.route, "llm")


if __name__ == "__main__":
    unittest.main()
