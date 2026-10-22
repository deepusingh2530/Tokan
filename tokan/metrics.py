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
        return (
            f"Session Token Savings: {self.saved_pct:.0f}% | "
            f"Cost Saved: ${self.cost_saved_usd:.2f} | "
            f"Requests: {self.requests} "
            f"(cache {self.cache_hits}, local {self.local_solves}, llm {self.llm_calls})"
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
        }

    def reset(self) -> None:
        self.requests = 0
        self.cache_hits = 0
        self.local_solves = 0
        self.llm_calls = 0
        self.original_tokens = 0
        self.optimized_tokens = 0
