from __future__ import annotations
"""The optimization pipeline: CDM -> cache -> JeV -> (LLM | local).

This is the shared core used by *both* integration patterns:

  * :class:`tokan.proxy.TokenOptimizerProxy` (transparent gateway)
  * :class:`tokan.client.OptimizedAgentClient` (SDK wrapper)

Flow per request::

    payload -- CDMNormalizer.fingerprint -- EdgeCache.get
                                              | hit -- return cached answer
                                              | miss
                                        JeVRouter.evaluate
                                              | local -- LocalExecutor answer
                                              | llm
                                   upstream LLM (compact payload)

Wire protocols: "openai" (chat completions), "anthropic"
(messages API), and "gemini" (generateContent).
Content is stored as plain text and shaped per protocol at serve time.
"""

import json
import uuid as uuid_mod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .cache import CDNConfig, EdgeCache
from .cdm import CanonicalRecord, CDMNormalizer, estimate_tokens
from .doc import DocumentStore
from .jev import JevDecision, JevRouter
from .metrics import SessionMetrics
from .session import PrefixReuse, SessionTracker

__all__ = [
    "OptimizationPipeline", "PipelineResult", "LocalExecutor",
    "upstream_cached_tokens", "usage_cached_tokens", "responses_input_to_messages",
    "PrefixReuse", "SessionTracker", "DocumentStore",
]


class LocalExecutor:
    """Deterministic System-0 answers that skip the LLM entirely.

    Deliberately tiny: pattern lookups over the *already loaded* payload.
    Swap in your own callables (AST queries, symbol tables, repo indexes).
    """

    def __init__(self, handlers=None):
        self._handlers = list(handlers or [])

    def add(self, handler):
        self._handlers.append(handler)

    def solve(self, query, *, context=""):
        for handler in self._handlers:
            answer = handler(query) if context else handler(query)
            if answer is not None:
                return answer
        return None


def usage_cached_tokens(usage: Mapping[str, Any], *, protocol: str = "openai") -> int:
    """Cached tokens from a *usage block* (works for whole responses and SSE frames)."""
    if protocol == "gemini":
        return int(usage.get("cachedContentTokenCount") or 0)
    if protocol == "anthropic":
        return int(usage.get("cache_read_input_tokens") or 0)
    # Chat wire: prompt_tokens_details; Responses wire: input_tokens_details.
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    return int(details.get("cached_tokens") or 0)


def responses_input_to_messages(input_value: Any) -> List[Dict[str, Any]]:
    """Map a Responses-API ``input`` to chat-shaped messages for the pipeline.

    The Responses wire carries conversation history as typed items (``message``,
    ``function_call``, ``function_call_output``, ``reasoning``, ...). The
    pipeline's cache digest, session prefix tracking and local executor all
    speak ``messages``, so items are *projected* here — never forwarded; the
    wire itself is compacted in place at forward time. Tool outputs project as
    ``tool`` role so CDM tool-elision and duplicate-dropping apply to them.
    """
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}] if input_value else []
    if not isinstance(input_value, list):
        return []
    messages: List[Dict[str, Any]] = []
    for item in input_value:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message" or (kind is None and item.get("role")):
            content = item.get("content")
            if isinstance(content, list):
                text = "\n".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and isinstance(p.get("text"), str)
                )
            else:
                text = str(content or "")
            messages.append({"role": str(item.get("role", "user")), "content": text})
        elif kind == "function_call":
            messages.append(
                {"role": "assistant", "content": f"{item.get('name', '')}({item.get('arguments', '')})"}
            )
        elif kind == "function_call_output":
            out = item.get("output")
            if isinstance(out, list):
                out = "\n".join(p.get("text", "") for p in out if isinstance(p, dict))
            messages.append({"role": "tool", "content": str(out or "")})
        # reasoning items are per-turn model scratchpad, not conversation:
        # excluded on purpose so the digest tracks what the user controls.
    return messages


def upstream_cached_tokens(response: Mapping[str, Any], *, protocol: str = "openai") -> int:
    """Tokens the *provider's* own prompt cache answered, across wire formats.

    Anthropic and OpenAI both bill cached input tokens at a steep discount when
    a stable prefix repeats; the number arrives on the response and today it
    was simply dropped on the floor. Surfacing it lets ``/metrics`` and the
    response headers show the full picture (Tokan's compaction *plus* the
    provider's prefix cache) instead of only our half.
    """
    if protocol == "gemini":
        return usage_cached_tokens(response.get("usageMetadata") or {}, protocol=protocol)
    return usage_cached_tokens(response.get("usage") or {}, protocol=protocol)


