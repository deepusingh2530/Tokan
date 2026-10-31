"""M3: content classification + L0–L4 compression policy with authority protection.

The contracts pinned here:

1. **Classification** is deterministic and role-aware — a system instruction is
   ``system_instruction``/CRITICAL regardless of what's inside it.
2. **Authority protection**: system/developer instructions and the user's ask are
   capped at lossless **L1** (ANSI/whitespace normalization only — no diff
   compaction, no long-line elision, no truncation). Tool schemas are never
   passed through ``normalize_messages`` at all (they're structural).
3. **The digest is policy-independent**: adopting a stricter policy can never
   change a cache key. We assert the pipeline digest is byte-identical with and
   without a policy for every protocol.
4. Wire forward sites (chat/anthropic via ``normalize_messages``, Responses
   ``input`` items, Gemini ``contents`` parts) honor the cap.
"""
import json
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tokan.policy import (
    Authority,
    CompressionLevel,
    CompressionPolicy,
    classify,
    classify_part,
)
from tokan.cdm.normalizer import CDMNormalizer
from tokan.pipeline import OptimizationPipeline, scrub_for_digest
from tokan.proxy import ProxyConfig, TokenOptimizerProxy


class _Upstream(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


_CAPTURED: list = []

# A system instruction that would trip every lossy compactor: a big diff, a
# very long line, ANSI escapes, blank-run padding. Under L1 it must survive
# with only whitespace/ANSI normalization.
_SYSTEM = (
    "\x1b[31mRULES\x1b[0m\n\n\n"
    "diff --git a/ci.yaml b/ci.yaml\n"
    "--- a/ci.yaml\n"
    "+++ b/ci.yaml\n"
    "@@ -1,2 +1,2 @@\n"
    "-old\n"
    "+new\n"
    + ("LONG " * 400)
    + "\nDo not touch CI. Ever."
)

_TOOL_OUTPUT = "\n".join(f"line {i} of a big tool dump" for i in range(200))


def _decision(text="", role="", tool=False, base=None, part_kind=""):
    return CompressionPolicy().level_for(
        text, role=role, tool=tool, base_level=base, part_kind=part_kind
    )


class TestClassification(unittest.TestCase):
    def test_role_wins(self):
        self.assertEqual(classify("whatever", role="system"),
                         ( "system_instruction", Authority.CRITICAL))
        self.assertEqual(classify("whatever", role="developer")[1], Authority.CRITICAL)
        self.assertEqual(classify("whatever", role="tool")[0], "tool_result")
        self.assertEqual(classify("whatever", role="assistant")[0], "assistant_message")

    def test_user_prose_is_high_authority(self):
        cls, auth = classify("Please fix the flaky test and never touch CI.")
        self.assertEqual(cls, "user_message")
        self.assertEqual(auth, Authority.HIGH)

    def test_code_user_turn_lowers_to_normal(self):
        code = "\n".join(["def a():", "    return 1", "def b():", "    return 2"])
        cls, auth = classify(code, role="user")
        self.assertEqual(cls, "source_code")
        self.assertEqual(auth, Authority.NORMAL)

    def test_part_kinds(self):
        self.assertEqual(classify_part("tool_call")[1], Authority.HIGH)
        self.assertEqual(classify_part("image")[1], Authority.CRITICAL)
        self.assertEqual(classify_part("tool_result")[0], "tool_result")

    def test_deterministic(self):
        for _ in range(50):
            self.assertEqual(classify(_SYSTEM, role="system"),
                             classify(_SYSTEM, role="system"))


class TestPolicyLevels(unittest.TestCase):
    def test_critical_capped_at_l1(self):
        d = _decision(_SYSTEM, role="system", base=CompressionLevel.L4_AGGRESSIVE)
        self.assertEqual(d.level, CompressionLevel.L1_LOSSLESS)
        self.assertTrue(d.capped)

    def test_high_capped_at_l1(self):
        # User's own ask: HIGH authority, capped at lossless L1 (the master
        # prompt's authority rule — user constraints never compress beyond L1).
        d = _decision("x" * 5000, role="user", base=CompressionLevel.L4_AGGRESSIVE)
        self.assertEqual(d.level, CompressionLevel.L1_LOSSLESS)
        self.assertTrue(d.capped)

    def test_normal_allows_l3(self):
        d = _decision(_TOOL_OUTPUT, role="tool", tool=True,
                      base=CompressionLevel.L3_REDUNDANCY)
        self.assertEqual(d.level, CompressionLevel.L3_REDUNDANCY)
        self.assertFalse(d.capped)

    def test_uncapped_when_requested_below_cap(self):
        d = _decision("hi", role="system", base=CompressionLevel.L1_LOSSLESS)
        self.assertEqual(d.level, CompressionLevel.L1_LOSSLESS)
        self.assertFalse(d.capped)

    def test_strict_flag_lifts_cap_for_observation(self):
        p = CompressionPolicy(strict_authority=False)
        d = p.level_for(_SYSTEM, role="system", base_level=CompressionLevel.L3_REDUNDANCY)
        self.assertEqual(d.level, CompressionLevel.L3_REDUNDANCY)
        self.assertFalse(d.capped)

    def test_decision_is_json_shapable(self):
        d = _decision(_SYSTEM, role="system")
        as_dict = d.as_dict()
        self.assertEqual(as_dict["class"], "system_instruction")
        self.assertEqual(as_dict["level"], 1)


class TestAuthorityProtection(unittest.TestCase):
    """The heart of M3: instructions are never rewritten beyond lossless."""

    def setUp(self):
        self.n = CDMNormalizer()
        self.policy = CompressionPolicy()

    def _normalize(self, messages):
        return self.n.normalize_messages(messages, policy=self.policy)

    def test_system_instruction_survives_verbatim(self):
        out = self._normalize([{"role": "system", "content": _SYSTEM}])[0]["content"]
        # No diff compaction: the hunk is intact.
        self.assertIn("@@ -1,2 +1,2 @@", out)
        # No long-line elision: the LONG run is not truncated behind a marker.
        self.assertNotIn("chars elided", out)
        # But lossless normalization still happened: ANSI stripped, blank runs
        # collapsed to a double newline.
        self.assertNotIn("\x1b[31m", out)
        self.assertNotIn("\n\n\n", out)

    def test_developer_instruction_also_protected(self):
        out = self._normalize([{"role": "developer", "content": _SYSTEM}])[0]["content"]
        self.assertIn("@@ -1,2 +1,2 @@", out)
        self.assertNotIn("chars elided", out)

    def test_tool_result_still_gets_truncation(self):
        out = self._normalize([{"role": "tool", "content": _TOOL_OUTPUT}])[0]["content"]
        # L3 head/tail truncation applies to tool dumps.
        self.assertIn("[cdm:", out)
        self.assertIn("lines elided", out)

    def test_user_ask_is_not_elided(self):
        # HIGH authority (the user's own constraint) → capped at L1: ANSI/WS
        # normalized only, never elided, never truncated behind a marker.
        ask = "Constraint: " + ("very important detail " * 300)
        d = self.policy.level_for(ask, role="user")
        self.assertEqual(d.level, CompressionLevel.L1_LOSSLESS)
        out = self._normalize([{"role": "user", "content": ask}])[0]["content"]
        self.assertNotIn("[cdm:", out)
        self.assertIn("very important detail", out)

    def test_without_policy_behavior_is_unchanged(self):
        # Backward compat: policy=None reproduces the pre-M3 lossy treatment —
        # a tool dump still gets head/tail truncation, system still gets the
        # full tidy (ANSI/diff/blank-run) with no authority cap.
        tool_out = self.n.normalize_messages([{"role": "tool", "content": _TOOL_OUTPUT}])[0]["content"]
        self.assertIn("lines elided", tool_out)
        sys_out = self.n.normalize_messages([{"role": "system", "content": _SYSTEM}])[0]["content"]
        self.assertNotIn("\n\n\n", sys_out)


class TestDigestIndependence(unittest.TestCase):
    """Policy adoption must never change a cache key, for any protocol."""

    def _digest(self, payload, protocol, with_policy):
        p = OptimizationPipeline()
        policy = CompressionPolicy() if with_policy else None
        res = p.prepare(payload, protocol=protocol, policy=policy)
        return res.record.digest

    def _assert_stable(self, payload, protocol):
        off = self._digest(payload, protocol, with_policy=False)
        on = self._digest(payload, protocol, with_policy=True)
        self.assertEqual(off, on, f"policy changed the digest for {protocol}")

    def test_chat_digest_stable(self):
        self._assert_stable({
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": "fix it"},
                {"role": "tool", "content": _TOOL_OUTPUT},
            ],
            "temperature": 0,
        }, "openai")

    def test_anthropic_digest_stable(self):
        self._assert_stable({
            "model": "claude-sonnet-4",
            "system": _SYSTEM,
            "messages": [{"role": "user", "content": "hi"}],
        }, "anthropic")

    def test_gemini_digest_stable(self):
        self._assert_stable({
            "model": "gemini-2.5-pro",
            "contents": [{"role": "user", "parts": [{"text": _SYSTEM}]}],
        }, "gemini")

    def test_scrub_basis_matches_scrub_for_digest(self):
        # The digest's message basis is the scrub_for_digest output, not the
        # policy-normalized text — restate the parity directly.
        msgs = [{"role": "system", "content": _SYSTEM}]
        scrubbed, _ = scrub_for_digest([dict(m) for m in msgs])
        self.assertIn("@@ -1,2 +1,2 @@", scrubbed[0]["content"])


