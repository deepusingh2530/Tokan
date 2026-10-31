"""Canonical request model: round-trip fidelity, digest parity, structure.

The M2 contract: ``parse(payload).to_payload() == payload`` for every
protocol, and ``fingerprint_view()`` reproduces ``pipeline.prepare()``'s
digest exactly — so when the pipeline adopts the canonical model (M3) no
existing cache entry changes key.
"""
import json
import unittest

from tokan.canonical import CanonicalRequest, parse, to_payload
from tokan.pipeline import (
    OptimizationPipeline,
    responses_input_to_messages,
    scrub_for_digest,
)

# ------------------------------------------------------------------ fixtures

CHAT_PLAIN = {
    "model": "gpt-4o",
    "messages": [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "Fix the flaky test."},
    ],
    "temperature": 0,
}

CHAT_FULL = {
    "model": "gpt-4o",
    "messages": [
        {"role": "system", "content": "Rules: never touch CI.", "name": "sys"},
        {"role": "user", "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "view", "arguments": '{"path": "a.py"}', "strict": True}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "def a(): ..."},
        {"role": "assistant", "content": [{"type": "text", "text": "done", "cache_control": {"type": "ephemeral"}}]},
    ],
    "tools": [
        {"type": "function", "function": {
            "name": "view", "description": "view a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }},
    ],
    "tool_choice": "auto",
    "max_tokens": 4096,
    "stream": True,
    "metadata": {"session_id": "abc"},   # unknown top-level field
}

RESPONSES_FULL = {
    "model": "gpt-5-codex",
    "instructions": "Be exact.",
    "input": [
        {"type": "message", "id": "msg_1", "role": "user",
         "content": [{"type": "input_text", "text": "q1"}]},
        {"type": "reasoning", "id": "rs_1", "summary": []},
        {"type": "function_call", "id": "fc_1", "call_id": "call_9",
         "name": "view", "arguments": '{"path":"a.py"}'},
        {"type": "function_call_output", "call_id": "call_9", "output": "ok"},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "answer"}]},
    ],
    "store": True,
}

ANTHROPIC_FULL = {
    "model": "claude-sonnet-4",
    "system": [{"type": "text", "text": "Be exact.", "cache_control": {"type": "ephemeral"}}],
    "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "hi"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "BBB"}},
        ]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "view", "input": {"path": "a.py"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "def a(): ..."},
        ]},
    ],
    "tools": [{"name": "view", "description": "view", "input_schema": {"type": "object"}}],
    "max_tokens": 1024,
    "metadata": {"user_id": "u-1"},
}

GEMINI_FULL = {
    "model": "gemini-2.5-pro",
    "contents": [
        {"role": "user", "parts": [{"text": "hello"}]},
        {"role": "model", "parts": [{"text": "hi"}, {"functionCall": {"name": "view", "args": {"path": "a.py"}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "view", "response": {"ok": True}}}]},
        {"role": "user", "parts": [{"inline_data": {"mime_type": "image/png", "data": "CCC"}}]},
    ],
    "tools": [{"functionDeclarations": [
        {"name": "view", "description": "view",
         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}},
    ]}],
    "generationConfig": {"maxOutputTokens": 2048},
    "systemInstruction": {"parts": [{"text": "Be exact."}]},
}


