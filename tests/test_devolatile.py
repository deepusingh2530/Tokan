"""Fingerprint-scope de-volatilization.

Clients inject rotating per-thread identifiers (uuids, timestamps, hex ids)
into system preambles. Two byte-identical asks in fresh chats then differ by
exactly those tokens — same length, different characters — which used to
defeat the cache every time. These tests pin the fix: system content is
scrubbed *for the digest only*; user/assistant content and the forwarded
payload stay exact.
"""

import unittest

from tokan.pipeline import OptimizationPipeline, devolatile


PREAMBLE = "You are an assistant. Trace {ident} started at {stamp}. Rules apply."


def _payload(ident_a: str, ident_b: str, role: str = "system"):
    def one(ident):
        return {
            "model": "gpt-5-codex",
            "messages": [
                {"role": role, "content": PREAMBLE.format(ident=ident, stamp="2026-10-29T07:24:22Z")},
                {"role": "user", "content": "explain agents.md"},
            ],
        }

    return one(ident_a), one(ident_b)


class TestDevolatile(unittest.TestCase):
    def test_scrubs_rotating_identifiers(self):
        text, hits = devolatile(PREAMBLE.format(ident="9f8e7d6c-1a2b-4c5d-8e9f-0a1b2c3d4e5f", stamp="2026-10-29T07:24:22Z"))
        self.assertEqual(hits, 2)
        self.assertIn("<uuid>", text)
        self.assertIn("<ts>", text)

    def test_plain_text_untouched(self):
        text, hits = devolatile("explain agents.md in this repository")
        self.assertEqual(hits, 0)
        self.assertEqual(text, "explain agents.md in this repository")


class TestDigestStability(unittest.TestCase):
    def setUp(self):
        self.pipeline = OptimizationPipeline.with_cdn(confidence_threshold=0.88)

    def test_rotating_system_ids_share_digest(self):
        a, b = _payload(
            "9f8e7d6c-1a2b-4c5d-8e9f-0a1b2c3d4e5f",
            "11112222-3333-4444-5555-666677778888",
        )
        first = self.pipeline.prepare(a)
        self.pipeline.record_llm(
            first,
            {"choices": [{"message": {"content": "Agents.md defines agent roles."}}], "usage": {"prompt_tokens": 90, "completion_tokens": 20}},
        )
        second = self.pipeline.prepare(b)
        self.assertEqual(first.record.digest, second.record.digest)
        self.assertTrue(second.cached)
        self.assertGreater(first.volatile_scrubbed, 0)

    def test_rotating_timestamp_and_hex_share_digest(self):
        a = {
            "model": "m",
            "messages": [
                {"role": "system", "content": "session 2026-10-29T07:24:22Z build a1b2c3d4e5f60718"},
                {"role": "user", "content": "explain agents.md"},
            ],
        }
        b = {
            "model": "m",
            "messages": [
                {"role": "system", "content": "session 2026-10-30T18:02:05Z build 9988776655443322"},
                {"role": "user", "content": "explain agents.md"},
            ],
        }
        self.assertEqual(
            self.pipeline.prepare(a).record.digest,
            self.pipeline.prepare(b).record.digest,
        )

    def test_user_content_never_scrubbed(self):
        a, b = _payload("same", "same", role="user")
        a["messages"][1]["content"] = "commit 9f8e7d6c-1a2b-4c5d-8e9f-0a1b2c3d4e5f"
        b["messages"][1]["content"] = "commit 11112222-3333-4444-5555-666677778888"
        first = self.pipeline.prepare(a)
        second = self.pipeline.prepare(b)
        self.assertNotEqual(first.record.digest, second.record.digest)
        self.assertEqual(first.volatile_scrubbed, 0)
        self.assertEqual(second.volatile_scrubbed, 0)

    def test_responses_input_preamble_scrubbed(self):
        def ask(ident):
            return {
                "model": "m",
                "input": [
                    {"type": "message", "role": "system", "content": [{"type": "input_text", "text": PREAMBLE.format(ident=ident, stamp="2026-10-29T07:24:22Z")}]},
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "explain agents.md"}]},
                ],
            }

        self.assertEqual(
            self.pipeline.prepare(ask("9f8e7d6c-1a2b-4c5d-8e9f-0a1b2c3d4e5f")).record.digest,
            self.pipeline.prepare(ask("11112222-3333-4444-5555-666677778888")).record.digest,
        )

    def test_wire_payload_keeps_original_bytes(self):
        payload, _ = _payload("9f8e7d6c-1a2b-4c5d-8e9f-0a1b2c3d4e5f", "unused")
        self.pipeline.prepare(payload)
        original = payload["messages"][0]["content"]
        self.assertIn("9f8e7d6c-1a2b-4c5d-8e9f-0a1b2c3d4e5f", original)
        self.assertNotIn("<uuid>", original)


if __name__ == "__main__":
    unittest.main()
