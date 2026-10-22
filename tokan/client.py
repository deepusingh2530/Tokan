"""SDK wrapper: drop-in client for agent loops that can't run a proxy.

    from tokan import OptimizedAgentClient, CDNConfig

    client = OptimizedAgentClient(upstream_provider="openai")
    response = client.chat_completion(messages=[...])
    print(response.metrics["saved_percent"])

Same pipeline as the gateway proxy, invoked in-process.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .cache import CDNConfig
from .pipeline import OptimizationPipeline, PipelineResult

__all__ = ["OptimizedAgentClient", "CDNConfig", "ChatResponse"]

_PROVIDER_BASE = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "together": "https://api.together.xyz/v1",
}


@dataclass
class ChatResponse:
    """OpenAI-shaped response plus optimizer telemetry."""

    payload: Dict[str, Any]
    result: PipelineResult

    @property
    def content(self) -> str:
        choices = self.payload.get("choices") or []
        if choices:
            return (choices[0].get("message") or {}).get("content") or ""
        return ""

    @property
    def route(self) -> str:
        return self.result.decision.route

    @property
    def metrics(self) -> Dict[str, Any]:
        rec = self.result.record
        return {
            "route": self.route,
            "confidence": self.result.decision.confidence,
            "signals": list(self.result.decision.signals),
            "original_tokens": rec.original_tokens,
            "optimized_tokens": rec.canonical_tokens,
            "tokens_saved": rec.saved_tokens,
            "saved_percent": rec.saved_pct,
            "digest": rec.digest,
        }

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.content


class OptimizedAgentClient:
    """Wraps an OpenAI-compatible chat endpoint with the optimization pipeline.

    Parameters
    ----------
    upstream_provider: key of a known provider, or an explicit ``base_url``.
    transport: injectable ``Callable[[dict, str, float], dict]`` for testing
        or for hooking an existing SDK instead of raw urllib.
    """

    def __init__(
        self,
        upstream_provider: str = "openai",
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        jev_confidence_threshold: float = 0.88,
        cdn_endpoint: Optional[str] = None,
        cdn_namespace: str = "default",
        price_per_million_tokens: float = 2.50,
        pipeline: Optional[OptimizationPipeline] = None,
        transport: Optional[Callable[..., Dict[str, Any]]] = None,
        timeout: float = 120.0,
    ) -> None:
        import os

        self.provider = upstream_provider
        self.base_url = (base_url or _PROVIDER_BASE.get(upstream_provider) or _PROVIDER_BASE["openai"]).rstrip("/")
        self.api_key = api_key or os.environ.get(f"{upstream_provider.upper()}_API_KEY") or os.environ.get("OPENAI_API_KEY")
        self.timeout = timeout
        self._transport = transport
        self.pipeline = pipeline or OptimizationPipeline.with_cdn(
            cdn_endpoint,
            namespace=cdn_namespace,
            confidence_threshold=jev_confidence_threshold,
            price_per_million_tokens=price_per_million_tokens,
        )

    # ------------------------------------------------------------- public API
    def chat_completion(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> ChatResponse:
        payload: Dict[str, Any] = {"messages": list(messages), **kwargs}
        if stream:
            payload["stream"] = False  # non-streaming core; wrap at the edge if needed
        result = self.pipeline.prepare(payload)

        if result.can_serve_locally:
            model = str(payload.get("model", "optimizer-local"))
            return ChatResponse(payload=self.pipeline.serve_local(result, model=model), result=result)

        response = self._call_upstream(payload, result)
        return ChatResponse(payload=response, result=result)

    def session_report(self) -> str:
        return self.pipeline.metrics.report()

    def metrics(self) -> Dict[str, Any]:
        return {
            "session": self.pipeline.metrics.as_dict(),
            "cache": self.pipeline.cache.stats(),
        }

    # -------------------------------------------------------------- upstream
    def _call_upstream(self, payload: Dict[str, Any], result: PipelineResult) -> Dict[str, Any]:
        forwarded = dict(payload)
        forwarded["messages"] = list(result.messages)
        forwarded.pop("stream", None)

        if self._transport is not None:
            response = self._transport(forwarded, self.api_key or "", self.timeout)
        else:
            response = self._urllib_call(forwarded)
        self.pipeline.record_llm(result, response)
        return response

    def _urllib_call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"upstream {exc.code}: {detail[:500]}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"upstream unreachable: {exc}") from exc
