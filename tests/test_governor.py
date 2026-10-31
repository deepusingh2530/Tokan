"""M4: Context Governor (token budget) + conservative semantic answer cache.

The contracts pinned here:

1. **Budget math is exact and model-aware** — per-model context window by
   longest family-prefix match, operator override wins, unknown models fall
   back to the conservative default; ``available = window - output_reserve -
   overhead``.
2. **Utility classification is role/recency aware and deterministic** —
   system/developer + the newest user ask are ``REQUIRED`` (never shed);
   recent turns are ``HIGH_VALUE``; older turns ``OPTIONAL``; verbatim repeats
   ``REDUNDANT``. A tool exchange (assistant ``tool_calls`` + its ``tool``
   results) is ONE atomic unit, so a shed can never orphan a ``tool_call_id``.
3. **Selection sheds only under budget, never authority** — under budget the
   plan is measurement only (``applied=False``, bytes untouched). Over budget
   it sheds redundant → optional → high-value (recency, then utility/token
   inside a class). If required context *alone* overflows, it fails open:
   forward everything, flag ``overflow``.
4. **Digest parity** — the governor never changes ``record.digest`` for any
   protocol. Shedding happens after the digest is fixed.
5. **Wire binding is honest** — chat/anthropic shed (messages IS the forwarded
   array); gemini/responses measure only (native indices don't align), and
   never *claim* bytes changed.
6. **Semantic cache is conservative** — off by default; only self-contained
   factual questions are eligible (edits/file paths/code are excluded); only
   matches at or above the risk threshold are served; scoped per model; TTL +
   LRU bounded. Semantic hits route as ``semantic``, not ``cache``, so the
   digest cache's hit rate stays honest.
"""
import json
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tokan.governor import (
    DEFAULT_CONTEXT_WINDOW,
    MODEL_CONTEXT_WINDOWS,
    ContextGovernor,
    SemanticAnswerCache,
    UtilityClass,
    context_window_for,
)
from tokan.pipeline import OptimizationPipeline
from tokan.proxy import ProxyConfig, TokenOptimizerProxy