def _default_handlers():
    """Built-in deterministic lookups (line numbers, exact counts)."""
    import re

    line_rx = re.compile(r"line\s*(?:number\s*)?(\d+)", re.I)

    def line_lookup(query):
        m = line_rx.search(query)
        if m and "context:" in query.lower():
            return f"(deterministic) requested line {m.group(1)}"
        return None

    return [line_lookup]


@dataclass
class PipelineResult:
    request: Dict[str, Any]
    record: CanonicalRecord
    decision: JevDecision
    cache_key: str
    messages: Sequence[Mapping[str, Any]]
    local_answer: Optional[str] = None
    cached_value: Any = None
    cached: bool = False
    # Session Governor: how much of this payload repeated the previous turn's
    # prefix (the base the provider's prompt cache discounts).
    prefix_reuse: Optional[PrefixReuse] = None
    # Doc Governor: set on /v1/doc/ask results. Carries the naive full-doc
    # baseline (``full_tokens``) and retrieval info so receipts/dashboard can
    # report "doc tokens avoided" against what a full resend would have cost.
    doc_savings: Optional[Dict[str, Any]] = None

    @property
    def route(self) -> str:
        return self.decision.route

    @property
    def can_serve_locally(self) -> bool:
        return self.decision.route in ("cache", "local") and (
            self.cached or self.local_answer is not None
        )


