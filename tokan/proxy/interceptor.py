"""OpenAI-compatible gateway proxy (transparent middleware).

Run it locally and point any coding agent at it::

    python -m token_optimizer.proxy --port 4040 \\
        --upstream https://api.openai.com/v1 --api-key $OPENAI_API_KEY

    # Claude Code / Cursor / Aider / OpenAI SDK:
    export OPENAI_BASE_URL=http://127.0.0.1:4040/v1

Every ``POST /v1/chat/completions`` flows through the optimization pipeline
(CDM -> edge cache -> JeV) before a single token reaches the upstream provider.
Cache and local hits never leave the machine.

Stdlib only — no third-party dependencies required to run the gateway.
"""
from __future__ import annotations

import argparse
import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from ..pipeline import OptimizationPipeline, PipelineResult

__all__ = ["TokenOptimizerProxy", "ProxyConfig"]


@dataclass
class ProxyConfig:
    upstream_base: str = "https://api.openai.com/v1"
    api_key: Optional[str] = None
    # Anthropic-native passthrough (Claude Code points here via ANTHROPIC_BASE_URL)
    anthropic_upstream: str = "https://api.anthropic.com"
    anthropic_api_key: Optional[str] = None
    host: str = "127.0.0.1"
    port: int = 4040
    request_timeout: float = 120.0
    extra_headers: Dict[str, str] = field(default_factory=dict)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # injected by the server factory
    pipeline: OptimizationPipeline
    config: ProxyConfig

    def log_message(self, fmt: str, *args: Any) -> None:  # silence default stderr spam
        pass

    # -------------------------------------------------------------- plumbing
    def _send_json(self, status: int, payload: Dict[str, Any], *, extra: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _route_headers(self, result: PipelineResult) -> Dict[str, str]:
        return {
            "X-Optimizer-Route": result.decision.route,
            "X-Optimizer-Saved-Tokens": str(result.record.saved_tokens),
            "X-Optimizer-Saved-Percent": str(result.record.saved_pct),
            "X-Optimizer-Digest": result.record.digest[:16],
        }

    # ---------------------------------------------------------------- routes
    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/healthz", "/health"):
            self._send_json(200, {"ok": True, "service": "token-optimizer"})
        elif self.path in ("/metrics", "/v1/metrics"):
            self._send_json(
                200,
                {
                    "session": self.pipeline.metrics.as_dict(),
                    "cache": self.pipeline.cache.stats(),
                },
            )
        else:
            self._send_json(404, {"error": {"message": f"unknown path {self.path}", "type": "invalid_request_error"}})

    def do_POST(self) -> None:  # noqa: N802
        protocol, endpoint = self._classify(self.path)
        if protocol is None:
            self._send_json(404, {"error": {"message": f"unknown path {self.path}", "type": "invalid_request_error"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": {"message": "invalid JSON body", "type": "invalid_request_error"}})
            return

        if endpoint == "count_tokens":
            # Anthropic bookkeeping endpoint — answer locally, zero tokens.
            self._send_json(200, self._count_tokens(payload))
            return

        result = self.pipeline.prepare(payload, protocol=protocol)

        # 1) Cache hit / deterministic local answer: zero upstream tokens.
        if result.can_serve_locally:
            response = self.pipeline.serve_local(
                result, model=str(payload.get("model", "optimizer-local")), protocol=protocol
            )
            if payload.get("stream"):
                if protocol == "anthropic":
                    self._stream_local_anthropic(response)
                else:
                    self._stream_local(response)
            else:
                self._send_json(200, response, extra=self._route_headers(result))
            return

        # 2) Forward compacted payload upstream.
        self._forward(payload, result, protocol=protocol)

    @staticmethod
    def _classify(path: str):
        """Map a request path to (protocol, endpoint)."""
        if path in ("/v1/chat/completions", "/chat/completions"):
            return "openai", "chat"
        if path in ("/v1/messages", "/messages"):
            return "anthropic", "messages"
        if path in ("/v1/messages/count_tokens", "/messages/count_tokens"):
            return "anthropic", "count_tokens"
        if path.startswith("/v1beta/models/") and ":generateContent" in path:
            return "gemini", "generate_content"
        return None, None

    @staticmethod
    def _count_tokens(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Local estimate of Anthropic input tokens (no upstream call)."""
        from ..cdm import estimate_tokens

        messages = payload.get("messages") or []
        system = payload.get("system")
        if isinstance(system, list):
            system = "\n".join(
                block.get("text", "") for block in system if isinstance(block, dict)
            )
        blob = (system or "") + "\n" + "\n".join(
            str(m.get("content", "")) if isinstance(m.get("content"), str) else json.dumps(m.get("content"))
            for m in messages
        )
        return {"input_tokens": estimate_tokens(blob)}

    # -------------------------------------------------------------- local out
    def _stream_local(self, response: Dict[str, Any]) -> None:
        chunk = {
            "id": response["id"],
            "object": "chat.completion.chunk",
            "model": response["model"],
            "choices": [response["choices"][0]],
        }
        body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream_local_anthropic(self, response: Dict[str, Any]) -> None:
        """Full Anthropic event sequence for a locally-served answer."""
        text = (response.get("content") or [{}])[0].get("text", "")
        events = [
            ("message_start", {"type": "message_start", "message": {
                "id": response["id"], "type": "message", "role": "assistant",
                "model": response["model"], "content": [],
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 1},
            }}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": text}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": response["usage"]["output_tokens"]}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        parts = []
        for event, data in events:
            parts.append(f"event: {event}\ndata: {json.dumps(data)}\n\n")
        body = "".join(parts).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------- upstream
    def _forward(self, payload: Dict[str, Any], result: PipelineResult, *, protocol: str = "openai") -> None:
        forwarded = dict(payload)
        # Only the compacted messages travel; everything else passes through.
        forwarded["messages"] = list(result.messages)

        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if protocol == "anthropic":
            url = self.config.anthropic_upstream.rstrip("/") + "/v1/messages"
            key = self.config.anthropic_api_key
            if key:
                headers["x-api-key"] = key
            headers.setdefault("anthropic-version", self.headers.get("anthropic-version", "2023-06-01"))
        else:
            url = self.config.upstream_base.rstrip("/") + "/chat/completions"
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update(self.config.extra_headers)

        req = urllib.request.Request(url, data=json.dumps(forwarded).encode("utf-8"), headers=headers, method="POST")
        try:
            upstream_resp = urllib.request.urlopen(req, timeout=self.config.request_timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                err = json.loads(detail)
            except ValueError:
                err = {"error": {"message": detail[:500], "type": "upstream_error"}}
            self._send_json(exc.code, err)
            return
        except (urllib.error.URLError, OSError) as exc:
            self._send_json(
                502,
                {"error": {"message": f"upstream unreachable: {exc}", "type": "upstream_error"}},
            )
            return

        if payload.get("stream"):
            self._relay_stream(upstream_resp, result, protocol=protocol)
            return

        with upstream_resp:
            body = upstream_resp.read()
        try:
            parsed = json.loads(body.decode("utf-8"))
        except ValueError:
            self._send_json(502, {"error": {"message": "upstream returned non-JSON", "type": "upstream_error"}})
            return

        self.pipeline.record_llm(result, parsed, protocol=protocol)
        self._send_json(200, parsed, extra=self._route_headers(result))

    def _relay_stream(
        self, upstream_resp: Any, result: PipelineResult, *, protocol: str = "openai"
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        for key, value in self._route_headers(result).items():
            self.send_header(key, value)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        buffered = ""
        try:
            while True:
                chunk = upstream_resp.read(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", "replace")
                buffered += text
                self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
        except OSError:
            pass  # client disconnected mid-stream
        finally:
            self.wfile.write(b"0\r\n\r\n")
            # Book prompt tokens only; completion tokens unknowable cheaply.
            self.pipeline.metrics.record(
                route="llm",
                original_tokens=result.record.original_tokens,
                optimized_tokens=result.record.canonical_tokens,
            )
            # Cache the *answer text*, never the SSE framing.
            answer = _extract_streamed_text(buffered, protocol)
            if answer:
                self.pipeline.cache.set(result.cache_key, answer)


def _extract_streamed_text(buffered: str, protocol: str) -> str:
    """Pull the assistant's answer out of relayed SSE frames.

    Raw ``data: {...}`` lines must never reach the cache — only the text the
    model actually produced. Handles both wire formats:
      * openai    → choices[0].delta.content
      * anthropic → content_block_delta delta.text
    """
    parts: List[str] = []
    for line in buffered.splitlines():
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if protocol == "anthropic":
            if obj.get("type") == "content_block_delta":
                delta = obj.get("delta") or {}
                if delta.get("type") == "text_delta":
                    parts.append(delta.get("text", ""))
            elif obj.get("type") == "content_block_start":
                block = obj.get("content_block") or {}
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
        else:
            choices = obj.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str):
                    parts.append(content)
    return "".join(parts)


class TokenOptimizerProxy:
    """Threaded OpenAI-compatible gateway. Use as a context manager."""
    def __init__(self, config: Optional[ProxyConfig] = None, pipeline: Optional[OptimizationPipeline] = None) -> None:
        self.config = config or ProxyConfig()
        self.pipeline = pipeline or OptimizationPipeline.with_cdn()

        outer = self

        class Handler(_Handler):
            pipeline = outer.pipeline
            config = outer.config

        self._server = ThreadingHTTPServer((self.config.host, self.config.port), Handler)
        self._server.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://{self.config.host}:{self.port}/v1"

    @property
    def origin(self) -> str:
        """Gateway root (what Anthropic's ANTHROPIC_BASE_URL expects).

        Never string-strip this — ``rstrip("/v1")`` would eat a trailing
        digit from an ephemeral port like ``:50181``.
        """
        return f"http://{self.config.host}:{self.port}"
    def start(self) -> "TokenOptimizerProxy":
        """Serve in a background thread and return self."""
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> "TokenOptimizerProxy":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.shutdown()


def main(argv: Optional[list] = None) -> None:
    import os

    parser = argparse.ArgumentParser(description="Token-optimizing OpenAI-compatible gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4040)
    parser.add_argument("--upstream", default="https://api.openai.com/v1")
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"))
    parser.add_argument("--cdn", default=None, help="remote edge-cache endpoint")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--threshold", type=float, default=0.88)
    args = parser.parse_args(argv)

    pipeline = OptimizationPipeline.with_cdn(
        args.cdn,
        namespace=args.namespace,
        confidence_threshold=args.threshold,
    )
    proxy = TokenOptimizerProxy(
        ProxyConfig(upstream_base=args.upstream, api_key=args.api_key, host=args.host, port=args.port),
        pipeline,
    )
    print(f"token-optimizer gateway on {proxy.base_url} -> {args.upstream}", flush=True)
    print(f"  cdn endpoint : {args.cdn or 'local LRU only'}", flush=True)
    print(f"  jev threshold: {args.threshold}", flush=True)
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        proxy.shutdown()


if __name__ == "__main__":
    main()
