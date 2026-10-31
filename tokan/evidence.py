"""Evidence-unit retrieval (M5): one ranked list over documents + repo symbols.

The waste this removes
----------------------
An agent answering "how does Tokan classify content?" typically re-reads whole
files (or re-asks the LLM about them) to find the two lines that matter.
``EvidenceRetriever`` answers *where the evidence lives* in one deterministic
call: the most relevant chunks of every ingested document plus the repo
symbols and files that match the question — each unit small enough to read or
to cite directly.

Honesty contract (read before trusting scores)
----------------------------------------------
* **Doc units** carry the TF-IDF cosine of their chunk (0..1, calibrated
  within a document) — a real similarity, not a heuristic.
* **Repo units** carry a *documented heuristic*: for each content term of the
  query, 1.0 exact-symbol / 0.7 substring-of-symbol / 0.5 substring-of-path,
  combined as ``best * (0.6 + 0.4 * term_coverage)`` capped at 0.99. It ranks
  within the repo store; **it is not calibrated against doc cosines**.
* Cross-store ordering therefore prefers doc scores when they're genuinely
  high, but a near-certain symbol hit (0.99) intentionally outranks a moderate
  chunk — a name match is stronger evidence than a passing similarity.
  Consumers get ``source`` + store-native ``score`` on every unit and should
  treat them as labeled, not interchangeable.
* Every unit is bounded (excerpt cap + token estimate) so a caller can budget
  before reading. Empty stores return ``[]`` — never an invented unit.

Stdlib only; deterministic; no embeddings, no network.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from .cdm import estimate_tokens
from .doc import DEFAULT_TOP_K, DocumentStore, STOP_TERMS, _terms
from .repo_index import RepoGraph

__all__ = ["EvidenceRetriever", "EvidenceUnit", "repo_match_score"]

DEFAULT_EXCERPT_CHARS = 1500
DEFAULT_TOP_K_EVIDENCE = 5


@dataclass(frozen=True)
class EvidenceUnit:
    """One piece of evidence: what it is, where it came from, why it matched."""

    source: str    # "doc" | "repo"
    key: str       # "d_<hash>#<chunk>" | "<symbol>@<path>"
    title: str     # short label for display/citation
    text: str      # bounded excerpt (never the whole doc/file)
    score: float   # store-native relevance (see module docstring)
    tokens: int    # estimate of title+text — budget before reading

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "key": self.key,
            "title": self.title,
            "text": self.text,
            "score": self.score,
            "tokens": self.tokens,
        }


def _content_terms(query: str) -> List[str]:
    terms = [t for t in _terms(query) if t not in STOP_TERMS]
    return terms or [t for t in _terms(query)]


def repo_match_score(query: str, symbol: str, path: str) -> float:
    """Documented heuristic in [0, 0.99]: does this symbol/path match the query?

    ``best term strength * (0.6 + 0.4 * fraction of terms matched)`` — an
    exact symbol-name match with full term coverage scores ~0.99 (the query
    literally named the symbol); a path-only substring hit scores lower.
    Zero when no term matches at all: no evidence, no unit.
    """
    terms = _content_terms(query)
    if not terms:
        return 0.0
    sl = symbol.lower()
    pl = path.lower()
    best = 0.0
    matched = 0
    for t in terms:
        if t == sl:
            s = 1.0
        elif t in sl:
            s = 0.7
        elif t in pl:
            s = 0.5
        else:
            s = 0.0
        if s > 0:
            matched += 1
        best = max(best, s)
    if matched == 0:
        return 0.0
    coverage = matched / len(terms)
    return min(0.99, best * (0.6 + 0.4 * coverage))


class EvidenceRetriever:
    """Ranked evidence over the gateway's documents and (optionally) a repo graph.

    Both stores are consulted independently and merged best-first; ``graph``
    may be ``None`` (docs-only — the HTTP surface, which never walks the
    filesystem; MCP passes a graph because it runs with the agent's own
    trust). Doc ids can be restricted with ``doc_ids`` (unknown ids are
    skipped, never an error).
    """

    def __init__(
        self,
        documents: DocumentStore,
        graph: Optional[RepoGraph] = None,
        *,
        max_excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
        max_referrers: int = 3,
    ) -> None:
        self.documents = documents
        self.graph = graph
        self.max_excerpt_chars = max(80, int(max_excerpt_chars))
        self.max_referrers = max(0, int(max_referrers))

    def search(
        self,
        query: str,
        *,
        top_k: int = DEFAULT_TOP_K_EVIDENCE,
        doc_ids: Optional[Sequence[str]] = None,
    ) -> List[EvidenceUnit]:
        """Best-first evidence for ``query`` (deduped, bounded, deterministic)."""
        top_k = max(1, min(50, int(top_k)))
        units: List[EvidenceUnit] = []
        units.extend(self._doc_units(query, top_k=top_k, doc_ids=doc_ids))
        if self.graph is not None:
            units.extend(self._repo_units(query, top_k=top_k))
        # Stable, fully-determined order: score first, then source/key so ties
        # never depend on dict iteration order.
        units.sort(key=lambda u: (-u.score, u.source, u.key))
        return units[:top_k]

    # --------------------------------------------------------------- doc side
    def _doc_units(
        self, query: str, *, top_k: int, doc_ids: Optional[Sequence[str]]
    ) -> List[EvidenceUnit]:
        units: List[EvidenceUnit] = []
        wanted = list(doc_ids) if doc_ids else self.documents.ids()
        for doc_id in wanted:
            doc = self.documents.get(doc_id)
            if doc is None:
                continue  # unknown/expired doc: skip, never error
            for idx, sim in doc.index.search_scored(query, top_k=top_k):
                chunk = doc.chunks[idx]
                text = chunk[: self.max_excerpt_chars]
                title = f"doc {doc_id} • chunk {idx + 1}/{doc.chunk_count}"
                units.append(
                    EvidenceUnit(
                        source="doc",
                        key=f"{doc_id}#{idx}",
                        title=title,
                        text=text,
                        score=round(float(sim), 4),
                        tokens=estimate_tokens(title) + estimate_tokens(text),
                    )
                )
        return units

    # -------------------------------------------------------------- repo side
    def _repo_units(self, query: str, *, top_k: int) -> List[EvidenceUnit]:
        assert self.graph is not None
        graph = self.graph
        scored: List[EvidenceUnit] = []
        for symbol, path in graph.definitions.items():
            score = repo_match_score(query, symbol, path)
            if score <= 0.0:
                continue
            refs = graph.references.get(symbol, [])[: self.max_referrers]
            tests = graph.tests.get(path, [])[: self.max_referrers]
            bits = [f"defined in {path}"]
            if refs:
                bits.append("used by " + ", ".join(refs))
            if tests:
                bits.append("tested by " + ", ".join(tests))
            title = f"symbol {symbol}"
            text = f"{path}::{symbol} — " + "; ".join(bits)
            scored.append(
                EvidenceUnit(
                    source="repo",
                    key=f"{symbol}@{path}",
                    title=title,
                    text=text,
                    score=round(score, 4),
                    tokens=estimate_tokens(title) + estimate_tokens(text),
                )
            )
        # Deterministic tie-break before the global sort: alpha by key.
        scored.sort(key=lambda u: (-u.score, u.key))
        return scored[: max(top_k * 4, top_k)]  # keep depth for cross-store merge