class TestRoundTrip(unittest.TestCase):
    """parse(payload).to_payload() == payload — nothing is lost."""

    def test_openai_chat_plain(self):
        self.assertEqual(to_payload(parse(CHAT_PLAIN, protocol="openai")), CHAT_PLAIN)

    def test_openai_chat_full(self):
        self.assertEqual(to_payload(parse(CHAT_FULL, protocol="openai")), CHAT_FULL)

    def test_responses_full(self):
        self.assertEqual(to_payload(parse(RESPONSES_FULL, protocol="responses")), RESPONSES_FULL)

    def test_anthropic_full(self):
        self.assertEqual(to_payload(parse(ANTHROPIC_FULL, protocol="anthropic")), ANTHROPIC_FULL)

    def test_gemini_full(self):
        self.assertEqual(to_payload(parse(GEMINI_FULL, protocol="gemini")), GEMINI_FULL)

    def test_protocol_inference(self):
        self.assertEqual(parse(RESPONSES_FULL).protocol, "responses")
        self.assertEqual(parse(GEMINI_FULL).protocol, "generate_content")
        self.assertEqual(parse(CHAT_PLAIN).protocol, "chat")

    def test_string_input_reconstructs_as_equivalent_items(self):
        # Documented transformation: a bare string input becomes the
        # equivalent message item (valid Responses wire; digest identical).
        req = parse({"model": "m", "input": "just this"}, protocol="responses")
        out = req.to_payload()
        self.assertEqual(out["input"][0]["content"], [{"type": "input_text", "text": "just this"}]
                         if out["input"][0]["content"] and isinstance(out["input"][0]["content"], list)
                         else "just this")
        self.assertEqual(req.fingerprint_view()[0],
                         [{"role": "user", "content": "just this"}])


class TestDigestParity(unittest.TestCase):
    """fingerprint_view() + scrub == pipeline.prepare()'s digest, exactly."""

    def _digest_via_canonical(self, payload, protocol, parse_protocol=None):
        pipeline = OptimizationPipeline()
        req = parse(payload, protocol=parse_protocol or protocol)
        messages, scope = req.fingerprint_view()
        scrubbed, _ = scrub_for_digest(messages)
        return pipeline.normalizer.fingerprint(scrubbed, scope).digest

    def _assert_parity(self, payload, protocol, parse_protocol=None):
        live = OptimizationPipeline().prepare(payload, protocol=protocol).record.digest
        model = self._digest_via_canonical(payload, protocol, parse_protocol)
        self.assertEqual(live, model, f"digest parity broken for protocol={protocol}")

    def test_chat_parity(self):
        self._assert_parity(CHAT_PLAIN, "openai")
        self._assert_parity(CHAT_FULL, "openai")

    def test_responses_parity(self):
        self._assert_parity(RESPONSES_FULL, "openai", parse_protocol="responses")

    def test_anthropic_parity(self):
        self._assert_parity(ANTHROPIC_FULL, "anthropic")

    def test_gemini_parity(self):
        self._assert_parity(GEMINI_FULL, "gemini")

    def test_parity_with_volatile_scrubbing(self):
        # uuid in system + Copilot datetime envelope in user content: the
        # canonical view must feed scrub_for_digest the same raw messages.
        payload = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "thread 5d91382c-1111-4222-8333-444455556666 instructions"},
                {"role": "user", "content": "<current_datetime>2026-10-29T12:00:00</current_datetime> then fix it"},
            ],
        }
        self._assert_parity(payload, "openai")

    def test_parity_scope_parameters(self):
        payload = dict(CHAT_PLAIN, temperature=0.7, top_p=0.9, user="alice")
        self._assert_parity(payload, "openai")
        # Different temperature must change the digest in both paths.
        other = dict(payload, temperature=0.2)
        self.assertNotEqual(
            OptimizationPipeline().prepare(payload, protocol="openai").record.digest,
            self._digest_via_canonical(other, "openai"),
        )

    def test_responses_projection_matches_pipeline(self):
        for value in (
            RESPONSES_FULL["input"],
            "plain string",
            [],
            [{"type": "reasoning", "summary": []}],   # excluded on both sides
            [{"type": "message", "role": "user", "content": "str content"}],
        ):
            req = parse({"model": "m", "input": value}, protocol="responses")
            self.assertEqual(
                req._responses_projection(),
                responses_input_to_messages(value),
                f"projection drift for {value!r}",
            )