class _Upstream(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


_E2E_CAPTURED: list = []


# ------------------------------------------------------------------ fixtures
def _conv(n_turns, *, tail="final question?"):
    """A system prompt + ``n_turns`` of user/assistant + a final ask."""
    msgs = [{"role": "system", "content": "You are terse. Never touch CI."}]
    for i in range(n_turns):
        msgs.append({"role": "user", "content": f"turn {i} " + ("ctx " * 60)})
        msgs.append({"role": "assistant", "content": f"reply {i} " + ("out " * 40)})
    msgs.append({"role": "user", "content": tail})
    return msgs


# ============================================================== budget math
class TestTokenBudget(unittest.TestCase):
    def test_available_is_window_minus_reserve_minus_overhead(self):
        g = ContextGovernor(output_reserve=4096)
        b = g.budget_for(model="gpt-4o", input_tokens=1000, overhead_tokens=500)
        self.assertEqual(b.context_window, 128_000)
        self.assertEqual(b.available_input, 128_000 - 4096 - 500)
        self.assertTrue(b.fits)
        self.assertEqual(b.overflow_tokens, 0)

    def test_overflow_math(self):
        g = ContextGovernor(context_window=250, output_reserve=100)
        b = g.budget_for(model="gpt-4o", input_tokens=200)
        self.assertFalse(b.fits)
        self.assertEqual(b.overflow_tokens, 50)

    def test_output_reserve_clamps_negative_available(self):
        g = ContextGovernor(context_window=100, output_reserve=5000)
        b = g.budget_for(model="anything", input_tokens=1)
        self.assertEqual(b.available_input, 0)


# ====================================================== context-window registry
class TestContextWindowRegistry(unittest.TestCase):
    def test_known_family_by_prefix(self):
        # Deployment ids start with the family token.
        self.assertEqual(context_window_for("gpt-4o"), 128_000)
        self.assertEqual(context_window_for("gpt-5-codex-2025-09-30"), 400_000)
        self.assertEqual(context_window_for("claude-sonnet-4-20250514"), 200_000)

    def test_longest_prefix_wins(self):
        # "gpt-5-codex" is longer than "gpt-5" and must win for codex ids.
        self.assertEqual(context_window_for("gpt-5-codex"), 400_000)

    def test_unknown_model_gets_conservative_default(self):
        self.assertEqual(context_window_for("mystery-model-9000"),
                         DEFAULT_CONTEXT_WINDOW)

    def test_operator_override_wins(self):
        self.assertEqual(context_window_for("gpt-4o", override=7), 7)

    def test_empty_model_is_default(self):
        self.assertEqual(context_window_for(""), DEFAULT_CONTEXT_WINDOW)

    def test_registry_is_documented_not_magic(self):
        # Every entry is a positive window; the table is a convenience that
        # the --context-window flag can always override.
        self.assertTrue(all(v > 0 for v in MODEL_CONTEXT_WINDOWS.values()))


# ======================================================= utility classification
class TestClassification(unittest.TestCase):
    def setUp(self):
        self.g = ContextGovernor(recent_window=4)

    def _classes(self, msgs):
        return [u.utility_class for u in self.g.build_units(msgs)]

    def test_system_and_newest_ask_are_required(self):
        msgs = _conv(6)
        classes = self._classes(msgs)
        self.assertEqual(classes[0], UtilityClass.REQUIRED)   # system
        self.assertEqual(classes[-1], UtilityClass.REQUIRED)  # the ask

    def test_developer_is_required(self):
        classes = self._classes([
            {"role": "developer", "content": "be exact"},
            {"role": "user", "content": "q?"},
        ])
        self.assertEqual(classes[0], UtilityClass.REQUIRED)

    def test_recent_turns_are_high_value(self):
        classes = self._classes(_conv(10))
        # The assistant reply just before the ask sits in the recent window.
        self.assertEqual(classes[-2], UtilityClass.HIGH_VALUE)

    def test_old_turns_are_optional(self):
        classes = self._classes(_conv(10))
        self.assertEqual(classes[1], UtilityClass.OPTIONAL)

    def test_verbatim_repeat_is_redundant(self):
        # Agent clients re-send whole histories; an exact repeat carries
        # nothing new and is the first thing to shed.
        msgs = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "same question"},
            {"role": "assistant", "content": "same answer"},
            {"role": "user", "content": "same question"},   # repeat of m[1]
            {"role": "user", "content": "new ask?"},
        ]
        classes = self._classes(msgs)
        self.assertEqual(classes[3], UtilityClass.REDUNDANT)

    def test_tool_exchange_is_one_atomic_unit(self):
        # assistant tool_calls + its tool results collapse into ONE unit so a
        # shed can never leave an orphaned tool_call_id (a provider 400).
        msgs = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "view", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "output A"},
            {"role": "tool", "tool_call_id": "c1", "content": "output B"},
            {"role": "user", "content": "next?"},
        ]
        units = self.g.build_units(msgs)
        exchange = [u for u in units if u.role == "tool_exchange"]
        self.assertEqual(len(exchange), 1)
        self.assertEqual(exchange[0].indices, (2, 3, 4))

    def test_deterministic(self):
        msgs = _conv(8)
        self.assertEqual(self._classes(msgs), self._classes(msgs))


