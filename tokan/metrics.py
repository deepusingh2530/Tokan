"""Session-level ROI metrics: tokens saved, requests routed, dollars saved."""
from __future__ import annotations

import threading
from typing import Dict

__all__ = ["SessionMetrics"]


class SessionMetrics:
    """Accumulates savings across an agent session.

    ``price_per_million_tokens`` is the blended price you would have paid
    upstream; set it to your provider/model to make ``report()`` meaningful.
    """

    def __init__(self, price_per_million_tokens: float = 2.50) -> None:
        self.price_per_million_tokens = price_per_million_tokens
        # The gateway is a ThreadingHTTPServer: every handler thread books
        # metrics concurrently. RLock (record_provider_prompt nests into
        # record_upstream_cached). Properties (tokens_saved, ratios) read the
        # counters without the lock — best-effort snapshots by design.
        self._lock = threading.RLock()
        self.requests = 0
        self.cache_hits = 0
        self.local_solves = 0
        self.llm_calls = 0
        self.original_tokens = 0
        self.optimized_tokens = 0
        self.upstream_cached_tokens = 0  # provider prompt-cache hits (surfaced, not ours)
        self.fallbacks = 0               # model_not_supported retries via --fallback-model
        # Session Governor: prefix stability + provider prompt-cache economics.
        self.prefix_reused_tokens = 0    # tokens that repeated the previous prefix
        self.provider_prompt_tokens = 0  # total prompt tokens billed upstream (llm calls)
        # Doc Governor: ingest-once / ask-many document Q&A economics.
        self.doc_ingests = 0             # documents ingested
        self.doc_ingested_tokens = 0     # tokens stored across those documents
        self.doc_questions = 0           # /v1/doc/ask requests served
        self.doc_full_tokens = 0         # naive baseline: full doc + question each time
        self.doc_sent_tokens = 0         # what Tokan actually forwarded
        self.doc_chunks_used = 0         # retrieved excerpts sent (quality signal)

    def record(self, route: str, original_tokens: int, optimized_tokens: int = 0) -> None:
        with self._lock:
            self.requests += 1
            if route == "cache":
                self.cache_hits += 1
            elif route == "local":
                self.local_solves += 1
            else:
                self.llm_calls += 1
            self.original_tokens += max(0, int(original_tokens))
            self.optimized_tokens += max(0, int(optimized_tokens))

    def record_upstream_cached(self, tokens: int) -> None:
        """Tokens the *provider's* prompt cache answered (billed at a discount).

        Surfaced for visibility only -- Tokan did not save these, the upstream
        did; counting them as Tokan savings would be dishonest.
        """
        with self._lock:
            self.upstream_cached_tokens += max(0, int(tokens))

    def record_prefix_reuse(self, tokens: int) -> None:
        """Tokens that repeated the previous turn's prefix (provider discounts).

        This is the base the provider's prompt cache applies to. Tokan keeps the
        prefix byte-stable so the discount actually fires, but the discount
        itself is the provider's -- tracked here to show the economics, never
        counted in ``tokens_saved``.
        """
        with self._lock:
            self.prefix_reused_tokens += max(0, int(tokens))

    def record_provider_prompt(self, prompt_tokens: int, cached_tokens: int) -> None:
        """Book one LLM call's upstream prompt billing (full + cached split)."""
        with self._lock:
            self.provider_prompt_tokens += max(0, int(prompt_tokens))
            self.record_upstream_cached(cached_tokens)

    @property
    def provider_cache_ratio(self) -> float:
        """Fraction of upstream prompt tokens the provider served from cache."""
        if not self.provider_prompt_tokens:
            return 0.0
        return round(self.upstream_cached_tokens / self.provider_prompt_tokens, 4)

    def record_fallback(self) -> None:
        with self._lock:
            self.fallbacks += 1

    # ---------------------------------------------------------- Doc Governor
    def record_ingest(self, chunks: int, tokens: int) -> None:
        """A document was ingested (stored once, reused by every question)."""
        with self._lock:
            self.doc_ingests += 1
            self.doc_ingested_tokens += max(0, int(tokens))

    def record_doc(self, *, full_tokens: int, sent_tokens: int, chunks_used: int = 0) -> None:
        """Book one document Q&A request against its naive full-resend baseline.

        Kept out of ``tokens_saved`` on purpose: compaction savings and
        retrieval avoidance have different baselines and mixing them would
        make both numbers harder to defend.
        """
        with self._lock:
            self.doc_questions += 1
            self.doc_full_tokens += max(0, int(full_tokens))
            self.doc_sent_tokens += max(0, int(sent_tokens))
            self.doc_chunks_used += max(0, int(chunks_used))

    @property
    def doc_tokens_avoided(self) -> int:
        """Tokens never sent because retrieval replaced the full-document resend."""
        return max(0, self.doc_full_tokens - self.doc_sent_tokens)

    @property
    def doc_avoided_pct(self) -> float:
        if not self.doc_full_tokens:
            return 0.0
        return round(100.0 * self.doc_tokens_avoided / self.doc_full_tokens, 2)

    @property
    def tokens_saved(self) -> int:
        return max(0, self.original_tokens - self.optimized_tokens)

    @property
    def saved_pct(self) -> float:
        if not self.original_tokens:
            return 0.0
        return round(100.0 * self.tokens_saved / self.original_tokens, 2)

    @property
    def cost_saved_usd(self) -> float:
        return self.tokens_saved / 1_000_000 * self.price_per_million_tokens

    def report(self) -> str:
        extra = ""
        if self.upstream_cached_tokens:
            extra += (
                f" | provider prompt-cache: {self.upstream_cached_tokens} tok"
                f" ({self.provider_cache_ratio:.0%} of prompt)"
            )
        if self.prefix_reused_tokens:
            extra += f" | prefix reuse: {self.prefix_reused_tokens} tok"
        if self.doc_questions:
            extra += (
                f" | doc Q&A: {self.doc_questions}q avoided {self.doc_tokens_avoided} tok"
                f" ({self.doc_avoided_pct:.0f}% vs full-doc resend)"
            )
        if self.fallbacks:
            extra += f" | model fallbacks: {self.fallbacks}"
        return (
            f"Session Token Savings: {self.saved_pct:.0f}% | "
            f"Cost Saved: ${self.cost_saved_usd:.2f} | "
            f"Requests: {self.requests} "
            f"(cache {self.cache_hits}, local {self.local_solves}, llm {self.llm_calls})"
            f"{extra}"
        )

    def as_dict(self) -> Dict[str, float]:
        with self._lock:  # consistent snapshot across all counters
            return {
                "requests": self.requests,
                "cache_hits": self.cache_hits,
                "local_solves": self.local_solves,
                "llm_calls": self.llm_calls,
                "original_tokens": self.original_tokens,
                "optimized_tokens": self.optimized_tokens,
                "tokens_saved": self.tokens_saved,
                "saved_pct": self.saved_pct,
                "cost_saved_usd": round(self.cost_saved_usd, 4),
                "price_per_million_tokens": self.price_per_million_tokens,
                "upstream_cached_tokens": self.upstream_cached_tokens,
                "fallbacks": self.fallbacks,
                "prefix_reused_tokens": self.prefix_reused_tokens,
                "provider_prompt_tokens": self.provider_prompt_tokens,
                "provider_cache_ratio": self.provider_cache_ratio,
                "doc_ingests": self.doc_ingests,
                "doc_ingested_tokens": self.doc_ingested_tokens,
                "doc_questions": self.doc_questions,
                "doc_full_tokens": self.doc_full_tokens,
                "doc_sent_tokens": self.doc_sent_tokens,
                "doc_chunks_used": self.doc_chunks_used,
                "doc_tokens_avoided": self.doc_tokens_avoided,
                "doc_avoided_pct": self.doc_avoided_pct,
            }

    def reset(self) -> None:
        with self._lock:
            self.requests = 0
            self.cache_hits = 0
            self.local_solves = 0
            self.llm_calls = 0
            self.original_tokens = 0
            self.optimized_tokens = 0
            self.upstream_cached_tokens = 0
            self.fallbacks = 0
            self.prefix_reused_tokens = 0
            self.provider_prompt_tokens = 0
            self.doc_ingests = 0
            self.doc_ingested_tokens = 0
            self.doc_questions = 0
            self.doc_full_tokens = 0
            self.doc_sent_tokens = 0
            self.doc_chunks_used = 0
