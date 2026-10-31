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
        self.semantic_cache_hits = 0  # fuzzy answer reuse (M4), separate from digest hits
        self.local_solves = 0
        self.llm_calls = 0
        # Context Governor (M4): requests where context was shed to fit the
        # model's window, tokens withheld (our estimate, never in tokens_saved),
        # and requests whose *required* context alone overflowed the window.
        self.governor_sheds = 0
        self.governor_shed_tokens = 0
        self.governor_overflows = 0
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
        self.doc_low_coverage = 0        # doc Q&As whose question outran the document (M5)
        self.evidence_searches = 0       # /v1/evidence + MCP evidence calls (M5)
        self.evidence_units = 0          # evidence units returned across those searches
        # M6 hardening: inbound auth posture, secret hygiene, quality gate.
        self.auth_failures = 0           # requests rejected by inbound auth
        self.secrets_detected = 0        # credential-shaped spans seen in payloads
        self.secrets_redacted = 0        # ... of which replaced before leaving the box
        self.doc_full_fallbacks = 0      # low-coverage asks sent the full doc (quality gate)
        self.router_requests = 0         # requests tier-classified (--task-router)
        self.tiers: Dict[str, int] = {}  # per-tier counts (tier_0/tier_1/tier_2/unknown)

    def record(self, route: str, original_tokens: int, optimized_tokens: int = 0) -> None:
        with self._lock:
            self.requests += 1
            if route == "cache":
                self.cache_hits += 1
            elif route == "semantic":
                # Fuzzy answer reuse. Booked separately from the digest cache
                # so the exact-match hit rate stays honest.
                self.semantic_cache_hits += 1
            elif route == "local":
                self.local_solves += 1
            else:
                self.llm_calls += 1
            self.original_tokens += max(0, int(original_tokens))
            self.optimized_tokens += max(0, int(optimized_tokens))

    def record_governor(self, *, shed_tokens: int, overflow: bool) -> None:
        """One request's Context Governor outcome (M4).

        ``shed_tokens`` is our own estimate of context withheld from the
        provider to fit the model's window; it is *not* provider-verified, so
        it never enters ``tokens_saved`` — that claim stays on measured
        compaction savings only.
        """
        with self._lock:
            self.governor_sheds += 1
            self.governor_shed_tokens += max(0, int(shed_tokens))
            if overflow:
                self.governor_overflows += 1

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

    def record_doc(
        self,
        *,
        full_tokens: int,
        sent_tokens: int,
        chunks_used: int = 0,
        low_coverage: bool = False,
        full_fallback: bool = False,
    ) -> None:
        """Book one document Q&A request against its naive full-resend baseline.

        Kept out of ``tokens_saved`` on purpose: compaction savings and
        retrieval avoidance have different baselines and mixing them would
        make both numbers harder to defend. ``low_coverage`` (M5) counts the
        asks whose question outran the document — a quality signal, not a
        saving. ``full_fallback`` (M6) counts the asks that spent the naive
        full-document cost on purpose because retrieval was too weak
        (``--doc-fallback-full``) — a deliberate quality-over-spend choice.
        """
        with self._lock:
            self.doc_questions += 1
            self.doc_full_tokens += max(0, int(full_tokens))
            self.doc_sent_tokens += max(0, int(sent_tokens))
            self.doc_chunks_used += max(0, int(chunks_used))
            if low_coverage:
                self.doc_low_coverage += 1
            if full_fallback:
                self.doc_full_fallbacks += 1

    def record_evidence(self, units: int) -> None:
        """One evidence search was served, returning ``units`` ranked units."""
        with self._lock:
            self.evidence_searches += 1
            self.evidence_units += max(0, int(units))

    # ------------------------------------------------------- M6 hardening
    def record_auth_failure(self) -> None:
        """A request was rejected by inbound authentication (401)."""
        with self._lock:
            self.auth_failures += 1

    def record_secrets(self, *, detected: int, redacted: int = 0) -> None:
        """Credential-shaped spans seen (and, if redacting, replaced)."""
        with self._lock:
            self.secrets_detected += max(0, int(detected))
            self.secrets_redacted += max(0, int(redacted))

    def record_tier(self, tier: str) -> None:
        """One request was classified by the Adaptive Task Router."""
        with self._lock:
            self.router_requests += 1
            key = str(tier)
            self.tiers[key] = self.tiers.get(key, 0) + 1

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
                "semantic_cache_hits": self.semantic_cache_hits,
                "local_solves": self.local_solves,
                "llm_calls": self.llm_calls,
                "governor_sheds": self.governor_sheds,
                "governor_shed_tokens": self.governor_shed_tokens,
                "governor_overflows": self.governor_overflows,
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
                "doc_low_coverage": self.doc_low_coverage,
                "evidence_searches": self.evidence_searches,
                "evidence_units": self.evidence_units,
                "auth_failures": self.auth_failures,
                "secrets_detected": self.secrets_detected,
                "secrets_redacted": self.secrets_redacted,
                "doc_full_fallbacks": self.doc_full_fallbacks,
                "router_requests": self.router_requests,
            }

    def reset(self) -> None:
        with self._lock:
            self.requests = 0
            self.cache_hits = 0
            self.semantic_cache_hits = 0
            self.local_solves = 0
            self.llm_calls = 0
            self.governor_sheds = 0
            self.governor_shed_tokens = 0
            self.governor_overflows = 0
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
            self.doc_low_coverage = 0
            self.evidence_searches = 0
            self.evidence_units = 0
            self.auth_failures = 0
            self.secrets_detected = 0
            self.secrets_redacted = 0
            self.doc_full_fallbacks = 0
            self.router_requests = 0
            self.tiers = {}
