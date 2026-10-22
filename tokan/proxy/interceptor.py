"""OpenAI-compatible gateway proxy (transparent middleware).

Run it locally and point any coding agent at it::

    python -m tokan.proxy --port 4040 \\
        --upstream https://api.openai.com/v1 --api-key $OPENAI_API_KEY

    # Claude Code / Cursor / Aider / OpenAI SDK:
    export OPENAI_BASE_URL=http://127.0.0.1:4040/v1

    # GitHub Copilot CLI (BYOK wire; the CLI appends /chat/completions):
    export COPILOT_PROVIDER_BASE_URL=http://127.0.0.1:4040/v1

Every ``POST /v1/chat/completions`` flows through the optimization pipeline
(CDM -> edge cache -> JeV) before a single token reaches the upstream provider.
Cache and local hits never leave the machine.

Auth is transparent: client credentials (BYOK keys, Copilot OAuth tokens) are
forwarded upstream unless the gateway holds its own key for that wire.

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
from typing import Any, Dict, List, Optional, Tuple

from ..pipeline import OptimizationPipeline, PipelineResult

__all__ = [
    "TokenOptimizerProxy",
    "ProxyConfig",
    "PASSTHROUGH_HEADERS",
    "COPILOT_UPSTREAMS",
    "COPILOT_FALLBACK_HEADERS",
    "resolve_upstream_flags",
]

#: Client headers relayed to the upstream unchanged: auth (so BYOK keys and
#: Copilot OAuth tokens reach the provider) plus the identity headers the
#: GitHub Copilot API expects from real clients (integration id, editor
#: version, conversation id for multi-turn continuity).
PASSTHROUGH_HEADERS = (
    "authorization",
    "x-api-key",
    "x-goog-api-key",
    "copilot-integration-id",
    "editor-version",
    "editor-plugin-version",
    "openai-organization",
    "openai-intent",
    "conversation-id",
    "user-agent",
    "x-request-id",
)

#: ``--copilot [plan]`` maps a Copilot subscription to its API origin.
COPILOT_UPSTREAMS = {
    "individual": "https://api.githubcopilot.com",
    "business": "https://api.business.githubcopilot.com",
    "enterprise": "https://api.enterprise.githubcopilot.com",
}

#: Stand-in identity headers for BYOK clients that send none of their own
#: (Copilot CLI in provider mode). Real client values always win — these are
#: applied with ``setdefault``, never as overrides.
COPILOT_FALLBACK_HEADERS = {
    "copilot-integration-id": "vscode-chat",
    "editor-version": "1.106.0",
    "editor-plugin-version": "copilot-chat/0.30.0",
}


def resolve_upstream_flags(args: Any) -> Tuple[str, Dict[str, str], Dict[str, str]]:
    """Turn serve flags into ``(upstream_base, extra_headers, fallback_headers)``.

    * ``--copilot [plan]`` points the OpenAI wire at the GitHub Copilot API
      origin for that plan and supplies fallback identity headers.
    * ``--extra-header "Key: Value"`` (repeatable) adds explicit upstream
      headers that override anything the client sent.

    Raises ``ValueError`` on a malformed header pair.
    """
    upstream = args.upstream
    extra: Dict[str, str] = {}
    fallback: Dict[str, str] = {}
    if getattr(args, "copilot", None):
        upstream = COPILOT_UPSTREAMS[args.copilot]
        fallback = dict(COPILOT_FALLBACK_HEADERS)
    for raw in getattr(args, "extra_header", None) or []:
        name, sep, value = raw.partition(":")
        if not sep or not name.strip():
            raise ValueError(f"--extra-header expects 'Key: Value', got {raw!r}")
        extra[name.strip()] = value.strip()
    return upstream, extra, fallback


def add_upstream_flags(parser: argparse.ArgumentParser) -> None:
    """Shared ``--copilot`` / ``--extra-header`` serve flags."""
    parser.add_argument(
        "--copilot",
        nargs="?",
        const="individual",
        default=None,
        choices=sorted(COPILOT_UPSTREAMS),
        metavar="PLAN",
        help="serve a GitHub Copilot plan as the OpenAI upstream: "
             "individual (default) | business | enterprise. "
             "Pass your GitHub token with --api-key, or let clients pass theirs.",
    )
    parser.add_argument(
        "--extra-header",
        action="append",
        default=[],
        metavar="KEY:VALUE",
        help="extra upstream header (repeatable), "
             "e.g. \"Copilot-Integration-Id: vscode-chat\"",
    )


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
    # Gemini-native passthrough (Gemini CLI points here via GOOGLE_GEMINI_BASE_URL)
    gemini_upstream: str = "https://generativelanguage.googleapis.com"
    gemini_api_key: Optional[str] = None
    extra_headers: Dict[str, str] = field(default_factory=dict)
    # Sent only where the client did not supply the header itself — BYOK
    # clients (Copilot CLI provider mode) send no identity headers of their own.
    fallback_headers: Dict[str, str] = field(default_factory=dict)


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
        elif self.path in ("/models", "/v1/models"):
            # Model pickers (VS Code Copilot, SDKs) list models before chat.
            self._forward_models()
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
            wants_stream = bool(payload.get("stream")) or endpoint == "generate_content_stream"
            if wants_stream:
                if protocol == "anthropic":
                    self._stream_local_anthropic(response)
                elif protocol == "gemini":
                    self._stream_local_gemini(response)
                else:
                    self._stream_local(response)
            else:
                self._send_json(200, response, extra=self._route_headers(result))
            return

        # 2) Forward compacted payload upstream.
        self._forward(payload, result, protocol=protocol, endpoint=endpoint)

    @staticmethod
    def _classify(path: str):
        """Map a request path to (protocol, endpoint)."""
        if path in ("/v1/chat/completions", "/chat/completions"):
            return "openai", "chat"
        if path in ("/v1/messages", "/messages"):
            return "anthropic", "messages"
        if path in ("/v1/messages/count_tokens", "/messages/count_tokens"):
            return "anthropic", "count_tokens"
        if path.startswith("/v1beta/models/") and ":streamGenerateContent" in path:
            return "gemini", "generate_content_stream"
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

    def _stream_local_gemini(self, response: Dict[str, Any]) -> None:
        """Gemini SSE frame for a locally-served answer (streamGenerateContent)."""
        body = f"data: {json.dumps(response)}\n\n".encode("utf-8")
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
    def _client_headers(self) -> Dict[str, str]:
        """Headers the client sent that the upstream must also see.

        This is what makes BYOK wiring work with zero gateway keys: the
        caller's ``Authorization`` (OpenAI key, Copilot OAuth token) and the
        Copilot identity headers travel untouched.
        """
        out: Dict[str, str] = {}
        for name in PASSTHROUGH_HEADERS:
            value = self.headers.get(name)
            if value:
                out[name] = value
        return out

    def _finalize_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """Explicit config overrides, then fallbacks fill what's still missing."""
        headers.update(self.config.extra_headers)
        for name, value in self.config.fallback_headers.items():
            headers.setdefault(name, value)
        return headers

    def _forward_models(self) -> None:
        """GET /models passthrough — model pickers call it before chatting."""
        headers = {"Accept": "application/json"}
        headers.update(self._client_headers())
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        self._finalize_headers(headers)

        req = urllib.request.Request(
            self.config.upstream_base.rstrip("/") + "/models",
            headers=headers,
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.request_timeout) as resp:
                body = resp.read()
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
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(
        self,
        payload: Dict[str, Any],
        result: PipelineResult,
        *,
        protocol: str = "openai",
        endpoint: str = "",
    ) -> None:
        forwarded = dict(payload)
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        headers.update(self._client_headers())

        if protocol == "gemini":
            # The Gemini wire speaks `contents`, not `messages`. Compact each
            # text part in place; non-text parts (inline_data, function_call)
            # and the generationConfig/systemInstruction envelope pass through
            # untouched — flattening them would corrupt the request.
            contents = []
            for c in payload.get("contents") or []:
                parts = []
                for part in c.get("parts") or []:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        parts.append(
                            {**part, "text": self.pipeline.normalizer.compact_text(part["text"])}
                        )
                    elif isinstance(part, dict):
                        parts.append(part)
                contents.append({**c, "parts": parts})
            forwarded["contents"] = contents
            forwarded.pop("messages", None)

            model = str(payload.get("model", "gemini-pro"))
            tail = (
                ":streamGenerateContent?alt=sse"
                if endpoint == "generate_content_stream"
                else ":generateContent"
            )
            url = self.config.gemini_upstream.rstrip("/") + f"/v1beta/models/{model}{tail}"
            key = self.config.gemini_api_key
            if key:
                headers["x-goog-api-key"] = key
                headers.pop("authorization", None)
        elif protocol == "anthropic":
            # Only the compacted messages travel; everything else passes through.
            forwarded["messages"] = list(result.messages)
            url = self.config.anthropic_upstream.rstrip("/") + "/v1/messages"
            key = self.config.anthropic_api_key
            if key:
                # Configured key wins: drop the client's credentials so the
                # upstream never sees two competing identities.
                headers["x-api-key"] = key
                headers.pop("authorization", None)
            headers.setdefault("anthropic-version", self.headers.get("anthropic-version", "2023-06-01"))
        else:
            forwarded["messages"] = list(result.messages)
            url = self.config.upstream_base.rstrip("/") + "/chat/completions"
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
        self._finalize_headers(headers)

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

        if payload.get("stream") or endpoint == "generate_content_stream":
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
    model actually produced. Handles all three wire formats:
      * openai    → choices[0].delta.content
      * anthropic → content_block_delta delta.text
      * gemini    → candidates[0].content.parts[].text
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
        if protocol == "gemini":
            candidates = obj.get("candidates") or []
            if candidates:
                for part in (candidates[0].get("content") or {}).get("parts") or []:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        parts.append(part["text"])
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