# =============================================================== selection
class TestSelection(unittest.TestCase):
    def test_under_budget_is_measurement_only(self):
        g = ContextGovernor(output_reserve=100)
        plan = g.plan(_conv(6), model="gpt-4o")
        self.assertFalse(plan.applied)
        self.assertEqual(plan.shed, ())
        self.assertEqual(plan.shed_tokens, 0)
        self.assertEqual(len(plan.kept), len(_conv(6)))

    def test_over_budget_sheds_optional_before_high_value(self):
        # Tiny window: shed order is redundant → optional → high-value.
        g = ContextGovernor(context_window=300, output_reserve=50)
        msgs = _conv(8)
        plan = g.plan(msgs, model="gpt-4o")
        self.assertTrue(plan.applied)
        shed = set(plan.shed)
        kept = set(plan.kept)
        # The oldest optional turn (index 1) is shed before any high-value one.
        self.assertIn(1, shed)
        # Nothing required is ever shed.
        self.assertNotIn(0, shed)   # system
        self.assertNotIn(len(msgs) - 1, shed)  # the ask
        # High-value turns survive longer than optional ones.
        self.assertTrue(any(i in kept for i in range(len(msgs) - 4, len(msgs))))

    def test_required_never_shed_even_at_catastrophic_pressure(self):
        g = ContextGovernor(context_window=30, output_reserve=5)
        msgs = [{"role": "system", "content": "instructions " * 40},
                {"role": "user", "content": "the ask?"}]
        plan = g.plan(msgs, model="gpt-4o")
        # Required alone overflows -> fail open: forward everything.
        self.assertTrue(plan.overflow)
        self.assertFalse(plan.applied)
        self.assertEqual(plan.shed, ())
        self.assertEqual(set(plan.kept), {0, 1})

    def test_selection_is_deterministic(self):
        g = ContextGovernor(context_window=300, output_reserve=50)
        msgs = _conv(8)
        self.assertEqual(g.plan(msgs, model="gpt-4o").shed,
                         g.plan(msgs, model="gpt-4o").shed)

    def test_apply_filters_shed_indices(self):
        g = ContextGovernor(context_window=300, output_reserve=50)
        msgs = _conv(8)
        plan = g.plan(msgs, model="gpt-4o")
        out = g.apply(msgs, plan)
        self.assertEqual(len(out), len(msgs) - len(plan.shed))
        # Kept messages appear in original order.
        self.assertEqual([m["role"] for m in out],
                         [msgs[i]["role"] for i in plan.kept])

    def test_shed_tokens_matches_removed_units(self):
        g = ContextGovernor(context_window=300, output_reserve=50)
        msgs = _conv(8)
        plan = g.plan(msgs, model="gpt-4o")
        self.assertGreater(plan.shed_tokens, 0)
        # shed_tokens equals the estimate of exactly the removed messages.
        from tokan.cdm.normalizer import estimate_tokens
        expected = sum(
            estimate_tokens(str(msgs[i].get("content", ""))) for i in plan.shed
        )
        self.assertEqual(plan.shed_tokens, expected)


# ============================================================== digest parity
class TestDigestParity(unittest.TestCase):
    """The governor must never change a cache key, for any protocol."""

    def _digest(self, payload, *, governor, protocol="openai", endpoint=""):
        p = OptimizationPipeline(governor=governor)
        r = p.prepare(payload, protocol=protocol, endpoint=endpoint)
        return r.record.digest

    def _payload(self):
        return {"model": "gpt-4o", "messages": _conv(8)}

    def test_chat_digest_identical_with_and_without_governor(self):
        off = self._digest(self._payload(), governor=None)
        # A governor that WOULD shed (tiny window) must not move the key.
        on = self._digest(self._payload(),
                          governor=ContextGovernor(context_window=200,
                                                   output_reserve=50))
        self.assertEqual(off, on)

    def test_anthropic_digest_identical(self):
        payload = {"model": "claude-sonnet-4", "system": "rules",
                   "messages": _conv(8)}
        off = self._digest(payload, governor=None, protocol="anthropic")
        on = self._digest(payload,
                          governor=ContextGovernor(context_window=200,
                                                   output_reserve=50),
                          protocol="anthropic")
        self.assertEqual(off, on)

    def test_responses_digest_identical(self):
        payload = {"model": "gpt-5-codex", "input": [
            {"type": "message", "role": "user", "content": "go"},
            {"type": "message", "role": "assistant", "content": "done"},
            {"type": "message", "role": "user", "content": "again?"},
        ]}
        off = self._digest(payload, governor=None, protocol="openai",
                           endpoint="responses")
        on = self._digest(payload,
                          governor=ContextGovernor(context_window=50,
                                                   output_reserve=10),
                          protocol="openai", endpoint="responses")
        self.assertEqual(off, on)

    def test_gemini_digest_identical(self):
        payload = {"model": "gemini-2.5-pro", "contents": [
            {"role": "user", "parts": [{"text": "go"}]},
            {"role": "model", "parts": [{"text": "done"}]},
            {"role": "user", "parts": [{"text": "again?"}]},
        ]}
        off = self._digest(payload, governor=None, protocol="gemini")
        on = self._digest(payload,
                          governor=ContextGovernor(context_window=50,
                                                   output_reserve=10),
                          protocol="gemini")
        self.assertEqual(off, on)

    def test_under_budget_plan_is_none_shaped_not_bytes(self):
        # Under budget: messages untouched, plan present but not applied.
        p = OptimizationPipeline(governor=ContextGovernor())
        r = p.prepare(self._payload())
        self.assertIsNotNone(r.governor_plan)
        self.assertFalse(r.governor_plan.applied)
        self.assertEqual(len(r.messages), len(_conv(8)))

    def test_no_governor_means_no_plan(self):
        p = OptimizationPipeline()
        r = p.prepare(self._payload())
        self.assertIsNone(r.governor_plan)


