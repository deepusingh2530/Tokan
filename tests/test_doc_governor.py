"""Doc Governor (Phase 2): ingest once, answer many — retrieval-augmented
document Q&A with measured savings.

What is under test:

* Chunking invariants (bounds, overlap, content coverage — nothing lost).
* TF-IDF retrieval quality: recall@k over a synthetic contract where every
  fact lives in exactly one clause (quality is measured, not asserted).
* Extractive summary: deterministic, strictly extractive, bounded.
* DocumentStore: content-addressed ids, LRU bounds, refresh-on-reingest.
* The /v1/ingest + /v1/doc/ask wire path end-to-end against a fake upstream
  acting as an *extractive oracle* (answers only if the gold fact actually
  reached the prompt) — proving retrieval preserves answerability at a
  fraction of the full-document tokens.
* Cache interplay: repeating a question is answered with zero upstream tokens.
* MCP tools doc_ingest / doc_search for agent-side reuse.
"""
import json
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

from tokan.cdm import estimate_tokens
from tokan.doc import (
    Document,
    DocumentStore,
    assemble_messages,
    chunk_text,
    full_resend_tokens,
    summarize,
    TfidfIndex,
)
from tokan.mcp import MCPServer
from tokan.metrics import SessionMetrics
from tokan.pipeline import OptimizationPipeline
from tokan.proxy.interceptor import ProxyConfig, TokenOptimizerProxy


# ------------------------------------------------------------------- fixtures
#: Distinctive gold facts. Each is a SINGLE sentence (no internal sentence
#: punctuation) so it is guaranteed to live whole inside exactly one chunk —
#: which is what makes recall and answerability assertions well-defined.
FACTS = {
    "governing law": "Section 12 (Governing law): this Agreement is governed by the laws of the State of Delaware.",
    "termination": "Section 19 (Termination): either party may terminate this Agreement upon sixty (60) days written notice.",
    "payment": "Section 27 (Payment): all invoices are payable net thirty (30) days from the invoice date.",
    "liability": "Section 34 (Limitation of liability): aggregate liability is capped at $500,000 per contract year.",
    "confidentiality": "Section 41 (Confidentiality): these obligations survive termination for five (5) years.",
    "arbitration": "Section 48 (Dispute resolution): all disputes shall be resolved by binding arbitration in Chicago.",
    "insurance": "Section 55 (Insurance): the Vendor shall maintain cyber liability coverage of at least $2,000,000.",
    "audit": "Section 62 (Audit rights): the Customer may audit the books no more than once per calendar year.",
}

_FILLER = (
    "Section {n} (Miscellaneous): the parties shall keep complete and accurate "
    "books and records relating to the services described herein, and shall "
    "provide reasonable access to such records upon request during normal "
    "business hours. Neither party may assign its rights without prior written "
    "consent except in connection with a merger or sale of substantially all "
    "assets. Notices must be delivered by registered mail or internationally "
    "recognized courier to the addresses listed in the preamble."
)

_FACT_CLAUSES = {12: "governing law", 19: "termination", 27: "payment",
                 34: "liability", 41: "confidentiality", 48: "arbitration",
                 55: "insurance", 62: "audit"}


def _contract() -> str:
    """A synthetic ~70-clause contract with8 known facts among filler clauses."""
    lines = [
        "MASTER SERVICES AGREEMENT",
        "This Master Services Agreement is entered into by Acme Customer and "
        "Beta Vendor, effective as of the first day of the fiscal year.",
    ]
    for n in range(1, 71):
        keyword = _FACT_CLAUSES.get(n)
        lines.append(FACTS[keyword] if keyword else _FILLER.format(n=n))
    return "\n\n".join(lines)


def _question(keyword: str) -> str:
    return {
        "governing law": "Which state's law governs this agreement?",
        "termination": "How much notice is required to terminate?",
        "payment": "What are the payment terms for invoices?",
        "liability": "What is the cap on liability?",
        "confidentiality": "How long do confidentiality obligations survive?",
        "arbitration": "Where does dispute resolution happen?",
        "insurance": "How much cyber liability insurance is required?",
        "audit": "How often can the customer audit the books?",
    }[keyword]