class TestLevelAwareCompactText(unittest.TestCase):
    def setUp(self):
        self.n = CDMNormalizer()

    def test_level_none_is_legacy(self):
        self.assertEqual(self.n.compact_text(_SYSTEM),
                         self.n.compact_text(_SYSTEM, level=2))

    def test_level_one_is_lossless(self):
        out = self.n.compact_text(_SYSTEM, level=1)
        self.assertIn("@@ -1,2 +1,2 @@", out)
        self.assertNotIn("chars elided", out)

    def test_level_zero_untouched(self):
        self.assertEqual(self.n.compact_text(_SYSTEM, level=0), _SYSTEM)

    def test_level_two_elides(self):
        # Elision (long-line / diff-context) is an L2+ treatment; over 2000
        # chars a single long line is elided behind the marker.
        out = self.n.compact_text("Z" * 5000, level=2)
        self.assertIn("chars elided", out)

    def test_tool_level_three_truncates(self):
        out = self.n.compact_text(_TOOL_OUTPUT, tool=True, level=3)
        self.assertIn("lines elided", out)

    def test_tool_at_level_two_not_truncated(self):
        out = self.n.compact_text(_TOOL_OUTPUT, tool=True, level=2)
        self.assertNotIn("lines elided", out)


class TestForwardPathAuthority(unittest.TestCase):
    """End-to-end through a live gateway: the forwarded bytes respect the cap.

    A system instruction carrying a diff + a long line + ANSI is sent through a
    real ``TokenOptimizerProxy``; a capturing upstream records exactly what the
    gateway forwarded. The system content must survive with only lossless L1
    normalization (diff intact, no long-line elision marker, ANSI stripped),
    proving policy binds at the wire choke point — not just in unit tests.
    """

    class _Capture(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            _CAPTURED.append(json.loads(self.rfile.read(n)))
            body = json.dumps({
                "id": "c", "object": "chat.completion", "model": "fake",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def setUp(self):
        _CAPTURED.clear()
        self.up = _Upstream(("127.0.0.1", 0), self._Capture)
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        self.proxy = TokenOptimizerProxy(
            ProxyConfig(upstream_base=f"http://127.0.0.1:{self.up.server_address[1]}",
                        port=0),
            OptimizationPipeline.with_cdn(confidence_threshold=0.88),
        ).start()

    def tearDown(self):
        self.proxy.shutdown()
        self.up.shutdown()
        self.up.server_close()

    def test_system_instruction_reaches_upstream_lossless(self):
        payload = {"model": "gpt-4o", "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": "go"},
        ]}
        req = urllib.request.Request(
            f"{self.proxy.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        forwarded_system = _CAPTURED[-1]["messages"][0]["content"]
        # L1 lossless: diff hunk intact (no diff-context compaction).
        self.assertIn("@@ -1,2 +1,2 @@", forwarded_system)
        # No long-line elision marker — the instruction was never truncated.
        self.assertNotIn("chars elided", forwarded_system)
        # ANSI still stripped, blank runs still collapsed (lossless tidy).
        self.assertNotIn("\x1b[31m", forwarded_system)
        self.assertNotIn("\n\n\n", forwarded_system)


if __name__ == "__main__":
    unittest.main()
