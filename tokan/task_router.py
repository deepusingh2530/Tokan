"""Adaptive Task Router (Tier 0/1/2) extending JeV.

Classifies queries into three tiers:

- Tier 0 (Local): file lookups, counts, simple AST extracts → LocalExecutor
- Tier 1 (Low-cost LLM): formatting, JSON parsing, single-line errors, small doc lookups → gpt-4o-mini / claude-3-5-haiku
- Tier 2 (High Complexity): Architectural refactoring, full code generation, multi-file synthesis → gpt-4o / claude-3-5-sonnet

The router can also rewrite the payload's model field for Tier 1/2 routing.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Union

from .jev import JevDecision, JevRouter

__all__ = [
    "AdaptiveTaskRouter",
    "TIER_LOCAL",
    "TIER_LOW_COST",
    "TIER_FRONTIER",
    "Tier",
    "TIERS",
]

#: Tier 0 — deterministic/local: file lookups, counts, simple AST extracts.
TIER_LOCAL = "tier_0"
#: Tier 1 — low complexity: formatting, JSON parsing, single-line errors, small docs.
TIER_LOW_COST = "tier_1"
#: Tier 2 — high complexity: architectural refactoring / code generation.
TIER_FRONTIER = "tier_2"

Tier = str
TIERS = (TIER_LOCAL, TIER_LOW_COST, TIER_FRONTIER)


class AdaptiveTaskRouter:
    """Routes queries to the appropriate tier and target model.

    The default routing heuristic uses keyword signals.  Operators can pass
    custom keyword lists (e.g. a curated stopword list) via constructor args;
    defaults keep the shipped behaviour.
    """

    def __init__(
        self,
        confidence_threshold: float = 0.88,
        low_cost_model: str = "gpt-4o-mini",
        frontier_model: str = "gpt-4o",
        cheap_alternate: str = "claude-3-5-haiku",
    ) -> None:
        self.confidence_threshold = confidence_threshold
        self.low_cost_model = low_cost_model
        self.frontier_model = frontier_model
        self.cheap_alternate = cheap_alternate
        self.router = JevRouter(confidence_threshold=confidence_threshold)

    # -------------------------------------------------------------- classification

    @staticmethod
    def _query_text(messages: List[dict]) -> str:
        parts: List[str] = []
        for msg in messages or []:
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.append(
                    "\n".join(
                        str(b.get("text", "")) if isinstance(b, dict) else str(b)
                        for b in content
                    )
                )
        return "\n".join(parts)

    def classify(self, messages: List[dict], *, payload: Optional[dict] = None) -> str:
        """Return the tier: ``"tier_0"``, ``"tier_1"``, ``"tier_2"``, or ``"unknown"``."""
        query = self._query_text(messages)
        blob = query if not payload else query + " " + self._query_text(payload.get("messages") or [])

        # ---- Tier 2 (high complexity): architecture, generation, synthesis.
        tier2_patterns = [
            r"refactor\w*", r"redesign\w*", r"restructur\w*", r"rewrite\w*",
            r"architect\w*", r"implement\w*", r"generat\w*", r"multi[-_]?file\w*",
            r"synthes\w*", r"migrat\w*", r"redesign", r"build\w*", r"create\w*", r"develop\w*",
        ]
        if any(re.search(p, blob, re.I) for p in tier2_patterns):
            return TIER_FRONTIER

        # ---- Tier 1 (low complexity): formatting, JSON parse, single-line
        #      error fix, small doc lookups, quick tweaks.
        tier1_patterns = [
            r"format", r"pretty[\s-]?print", r"parse json", r"json valid", r"indent",
            r"fix error", r"single line", r"one line", r"small doc", r"quick fix",
            r"minor change", r"simple fix", r"small change", r"simplify", r"cleanup",
            r"validate schema", r"json parsing",
        ]
        if any(re.search(p, blob, re.I) for p in tier1_patterns):
            return TIER_LOW_COST

        # ---- Tier 0 (deterministic/local): counts, exact lookups, simple
        #      AST file/line extracts.
        tier0_patterns = [
            r"how many", r"number of", r"count", r"total lines", r"exact line",
            r"what line", r"which line", r"occurrence", r"line number", r"current value",
            r"locate", r"search", r"find", r"find the", r"list all", r"look up",
            r"extract", r"parse", r"read the", r"show the", r"get the",
        ]
        if any(re.search(p, blob, re.I) for p in tier0_patterns):
            return TIER_LOCAL

        # ---- Heuristic tiers from payload shape (no LLM needed to look).
        if payload:
            has_code = bool(re.search(r"\b(def|class|function|func|import|struct|interface)\b", blob, re.I))
            long_blob = len(blob) > 4000
            multi_file = bool(re.search(r"\.py\b.*\.py\b|\.ts\b.*\.ts\b|\.go\b.*\.go\b", blob, re.I))
            if has_code and long_blob:
                return TIER_FRONTIER

        return "unknown"

    # -------------------------------------------------------------- model rewrites

    def tier_model(self, tier: Union[str, None]) -> str:
        """Cheapest acceptable model id for a tier (``"local"`` for Tier 0)."""
        if tier == TIER_LOCAL:
            return "local"
        if tier == TIER_LOW_COST:
            return self.low_cost_model
        return self.frontier_model

    def rewrite_payload(self, payload: dict, tier: str) -> dict:
        """Rewrite the payload's model field for the chosen tier."""
        out = dict(payload or {})
        if tier == TIER_LOCAL:
            out["model"] = "local"
        elif tier == TIER_LOW_COST:
            out["model"] = self.low_cost_model
        elif tier == TIER_FRONTIER:
            out["model"] = self.frontier_model
        out["optimized_tier"] = tier
        return out

    # -------------------------------------------------------------- routing

    def route(self, messages: List[dict], *, payload: Optional[dict] = None) -> Dict[str, Any]:
        """
        Classify the tier, rewrite the model when needed, and let the JeV
        engine make the final routing decision.
        """
        tier = self.classify(messages, payload=payload)
        payload_out = self.rewrite_payload(payload, tier)

        decision = self.router.evaluate(messages, deterministic_answer=None)

        # Honour a local decision coming back from JeV (deterministic solve).
        effective_tier = TIER_LOCAL if decision.route == "local" else tier

        return {
            "tier": effective_tier,
            "route": decision.route,
            "confidence": decision.confidence,
            "signals": decision.signals,
            "estimated_llm_tokens": decision.estimated_llm_tokens,
            "model": payload_out.get("model", self.tier_model(effective_tier)),
            "payload": payload_out,
        }

    # -------------------------------------------------------------- convenience

    def get_tier(self, messages: List[dict], *, payload: Optional[dict] = None) -> str:
        """Just return the tier without rewriting the payload."""
        return self.classify(messages, payload=payload)