class TestStructuredPreservation(unittest.TestCase):
    """Structured content stays typed — never flattened into text."""

    def test_images_and_tool_calls_are_typed(self):
        req = parse(CHAT_FULL, protocol="openai")
        user = req.messages[1]
        self.assertEqual([p.kind for p in user.parts], ["text", "image"])
        self.assertIn("data:image/png", user.parts[1].data)
        assistant = req.messages[2]
        self.assertEqual([p.kind for p in assistant.parts], ["tool_call"])
        self.assertEqual(assistant.parts[0].name, "view")
        self.assertEqual(assistant.parts[0].tool_call_id, "call_1")

    def test_text_projection_excludes_tool_payloads(self):
        req = parse(CHAT_FULL, protocol="openai")
        # Digest/state text view: tool arguments never leak into text().
        self.assertEqual(req.messages[2].text(), "")
        self.assertEqual(req.messages[3].text(), "def a(): ...")

    def test_anthropic_blocks_stay_blocks(self):
        req = parse(ANTHROPIC_FULL, protocol="anthropic")
        kinds = [p.kind for p in req.messages[1].parts]
        self.assertEqual(kinds, ["tool_call"])
        part = req.messages[1].parts[0]
        self.assertEqual(json.loads(part.data), {"path": "a.py"})
        self.assertEqual(req.messages[2].parts[0].kind, "tool_result")

    def test_gemini_function_parts_stay_typed(self):
        req = parse(GEMINI_FULL, protocol="gemini")
        model_msg = req.messages[1]
        self.assertEqual([p.kind for p in model_msg.parts], ["text", "tool_call"])
        self.assertEqual(model_msg.parts[1].name, "view")

    def test_newest_text_message_skips_tool_only_turns(self):
        req = parse(CHAT_FULL, protocol="openai")
        self.assertEqual(req.newest_text_message(), ("assistant", "done"))
        # Responses: tool-output items carry no text on the canonical view.
        rreq = parse(RESPONSES_FULL, protocol="responses")
        self.assertEqual(rreq.newest_text_message(), ("assistant", "answer"))


class TestExtensions(unittest.TestCase):
    """Unknown fields survive parse → reconstruct verbatim."""

    def test_unknown_top_level_fields_survive(self):
        out = to_payload(parse(CHAT_FULL, protocol="openai"))
        self.assertEqual(out["metadata"], {"session_id": "abc"})
        out = to_payload(parse(ANTHROPIC_FULL, protocol="anthropic"))
        self.assertEqual(out["metadata"], {"user_id": "u-1"})

    def test_unknown_message_and_block_fields_survive(self):
        out = to_payload(parse(CHAT_FULL, protocol="openai"))
        self.assertEqual(out["messages"][0]["name"], "sys")
        self.assertEqual(out["messages"][4]["content"][0]["cache_control"], {"type": "ephemeral"})

    def test_tool_wrapper_extras_survive(self):
        payload = {"model": "m", "messages": [{"role": "user", "content": "q"}],
                   "tools": [{"type": "function", "strict": True, "function": {
                       "name": "f", "description": "", "parameters": {},
                       "strict": True}}]}
        out = to_payload(parse(payload, protocol="openai"))
        self.assertEqual(out["tools"], payload["tools"])

    def test_canonical_request_defaults(self):
        req = CanonicalRequest(provider="openai", protocol="chat")
        self.assertEqual(req.to_payload()["messages"], [])
        self.assertIsNone(req.newest_text_message())


class TestManifestStats(unittest.TestCase):
    """The counts the gateway observer records per request."""

    def test_stats_shape(self):
        req = parse(CHAT_FULL, protocol="openai")
        stats = {
            "messages": len(req.messages),
            "tools": len(req.tools),
            "parts": sum(len(m.parts) for m in req.messages),
            "extensions": len(req.extensions),
        }
        self.assertEqual(stats["messages"], 5)
        self.assertEqual(stats["tools"], 1)
        self.assertGreaterEqual(stats["parts"], 6)


if __name__ == "__main__":
    unittest.main()
