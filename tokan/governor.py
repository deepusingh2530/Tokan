"""Context Governor + Token Budget Controller.

Two independent, deterministic mechanisms that both answer *how much context
may we send, and which part of it*:

1. **Token budget** (:class:`TokenBudget` + :class:`ContextGovernor`).
   Every model has a context window, and part of that window must be reserved
   for the completion. The governor compares the request's estimated input
   against the *available* input budget and, **only when the request would
   overflow the window**, sheds the least valuable context units. Under-budget
   requests are forwarded byte-for-byte unchanged — the governor is a
   circuit-breaker against a hard provider 400, not a compressor.

   Shedding is *quality-constrained*: it is ordered by utility class, then
   recency, then utility-per-token, and required context is never shed at all.
   See :meth:`ContextGovernor.select` for why a pure per-token knapsack is the
   wrong ordering for conversation history.

2. **Semantic answer cache** (:class:`SemanticAnswerCache`). Off by default.
   When enabled it reuses a previous answer for a *near-identical* question,
   gated by a high similarity threshold (the risk threshold) and a task-class
   check that admits only self-contained factual questions. It never invents
   an answer and never fires for instructions, edits, or anything tool-shaped.

Both are pure and clock-free by construction (the cache takes an injected
``time_fn``). The digest basis — cache keys — is computed **before** any
shedding, so governor decisions can never change a cache key or invalidate an
earlier entry. That is the same independence guarantee M3 established for the
compression policy, and :mod:`tests.test_governor` asserts it.
"""
from __future__ import annotations

import difflib
import re
import time as _time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .cdm.normalizer import estimate_tokens

__all__ = [
    "UtilityClass",
    "ContextUnit",
    "TokenBudget",
    "GovernorPlan",
    "ContextGovernor",
    "SemanticAnswerCache",
    "context_window_for",
    "MODEL_CONTEXT_WINDOWS",
    "DEFAULT_CONTEXT_WINDOW",
]

# ---------------------------------------------------------------------------
# Per-model context windows (approximate public figures; overridable).
#
# These are best-effort public numbers maintained per model *family* and matched
# by prefix, because deployment-specific ids (``gpt-5-codex-2025-...``,
# foundry deployment names) always start with the family token. They are
# deliberately conservative: an unknown model falls back to
# ``DEFAULT_CONTEXT_WINDOW`` rather than guessing large, and every value can be
# overridden at the boundary with ``--context-window``.
# ---------------------------------------------------------------------------
MODEL_CONTEXT_WINDOWS: Dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4.1": 1_047_576,
    "gpt-5": 400_000,
    "gpt-5-codex": 400_000,
    "o1": 200_000,
    "o3": 200_000,
    "o4-mini": 200_000,
    "claude": 200_000,
    "gemini-1.5": 1_048_576,
    "gemini-2": 1_048_576,
}
#: Used when no family prefix matches — the smallest window we know of, so an
#: unknown model sheds (or reports overflow) rather than overflowing into a 400.
DEFAULT_CONTEXT_WINDOW = 128_000


def context_window_for(model: str, override: Optional[int] = None) -> int:
    """Context window for ``model`` by longest family-prefix match.

    ``override`` always wins — it is the operator's explicit statement about
    their own deployment and takes precedence over the built-in table.
    """
    if override:
        return int(override)
    name = (model or "").lower()
    best = 0
    window = DEFAULT_CONTEXT_WINDOW
    for family, tokens in MODEL_CONTEXT_WINDOWS.items():
        if name.startswith(family) and len(family) > best:
            best, window = len(family), tokens
    return window


class UtilityClass(str):
    """How much a context unit is worth keeping (JSON-friendly constants)."""

    REQUIRED = "required"        # never shed: instructions, schemas, the ask
    HIGH_VALUE = "high_value"    # shed last: the recent working set
    OPTIONAL = "optional"        # shed first: older, still-coherent history
    REDUNDANT = "redunent"       # shed first: verbatim repeats of earlier units


# Rank used for shed ordering (lower sheds earlier). REQUIRED is never shed, so
# its rank exists only to keep the sort total.
_CLASS_RANK: Dict[str, int] = {
    UtilityClass.REDUNDANT: 0,
    UtilityClass.OPTIONAL: 1,
    UtilityClass.HIGH_VALUE: 2,
    UtilityClass.REQUIRED: 3,
}