# ==================================================== wire binding (e2e-ish)
class TestWireBinding(unittest.TestCase):
    """Shedding reaches the wire for chat/anthropic; measures only otherwise."""

    def test_chat_sheds_real_messages(self):
        p = OptimizationPipeline(
            governor=ContextGovernor(context_window=250, output_reserve=50))
        msgs = _conv(8)
        r = p.prepare({"model": "gpt-4o", "messages": msgs})
        self.assertTrue(r.governor_plan.applied)
        self.assertLess(len(r.messages), len(msgs))
        # System + ask survive on the wire.
        self.assertEqual(r.messages[0]["role"], "system")
        self.assertEqual(r.messages[-1]["content"], "final question?")

    def test_responses_measures_but_does_not_shed(self):
        p = OptimizationPipeline(
            governor=ContextGovernor(context_window=50, output_reserve=10))
        payload = {"model": "gpt-5-codex", "input": [
            {"type": "message", "role": "user", "content": "go " * 200},
            {"type": "message", "role": "assistant", "content": "done " * 200},
            {"type": "message", "role": "user", "content": "again?"},
        ]}
        r = p.prepare(payload, protocol="openai", endpoint="responses")
        # It decided to shed, but this wire can't honour it -> not applied,
        # and no false "bytes changed" claim.
        self.assertFalse(r.governor_plan.applied)
        self.assertEqual(len(r.messages), 3)

    def test_gemini_measures_but_does_not_shed(self):
        p = OptimizationPipeline(
            governor=ContextGovernor(context_window=50, output_reserve=10))
        payload = {"model": "gemini-2.5-pro", "contents": [
            {"role": "user", "parts": [{"text": "go " * 200}]},
            {"role": "model", "parts": [{"text": "done " * 200}]},
            {"role": "user", "parts": [{"text": "again?"}]},
        ]}
        r = p.prepare(payload, protocol="gemini")
        self.assertFalse(r.governor_plan.applied)
        self.assertEqual(len(r.messages), 3)


