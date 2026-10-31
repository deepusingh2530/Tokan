"""M6 hardening: inbound auth, secret posture, quality gate, routing wire-up.

What is under test:

* ensure_bind_safety: non-loopback binds fail closed without keys.
* Inbound auth: Bearer + X-Optimizer-Api-Key accepted, wrong/missing key →
  401 (constant-time compare, no key echo, /healthz exempt), failures
  counted; authorized requests produce byte-identical receipts.
* Secret posture e2e: detect-only leaves the wire untouched but discloses;
  --redact-secrets replaces before the upstream sees anything (and before
  the digest is computed — cache keys never encode secrets).
* Quality gate e2e: --doc-fallback-full sends the full document when
  coverage is low, honestly reporting ~zero savings.
* Routing wire-up: --task-router discloses tiers; model rewrites happen
  only with an explicit --tier1-model.
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tokan.proxy.interceptor import ProxyConfig, TokenOptimizerProxy, ensure_bind_safety

_AWS = "AKIA" + "A1B2C3D4E5F6G7H8"


# --------------------------------------------------------------------- utils
def _post(url, payload, headers=None):
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urlopen(req, timeout=5) as resp:
        return resp.status, dict(resp.headers), json.loads(resp.read().decode("utf-8"))


def _post_status(url, payload, headers=None):
    try:
        return _post(url, payload, headers)
    except HTTPError as exc:
        return exc.code, dict(exc.headers), json.loads(exc.read().decode("utf-8"))


def _get(url, headers=None):
    req = Request(url, headers=dict(headers or {}))
    with urlopen(req, timeout=5) as resp:
        return resp.status, dict(resp.headers), resp.read().decode("utf-8")


def _get_status(url, headers=None):
    try:
        return _get(url, headers)
    except HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


class _EchoUpstream(BaseHTTPRequestHandler):
    """Records the bodies it receives; answers 'ok'."""

    state = {"bodies": [], "models": []}

    @classmethod
    def reset(cls):
        cls.state = {"bodies": [], "models": []}

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        type(self).state["bodies"].append(body)
        type(self).state["models"].append(body.get("model"))
        raw = json.dumps({
            "id": "chatcmpl-1", "object": "chat.completion",
            "model": body.get("model", "m"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 2, "total_tokens": 52},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _run(config, fn):
    """Fake upstream + gateway; fn(gateway, base). ``config`` may be a callable."""

    class _Srv(ThreadingHTTPServer):
        allow_reuse_address = True

    up = _Srv(("127.0.0.1", 0), _EchoUpstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    base_up = f"http://127.0.0.1:{up.server_address[1]}/v1"
    if callable(config):
        config = config(base_up)
    config.upstream_base = base_up
    config.port = 0  # ephemeral — never collide with a real gateway
    gw = TokenOptimizerProxy(config)
    gw.start()
    try:
        fn(gw, f"http://127.0.0.1:{gw.port}")
    finally:
        gw.shutdown()
        up.shutdown()
        up.server_close()


_CHAT = {"model": "m", "messages": [{"role": "user", "content": "hello world"}]}


def _chat_payload(question):
    return {"model": "m", "messages": [{"role": "user", "content": question}]}


# ---------------------------------------------------------------- bind safety
class TestEnsureBindSafety(unittest.TestCase):
    def test_loopback_needs_no_keys(self):
        ensure_bind_safety("127.0.0.1", ())
        ensure_bind_safety("localhost", ())
        ensure_bind_safety("127.0.0.5", ())

    def test_non_loopback_without_keys_refuses_to_start(self):
        with self.assertRaises(SystemExit) as ctx:
            ensure_bind_safety("0.0.0.0", ())
        self.assertIn("--api-keys", str(ctx.exception))

    def test_non_loopback_with_keys_is_allowed(self):
        ensure_bind_safety("0.0.0.0", ("k",))

    def test_allow_insecure_acknowledges(self):
        ensure_bind_safety("10.0.0.5", (), allow_insecure=True)


# ----------------------------------------------------------------- inbound auth
class TestInboundAuth(unittest.TestCase):
    def test_no_keys_is_fully_backward_compatible(self):
        def fn(gw, base):
            status, _, body = _post(f"{base}/v1/chat/completions", _CHAT)
            self.assertEqual(status, 200)
            self.assertEqual(body["choices"][0]["message"]["content"], "ok")

        _run(ProxyConfig(api_keys=()), fn)

    def test_missing_key_is_401_and_counted(self):
        def fn(gw, base):
            status, headers, body = _post_status(
                f"{base}/v1/chat/completions", _CHAT)
            self.assertEqual(status, 401)
            self.assertIn("Bearer", headers.get("WWW-Authenticate", ""))
            self.assertIn("authentication_error", body["error"]["type"])
            self.assertEqual(gw.pipeline.metrics.as_dict()["auth_failures"], 1)
            # nothing reached upstream
            self.assertEqual(_EchoUpstream.state["bodies"], [])
            # no receipt for a request that never ran the pipeline
            self.assertEqual(len(gw.receipts), 0)

        _EchoUpstream.reset()
        _run(ProxyConfig(api_keys=("secret-key-a",)), fn)

    def test_wrong_key_is_401_and_the_key_is_never_echoed(self):
        def fn(gw, base):
            status, _, body = _post_status(
                f"{base}/v1/chat/completions", _CHAT,
                headers={"Authorization": "Bearer not-the-key"})
            self.assertEqual(status, 401)
            self.assertNotIn("not-the-key", json.dumps(body))
            self.assertNotIn("secret-key-a", json.dumps(body))

        _run(ProxyConfig(api_keys=("secret-key-a",)), fn)

    def test_bearer_and_optimizer_header_both_accepted(self):
        def fn(gw, base):
            status, _, _ = _post(
                f"{base}/v1/chat/completions", _CHAT,
                headers={"Authorization": "Bearer secret-key-a"})
            self.assertEqual(status, 200)
            status, _, _ = _post(
                f"{base}/v1/chat/completions", _CHAT,
                headers={"X-Optimizer-Api-Key": "secret-key-b"})
            self.assertEqual(status, 200)
            # both got past auth: first went upstream, second digest-hit
            self.assertEqual([r["route"] for r in gw.receipts], ["llm", "cache"])

        _EchoUpstream.reset()
        _run(ProxyConfig(api_keys=("secret-key-a", "secret-key-b")), fn)

    def test_health_stays_open_for_probes(self):
        def fn(gw, base):
            status, _, body = _get(f"{base}/healthz")
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["ok"])

        _run(ProxyConfig(api_keys=("k",)), fn)

    def test_metrics_and_dashboard_require_the_key(self):
        def fn(gw, base):
            status, _, _ = _get_status(f"{base}/metrics")
            self.assertEqual(status, 401)
            status, _, body = _get(f"{base}/metrics",
                                   headers={"X-Optimizer-Api-Key": "k"})
            self.assertEqual(status, 200)
            self.assertIn("session", json.loads(body))

        _run(ProxyConfig(api_keys=("k",)), fn)

    def test_comparison_uses_the_constant_time_primitive(self):
        def fn(gw, base):
            with mock.patch(
                "tokan.proxy.interceptor.hmac.compare_digest",
                wraps=__import__("hmac").compare_digest,
            ) as spy:
                _post(f"{base}/v1/chat/completions", _CHAT,
                      headers={"Authorization": "Bearer k"})
                _post_status(f"{base}/v1/chat/completions", _CHAT,
                             headers={"Authorization": "Bearer wrong"})
                self.assertGreaterEqual(spy.call_count, 2)

        _run(ProxyConfig(api_keys=("k",)), fn)

    def test_byok_passthrough_credential_plus_gateway_key_is_accepted(self):
        """A provider credential in Authorization must not mask the real key.

        BYOK clients send their provider credential as Bearer (which the
        gateway forwards upstream untouched) *and* can present the separate
        X-Optimizer-Api-Key — the request must be admitted on the second
        candidate, not rejected because the bearer isn't a gateway key.
        """
        def fn(gw, base):
            status, _, _ = _post(
                f"{base}/v1/chat/completions", _CHAT,
                headers={"Authorization": "Bearer sk-provider-cred-not-a-gateway-key",
                         "X-Optimizer-Api-Key": "sekrit-9x"})
            self.assertEqual(status, 200)
            self.assertEqual(gw.receipts[-1]["route"], "llm")

        _EchoUpstream.reset()
        _run(ProxyConfig(api_keys=("sekrit-9x",)), fn)

    def test_bearer_that_is_neither_key_nor_configured_is_rejected(self):
        def fn(gw, base):
            status, _, _ = _post_status(
                f"{base}/v1/chat/completions", _CHAT,
                headers={"Authorization": "Bearer random-credential"})
            self.assertEqual(status, 401)

        _run(ProxyConfig(api_keys=("sekrit-9x",)), fn)

    def test_authorized_receipts_carry_no_auth_material(self):
        def fn(gw, base):
            _post(f"{base}/v1/chat/completions", _CHAT,
                  headers={"Authorization": "Bearer sekrit-9x"})
            rec = gw.receipts[-1]
            self.assertNotIn("sekrit-9x", json.dumps(rec))

        _run(ProxyConfig(api_keys=("sekrit-9x",)), fn)


# -------------------------------------------------------------- secret posture
class TestSecretPosture(unittest.TestCase):
    def test_detect_only_leaves_the_wire_untouched_but_discloses(self):
        def fn(gw, base):
            status, headers, _ = _post(
                f"{base}/v1/chat/completions",
                _chat_payload(f"review: API_KEY={_AWS}"))
            self.assertEqual(status, 200)
            sent = _EchoUpstream.state["bodies"][-1]
            self.assertIn(_AWS, json.dumps(sent))  # untouched by default
            self.assertNotIn("X-Optimizer-Secrets-Redacted", headers)
            m = gw.pipeline.metrics.as_dict()
            self.assertEqual(m["secrets_detected"], 1)
            self.assertEqual(m["secrets_redacted"], 0)
            self.assertEqual(gw.receipts[-1]["secrets_detected"], 1)
            self.assertNotIn("secrets_redacted", gw.receipts[-1])

        _EchoUpstream.reset()
        _run(ProxyConfig(redact_secrets=False), fn)

    def test_redact_secrets_replaces_before_anything_leaves(self):
        def fn(gw, base):
            status, headers, _ = _post(
                f"{base}/v1/chat/completions",
                _chat_payload(f"review: API_KEY={_AWS}"),
                headers={"X-Optimizer-Session": "s1"})
            self.assertEqual(status, 200)
            sent = json.dumps(_EchoUpstream.state["bodies"][-1])
            self.assertNotIn(_AWS, sent)
            self.assertIn("[REDACTED:aws_access_key_id]", sent)
            self.assertEqual(headers.get("X-Optimizer-Secrets-Redacted"), "1")
            m = gw.pipeline.metrics.as_dict()
            self.assertEqual(m["secrets_detected"], 1)
            self.assertEqual(m["secrets_redacted"], 1)
            self.assertEqual(gw.receipts[-1]["secrets_redacted"], 1)

        _EchoUpstream.reset()
        _run(ProxyConfig(redact_secrets=True), fn)

    def test_two_secrets_same_question_share_one_cache_entry(self):
        """Redaction is deterministic: differing secrets → one digest."""
        def fn(gw, base):
            _post(f"{base}/v1/chat/completions",
                  _chat_payload(f"review: API_KEY={_AWS}"))
            _post(f"{base}/v1/chat/completions",
                  _chat_payload("review: API_KEY=AKIA" + "B1B2C3D4E5F6G7H8"))
            self.assertEqual(gw.receipts[0]["digest"], gw.receipts[1]["digest"])
            self.assertEqual(gw.receipts[1]["route"], "cache")
            self.assertEqual(len(_EchoUpstream.state["bodies"]), 1)

        _EchoUpstream.reset()
        _run(ProxyConfig(redact_secrets=True), fn)


# ----------------------------------------------------------------- quality gate
#: (title, prose) — prose is topic-distinctive so sections spread across
#: ~11 chunks at the 1200-char default; retrieval then sends a fraction.
_DOC_SECTIONS = [
    ("Section 1 (Parties)",
     "This Agreement is between Acme Customer Inc., a Delaware corporation, and "
     "Beta Vendor Ltd. Each party shall appoint a primary commercial contact and "
     "shall notify the other of any change within ten business days. Vendor "
     "represents that it is duly organized and has authority to enter this "
     "Agreement. Customer represents that its signatory is authorized to bind it."),
    ("Section 12 (Governing law)",
     "This Agreement is governed by the laws of the State of Delaware, without "
     "regard to conflict-of-laws rules. The parties consent to the exclusive "
     "jurisdiction of the state and federal courts located in Wilmington, "
     "Delaware for any action arising under this Agreement. No term of this "
     "Agreement may be construed against the drafter."),
    ("Section 19 (Termination)",
     "Either party may terminate this Agreement upon sixty (60) days written "
     "notice to the other party. Termination for material breach requires thirty "
     "(30) days written notice and a failure to cure within that period. Upon "
     "termination Vendor shall cease all use of Customer data and shall certify "
     "deletion within thirty days, except where retention is required by law."),
    ("Section 27 (Payment)",
     "All invoices are payable net thirty (30) days from the invoice date. "
     "Undisputed amounts outstanding beyond thirty days accrue interest at one "
     "and one-half percent per month or the maximum lawful rate, whichever is "
     "lower. Customer shall dispute invoice line items in writing within fifteen "
     "days of receipt. Fees are exclusive of taxes, which remain Customer's "
     "responsibility except for taxes on Vendor's net income."),
    ("Section 34 (Limitation of liability)",
     "Aggregate liability of either party under this Agreement is capped at "
     "$500,000 per contract year. Neither party is liable for indirect, "
     "incidental, special, consequential or punitive damages, including lost "
     "profits, data or goodwill, even if advised of the possibility. These "
     "caps do not apply to breaches of confidentiality, data protection "
     "obligations, or a party's indemnification duties."),
    ("Section 41 (Confidentiality)",
     "Each party shall protect the other's confidential information with at "
     "least the same care it uses for its own, and no less than reasonable "
     "care. These obligations survive termination for five (5) years, and "
     "indefinitely for trade secrets. Confidential information excludes material "
     "that becomes public through no fault, was lawfully known beforehand, or "
     "is independently developed without use of the disclosing party's data."),
    ("Section 47 (Data protection)",
     "The Vendor shall implement encryption at rest and in transit using "
     "industry-standard algorithms, maintain access controls with "
     "least-privilege defaults, and log all administrative access. Vendor shall "
     "notify Customer of any personal data breach without undue delay and in "
     "any case within seventy-two (72) hours of discovery, and shall cooperate "
     "with reasonable investigation and remediation requests."),
    ("Section 52 (Insurance)",
     "The Vendor shall maintain cyber liability coverage of at least "
     "$2,000,000 per occurrence, commercial general liability coverage of at "
     "least $1,000,000 per occurrence, and workers' compensation coverage as "
     "required by law. Certificates of insurance shall be furnished to Customer "
     "on request. Coverage shall remain in force for two years after "
     "termination of this Agreement."),
    ("Section 58 (Audit rights)",
     "The Customer may audit books and records relating to fees and compliance "
     "no more than once per calendar year, upon fifteen (15) days written "
     "notice, during normal business hours. Audits shall be conducted by an "
     "independent third party under confidentiality. If an audit reveals an "
     "underpayment exceeding five percent, Vendor shall bear the audit costs "
     "and promptly remediate the shortfall."),
    ("Section 63 (Force majeure)",
     "Neither party is liable for delays or failures in performance caused by "
     "events beyond its reasonable control, including natural disasters, war, "
     "terrorism, epidemic, government action, or prolonged utility or network "
     "outages. The affected party shall notify the other promptly and shall "
     "resume performance as soon as practicable. If force majeure continues "
     "more than ninety (90) days, either party may terminate."),
    ("Section 71 (Assignment)",
     "Neither party may assign this Agreement, in whole or in part, without the "
     "prior written consent of the other, except to a successor in interest in "
     "connection with a merger, acquisition or sale of substantially all "
     "assets, provided the successor assumes all obligations. Any attempted "
     "assignment in violation of this section is void. This Agreement binds and "
     "benefits the parties' permitted successors."),
    ("Section 77 (Notices)",
     "All notices under this Agreement shall be delivered to the addresses "
     "listed in Exhibit A, by personal delivery, internationally recognized "
     "courier, or certified mail with return receipt requested. Notices are "
     "deemed given on delivery confirmation. Either party may change its notice "
     "address upon written notice. Operational day-to-day communications may "
     "occur by email."),
    ("Section 84 (Severability)",
     "If any provision of this Agreement is held invalid, illegal or "
     "unenforceable, the remaining provisions continue in full force and "
     "effect. The invalid provision shall be modified to the minimum extent "
     "necessary to make it valid and enforceable while preserving the parties' "
     "original intent. No waiver of any provision is effective unless in "
     "writing and signed by the waiving party."),
    ("Section 91 (Entire agreement)",
     "This document, together with its exhibits, constitutes the entire "
     "agreement between the parties and supersedes all prior discussions, "
     "negotiations, representations and proposals, whether oral or written. "
     "Any amendment must be in writing and signed by authorized representatives "
     "of both parties. Purchase orders issued by Customer are administrative "
     "documents only and add no terms."),
    ("Section 99 (Records)",
     "The retention schedule is alpha bravo charlie delta echo: contractual "
     "records are retained seven years, payment records ten years, and "
     "operational logs ninety days. Records shall be stored in a resilient "
     "medium with tested restoration procedures. On written request Vendor "
     "shall provide copies of retained records within a reasonable time and "
     "shall certify destruction at schedule expiry."),
]
_DOC = "MASTER SERVICES AGREEMENT. " + " ".join(
    f"{title}. {prose}" for title, prose in _DOC_SECTIONS
)


def _doc_config(full_fallback):
    def build(base_up):
        return ProxyConfig(upstream_base=base_up, doc_fallback_full=full_fallback)
    return build


class TestDocQualityGate(unittest.TestCase):
    def test_low_coverage_falls_back_to_full_document_when_enabled(self):
        def fn(gw, base):
            _, _, ing = _post(f"{base}/v1/ingest", {"text": _DOC, "doc_id": "d_c"})
            self.assertEqual(ing["doc_id"], "d_c")
            _, _, ans = _post(f"{base}/v1/doc/ask", {
                "doc_id": "d_c",
                "question": "How does quantum entanglement work?",
                "model": "m",
            })
            sent = json.dumps(_EchoUpstream.state["bodies"][-1])
            # the FULL document went upstream, every section included —
            # summary + top-k excerpts would never carry all 15 sections.
            # (normalize first: wrapping turns some "Section 84" breaks into
            # "Section\n84" — content intact, substring count would lie)
            flat = sent.replace("\\n", " ")
            self.assertEqual(flat.count("Section "), len(_DOC_SECTIONS))
            self.assertIn("Section 84 (Severability)", flat)
            # and it arrived whole: this fixture is one long line, which the
            # CDM normalizer would elide at ~2k chars if the fallback had
            # skipped wrapping — silently truncating the tail.
            self.assertIn("certify destruction at schedule expiry", flat)
            opt = ans["optimizer"]
            self.assertEqual(opt["doc_fallback"], "full_doc")
            self.assertTrue(opt["low_coverage"])
            self.assertEqual(opt["chunks_used"], 0)      # no excerpts — full doc instead
            self.assertEqual(opt["retrieved_chunks"], [])
            # honest accounting: the naive document cost was (nearly) spent;
            # compaction still applies, so some savings remain, but this ask
            # saves far less than retrieval would have.
            self.assertGreater(opt["sent_tokens"], opt["full_doc_tokens"] * 0.5)
            self.assertEqual(gw.pipeline.metrics.as_dict()["doc_full_fallbacks"], 1)
            self.assertEqual(gw.receipts[-1]["doc_fallback"], "full_doc")

        _EchoUpstream.reset()
        _run(_doc_config(True), fn)

    def test_healthy_coverage_keeps_retrieval_with_fallback_enabled(self):
        def fn(gw, base):
            _post(f"{base}/v1/ingest", {"text": _DOC, "doc_id": "d_c"})
            _, _, ans = _post(f"{base}/v1/doc/ask", {
                "doc_id": "d_c", "question": "What is the cap on liability?",
                "model": "m", "top_k": 1,
            })
            self.assertNotIn("doc_fallback", ans["optimizer"])
            self.assertFalse(ans["optimizer"]["low_coverage"])
            self.assertEqual(ans["optimizer"]["chunks_used"], 1)
            # excerpts only: most of the document stayed home
            self.assertGreater(ans["optimizer"]["tokens_avoided"],
                               ans["optimizer"]["full_doc_tokens"] * 0.4)
            self.assertEqual(gw.pipeline.metrics.as_dict()["doc_full_fallbacks"], 0)

        _EchoUpstream.reset()
        _run(_doc_config(True), fn)

    def test_low_coverage_without_flag_behaves_as_before(self):
        def fn(gw, base):
            _post(f"{base}/v1/ingest", {"text": _DOC, "doc_id": "d_c"})
            _, _, ans = _post(f"{base}/v1/doc/ask", {
                "doc_id": "d_c",
                "question": "How does quantum entanglement work?",
                "model": "m",
            })
            sent = json.dumps(_EchoUpstream.state["bodies"][-1])
            # retrieval economics as before: far less than the whole document
            self.assertLess(sent.count("Section "), len(_DOC_SECTIONS))
            self.assertGreater(ans["optimizer"]["tokens_avoided"], 0)
            self.assertNotIn("doc_fallback", ans["optimizer"])
            self.assertTrue(ans["optimizer"]["low_coverage"])  # still disclosed

        _EchoUpstream.reset()
        _run(_doc_config(False), fn)


# -------------------------------------------------------------- routing wire-up
class TestTaskRouter(unittest.TestCase):
    def test_tier_is_disclosed_on_receipts_headers_and_metrics(self):
        def fn(gw, base):
            status, headers, _ = _post(
                f"{base}/v1/chat/completions",
                _chat_payload("please refactor the architecture of this module"))
            self.assertEqual(status, 200)
            self.assertEqual(gw.receipts[-1]["tier"], "tier_2")
            self.assertEqual(
                gw.pipeline.metrics.tiers, {"tier_2": 1})
            self.assertEqual(
                gw.pipeline.metrics.as_dict()["router_requests"], 1)
            # model untouched: classify-only is the default
            self.assertEqual(_EchoUpstream.state["models"][-1], "m")

        _EchoUpstream.reset()
        _run(ProxyConfig(task_router=True), fn)

    def test_no_tier_key_without_the_router(self):
        def fn(gw, base):
            _post(f"{base}/v1/chat/completions",
                  _chat_payload("refactor the architecture"))
            self.assertNotIn("tier", gw.receipts[-1])
            self.assertEqual(gw.pipeline.metrics.as_dict()["router_requests"], 0)

        _EchoUpstream.reset()
        _run(ProxyConfig(task_router=False), fn)

    def test_tier1_rewrite_only_with_explicit_model(self):
        def fn(gw, base):
            _post(f"{base}/v1/chat/completions",
                  _chat_payload("please format this json pretty print"))
            self.assertEqual(_EchoUpstream.state["models"][-1], "m")  # untouched

        _EchoUpstream.reset()
        _run(ProxyConfig(task_router=True, tier1_model=None), fn)

    def test_tier1_rewrite_applies_when_operator_opts_in(self):
        def fn(gw, base):
            _post(f"{base}/v1/chat/completions",
                  _chat_payload("please format this json pretty print"))
            self.assertEqual(_EchoUpstream.state["models"][-1], "gpt-4o-mini")
            self.assertEqual(gw.receipts[-1]["tier"], "tier_1")
            # frontier-classified requests keep the requested model
            _post(f"{base}/v1/chat/completions",
                  _chat_payload("refactor the whole architecture"))
            self.assertEqual(_EchoUpstream.state["models"][-1], "m")

        _EchoUpstream.reset()
        _run(ProxyConfig(task_router=True, tier1_model="gpt-4o-mini"), fn)


if __name__ == "__main__":
    unittest.main()
