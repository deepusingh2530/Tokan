"""Tests for CDM normalization: determinism, noise stripping, token savings."""
import unittest

from tokan.cdm import CDMNormalizer, estimate_tokens, strip_ansi


class TestStripAnsi(unittest.TestCase):
    def test_strips_colors(self):
        colored = "\x1b[32mgreen\x1b[0m text"
        self.assertEqual(strip_ansi(colored), "green text")

    def test_strips_osc_hyperlink(self):
        linked = "\x1b]8;;https://x.test\x07link\x1b]8;;\x07"
        self.assertEqual(strip_ansi(linked), "link")


class TestFingerprint(unittest.TestCase):
    def setUp(self):
        self.n = CDMNormalizer()

    def test_formatting_noise_does_not_change_digest(self):
        a = [{"role": "user", "content": "hello   world\n\n\n"}]
        b = [{"role": "user", "content": "\x1b[31mhello\x1b[0m world"}]
        self.assertEqual(self.n.fingerprint(a).digest, self.n.fingerprint(b).digest)

    def test_content_changes_digest(self):
        a = [{"role": "user", "content": "hello"}]
        b = [{"role": "user", "content": "hello!"}]
        self.assertNotEqual(self.n.fingerprint(a).digest, self.n.fingerprint(b).digest)

    def test_scope_model_changes_digest(self):
        msgs = [{"role": "user", "content": "hi"}]
        gpt4 = self.n.fingerprint(msgs, {"model": "gpt-4"})
        gpt35 = self.n.fingerprint(msgs, {"model": "gpt-3.5-turbo"})
        self.assertNotEqual(gpt4.digest, gpt35.digest)

    def test_reported_savings_non_negative(self):
        msgs = [{"role": "tool", "content": "\n".join(f"line {i}  " for i in range(200))}]
        rec = self.n.fingerprint(msgs)
        self.assertGreater(rec.original_tokens, 0)
        self.assertGreaterEqual(rec.saved_tokens, 0)


class TestToolElision(unittest.TestCase):
    def test_long_tool_output_is_elided(self):
        n = CDMNormalizer(tool_head=5, tool_tail=3)
        tool_content = "\n".join(f"row {i}" for i in range(500))
        out = n.normalize_messages([{"role": "tool", "content": tool_content}])[0]["content"]
        self.assertIn("elided", out)
        self.assertLess(len(out), len(tool_content) // 2)
        self.assertIn("row 0", out)          # head preserved
        self.assertIn("row 499", out)         # tail preserved

    def test_short_tool_output_untouched(self):
        n = CDMNormalizer()
        out = n.normalize_messages([{"role": "tool", "content": "ok"}])[0]["content"]
        self.assertEqual(out, "ok")


class TestConsecutiveDedup(unittest.TestCase):
    def test_exact_duplicate_consecutive_messages_collapse(self):
        n = CDMNormalizer()
        msgs = [
            {"role": "user", "content": "same"},
            {"role": "user", "content": "same"},
            {"role": "assistant", "content": "answer"},
        ]
        self.assertEqual(len(n.normalize_messages(msgs)), 2)


class TestFileTree(unittest.TestCase):
    def test_compact_tree_groups_by_directory(self):
        n = CDMNormalizer()
        paths = [f"src/module_{i}.py" for i in range(12)] + [f"tests/test_{i}.py" for i in range(8)]
        tree = n.compact_file_tree(paths)
        self.assertIn("src/:module_0.py", tree)
        self.assertIn("[tree 20 files]", tree)
        raw = "\n".join(paths)  # what a naive agent sends
        self.assertLess(len(tree), len(raw))


class TestCompactText(unittest.TestCase):
    def test_strips_noise_and_elides(self):
        n = CDMNormalizer()
        out = n.compact_text("hello \x1b[0;31mred\x1b[0m \n\n\n\nworld")
        self.assertNotIn("\x1b", out)
        self.assertIn("red", out)
        self.assertNotIn("\n\n\n", out)  # blank runs collapsed

    def test_long_lines_elided_with_marker(self):
        n = CDMNormalizer(max_line_chars=50)
        out = n.compact_text("x" * 500)
        self.assertIn("[cdm:", out)
        self.assertLess(len(out), 100)


class TestEstimate(unittest.TestCase):
    def test_zero_and_positive(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertGreater(estimate_tokens("word " * 100), 0)


if __name__ == "__main__":
    unittest.main()
