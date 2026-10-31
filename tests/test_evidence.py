"""Evidence-unit retrieval (M5): ranked units across documents and the repo graph."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tokan.doc import DocumentStore
from tokan.evidence import EvidenceRetriever, repo_match_score
from tokan.repo_index import DEFAULT_IGNORE, build_graph

_CONTRACT = (
    "MASTER SERVICES AGREEMENT. Section 19 (Termination): either party may "
    "terminate upon sixty (60) days written notice. Section 34 (Limitation of "
    "liability): aggregate liability is capped at $500,000 per contract year. "
    "Section 27 (Payment): all invoices are payable net thirty (30) days."
)
_README = (
    "Tokan README. Install with pip install tokan. Configuration happens via "
    "environment variables. The gateway proxies OpenAI and Anthropic wires."
)


def _fixture_repo(root: Path) -> None:
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "billing.py").write_text(
        "class Invoice:\n    pass\n\ndef compute_total():\n    return 0\n",
        encoding="utf-8",
    )
    (root / "pkg" / "governor.py").write_text(
        "class ContextGovernor:\n    pass\n", encoding="utf-8"
    )


class TestRepoMatchScore(unittest.TestCase):
    def test_exact_symbol_match_is_near_certain(self):
        self.assertAlmostEqual(
            repo_match_score("CompressionPolicy", "CompressionPolicy", "tokan/policy.py"),
            0.99,
        )

    def test_substring_beats_path_only(self):
        sub = repo_match_score("policy rules", "CompressionPolicy", "x/y.py")
        path = repo_match_score("policy rules", "SomethingElse", "policy/rules.py")
        self.assertGreater(sub, path)

    def test_no_shared_terms_scores_zero(self):
        self.assertEqual(
            repo_match_score("quantum entanglement", "Invoice", "pkg/billing.py"), 0.0
        )
        self.assertEqual(repo_match_score("", "Invoice", "pkg/billing.py"), 0.0)

    def test_path_match_survives_stopword_noise(self):
        # "where" is a stopword; "billing" still reaches the path.
        self.assertGreater(
            repo_match_score("where is billing handled", "compute_total", "pkg/billing.py"),
            0.0,
        )


class TestEvidenceDocOnly(unittest.TestCase):
    def setUp(self):
        self.store = DocumentStore()
        self.contract = self.store.ingest(_CONTRACT, doc_id="d_contract")
        self.readme = self.store.ingest(_README, doc_id="d_readme")

    def test_query_returns_only_matching_doc_units(self):
        units = EvidenceRetriever(self.store).search("termination notice", top_k=5)
        self.assertTrue(units)
        self.assertTrue(all(u.source == "doc" for u in units))
        self.assertIn("d_contract", units[0].key)
        self.assertIn("terminate", units[0].text)

    def test_relevance_ordering_best_doc_first(self):
        units = EvidenceRetriever(self.store).search("termination notice", top_k=10)
        keys = [u.key for u in units]
        self.assertIn("d_contract", keys[0])
        # the readme should not outrank the contract for a contract question
        if "d_readme" in keys:
            self.assertGreater(units[0].score, units[keys.index("d_readme")].score)

    def test_doc_ids_restriction_and_unknown_ids_skipped(self):
        units = EvidenceRetriever(self.store).search(
            "termination notice", top_k=5, doc_ids=["d_readme", "d_missing"]
        )
        self.assertTrue(all("d_readme" in u.key for u in units))
        self.assertTrue(all("d_contract" not in u.key for u in units))

    def test_empty_query_returns_nothing(self):
        self.assertEqual(EvidenceRetriever(self.store).search("", top_k=5), [])

    def test_empty_store_returns_nothing(self):
        self.assertEqual(EvidenceRetriever(DocumentStore()).search("anything", top_k=5), [])

    def test_units_carry_token_bounds_and_as_dict(self):
        units = EvidenceRetriever(self.store).search("termination", top_k=5)
        for u in units:
            self.assertGreater(u.tokens, 0)
            self.assertLess(u.tokens, 600)  # bounded excerpt, never the whole doc
            d = u.as_dict()
            self.assertEqual(d["source"], u.source)
            self.assertEqual(d["score"], u.score)


class TestEvidenceCrossStore(unittest.TestCase):
    def setUp(self):
        self.store = DocumentStore()
        self.doc = self.store.ingest(_CONTRACT, doc_id="d_contract")
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _fixture_repo(self.root)
        self.graph = build_graph(self.root, DEFAULT_IGNORE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_repo_units_join_doc_units_with_source_labels(self):
        units = EvidenceRetriever(self.store, self.graph).search(
            "liability billing", top_k=10
        )
        sources = {u.source for u in units}
        self.assertIn("doc", sources)    # contract mentions invoices
        self.assertIn("repo", sources)   # Invoice symbol + billing.py path
        for u in units:
            self.assertIn(u.source, ("doc", "repo"))

    def test_symbol_query_surfaces_defining_unit(self):
        units = EvidenceRetriever(self.store, self.graph).search(
            "ContextGovernor", top_k=5
        )
        repo_units = [u for u in units if u.source == "repo"]
        self.assertTrue(repo_units)
        self.assertIn("ContextGovernor", repo_units[0].key)
        self.assertIn("pkg/governor.py", repo_units[0].text)

    def test_repo_unit_text_carries_relations(self):
        units = EvidenceRetriever(self.store, self.graph).search(
            "compute_total", top_k=5
        )
        unit = next(u for u in units if "compute_total" in u.key)
        self.assertIn("defined in pkg/billing.py", unit.text)

    def test_exact_symbol_outranks_moderate_doc_cosine(self):
        units = EvidenceRetriever(self.store, self.graph).search(
            "ContextGovernor", top_k=10
        )
        self.assertEqual(units[0].source, "repo")
        self.assertGreater(units[0].score, 0.9)

    def test_merge_is_deterministic(self):
        r = EvidenceRetriever(self.store, self.graph)
        first = [u.as_dict() for u in r.search("invoice total billing", top_k=10)]
        second = [u.as_dict() for u in r.search("invoice total billing", top_k=10)]
        self.assertEqual(first, second)

    def test_top_k_bounds_output_even_with_broad_query(self):
        units = EvidenceRetriever(self.store, self.graph).search("pkg", top_k=3)
        self.assertLessEqual(len(units), 3)
        # ordering is fully determined: score desc, then source, then key
        order = [(-u.score, u.source, u.key) for u in units]
        self.assertEqual(order, sorted(order))


if __name__ == "__main__":
    unittest.main()
