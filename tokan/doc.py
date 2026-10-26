"""Doc Governor: ingest a long document once, answer many questions from it.

The waste this removes
----------------------
Document Q&A in its naive form resends the **entire document on every
question**::

    turn 1: [9,000-token contract] + "termination clause?"   -> 9,000 tok
    turn 2: [9,000-token contract] + "governing law?"        -> 9,000 tok  (resent!)
    turn 3: [9,000-token contract] + "payment terms?"        -> 9,000 tok  (resent!)

The Doc Governor turns that into an **ingest-once / ask-many** flow:

    POST /v1/ingest   {"text": "<contract>"}        -> doc_id
    POST /v1/doc/ask  {"doc_id": ..., "question":..} -> [summary + top-k chunks + Q]

Only the parts of the document that matter for *this* question travel to the
model — typically an order of magnitude fewer tokens — while an extractive
summary keeps the global context in the prompt.

Honesty contract
----------------
* ``full_tokens`` (the "naive baseline") is what resending the whole document
  plus the question would cost; ``sent_tokens`` is what Tokan actually sent.
  The difference is reported as **doc tokens avoided** — Tokan's own doing,
  tracked separately from compaction savings and from provider cache discounts.
* Retrieval is **lexically** motivated (TF-IDF). Benchmarks report **recall@k**
  — how often the answer-bearing chunk is actually in the retrieved set — so
  quality claims stay measurable instead of asserted.

Stdlib only: no numpy, no BM25 dependency, no embeddings.
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from collections import Counter, OrderedDict
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .cdm import estimate_tokens

__all__ = [
    "assemble_messages",
    "chunk_text",
    "Document",
    "DocumentStore",
    "full_resend_tokens",
    "summarize",
    "TfidfIndex",
]

# Sentence-ish boundaries: end-of-sentence punctuation or blank lines.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n{2,}")
# Lowercase alphanumeric terms — cheap, language-agnostic, deterministic.
_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Gateway defaults (override per DocumentStore via constructor kwargs).
DEFAULT_CHUNK_CHARS = 1200
DEFAULT_CHUNK_OVERLAP = 150
DEFAULT_SUMMARY_SENTENCES = 6
DEFAULT_TOP_K = 3


def _terms(text: str) -> List[str]:
    return _TOKEN_RE.findall(text.lower())


# -------------------------------------------------------------------- chunking
def chunk_text(
    text: str,
    *,
    max_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> List[str]:
    """Split ``text`` into overlapping, sentence/paragraph-aware chunks.

    Boundaries prefer sentence and blank-line breaks so excerpts read as
    prose, not mid-word cuts. A single oversized blob (minified JSON, base64)
    is hard-windowed with ``overlap`` so retrieval can still reach its middle.
    ``overlap`` also carries the tail of each chunk into the next, which keeps
    facts straddling a boundary retrievable.
    """
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    pieces = [p.strip() for p in _SENT_SPLIT.split(text) if p and p.strip()]
    chunks: List[str] = []
    current = ""
    for piece in pieces:
        if len(piece) > max_chars:
            # Oversized piece: flush, then hard-window it.
            if current:
                chunks.append(current)
                current = ""
            start = 0
            while start < len(piece):
                end = start + max_chars
                chunks.append(piece[start:end])
                if end >= len(piece):
                    break
                start = end - overlap if overlap and overlap < max_chars else end
            continue
        if current and len(current) + 1 + len(piece) > max_chars:
            chunks.append(current)
            # Carry the tail into the next chunk (boundary robustness for
            # retrieval), clipped so the result still respects max_chars.
            keep = max_chars - 1 - len(piece)
            if overlap and keep > 0:
                current = current[-min(overlap, keep):] + " " + piece
            else:
                current = piece
        else:
            current = f"{current} {piece}" if current else piece
    if current:
        chunks.append(current)
    return [c for c in chunks if c]


# ---------------------------------------------------------------------- index
class TfidfIndex:
    """Unit-normalized TF-IDF vectors over chunks; cosine via dot product.

    Deterministic and dependency-free. Query terms unseen in the corpus are
    dropped (they cannot match any chunk vector), so ranking degrades gracefully
    to zero rather than exploding.
    """

    def __init__(self, chunks: Sequence[str]) -> None:
        self.chunks: List[str] = list(chunks)
        self.idf: Dict[str, float] = {}
        self.vectors: List[Dict[str, float]] = []
        self._build()

    def _build(self) -> None:
        if not self.chunks:
            return
        n = len(self.chunks)
        doc_freq: Counter = Counter()
        term_freqs: List[Counter] = []
        for chunk in self.chunks:
            tf = Counter(_terms(chunk))
            term_freqs.append(tf)
            doc_freq.update(tf.keys())
        # Smoothed idf: frequent-but-not-universal terms still weigh, rare ones
        # weigh most. +1 keeps it strictly positive.
        self.idf = {t: math.log((1 + n) / (1 + d)) + 1.0 for t, d in doc_freq.items()}
        for tf in term_freqs:
            vec = {t: (1.0 + math.log(f)) * self.idf[t] for t, f in tf.items()}
            norm = math.sqrt(sum(w * w for w in vec.values())) or 1.0
            self.vectors.append({t: w / norm for t, w in vec.items()})

    def search(self, query: str, top_k: int = DEFAULT_TOP_K) -> List[int]:
        """Chunk indices ranked by cosine similarity (ties broken by position).

        Only strictly-positive matches are returned — if the query shares no
        terms with the corpus there is no evidence of relevance, and inventing
        one would be dishonest.
        """
        if not self.vectors:
            return []
        qtf = Counter(_terms(query))
        if not qtf:
            return []
        default_idf = math.log(1 + len(self.chunks)) + 1.0
        qvec = {
            t: (1.0 + math.log(f)) * self.idf[t]
            for t, f in qtf.items()
            if t in self.idf
        }
        if not qvec:
            return []
        norm = math.sqrt(sum(w * w for w in qvec.values())) or 1.0
        scored = []
        for i, vec in enumerate(self.vectors):
            # Chunk vectors are already unit-normalized.
            sim = sum(w * vec.get(t, 0.0) for t, w in qvec.items()) / norm
            if sim > 0.0:
                scored.append((sim, i))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [i for _, i in scored[: max(1, int(top_k))]]


# --------------------------------------------------------------------- summary
def summarize(
    chunks: Sequence[str],
    index: TfidfIndex,
    *,
    max_sentences: int = DEFAULT_SUMMARY_SENTENCES,
    min_chars: int = 25,
) -> str:
    """Extractive summary: the most salient sentences, in document order.

    Salience = average IDF of the sentence's distinct terms (rare, specific
    words beat "the of and"). Selecting in two steps — rank, then restore
    document order — keeps the summary deterministic and readable, and avoids
    the model-call cost (and non-determinism) of an abstractive summary.
    """
    if not chunks:
        return ""
    sentences: List[Tuple[Tuple[int, int], str]] = []
    for ci, chunk in enumerate(chunks):
        for si, raw in enumerate(_SENT_SPLIT.split(chunk)):
            sentence = " ".join(raw.split())
            if len(sentence) >= min_chars:
                sentences.append(((ci, si), sentence))
    if not sentences:
        # Degenerate text (no sentence structure): fall back to a lead excerpt.
        return chunks[0][:500]

    def salience(item: Tuple[Tuple[int, int], str]) -> float:
        terms = set(_terms(item[1]))
        if not terms:
            return 0.0
        return sum(index.idf.get(t, 0.0) for t in terms) / math.sqrt(len(terms))

    # rank first (stable sort keeps source order on ties), then restore order.
    ranked = sorted(sentences, key=salience, reverse=True)[: max(1, max_sentences)]
    ranked.sort(key=lambda item: item[0])
    return " ".join(sentence for _, sentence in ranked)


# -------------------------------------------------------------------- document
class Document:
    """An ingested document: chunks + TF-IDF index + extractive summary."""

    def __init__(
        self,
        doc_id: str,
        text: str,
        *,
        metadata: Optional[Mapping[str, Any]] = None,
        max_chars: int = DEFAULT_CHUNK_CHARS,
        overlap: int = DEFAULT_CHUNK_OVERLAP,
        summary_sentences: int = DEFAULT_SUMMARY_SENTENCES,
    ) -> None:
        self.doc_id = doc_id
        self.text = text
        self.metadata: Dict[str, Any] = dict(metadata or {})
        self.chunks = chunk_text(text, max_chars=max_chars, overlap=overlap)
        self.index = TfidfIndex(self.chunks)
        self.summary = summarize(self.chunks, self.index, max_sentences=summary_sentences)
        self.total_tokens = estimate_tokens(text)
        self.summary_tokens = estimate_tokens(self.summary)
        self.created = time.time()

    def retrieve(self, question: str, top_k: int = DEFAULT_TOP_K) -> Tuple[List[str], List[int]]:
        """Top-k chunks for ``question``; falls back to the lead chunks when
        the query shares no vocabulary with the document (an executive-summary
        bias rather than an empty prompt)."""
        idxs = self.index.search(question, top_k=top_k)
        if not idxs:
            idxs = list(range(min(max(1, top_k), len(self.chunks))))
        return [self.chunks[i] for i in idxs], idxs

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)


def full_resend_tokens(doc: Document, question: str) -> int:
    """Tokens a naive client spends per question: whole document + question."""
    return doc.total_tokens + estimate_tokens(question)


def assemble_messages(
    doc: Document,
    question: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    max_excerpt_chars: int = 1500,
) -> Tuple[List[Dict[str, str]], List[int], List[str]]:
    """Build ``[summary + top-k excerpts + question]`` as OpenAI-wire messages.

    Returns ``(messages, retrieved_indices, retrieved_chunks)``. Each excerpt
    is labeled with its chunk number so the model can cite it, and excerpts
    are truncated to ``max_excerpt_chars`` to bound worst-case prompt size.
    """
    chunks, idxs = doc.retrieve(question, top_k=top_k)
    excerpts = "\n\n".join(
        f"[{pos}] (chunk {idx + 1}) {chunk[:max_excerpt_chars]}"
        for pos, (chunk, idx) in enumerate(zip(chunks, idxs), start=1)
    )
    system = (
        "Answer the user's question using ONLY the document excerpts below. "
        "Quote the relevant text when it helps. If the answer is not present "
        "in the excerpts, say you cannot find it in the provided document."
    )
    user = (
        f"Document summary:\n{doc.summary}\n\n"
        f"Excerpts:\n{excerpts}\n\n"
        f"Question: {question}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], idxs, chunks


# ---------------------------------------------------------------------- store
class DocumentStore:
    """Bounded, thread-safe LRU of ingested documents.

    doc_id defaults to a content hash, so re-ingesting identical text is
    idempotent (same id, refreshed entry) and a client may always pass its own
    ``doc_id`` to override.
    """

    def __init__(self, max_docs: int = 64, **doc_kwargs: Any) -> None:
        self.max_docs = max(1, int(max_docs))
        self._docs: "OrderedDict[str, Document]" = OrderedDict()
        self._lock = threading.Lock()
        self._doc_kwargs = doc_kwargs

    def ingest(
        self,
        text: str,
        *,
        metadata: Optional[Mapping[str, Any]] = None,
        doc_id: Optional[str] = None,
    ) -> Document:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("document text must be a non-empty string")
        resolved = (doc_id or "").strip() or (
            "d_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        )
        doc = Document(resolved, text, metadata=metadata, **self._doc_kwargs)
        with self._lock:
            self._docs.pop(resolved, None)  # re-ingest refreshes position
            self._docs[resolved] = doc
            self._docs.move_to_end(resolved)
            while len(self._docs) > self.max_docs:
                self._docs.popitem(last=False)
        return doc

    def get(self, doc_id: str) -> Optional[Document]:
        with self._lock:
            doc = self._docs.get(doc_id)
            if doc is not None:
                self._docs.move_to_end(doc_id)
            return doc

    def __len__(self) -> int:
        with self._lock:
            return len(self._docs)

    def clear(self) -> None:
        with self._lock:
            self._docs.clear()