# Class weight feeding ``utility_per_token``. Recency multiplies this, so the
# per-token figure still prefers recent content over stale trivia.
_CLASS_SCORE: Dict[str, float] = {
    UtilityClass.REDUNDANT: 1.0,
    UtilityClass.OPTIONAL: 2.0,
    UtilityClass.HIGH_VALUE: 4.0,
    UtilityClass.REQUIRED: 8.0,
}


@dataclass(frozen=True)
class ContextUnit:
    """One atomically-sheddable slice of context.

    ``indices`` are positions in the *projected message list*, and a unit owns
    every index it spans: a tool exchange (the assistant's ``tool_calls`` plus
    the tool results that answer it) is a single unit, because shedding half of
    one leaves an orphaned ``tool_call_id`` that providers reject with a 400.
    """

    indices: Tuple[int, ...]
    role: str
    utility_class: str
    tokens: int
    utility: float

    @property
    def utility_per_token(self) -> float:
        return self.utility / max(1, self.tokens)


@dataclass(frozen=True)
class TokenBudget:
    """What one request may spend on input tokens for a given model."""

    model: str
    context_window: int
    output_reserve: int
    overhead_tokens: int = 0   # tool schemas + system prompt (never sheddable)
    input_tokens: int = 0      # estimated tokens of the sheddable message units

    @property
    def available_input(self) -> int:
        """Input tokens left once the completion reservation is set aside."""
        return max(0, self.context_window - self.output_reserve - self.overhead_tokens)

    @property
    def fits(self) -> bool:
        return self.input_tokens <= self.available_input

    @property
    def overflow_tokens(self) -> int:
        return max(0, self.input_tokens - self.available_input)


@dataclass(frozen=True)
class GovernorPlan:
    """The governor's decision: what to keep, what to shed, and why."""

    budget: TokenBudget
    kept: Tuple[int, ...] = ()          # message indices kept, ascending
    shed: Tuple[int, ...] = ()          # message indices shed, ascending
    shed_units: int = 0
    overflow: bool = False              # required context alone exceeded budget
    applied: bool = False               # True only when bytes actually changed
    shed_tokens: int = 0                # estimated tokens actually removed

    def as_dict(self) -> Dict[str, Any]:
        return {
            "model": self.budget.model,
            "context_window": self.budget.context_window,
            "output_reserve": self.budget.output_reserve,
            "overhead_tokens": self.budget.overhead_tokens,
            "input_tokens": self.budget.input_tokens,
            "available_input": self.budget.available_input,
            "overflow_tokens": self.budget.overflow_tokens,
            "overflow": self.overflow,
            "applied": self.applied,
            "shed_units": self.shed_units,
            "shed_tokens": self.shed_tokens,
            "kept_units_count": len(self.kept),
        }


