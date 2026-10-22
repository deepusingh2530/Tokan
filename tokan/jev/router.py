"""JeV: System-1 judgment engine that decides whether an LLM is needed at all.

The router scores a request cheaply (regex features + payload shape, no model
calls). If confidence >= threshold the request is *served locally* or flagged
as cache-fresh; otherwise it falls through to the upstream LLM with the
compacted CDM payload only.

Scoring is intentionally transparent: every decision carries the features that
drove it so the dashboard / audit log can explain itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

__all__ = ["JevDecision", "JevRouter"]


@dataclass(frozen=True)
class JevDecision:
    """Outcome of a System-1 evaluation.

    ``route``:
      * ``"local"``  - answer deterministically, no LLM tokens spent
      * ``"llm"``    - forward (compacted) payload upstream
      * ``"cache"``  - semantic cache already has this answer (set by pipeline)
    """

    route: str
    confidence: float
    signals: tuple = ()
    estimated_llm_tokens: int = 0

    @property
    def needs_llm(self) -> bool:
        return self.route == "llm"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "route": self.route,
            "confidence": round(self.confidence, 4),
            "signals": list(self.signals),
            "estimated_llm_tokens": self.estimated_llm_tokens,
        }


# Features that suggest the answer is a deterministic lookup.
_LOCAL_PATTERNS = [
    ("exact_lookup", re.compile(r"\bwhat(?:'s| is)\s+the\s+(exact|literal|current)\b", re.I)),
    ("regex_count", re.compile(r"\bhow many\b.*\b(occurrences|matches|lines|files)\b", re.I)),
    ("line_number", re.compile(r"\b(line|offset|byte)\s*(number|no\.?|#)\b", re.I)),
    ("math", re.compile(r"\b\d+\s*[\+\-\*/x]\s*\d+\b")),
    ("yes_no", re.compile(r"^\s*(is|are|does|do|can|should)\b.{0,80}\?\s*$", re.I)),
    ("enum_list", re.compile(r"\blist\s+(?:all|the)\s+\w+\b", re.I)),
    ("translate_ident", re.compile(r"\b(?:rename|convert|snake_case|camelCase|kebab)\b", re.I)),
]

# Features that suggest real reasoning / generation work.
_LLM_PATTERNS = [
    ("refactor", re.compile(r"\b(refactor|redesign|restructure|rewrite)\b", re.I)),
    ("explain_reasoning", re.compile(r"\b(why|explain|walk me through|trade-?offs?)\b", re.I)),
    ("debug", re.compile(r"\b(debug|root cause|stack ?trace|segfault|race condition)\b", re.I)),
    ("multi_step", re.compile(r"\b(step[- ]by[- ]step|plan|implement|build|add support)\b", re.I)),
    ("subjective", re.compile(r"\b(best|should i|opinion|compare|evaluate)\b", re.I)),
]

_CODE_FENCE_RE = re.compile(r"```")
# Bounded components: the unbounded form `(?:[\w.-]+/)+[\w.-]+\.\w+` backtracks
# exponentially on long slash-free lines (~19s on a 2MB paste of long words),
# which any log dump can contain. Bounding each segment keeps worst-case
# linear. Realistic paths are far below these limits.
_FILE_PATH_RE = re.compile(r"(?:[\w.-]{1,100}/){1,6}[\w.-]{1,100}\.\w{1,5}")

# Feature scans only need a sample of the text, not the whole thing — routing
# must stay cheap no matter how huge the pasted context is. Head+tail carries
# the real signals: file trees and headers sit at the top of payloads, the
# question itself at the bottom.
_FEATURE_BLOB_CAP = 65_536


def _sample(text: str, cap: int = _FEATURE_BLOB_CAP) -> str:
    if len(text) <= cap:
        return text
    half = cap // 2
    return text[:half] + text[-half:]


@dataclass
class JevRouter:
    """Heuristic System-1 router.

    Parameters
    ----------
    confidence_threshold:
        Score >= threshold routes ``local``; below it goes to the LLM.
    system_prompt_tokens:
        Declared weight of the standing system prompt (evidence that the
        context is heavy enough to matter).
    """

    confidence_threshold: float = 0.88
    system_prompt_tokens: int = 0
    _local_weight: float = field(default=1.0, repr=False)
    _llm_weight: float = field(default=1.0, repr=False)

    # ------------------------------------------------------------- features
    @staticmethod
    def _last_user_text(messages: Sequence[Mapping[str, Any]]) -> str:
        for msg in reversed(list(messages)):
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return "\n".join(
                        str(b.get("text", "")) if isinstance(b, dict) else str(b)
                        for b in content
                    )
        return ""

    @staticmethod
    def _blob(messages: Sequence[Mapping[str, Any]]) -> str:
        parts: List[str] = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(
                    str(b.get("text", "")) if isinstance(b, dict) else str(b)
                    for b in content
                )
        return "\n".join(parts)

    # -------------------------------------------------------------- scoring
    def evaluate(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        cache_hit: bool = False,
        deterministic_answer: Optional[str] = None,
    ) -> JevDecision:
        """Score the request and pick a route.

        ``deterministic_answer`` is what a local executor (regex/AST lookup)
        produced, if any. Its presence is the strongest local signal.
        """
        if cache_hit:
            return JevDecision(route="cache", confidence=1.0, signals=("cache_hit",))

        query = self._last_user_text(messages)
        blob = self._blob(messages)

        # Pattern scans run on a bounded sample: one pasted megabyte must not
        # decide how long routing takes.
        scan_query = _sample(query)
        local_hits = [name for name, rx in _LOCAL_PATTERNS if rx.search(scan_query)]
        llm_hits = [name for name, rx in _LLM_PATTERNS if rx.search(scan_query)]

        evidence = 0.0
        total = 0.0

        if deterministic_answer is not None:
            local_hits.append("local_executor")
            evidence += 3.0
        total += 3.0

        # Long payloads with code lean LLM; tiny lookups lean local.
        scan = _sample(blob)
        code_blocks = len(_CODE_FENCE_RE.findall(scan)) // 2
        paths = len(set(_FILE_PATH_RE.findall(scan)))
        query_len = len(query)

        local_score = len(local_hits) * self._local_weight
        llm_score = len(llm_hits) * self._llm_weight
        llm_score += min(code_blocks, 4) * 0.8 + min(paths, 6) * 0.35
        if query_len > 600:
            llm_score += 1.0
        if query_len < 60 and not llm_hits:
            local_score += 1.0
        if self.system_prompt_tokens > 800:
            llm_score += 0.4  # heavy standing context implies real task

        total += llm_score + local_score
        if total <= 0:
            return JevDecision(
                route="llm",
                confidence=0.0,
                signals=("no_signal",),
                estimated_llm_tokens=self._estimate(messages),
            )
        # Blend: a real executor answer dominates; heuristic ratio breaks ties.
        # The pipeline only serves `local` when an executor answer exists, so a
        # high heuristic score alone can never cause a fabricated response.
        denom = local_score + llm_score
        ratio = (local_score / denom) if denom > 0 else 0.0
        local_conf = min(1.0, (evidence * 0.7) + ratio * 0.9)

        signals = tuple(sorted(set(local_hits + llm_hits)))
        if local_conf >= self.confidence_threshold and not llm_hits:
            return JevDecision(
                route="local",
                confidence=local_conf,
                signals=signals or ("short_query",),
                estimated_llm_tokens=0,
            )
        if local_conf >= self.confidence_threshold and llm_hits and deterministic_answer is not None:
            # Executor answered it even though prose looks complex.
            return JevDecision(route="local", confidence=local_conf, signals=signals)

        return JevDecision(
            route="llm",
            confidence=round(1.0 - local_conf, 4),
            signals=signals or ("default_llm",),
            estimated_llm_tokens=self._estimate(messages),
        )

    def _estimate(self, messages: Sequence[Mapping[str, Any]]) -> int:
        from ..cdm import estimate_tokens

        return estimate_tokens(self._blob(messages))