@dataclass
class OptimizationPipeline:
    """CDM normalization + edge cache + JeV routing in one object."""

    normalizer: CDMNormalizer = field(default_factory=CDMNormalizer)
    cache: EdgeCache = field(default_factory=EdgeCache)
    router: JevRouter = field(default_factory=JevRouter)
    executor: Optional[LocalExecutor] = None
    metrics: SessionMetrics = field(default_factory=SessionMetrics)
    # Session Governor: per-session canonical payload tracking for prefix reuse.
    session: SessionTracker = field(default_factory=SessionTracker)
    # Doc Governor: ingest-once document store backing /v1/ingest + /v1/doc/ask.
    documents: DocumentStore = field(default_factory=DocumentStore)
    # Scope keys that change the answer (and therefore the cache key).
    scope_keys: tuple = ("model", "temperature", "top_p", "user")

    def __post_init__(self) -> None:
        if self.executor is None:
            self.executor = LocalExecutor(_default_handlers())

    @classmethod
    def with_cdn(
        cls,
        cdn_endpoint: Optional[str] = None,
        *,
        namespace: str = "default",
        confidence_threshold: float = 0.88,
        ttl_seconds: int = 3600,
        price_per_million_tokens: float = 2.50,
        cache_db: Optional[str] = None,
    ) -> "OptimizationPipeline":
        from .cache import CDNConfig
        config = CDNConfig(endpoint=cdn_endpoint, namespace=namespace, ttl_seconds=ttl_seconds)
        store = None
        if cache_db:
            # Persistent tier: cache entries survive gateway restarts.
            from .cache import SqliteStore
            store = SqliteStore(cache_db, ttl_seconds=ttl_seconds)
        return cls(
            cache=EdgeCache(config, store=store),
            router=JevRouter(confidence_threshold=confidence_threshold),
            metrics=SessionMetrics(price_per_million_tokens=price_per_million_tokens),
        )

    # ------------------------------------------------------------- stage 1-2
    def prepare(
        self,
        payload: Mapping[str, Any],
        *,
        protocol: str = "openai",
        session_key: Optional[str] = None,
    ) -> PipelineResult:
        """Run CDM + cache probe + JeV decision. Does NOT touch the LLM.

        ``protocol`` is ``"openai"``, ``"anthropic"``, or ``"gemini"``.

        * ``"openai"``: standard chat-completions payload with ``messages``.
        * ``"anthropic"``: Messages API payload with top-level ``system`` field;
          the system prompt is folded into the digest scope.
        * ``"gemini"``: Generative Language API payload with ``contents`` and
          optional ``generationConfig``. The ``contents`` array is converted to
          messages for cache digesting; only the text parts are considered.

        ``session_key`` names the conversation for the Session Governor (an
        explicit ``X-Optimizer-Session`` header, or ``None`` to derive it from
        the message root). It drives prefix-reuse measurement only — it never
        changes the cache digest.
        """
        messages = payload.get("messages") or []
        if not messages and "input" in payload:
            # Responses API wire: the conversation lives in `input` (a string
            # or typed items), not `messages`. Project it so the digest,
            # session prefix tracking and cache probes see the real request —
            # without this every responses payload fingerprints identically.
            messages = responses_input_to_messages(payload.get("input"))
        scope = {k: payload.get(k) for k in self.scope_keys}
        if protocol == "anthropic" and payload.get("system") is not None:
            scope = dict(scope)
            scope["system"] = payload["system"]
        if protocol == "gemini":
            # Convert Gemini contents to a messages list for digesting.
            # Gemini format: {"contents": [{"role": "user", "parts": [{"text": "..."}]}], ...}
            contents = payload.get("contents") or []
            gemini_messages: list = []
            for c in contents:
                role = c.get("role", "user")
                parts = c.get("parts") or []
                text_parts = [p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p]
                if text_parts:
                    gemini_messages.append({"role": role, "content": "\n".join(text_parts)})
            messages = gemini_messages
            # Include model in scope so different models don't share cache entries.
            scope["model"] = payload.get("model", scope.get("model"))
        record = self.normalizer.fingerprint(messages, scope)
        # Session Governor: deterministic per-message strings + how much of this
        # payload repeats the previous turn's prefix (the base the provider's
        # prompt cache discounts).
        msg_strings = self.normalizer.canonical_message_strings(messages)
        prefix_reuse = self.session.observe(messages, msg_strings, explicit_key=session_key)

        cached_value = self.cache.get(record.digest)
        if cached_value is not None:
            decision = JevDecision(route="cache", confidence=1.0, signals=("cache_hit",))
            return PipelineResult(
                request=dict(payload),
                record=record,
                decision=decision,
                cache_key=record.digest,
                messages=self.normalizer.normalize_messages(messages),
                cached_value=cached_value,
                cached=True,
                prefix_reuse=prefix_reuse,
            )

        query = self._last_user(messages)
        blob = self.normalizer.canonical_json(messages)
        answer = self.executor.solve(query, context=blob) if self.executor else None
        decision = self.router.evaluate(messages, deterministic_answer=answer)

        # Router may say "local" without an executor answer; never fabricate.
        if decision.route == "local" and answer is None:
            decision = JevDecision(
                route="llm",
                confidence=decision.confidence,
                signals=decision.signals + ("no_executor_answer",),
                estimated_llm_tokens=record.canonical_tokens,
            )

        return PipelineResult(
            request=dict(payload),
            record=record,
            decision=decision,
            cache_key=record.digest,
            messages=self.normalizer.normalize_messages(messages),
            local_answer=answer,
            prefix_reuse=prefix_reuse,
        )

    # ------------------------------------------------------------- stage 3-4
    def serve_local(
        self,
        result: PipelineResult,
        *,
        model: str = "local",
        protocol: str = "openai",
        endpoint: str = "",
    ) -> Dict[str, Any]:
        """Build a protocol-shaped response for a cache/local hit and book it."""
        if result.cached:
            content = result.cached_value
            route = "cache"
        else:
            content = result.local_answer or ""
            route = "local"
        if protocol == "anthropic":
            response = self._anthropic_response(content, model=model)
        elif protocol == "gemini":
            response = self._gemini_response(content, model=model)
        elif endpoint == "responses":
            # Same cached text, Responses wire: `output` items, not `choices`.
            response = self._responses_response(
                content, model=model, input_tokens=result.record.canonical_tokens
            )
        else:
            response = self._openai_response(content, model=model)
        self.metrics.record(route=route, original_tokens=result.record.original_tokens, optimized_tokens=0)
        # Doc Governor: cache/local answers never left the machine, so the whole
        # full-doc resend was avoided.
        self.record_doc(result, sent_tokens=0)
        return response

    def record_llm(
        self,
        result: PipelineResult,
        response: Mapping[str, Any],
        *,
        protocol: str = "openai",
    ) -> None:
        """Book an upstream LLM response: metrics + cache fill (text only)."""
        usage = response.get("usage") or {}
        prompt_tokens = int(
            usage.get("prompt_tokens") or usage.get("input_tokens") or result.record.canonical_tokens
        )
        # Honest basis for `tokens_saved`: the local chars/4 estimate of the
        # raw payload and the provider's tokenizer disagree by a few percent
        # even when nothing was compacted, and subtracting one from the other
        # would book that drift as "saved" (it doubled the claim in a live
        # A/B measured against Copilot's bills). Project the raw estimate
        # onto the provider's own basis — the raw/sent ratio applied to what
        # was actually billed — so a clean payload claims exactly 0 and only
        # real compaction shows up. Completion tokens are the model's
        # output, never context savings, so they stay out entirely.
        sent_estimate = max(1, result.record.canonical_tokens)
        original_tokens = int(
            round(result.record.original_tokens * prompt_tokens / sent_estimate)
        )
        self.metrics.record(
            route="llm",
            original_tokens=original_tokens,
            optimized_tokens=prompt_tokens,
        )
        cached = upstream_cached_tokens(response, protocol=protocol)
        # Session Governor economics: book the upstream prompt billing split
        # (full-price vs provider-cached) and the reused-prefix base. Both are
        # the provider's discount, surfaced for visibility — never counted as
        # *Tokan's* savings.
        self.metrics.record_provider_prompt(prompt_tokens, cached)
        if result.prefix_reuse is not None:
            self.metrics.record_prefix_reuse(result.prefix_reuse.reused_tokens)
        # Doc Governor: prompt_tokens is what actually reached the provider for
        # this assembled [summary + chunks + question] prompt.
        self.record_doc(result, sent_tokens=prompt_tokens)
        content = self._extract_content(response, protocol=protocol)
        if content:
            # Cache the plain text answer; shaping happens at serve time, so
            # an answer cached via Anthropic can be served via OpenAI too.
            self.cache.set(result.cache_key, content)

    def record_doc(self, result: PipelineResult, *, sent_tokens: int) -> None:
        """Book Doc Governor economics for a /v1/doc/ask result.

        ``full_tokens`` is the naive baseline (whole document + question);
        ``sent_tokens`` is what Tokan actually forwarded (0 on cache/local
        hits). The difference is *doc tokens avoided* — Tokan's own doing,
        reported separately from compaction savings.
        """
        ds = result.doc_savings
        if not ds:
            return
        self.metrics.record_doc(
            full_tokens=int(ds.get("full_tokens", 0)),
            sent_tokens=sent_tokens,
            chunks_used=int(ds.get("chunks_used", 0)),
        )

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _last_user(messages: Sequence[Mapping[str, Any]]) -> str:
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
    def _extract_content(response: Mapping[str, Any], *, protocol: str = "openai") -> str:
        if protocol == "anthropic":
            # content: [{"type": "text", "text": ...}, ...]
            blocks = response.get("content")
            if isinstance(blocks, list):
                return "".join(
                    block.get("text", "")
                    for block in blocks
                    if isinstance(block, dict) and block.get("type") == "text"
                )
        if protocol == "gemini":
            # candidates: [{"content": {"parts": [{"text": "..."}], "role": "model"}, "finishReason": "STOP"}]
            candidates = response.get("candidates") or []
            if candidates:
                parts = candidates[0].get("content", {}).get("parts") or []
                for p in parts:
                    if isinstance(p, dict) and "text" in p:
                        return p["text"]
        choices = response.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                return content
        # Responses wire: output items with output_text parts, no `choices`.
        output = response.get("output")
        if isinstance(output, list):
            texts = []
            for item in output:
                if isinstance(item, dict) and item.get("type") == "message":
                    for p in item.get("content") or []:
                        if (
                            isinstance(p, dict)
                            and p.get("type") in ("output_text", "text")
                            and isinstance(p.get("text"), str)
                        ):
                            texts.append(p["text"])
            if texts:
                return "".join(texts)
        content = response.get("content")
        if isinstance(content, str):
            return content
        return ""

    @staticmethod
    def _openai_response(content: str, *, model: str, cached: bool = False) -> Dict[str, Any]:
        from .cdm import estimate_tokens
        completion_tokens = estimate_tokens(content)
        return {
            "id": "msg_local_optimizer",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": completion_tokens,
                "total_tokens": completion_tokens,
            },
            "optimizer": {
                "route": "cache" if cached else "local",
                "served_without_llm": True,
            },
        }

    @staticmethod
    def _anthropic_response(content: str, *, model: str) -> Dict[str, Any]:
        """Anthropic Messages shape for a locally-served answer."""
        return {
            "id": f"msg_optimizer_{uuid_mod.uuid4().hex[:20]}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": content}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 0,
                "output_tokens": estimate_tokens(content),
            },
        }

    @staticmethod
    def _gemini_response(content: str, *, model: str) -> Dict[str, Any]:
        """Gemini generateContent shape for a locally-served answer."""
        return {
            "candidates": [
                {
                    "content": {"parts": [{"text": content}], "role": "model"},
                    "finishReason": "STOP",
                }
            ]
        }

    @staticmethod
    def _responses_response(content: str, *, model: str, input_tokens: int = 0) -> Dict[str, Any]:
        """Responses API shape for a locally-served (cache/local) answer.

        Same cached text as the chat wire would get, but shaped as a completed
        response object so Responses-API clients (gpt-5/codex in Copilot BYOK)
        can parse it without touching the provider.
        """
        output_tokens = estimate_tokens(content)
        return {
            "id": f"resp_optimizer_{uuid_mod.uuid4().hex[:20]}",
            "object": "response",
            "status": "completed",
            "model": model,
            "output": [
                {
                    "type": "message",
                    "id": f"msg_optimizer_{uuid_mod.uuid4().hex[:20]}",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content, "annotations": []}],
                }
            ],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            "optimizer": {"served_without_llm": True},
        }