class ContextGovernor:
    """Decides which context units survive a budget squeeze.

    ``recent_window`` is how many trailing messages count as the working set
    (high-value rather than optional). ``output_reserve`` is the completion the
    provider will generate, which must be set aside before input is admitted;
    a request-level ``max_tokens``/``max_output_tokens`` overrides the default.
    """

    def __init__(
        self,
        *,
        context_window: Optional[int] = None,
        output_reserve: int = 4096,
        recent_window: int = 6,
    ) -> None:
        self.context_window = context_window
        self.output_reserve = int(output_reserve)
        self.recent_window = int(recent_window)

    # ------------------------------------------------------------- budgets
    def budget_for(
        self,
        *,
        model: str,
        input_tokens: int,
        overhead_tokens: int = 0,
        output_reserve: Optional[int] = None,
    ) -> TokenBudget:
        """Build the budget for one request.

        ``overhead_tokens`` is tool-schema + system-prompt weight: it counts
        against the window but is never sheddable, so it is subtracted up front
        rather than competing with conversation units for space.
        """
        window = context_window_for(model, self.context_window)
        reserve = self.output_reserve if output_reserve is None else int(output_reserve)
        return TokenBudget(
            model=model,
            context_window=window,
            output_reserve=reserve,
            overhead_tokens=max(0, int(overhead_tokens)),
            input_tokens=max(0, int(input_tokens)),
        )

    # ------------------------------------------------------------- units
    def build_units(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        estimate: Callable[[str], int] = estimate_tokens,
    ) -> List[ContextUnit]:
        """Project a message list into sheddable units.

        Tool exchanges collapse into one unit so a shed never orphans a
        ``tool_call_id``; everything else is its own unit.
        """
        units: List[ContextUnit] = []
        count = len(messages)
        i = 0
        while i < count:
            msg = messages[i]
            role = str(msg.get("role", "unknown"))
            if role == "assistant" and msg.get("tool_calls"):
                j = i
                tokens = estimate(_text_of(msg))
                while j + 1 < count and str(messages[j + 1].get("role", "")) == "tool":
                    j += 1
                    tokens += estimate(_text_of(messages[j]))
                units.append(ContextUnit(
                    indices=tuple(range(i, j + 1)),
                    role="tool_exchange",
                    utility_class=UtilityClass.OPTIONAL,  # reclassified below
                    tokens=tokens,
                    utility=0.0,
                ))
                i = j + 1
            else:
                units.append(ContextUnit(
                    indices=(i,),
                    role=role,
                    utility_class=UtilityClass.OPTIONAL,  # reclassified below
                    tokens=estimate(_text_of(msg)),
                    utility=0.0,
                ))
                i += 1

        last_ask_index = _last_user_index(messages)
        return [
            self._classify(u, messages, count=count, last_ask_index=last_ask_index)
            for u in units
        ]

    def _classify(
        self,
        unit: ContextUnit,
        messages: Sequence[Mapping[str, Any]],
        *,
        count: int,
        last_ask_index: int,
    ) -> ContextUnit:
        """Assign a utility class and recency-weighted utility score."""
        # The ask and everything that answers it are load-bearing: the model
        # cannot act on a question it was never given.
        is_ask = last_ask_index in unit.indices
        first_role = unit.role
        if first_role in ("system", "developer") or is_ask:
            klass = UtilityClass.REQUIRED
        elif _is_verbatim_repeat(unit, messages):
            # A verbatim repeat of an earlier unit carries no new information.
            klass = UtilityClass.REDUNDANT
        elif unit.indices[-1] >= count - self.recent_window:
            klass = UtilityClass.HIGH_VALUE
        else:
            klass = UtilityClass.OPTIONAL

        # Recency in [0, 1]: the last unit scores 1.0, the first ~1/count.
        recency = (unit.indices[-1] + 1) / max(1, count)
        utility = _CLASS_SCORE[klass] * recency
        return ContextUnit(
            indices=unit.indices,
            role=first_role,
            utility_class=klass,
            tokens=unit.tokens,
            utility=utility,
        )

    # ------------------------------------------------------------ selection
    def select(
        self,
        units: Sequence[ContextUnit],
        budget: TokenBudget,
    ) -> Tuple[List[ContextUnit], List[ContextUnit], bool]:
        """Progressive expansion under the budget: (kept, shed, overflow).

        Start from the required set — the minimum coherent request — and expand
        outward by shed order until the available budget is exhausted.

        Ordering is **class, then recency, then utility-per-token**. The
        milestone asks for utility-per-token selection, and it is used here,
        but as the *final* refinement rather than the primary key: a pure
        per-token knapsack over conversation history keeps dozens of tiny stale
        turns and discards the one large recent turn that actually carries the
        working state, which measurably hurts agent answers. Class and recency
        dominate; utility-per-token breaks the remaining ties.
        """
        required = [u for u in units if u.utility_class == UtilityClass.REQUIRED]
        required_tokens = sum(u.tokens for u in required)
        if required_tokens > budget.available_input:
            # Fail open: required context alone overflows. Shedding it would
            # break authority protection (M3) and strip the model's
            # instructions, so we forward everything and flag the overflow —
            # the provider's own error is more honest than a silently
            # gutted request.
            return list(units), [], True

        budget_left = budget.available_input - required_tokens
        sheddable = [u for u in units if u.utility_class != UtilityClass.REQUIRED]
        sheddable.sort(key=_shed_key)

        kept: List[ContextUnit] = list(required)
        shed: List[ContextUnit] = []
        for unit in sheddable:
            if unit.tokens <= budget_left:
                kept.append(unit)
                budget_left -= unit.tokens
            else:
                shed.append(unit)

        kept.sort(key=lambda u: u.indices[0])
        shed.sort(key=lambda u: u.indices[0])
        return kept, shed, False

    def plan(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str,
        overhead_tokens: int = 0,
        output_reserve: Optional[int] = None,
        estimate: Callable[[str], int] = estimate_tokens,
    ) -> GovernorPlan:
        """Full decision for one request: units → budget → selection.

        Under budget this is a measurement only — ``applied`` stays False and
        no unit is shed — so the common path keeps its bytes and its cache key.
        """
        units = self.build_units(messages, estimate=estimate)
        input_tokens = sum(u.tokens for u in units)
        budget = self.budget_for(
            model=model,
            input_tokens=input_tokens,
            overhead_tokens=overhead_tokens,
            output_reserve=output_reserve,
        )
        if budget.fits:
            kept_indices = tuple(i for u in units for i in u.indices)
            return GovernorPlan(budget=budget, kept=kept_indices, applied=False)

        kept, shed, overflow = self.select(units, budget)
        kept_indices = tuple(sorted(i for u in kept for i in u.indices))
        shed_indices = tuple(sorted(i for u in shed for i in u.indices))
        shed_tokens = sum(u.tokens for u in shed)
        return GovernorPlan(
            budget=budget,
            kept=kept_indices,
            shed=shed_indices,
            shed_units=len(shed),
            overflow=overflow,
            applied=bool(shed_indices) and not overflow,
            shed_tokens=shed_tokens,
        )

    def apply(
        self,
        messages: Sequence[Mapping[str, Any]],
        plan: GovernorPlan,
    ) -> List[Dict[str, Any]]:
        """Return the message list with the plan's shed indices removed.

        Only ever called after the digest has been computed, so the cache key
        is already fixed and shedding cannot invalidate it.
        """
        if not plan.applied:
            return list(messages)
        keep = set(plan.kept)
        return [m for i, m in enumerate(messages) if i in keep]


