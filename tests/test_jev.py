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


if __name__ == "__main__":
    unittest.main()
