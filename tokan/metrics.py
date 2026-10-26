"""Session-level ROI metrics: tokens saved, requests routed, dollars saved."""
from __future__ import annotations

from typing import Dict

__all__ = ["SessionMetrics"]


class SessionMetrics:
    """Accumulates savings across an agent session.

    ``price_per_million_tokens`` is the blended price you would have paid
    upstream; set it to your provider/model to make ``report()`` meaningful.
    """

    def __init__(self, price_per_million_tokens: float = 2.50) -> None:
        self.price_per_million_tokens = price_per_million_tokens
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

    def record(self, route: str, original_tokens: int, optimized_tokens: int = 0) -> None:
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
        self.upstream_cached_tokens += max(0, int(tokens))

    def record_prefix_reuse(self, tokens: int) -> None:
        """Tokens that repeated the previous turn's prefix (provider discounts).

        This is the base the provider's prompt cache applies to. Tokan keeps the
        prefix byte-stable so the discount actually fires, but the discount
        itself is the provider's -- tracked here to show the economics, never
        counted in ``tokens_saved``.
        """
        self.prefix_reused_tokens += max(0, int(tokens))

    def record_provider_prompt(self, prompt_tokens: int, cached_tokens: int) -> None:
        """Book one LLM call's upstream prompt billing (full + cached split)."""
        self.provider_prompt_tokens += max(0, int(prompt_tokens))
        self.record_upstream_cached(cached_tokens)

    @property
    def provider_cache_ratio(self) -> float:
        """Fraction of upstream prompt tokens the provider served from cache."""
        if not self.provider_prompt_tokens:
            return 0.0
        return round(self.upstream_cached_tokens / self.provider_prompt_tokens, 4)

    def record_fallback(self) -> None:
        self.fallbacks += 1

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
        }

    def reset(self) -> None:
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