# ---------------------------------------------------------------------------
# Semantic answer cache — conservative, opt-in, risk-thresholded.
# ---------------------------------------------------------------------------

# Task-class gate: only self-contained factual questions are eligible. Anything
# that asks for a change, names a file, contains code, or is phrased as an
# instruction is excluded, because reusing an answer for those is how a cache
# silently corrupts work.
_EDIT_IMPERATIVES = re.compile(
    r"\b(fix|add|implement|refactor|write|create|delete|remove|update|change|"
    r"rename|move|debug|patch|rewrite|insert|replace|optimize|port|migrate)\b",
    re.IGNORECASE,
)
_FILE_PATH = re.compile(r"[\w./-]*\.\w{1,6}\b|/")
_CODE_FENCE = re.compile(r"```")
_INTERROGATIVE = re.compile(
    r"^(what|who|when|where|why|how|which|whose|whom|is|are|was|were|can|could|"
    r"does|do|did|has|have|had|will|would|should)\b",
    re.IGNORECASE,
)


class SemanticAnswerCache:
    """Near-identical-question answer reuse, gated by a risk threshold.

    Conservative by construction:

    * **Off by default** — serving a cached answer for a *different* question
      is a quality risk, so it requires an explicit opt-in.
    * **High similarity threshold** (default 0.97) — only near-paraphrases
      match, never "related" questions.
    * **Task-class gate** (:meth:`eligible`) — only short, self-contained
      factual questions qualify. Edits, file paths, code and instructions are
      excluded, so the cache can never answer "fix the bug" with last week's
      prose.
    * **Bounded and TTL'd** — entries expire and the store is capped, so a
      long-lived gateway cannot serve answers from a stale world.

    The residual risk — a near-paraphrase whose correct answer depends on
    context this question did not carry — is real and is the reason this stays
    opt-in. It is measured, not assumed: every hit is counted separately in
    ``SessionMetrics.semantic_cache_hits``.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        threshold: float = 0.97,
        max_entries: int = 256,
        ttl_seconds: float = 3600.0,
        time_fn: Callable[[], float] = _time.time,
    ) -> None:
        self.enabled = bool(enabled)
        self.threshold = float(threshold)
        self.max_entries = int(max_entries)
        self.ttl_seconds = float(ttl_seconds)
        self._time = time_fn
        # (model, normalized question) -> (answer, stored_at); OrderedDict is our
        # LRU. Scoped by model because an answer from one model is not valid
        # for another.
        self._entries: "OrderedDict[Tuple[str, str], Tuple[str, float]]" = OrderedDict()

    # ------------------------------------------------------------ gate
    @staticmethod
    def normalize(question: str) -> Tuple[str, str]:
        """(normalized_text, task_class) for a question.

        Normalization is case-folding plus whitespace/punctuation collapse, so
        "What is CI?" and "what is ci" are the same question. The task class is
        the conservative gate: only ``factual`` questions are ever cached.
        """
        text = (question or "").strip()
        lowered = text.lower()
        collapsed = re.sub(r"\s+", " ", lowered).strip()
        if not collapsed or len(collapsed) < 6:
            return collapsed, "excluded"
        if len(collapsed) > 400:
            return collapsed, "excluded"
        if _CODE_FENCE.search(text) or _FILE_PATH.search(text):
            return collapsed, "excluded"
        if _EDIT_IMPERATIVES.search(collapsed):
            return collapsed, "excluded"
        # Strip a trailing question mark so it never affects the match.
        core = collapsed.rstrip("?").strip()
        if _INTERROGATIVE.search(core) or core.endswith("?"):
            return core, "factual"
        return core, "excluded"

    def eligible(self, question: str) -> bool:
        """True when ``question`` is a cacheable self-contained factual ask."""
        return self.normalize(question)[1] == "factual"

    # ------------------------------------------------------------ probe
    def probe(self, question: str, *, model: str = "") -> Optional[Tuple[str, float]]:
        """Return ``(answer, similarity)`` if a near-identical entry exists.

        Only ``factual`` questions are probed, and only matches at or above the
        risk threshold are served. Matching is scoped to ``model``: answers are
        model-specific, so a cached reply from one model is never served for
        another. Returns None otherwise — including when the cache is disabled,
        so the disabled path costs one flag check.
        """
        if not self.enabled:
            return None
        key, task_class = self.normalize(question)
        if task_class != "factual" or not key:
            return None
        self._evict_expired()
        best_key = ""
        best_answer = ""
        best_ratio = 0.0
        for (entry_model, stored_key), (answer, _) in list(self._entries.items()):
            if entry_model != model:
                continue
            ratio = difflib.SequenceMatcher(None, key, stored_key).ratio()
            if ratio > best_ratio:
                best_key, best_answer, best_ratio = (entry_model, stored_key), answer, ratio
        if best_ratio >= self.threshold and best_answer:
            # Refresh recency so a frequently re-asked question survives eviction.
            self._entries.move_to_end(best_key)
            return best_answer, round(best_ratio, 4)
        return None

    # ------------------------------------------------------------ store
    def store(self, question: str, answer: str, *, model: str = "") -> bool:
        """Record an answer for a future near-identical question.

        Only ``factual`` questions with a real answer are stored, and the entry
        is scoped to ``model``. Returns True when the entry was kept.
        """
        if not self.enabled or not (answer or "").strip():
            return False
        key, task_class = self.normalize(question)
        if task_class != "factual" or not key:
            return False
        entry_key = (model, key)
        self._entries[entry_key] = (answer, self._time())
        self._entries.move_to_end(entry_key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return True

    def __len__(self) -> int:
        return len(self._entries)

    def _evict_expired(self) -> None:
        if self.ttl_seconds <= 0:
            return
        now = self._time()
        expired = [k for k, (_, ts) in self._entries.items() if now - ts > self.ttl_seconds]
        for k in expired:
            del self._entries[k]


# --------------------------------------------------------------------------- helpers
def _text_of(message: Mapping[str, Any]) -> str:
    """Flatten a message's content to text for token estimation."""
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str)
        )
    return str(content)