class _GatewayServer(ThreadingHTTPServer):
    """Burst-tolerant gateway server.

    The stdlib default backlog of 5 drops SYNs when many agents connect at
    the same instant (measured: ~246 resets per 480 requests at ~2.4k
    conn/s in benchmarks/bench_gateway.py scenario B6). ``listen()`` picks
    the backlog up at construction, so it must be a class attribute.
    """

    request_queue_size = 128
    daemon_threads = True


class TokenOptimizerProxy:
    """Threaded OpenAI-compatible gateway. Use as a context manager."""
    def __init__(self, config: Optional[ProxyConfig] = None, pipeline: Optional[OptimizationPipeline] = None) -> None:
        self.config = config or ProxyConfig()
        self.pipeline = pipeline or OptimizationPipeline.with_cdn()

        outer = self

        class Handler(_Handler):
            pipeline = outer.pipeline
            config = outer.config

        self._server = _GatewayServer((self.config.host, self.config.port), Handler)
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
    add_upstream_flags(parser)
    parser.add_argument("--gemini-upstream", default="https://generativelanguage.googleapis.com")
    parser.add_argument("--gemini-api-key", default=os.environ.get("GEMINI_API_KEY"))
    parser.add_argument("--cdn", default=None, help="remote edge-cache endpoint")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--threshold", type=float, default=0.88)
    args = parser.parse_args(argv)
    try:
        upstream, extra_headers, fallback_headers = resolve_upstream_flags(args)
    except ValueError as exc:
        parser.error(str(exc))

    pipeline = OptimizationPipeline.with_cdn(
        args.cdn,
        namespace=args.namespace,
        confidence_threshold=args.threshold,
    )
    proxy = TokenOptimizerProxy(
        ProxyConfig(
            upstream_base=upstream,
            api_key=args.api_key,
            host=args.host,
            port=args.port,
            gemini_upstream=args.gemini_upstream,
            gemini_api_key=args.gemini_api_key,
            extra_headers=extra_headers,
            fallback_headers=fallback_headers,
        ),
        pipeline,
    )
    print(f"token-optimizer gateway on {proxy.base_url} -> {upstream}", flush=True)
    print(f"  cdn endpoint : {args.cdn or 'local LRU only'}", flush=True)
    print(f"  jev threshold: {args.threshold}", flush=True)
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        proxy.shutdown()


if __name__ == "__main__":
    main()