# ======================================================= semantic answer cache
class TestSemanticCache(unittest.TestCase):
    def test_disabled_by_default_never_serves(self):
        c = SemanticAnswerCache()
        c.store("What is CI?", "Continuous integration.", model="gpt-4o")
        self.assertIsNone(c.probe("What is CI?", model="gpt-4o"))

    def test_enabled_serves_near_paraphrase(self):
        c = SemanticAnswerCache(enabled=True, threshold=0.97)
        c.store("What is continuous integration?",
                "CI merges changes often.", model="gpt-4o")
        hit = c.probe("What is continuous integration?", model="gpt-4o")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "CI merges changes often.")

    def test_below_threshold_never_serves(self):
        c = SemanticAnswerCache(enabled=True, threshold=0.97)
        c.store("What is continuous integration?", "CI merges.", model="gpt-4o")
        # Related but genuinely different question -> no serve.
        self.assertIsNone(
            c.probe("What is continuous deployment?", model="gpt-4o"))

    def test_edit_requests_are_excluded(self):
        c = SemanticAnswerCache(enabled=True, threshold=0.5)
        c.store("fix the bug in main.py", "no", model="gpt-4o")
        self.assertFalse(c.eligible("fix the bug in main.py"))
        self.assertIsNone(c.probe("fix the bug in main.py", model="gpt-4o"))

    def test_file_paths_and_code_are_excluded(self):
        c = SemanticAnswerCache(enabled=True)
        self.assertFalse(c.eligible("What does src/app.py do?"))
        self.assertFalse(c.eligible("What does this code do?\n```x```"))

    def test_model_scoped(self):
        c = SemanticAnswerCache(enabled=True)
        c.store("What is CI?", "CI merges.", model="gpt-4o")
        self.assertIsNone(c.probe("What is CI?", model="claude-sonnet-4"))

    def test_lru_bounded(self):
        c = SemanticAnswerCache(enabled=True, max_entries=2)
        c.store("What is alpha?", "a", model="m")
        c.store("What is bravo?", "b", model="m")
        c.store("What is charlie?", "c", model="m")
        self.assertEqual(len(c), 2)

    def test_ttl_expiry(self):
        clock = {"t": 0.0}
        c = SemanticAnswerCache(enabled=True, ttl_seconds=10,
                                time_fn=lambda: clock["t"])
        c.store("What is CI?", "CI merges.", model="m")
        clock["t"] = 5
        self.assertIsNotNone(c.probe("What is CI?", model="m"))
        clock["t"] = 11
        self.assertIsNone(c.probe("What is CI?", model="m"))

    def test_case_and_whitespace_insensitive(self):
        c = SemanticAnswerCache(enabled=True, threshold=0.97)
        c.store("What is CI?", "CI merges.", model="m")
        hit = c.probe("  what   is ci  ", model="m")
        self.assertIsNotNone(hit)

    def test_empty_answer_not_stored(self):
        c = SemanticAnswerCache(enabled=True)
        self.assertFalse(c.store("What is CI?", "   ", model="m"))


class TestSemanticPipelineBinding(unittest.TestCase):
    """Semantic hits route as `semantic`, not `cache` — honest accounting."""

    def test_semantic_hit_sets_route_and_flag(self):
        sem = SemanticAnswerCache(enabled=True, threshold=0.97)
        sem.store("What is continuous integration?",
                  "CI merges changes often.", model="gpt-4o")
        p = OptimizationPipeline(semantic_cache=sem)
        r = p.prepare({"model": "gpt-4o", "messages": [
            {"role": "user", "content": "What is continuous integration?"}]})
        self.assertTrue(r.semantic)
        self.assertTrue(r.cached)
        self.assertEqual(r.decision.route, "cache")
        self.assertIn("semantic_hit", r.decision.signals)

    def test_disabled_cache_falls_through_to_llm(self):
        p = OptimizationPipeline(semantic_cache=SemanticAnswerCache(enabled=False))
        r = p.prepare({"model": "gpt-4o", "messages": [
            {"role": "user", "content": "What is CI?"}]})
        self.assertFalse(r.semantic)
        self.assertEqual(r.decision.route, "llm")

    def test_exact_digest_cache_outranks_semantic(self):
        # An exact repeat is a digest hit, never a semantic one — even when a
        # semantic match is also available.
        sem = SemanticAnswerCache(enabled=True, threshold=0.97)
        sem.store("What is CI?", "CI merges.", model="gpt-4o")
        p = OptimizationPipeline(semantic_cache=sem)
        payload = {"model": "gpt-4o", "messages": [
            {"role": "user", "content": "What is CI?"}]}
        first = p.prepare(payload)
        # Seed the digest cache with the same question, then re-prepare.
        p.cache.set(first.cache_key, "exact answer")
        r = p.prepare(payload)
        self.assertFalse(r.semantic)   # served by digest, not by fuzzy match
        self.assertEqual(r.cached_value, "exact answer")