def _last_user_index(messages: Sequence[Mapping[str, Any]]) -> int:
    for i in range(len(messages) - 1, -1, -1):
        if str(messages[i].get("role", "")) == "user":
            return i
    return -1


def _is_verbatim_repeat(unit: ContextUnit, messages: Sequence[Mapping[str, Any]]) -> bool:
    """True when this unit's text repeats an earlier unit's text exactly.

    Cheap redundancy signal: agent clients re-send whole histories, and a turn
    that is byte-identical to one already present carries nothing new. The
    comparison is on flattened text, and a unit that spans a tool exchange is
    never treated as redundant (its pairing is load-bearing).
    """
    if len(unit.indices) != 1:
        return False
    text = _text_of(messages[unit.indices[0]])
    if not text.strip():
        return False
    for j in range(unit.indices[0]):
        if _text_of(messages[j]) == text:
            return True
    return False


def _shed_key(unit: ContextUnit) -> Tuple[int, float, float]:
    """Sort key shedding the least valuable unit first.

    Primary: utility class (redundant, then optional, then high-value).
    Secondary: recency (older units carry a smaller index, so negate).
    Tertiary: utility-per-token, so among equals the cheapest to keep wins.
    """
    return (
        _CLASS_RANK.get(unit.utility_class, 1),
        -unit.indices[-1],
        -unit.utility_per_token,
    )