# --------------------------------------------------------------------- utils
def _post(url, payload, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = Request(url, data=json.dumps(payload).encode("utf-8"), headers=h, method="POST")
    with urlopen(req, timeout=10) as resp:
        return resp.status, dict(resp.headers), json.loads(resp.read().decode("utf-8"))


def _post_status(url, payload):
    """POST returning (status, body) for 2xx and 4xx alike."""
    try:
        status, _, body = _post(url, payload)
        return status, body
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get(url):
    with urlopen(url, timeout=5) as resp:
        return resp.status, dict(resp.headers), resp.read().decode("utf-8")


def _oracle_upstream():
    """Fake upstream that answers ONLY if a gold fact reached the prompt.

    This is what makes the end-to-end tests meaningful: the 'model' cannot
    answer from parameters or priors — only from the context Tokan assembled.
    """
    state = {"bodies": [], "calls": 0}

    class _Up(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            state["bodies"].append(body)
            state["calls"] += 1
            prompt = "\n".join(
                str(m.get("content", "")) for m in body.get("messages") or []
            )
            # Extractive oracle: report every gold fact that actually reached
            # the prompt — the model 'knows' only what context provides.
            found = [fact for fact in FACTS.values() if fact in prompt]
            answer = (
                "The document states: " + " ".join(found)
                if found
                else "I cannot find it in the provided document."
            )
            prompt_tokens = estimate_tokens(prompt)
            raw = json.dumps({
                "id": "chatcmpl-doc", "object": "chat.completion",
                "model": body.get("model", "m"),
                "choices": [{"index": 0, "message": {"role": "assistant",
                                                     "content": answer},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 12,
                          "total_tokens": prompt_tokens + 12},
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    _Up.state = state
    return _Up


def _make_server(handler_cls):
    import threading

    class _Srv(ThreadingHTTPServer):
        allow_reuse_address = True

    srv = _Srv(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _run(handler_cls, config=None, pipeline=None, fn=None):
    up = _make_server(handler_cls)
    base = f"http://127.0.0.1:{up.server_address[1]}/v1"
    pipe = pipeline or OptimizationPipeline()
    cfg = config(base) if callable(config) else (config or ProxyConfig(upstream_base=base))
    cfg.port = 0
    gw = TokenOptimizerProxy(cfg, pipe).start()
    try:
        return fn(gw, gw.origin)
    finally:
        gw.shutdown()
        up.shutdown()
        up.server_close()


# -------------------------------------------------------------------- chunking
class TestChunking(unittest.TestCase):
    def test_empty_and_short_text(self):
        self.assertEqual(chunk_text(""), [])
        self.assertEqual(chunk_text("   \n "), [])
        self.assertEqual(chunk_text("One short document."), ["One short document."])

    def test_respects_max_chars(self):
        chunks = chunk_text(_contract(), max_chars=800)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(c) <= 800 for c in chunks))

    def test_no_content_lost(self):
        # Every gold clause must be fully present in at least one chunk.
        chunks = chunk_text(_contract(), max_chars=700)
        missing = [k for k, fact in FACTS.items()
                   if not any(fact in c for c in chunks)]
        self.assertEqual(missing, [], f"facts split or dropped: {missing}")

    def test_oversized_blob_hard_windows_with_overlap(self):
        blob = "x" * 5000  # no sentence boundaries at all
        chunks = chunk_text(blob, max_chars=1000, overlap=150)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertGreater(len(chunks), 3)
        # Consecutive windows share exactly `overlap` characters.
        for a, b in zip(chunks, chunks[1:]):
            self.assertEqual(a[-150:], b[:150])

    def test_deterministic(self):
        text = _contract()
        self.assertEqual(chunk_text(text), chunk_text(text))


# -------------------------------------------------------------------- retrieval
class TestRetrieval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = Document("d_facts", _contract())

    def test_recall_at_3_all_facts(self):
        # Quality is measured, not asserted: every gold fact's chunk must be
        # inside the top-3 retrieved chunks for its question.
        misses = []
        for keyword, fact in FACTS.items():
            idxs = self.doc.index.search(_question(keyword), top_k=3)
            if not any(fact in self.doc.chunks[i] for i in idxs):
                misses.append(keyword)
        self.assertEqual(misses, [], f"recall@3 misses: {misses}")

    def test_recovers_gold_chunk_position(self):
        fact = FACTS["liability"]
        gold = next(i for i, c in enumerate(self.doc.chunks) if fact in c)
        idxs = self.doc.index.search(_question("liability"), top_k=1)
        self.assertEqual(idxs, [gold])

    def test_no_lexical_match_returns_nothing(self):
        # The index never invents relevance: zero shared terms -> zero hits.
        index = TfidfIndex(["alpha beta gamma", "delta epsilon"])
        self.assertEqual(index.search("zzz qqq"), [])

    def test_search_is_deterministic(self):
        q = _question("payment")
        self.assertEqual(
            self.doc.index.search(q, top_k=5),
            self.doc.index.search(q, top_k=5),
        )

    def test_retrieve_falls_back_to_lead_chunks(self):
        chunks, idxs = self.doc.retrieve("qqq zzz", top_k=2)
        self.assertEqual(idxs, [0, 1])
        self.assertEqual(len(chunks), 2)


# --------------------------------------------------------------------- summary
class TestSummary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = Document("d_facts", _contract())
        cls.doc_text = " ".join(_contract().split())

    def test_strictly_extractive(self):
        # Every summary sentence appears verbatim (whitespace-normalized) in
        # the source document — the summary invents nothing.
        for sentence in self.doc.summary.split(". "):
            sentence = sentence.rstrip(".").strip()
            if len(sentence) > 10:
                self.assertIn(sentence, self.doc_text)

    def test_deterministic(self):
        doc2 = Document("d_facts", _contract())
        self.assertEqual(self.doc.summary, doc2.summary)
        self.assertEqual(
            summarize(self.doc.chunks, self.doc.index),
            summarize(self.doc.chunks, self.doc.index),
        )

    def test_bounded_and_smaller_than_doc(self):
        self.assertLess(self.doc.summary_tokens, self.doc.total_tokens)
        # 6 selected sentences; splitting on ". " may yield a few extra tokens
        # but nothing near the full document.
        self.assertLessEqual(len(self.doc.summary.split(". ")), 8)

    def test_empty_doc(self):
        self.assertEqual(summarize([], TfidfIndex([])), "")


# ---------------------------------------------------------------------- store
class TestDocumentStore(unittest.TestCase):
    def test_content_addressed_ids_are_stable(self):
        store = DocumentStore()
        a = store.ingest("alpha contract text")
        b = store.ingest("alpha contract text")
        self.assertEqual(a.doc_id, b.doc_id)
        self.assertTrue(a.doc_id.startswith("d_"))
        self.assertEqual(len(store), 1)

    def test_explicit_doc_id_honored(self):
        store = DocumentStore()
        doc = store.ingest("some text", doc_id="my-doc")
        self.assertEqual(doc.doc_id, "my-doc")
        self.assertIs(store.get("my-doc"), doc)

    def test_unknown_id_is_none(self):
        self.assertIsNone(DocumentStore().get("nope"))

    def test_empty_text_rejected(self):
        with self.assertRaises(ValueError):
            DocumentStore().ingest("   ")

    def test_lru_eviction(self):
        store = DocumentStore(max_docs=2)
        first = store.ingest("document one").doc_id
        store.ingest("document two")
        store.ingest("document three")
        self.assertIsNone(store.get(first))
        self.assertEqual(len(store), 2)

    def test_get_refreshes_lru_position(self):
        store = DocumentStore(max_docs=2)
        first = store.ingest("document one").doc_id
        store.ingest("document two")
        store.get(first)              # touch first -> two is now oldest
        store.ingest("document three")
        self.assertIsNotNone(store.get(first))


# ------------------------------------------------------------------- assembling
class TestAssemble(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = Document("d", _contract())

    def test_prompt_contains_summary_excerpts_and_question(self):
        question = _question("liability")
        messages, idxs, chunks = assemble_messages(self.doc, question, top_k=3)
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertIn("ONLY the document excerpts", messages[0]["content"])
        user = messages[1]["content"]
        self.assertIn(self.doc.summary, user)
        self.assertIn(question, user)
        self.assertIn("(chunk", user)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(len(idxs), 3)
        # The gold fact made it into the assembled prompt.
        self.assertIn(FACTS["liability"], user)

    def test_excerpts_are_exactly_the_top_k_chunks(self):
        # Precision: nothing but the ranked chunks travels (plus the summary).
        question = _question("audit")
        top_k = 3
        idxs = self.doc.index.search(question, top_k=top_k)
        messages, got_idxs, _ = assemble_messages(self.doc, question, top_k=top_k)
        self.assertEqual(got_idxs, idxs)
        user = messages[1]["content"]
        for i in idxs:
            self.assertIn(self.doc.chunks[i][:1500], user)

    def test_full_resend_baseline_is_the_whole_doc(self):
        question = "What is the liability cap?"
        self.assertEqual(
            full_resend_tokens(self.doc, question),
            self.doc.total_tokens + estimate_tokens(question),
        )
        _, _, chunks = assemble_messages(self.doc, question, top_k=3)
        assembled = sum(estimate_tokens(c) for c in chunks) + self.doc.summary_tokens
        self.assertLess(assembled, full_resend_tokens(self.doc, question))


# --------------------------------------------------------------------- metrics
class TestDocMetrics(unittest.TestCase):
    def test_record_doc_math(self):
        m = SessionMetrics()
        m.record_ingest(chunks=10, tokens=5000)
        m.record_doc(full_tokens=5100, sent_tokens=700, chunks_used=3)
        m.record_doc(full_tokens=5100, sent_tokens=0, chunks_used=3)  # cache hit
        self.assertEqual(m.doc_ingests, 1)
        self.assertEqual(m.doc_ingested_tokens, 5000)
        self.assertEqual(m.doc_questions, 2)
        self.assertEqual(m.doc_full_tokens, 10200)
        self.assertEqual(m.doc_sent_tokens, 700)
        self.assertEqual(m.doc_tokens_avoided, 9500)
        self.assertAlmostEqual(m.doc_avoided_pct, 93.14, places=1)

    def test_doc_avoidance_never_leaks_into_compaction_savings(self):
        m = SessionMetrics()
        m.record(route="llm", original_tokens=1000, optimized_tokens=1000)
        m.record_doc(full_tokens=9000, sent_tokens=1000, chunks_used=3)
        self.assertEqual(m.tokens_saved, 0)          # compaction savings only
        self.assertEqual(m.doc_tokens_avoided, 8000) # reported separately

    def test_pipeline_books_doc_metrics(self):
        pipe = OptimizationPipeline()
        payload = {"model": "m", "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q about liability"},
        ]}
        result = pipe.prepare(payload)
        result.doc_savings = {"doc_id": "d1", "full_tokens": 7000, "chunks_used": 3}
        pipe.serve_local(result)  # cache/local path books with sent=0
        self.assertEqual(pipe.metrics.doc_questions, 1)
        self.assertEqual(pipe.metrics.doc_tokens_avoided, 7000)


# ---------------------------------------------------------------- end-to-end
class TestDocGovernorEndToEnd(unittest.TestCase):
    def _ingest(self, base):
        status, body = _post_status(f"{base}/ingest", {"text": _contract()})
        self.assertEqual(status, 200, body)
        return body

    def test_ingest_returns_stats_and_is_idempotent(self):
        Up = _oracle_upstream()

        def fn(gw, base):
            body = self._ingest(base)
            self.assertTrue(body["doc_id"].startswith("d_"))
            self.assertGreater(body["chunks"], 1)
            self.assertGreater(body["total_tokens"], body["summary_tokens"])
            self.assertTrue(body["summary"])
            # Same text -> same content-addressed id, still one stored doc.
            body2 = self._ingest(base)
            self.assertEqual(body2["doc_id"], body["doc_id"])
            self.assertEqual(len(gw.pipeline.documents), 1)
            m = gw.pipeline.metrics.as_dict()
            self.assertEqual(m["doc_ingests"], 2)
            self.assertEqual(m["doc_ingested_tokens"], body["total_tokens"] * 2)

        _run(Up, fn=fn)

    def test_ingest_rejects_missing_text(self):
        Up = _oracle_upstream()

        def fn(gw, base):
            for bad in ({}, {"text": "   "}, {"text": 42}):
                status, body = _post_status(f"{base}/ingest", bad)
                self.assertEqual(status, 400)
                self.assertIn("text", body["error"]["message"])

        _run(Up, fn=fn)

    def test_ask_answers_from_retrieved_context_with_big_savings(self):
        Up = _oracle_upstream()

        def fn(gw, base):
            ing = self._ingest(base)
            doc_id = ing["doc_id"]
            question = _question("liability")
            status, headers, ans = _post(f"{base}/doc/ask", {
                "doc_id": doc_id, "question": question, "model": "m",
            })
            self.assertEqual(status, 200)

            # The oracle answered *because* Tokan put the gold fact in the prompt.
            content = ans["choices"][0]["message"]["content"]
            self.assertIn("$500,000", content)
            sent = json.dumps(Up.state["bodies"][-1])
            self.assertIn(FACTS["liability"], sent)
            # ...and the gold chunk was the one retrieved (its marker travels).
            doc = gw.pipeline.documents.get(doc_id)
            gold = next(i for i, c in enumerate(doc.chunks) if FACTS["liability"] in c)
            self.assertIn(f"(chunk {gold + 1})", sent)

            # Response discloses the doc economics.
            opt = ans["optimizer"]
            self.assertEqual(opt["mode"], "doc")
            self.assertEqual(opt["doc_id"], doc_id)
            self.assertIn(gold + 1, opt["retrieved_chunks"])
            self.assertGreater(opt["full_doc_tokens"], opt["sent_tokens"])
            self.assertGreater(opt["avoided_pct"], 50)
            self.assertEqual(headers["X-Optimizer-Doc-Id"], doc_id)
            self.assertGreater(int(headers["X-Optimizer-Doc-Avoided-Tokens"]), 0)

            # Receipt + metrics tell the same story.
            rec = gw.receipts[-1]
            self.assertEqual(rec["doc_id"], doc_id)
            self.assertEqual(rec["original_tokens"],
                             ing["total_tokens"] + estimate_tokens(question))
            self.assertGreater(rec["saved_pct"], 50)
            self.assertEqual(rec["session"], f"doc:{doc_id}")
            m = gw.pipeline.metrics.as_dict()
            self.assertEqual(m["doc_questions"], 1)
            self.assertGreater(m["doc_tokens_avoided"], 0)
            self.assertGreater(m["doc_avoided_pct"], 50)
            # Compaction savings stay separate: they may only reflect trimming
            # the assembled payload itself (tiny), never the doc avoidance.
            self.assertLess(m["tokens_saved"], 100)
            self.assertLess(m["tokens_saved"], m["doc_tokens_avoided"] // 10)

        _run(Up, fn=fn)

    def test_repeat_question_hits_cache_with_zero_upstream_calls(self):
        Up = _oracle_upstream()

        def fn(gw, base):
            doc_id = self._ingest(base)["doc_id"]
            payload = {"doc_id": doc_id, "question": _question("audit"), "model": "m"}
            _post(f"{base}/doc/ask", payload)
            _, _, ans2 = _post(f"{base}/doc/ask", payload)
            self.assertEqual(Up.state["calls"], 1)  # second ask never left the box
            rec2 = gw.receipts[-1]
            self.assertEqual(rec2["route"], "cache")
            self.assertEqual(rec2["sent_tokens"], 0)
            self.assertEqual(rec2["saved_pct"], 100)
            self.assertEqual(ans2["optimizer"]["route"], "cache")
            self.assertEqual(ans2["optimizer"]["tokens_avoided"],
                             ans2["optimizer"]["full_doc_tokens"])
            m = gw.pipeline.metrics.as_dict()
            self.assertEqual(m["doc_questions"], 2)
            # Only the first ask actually spent prompt tokens upstream; the
            # oracle bills what it received (joined message contents).
            first_prompt = "\n".join(
                str(x.get("content", "")) for x in Up.state["bodies"][0]["messages"]
            )
            self.assertEqual(m["doc_sent_tokens"], estimate_tokens(first_prompt))

        _run(Up, fn=fn)

    def test_openai_style_messages_accepted_as_question(self):
        Up = _oracle_upstream()

        def fn(gw, base):
            doc_id = self._ingest(base)["doc_id"]
            _, _, ans = _post(f"{base}/doc/ask", {
                "doc_id": doc_id, "model": "m",
                "messages": [{"role": "user", "content": _question("arbitration")}],
            })
            self.assertIn("Chicago", ans["choices"][0]["message"]["content"])

        _run(Up, fn=fn)

    def test_request_errors_are_explicit(self):
        Up = _oracle_upstream()

        def fn(gw, base):
            doc_id = self._ingest(base)["doc_id"]
            cases = [
                (400, {}, "doc_id"),
                (404, {"doc_id": "missing", "question": "q", "model": "m"}, "unknown doc_id"),
                (400, {"doc_id": doc_id, "model": "m"}, "question"),
                (400, {"doc_id": doc_id, "question": "q"}, "model"),
            ]
            for expected, payload, needle in cases:
                status, body = _post_status(f"{base}/doc/ask", payload)
                self.assertEqual(status, expected)
                self.assertIn(needle, body["error"]["message"])
            self.assertEqual(Up.state["calls"], 0)  # no bad request reached upstream

        _run(Up, fn=fn)

    def test_dashboard_shows_doc_tiles_and_row_marker(self):
        Up = _oracle_upstream()

        def fn(gw, base):
            doc_id = self._ingest(base)["doc_id"]
            _post(f"{base}/doc/ask",
                  {"doc_id": doc_id, "question": _question("payment"), "model": "m"})
            _, _, body = _get(base + "/dashboard")
            self.assertIn("Doc Q&amp;A", body)
            self.assertIn("Doc avoided", body)
            self.assertIn("doc</span>", body)  # per-row doc marker

        _run(Up, fn=fn)


# ------------------------------------------------------------------------ MCP
class TestDocMcpTools(unittest.TestCase):
    def _call(self, server, name, args):
        resp = server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args},
        })
        result = resp["result"]
        text = result["content"][0]["text"]
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None  # tool-error payloads are prose, not JSON
        return result, parsed

    def test_tools_listed(self):
        resp = MCPServer().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertIn("doc_ingest", names)
        self.assertIn("doc_search", names)

    def test_ingest_then_search_returns_smaller_context(self):
        server = MCPServer()
        _, ingested = self._call(server, "doc_ingest", {"text": _contract()})
        self.assertTrue(ingested["doc_id"].startswith("d_"))
        self.assertGreater(ingested["chunks"], 1)

        _, found = self._call(server, "doc_search", {
            "doc_id": ingested["doc_id"], "query": _question("liability"),
        })
        self.assertIn(FACTS["liability"], json.dumps(found["excerpts"]))
        self.assertLess(found["retrieved_tokens"], found["full_document_tokens"])
        self.assertGreater(len(found["excerpts"]), 0)

    def test_search_unknown_doc_is_tool_error(self):
        server = MCPServer()
        result, _ = self._call(server, "doc_search",
                               {"doc_id": "missing", "query": "anything"})
        self.assertTrue(result["isError"])
        self.assertIn("doc_ingest", result["content"][0]["text"])

    def test_search_requires_args(self):
        server = MCPServer()
        result, _ = self._call(server, "doc_search", {"doc_id": "x"})
        self.assertTrue(result["isError"])


if __name__ == "__main__":
    unittest.main()