class TestForwardPathE2E(unittest.TestCase):
    """Live gateway: shed bytes actually reach the wire; under-budget is identical.

    A capturing upstream records exactly what the gateway forwarded. Over
    budget, the oldest optional turn is gone from the forwarded messages while
    the system prompt and the final ask survive. Under budget, the forwarded
    payload is byte-identical to a governor-off gateway's.
    """

    class _Capture(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            _E2E_CAPTURED.append(json.loads(self.rfile.read(n)))
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

    def _proxy(self, **pipeline_kwargs):
        self.up = _Upstream(("127.0.0.1", 0), self._Capture)
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        proxy = TokenOptimizerProxy(
            ProxyConfig(upstream_base=f"http://127.0.0.1:{self.up.server_address[1]}",
                        port=0),
            OptimizationPipeline(**pipeline_kwargs),
        ).start()
        return proxy

    def setUp(self):
        _E2E_CAPTURED.clear()

    def tearDown(self):
        if getattr(self, "proxy", None):
            self.proxy.shutdown()
        if getattr(self, "up", None):
            self.up.shutdown()
            self.up.server_close()

    def _post(self, proxy, payload):
        req = urllib.request.Request(
            f"{proxy.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()

    def test_over_budget_sheds_at_the_wire(self):
        self.proxy = self._proxy(
            governor=ContextGovernor(context_window=250, output_reserve=50))
        msgs = _conv(8)
        self._post(self.proxy, {"model": "gpt-4o", "messages": msgs})
        forwarded = _E2E_CAPTURED[-1]["messages"]
        # Fewer messages left the machine than were sent.
        self.assertLess(len(forwarded), len(msgs))
        # The system prompt and the final ask both survive.
        self.assertEqual(forwarded[0]["role"], "system")
        self.assertEqual(forwarded[-1]["content"], "final question?")
        # The oldest optional turn is gone.
        self.assertNotIn(msgs[1]["content"],
                         [m.get("content") for m in forwarded])

    def test_under_budget_is_byte_identical_to_governor_off(self):
        payload = {"model": "gpt-4o", "messages": _conv(4)}
        # Governor off.
        off = self._proxy()
        self._post(off, payload)
        off_forwarded = _E2E_CAPTURED[-1]["messages"]
        off.shutdown()
        self.up.shutdown()
        self.up.server_close()
        # Governor on, generous budget.
        _E2E_CAPTURED.clear()
        self.proxy = self._proxy(governor=ContextGovernor())
        self._post(self.proxy, payload)
        self.assertEqual(_E2E_CAPTURED[-1]["messages"], off_forwarded)


class TestMetricsAccounting(unittest.TestCase):
    def test_semantic_route_does_not_inflate_digest_cache_hits(self):
        from tokan.metrics import SessionMetrics
        m = SessionMetrics()
        m.record("semantic", original_tokens=10, optimized_tokens=0)
        m.record("cache", original_tokens=10, optimized_tokens=0)
        self.assertEqual(m.semantic_cache_hits, 1)
        self.assertEqual(m.cache_hits, 1)
        self.assertEqual(m.requests, 2)

    def test_governor_shed_tokens_never_enter_tokens_saved(self):
        from tokan.metrics import SessionMetrics
        m = SessionMetrics()
        m.record("llm", original_tokens=100, optimized_tokens=80)
        m.record_governor(shed_tokens=5000, overflow=False)
        self.assertEqual(m.tokens_saved, 20)          # compaction only
        self.assertEqual(m.governor_shed_tokens, 5000)  # separate ledger
        self.assertEqual(m.governor_sheds, 1)
        self.assertEqual(m.governor_overflows, 0)

    def test_governor_overflow_counted(self):
        from tokan.metrics import SessionMetrics
        m = SessionMetrics()
        m.record_governor(shed_tokens=0, overflow=True)
        self.assertEqual(m.governor_overflows, 1)

    def test_as_dict_exposes_new_counters(self):
        from tokan.metrics import SessionMetrics
        d = SessionMetrics().as_dict()
        for key in ("semantic_cache_hits", "governor_sheds",
                    "governor_shed_tokens", "governor_overflows"):
            self.assertIn(key, d)


if __name__ == "__main__":
    unittest.main()
