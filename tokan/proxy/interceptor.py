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
import html
import json
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Deque, Dict, List, Optional, Tuple

from ..pipeline import OptimizationPipeline, PipelineResult, usage_cached_tokens, upstream_cached_tokens

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
    #: Retry once with this model when the upstream rejects the requested one
    #: (model_not_supported / model_not_found) — e.g. plan-gated Copilot IDs.
    fallback_model: Optional[str] = None
    #: Print one receipt line per request to the gateway console.
    log_requests: bool = False
    #: How many receipts the /dashboard keeps in its rolling window.
    receipt_window: int = 500


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # injected by the server factory
    pipeline: OptimizationPipeline
    config: ProxyConfig
    # Rolling per-request receipts (deque shared across handler instances);
    # None disables recording (direct-handler tests).
    receipts: Optional[Deque[Dict[str, Any]]] = None

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

    # ------------------------------------------------------------- receipts
    def _record_receipt(
        self,
        payload: Dict[str, Any],
        result: PipelineResult,
        *,
        route: str,
        status: int,
        ms: float,
        upstream_cached: int = 0,
        fallback: str = "",
    ) -> None:
        """One line of truth per request: route, tokens, latency, disclosures.

        This is what makes savings *visible* — every request that passes
        through the gateway leaves a receipt here (and, when ``log_requests``
        is on, a line on the console).
        """
        if self.receipts is None:
            return
        original = result.record.original_tokens
        # What actually left the machine: cache/local hits send nothing.
        sent = 0 if route in ("cache", "local") else result.record.canonical_tokens
        saved_pct = round(100.0 * (original - sent) / original, 2) if original else 0.0
        rec: Dict[str, Any] = {
            "ts": time.time(),
            "path": self.path,
            "model": str(payload.get("model", "")),
            "route": route,
            "status": status,
            "original_tokens": original,
            "sent_tokens": sent,
            "saved_pct": saved_pct,
            "ms": round(ms, 2),
            "digest": result.record.digest[:16],
            "upstream_cached": upstream_cached,
            "fallback": fallback,
        }
        self.receipts.append(rec)
        if self.config.log_requests:
            line = (
                f"{time.strftime('%H:%M:%S')} {self.path} route={route} status={status} "
                f"model={rec['model'] or '-'} {original}->{sent} tok ({saved_pct}%) "
                f"{rec['ms']}ms"
            )
            if upstream_cached:
                line += f" upstream_cache={upstream_cached}"
            if fallback:
                line += f" fallback={fallback}"
            print(line, flush=True)

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
        elif self.path in ("/dashboard", "/v1/dashboard"):
            self._dashboard()
        else:
            self._send_json(404, {"error": {"message": f"unknown path {self.path}", "type": "invalid_request_error"}})

    # ------------------------------------------------------------ dashboard
    def _dashboard(self) -> None:
        """Single-page live view: session savings, cache, recent receipts."""
        import html as html_mod

        m = self.pipeline.metrics.as_dict()
        c = self.pipeline.cache.stats()
        receipts = list(self.receipts or [])[-40:][::-1]

        def esc(value: Any) -> str:
            return html_mod.escape(str(value))

        rows = "".join(
            "<tr>"
            f"<td>{esc(time.strftime('%H:%M:%S', time.localtime(r['ts'])))}</td>"
            f"<td class='rt-{esc(r['route'])}'>{esc(r['route'])}</td>"
            f"<td>{esc(r['model'])}</td>"
            f"<td>{esc(r['original_tokens'])}&rarr;{esc(r['sent_tokens'])}</td>"
            f"<td>{esc(r['saved_pct'])}%</td>"
            f"<td>{esc(r['status'])}</td>"
            f"<td>{esc(r['ms'])}ms</td>"
            f"<td>{esc(r['upstream_cached'])}</td>"
            f"<td>{esc(r['fallback'])}</td>"
            "</tr>"
            for r in receipts
        ) or "<tr><td colspan='9' class='empty'>no requests yet</td></tr>"

        doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Tokan Gateway</title>
<meta http-equiv="refresh" content="2">
<style>
 body {{ font: 13px/1.5 -apple-system, sans-serif; background:#0d1117; color:#e6edf3; margin:2rem; }}
 h1 {{ font-size:1.2rem; }} h1 span {{ color:#3fb950; }}
 .cards {{ display:flex; gap:1rem; flex-wrap:wrap; margin:1rem 0; }}
 .card {{ background:#161b22; border:1px solid #30363d; border-radius:6px; padding:.75rem 1rem; min-width:140px; }}
 .card .k {{ color:#8b949e; font-size:11px; text-transform:uppercase; }}
 .card .v {{ font-size:1.4rem; font-weight:600; }}
 table {{ border-collapse:collapse; width:100%; margin-top:.5rem; }}
 th,td {{ padding:.3rem .6rem; border-bottom:1px solid #21262d; text-align:left; }}
 th {{ color:#8b949e; font-size:11px; text-transform:uppercase; }}
 .rt-cache {{ color:#3fb950; }} .rt-local {{ color:#58a6ff; }} .rt-llm {{ color:#d29922; }}
 .empty {{ color:#8b949e; font-style:italic; }}
</style></head><body>
<h1><span>Tokan</span> Gateway &mdash; {esc(self.config.host)}:{esc(self.config.port)}</h1>
<div class="cards">
 <div class="card"><div class="k">Requests</div><div class="v">{esc(m['requests'])}</div></div>
 <div class="card"><div class="k">Cache hits</div><div class="v">{esc(m['cache_hits'])}</div></div>
 <div class="card"><div class="k">Local solves</div><div class="v">{esc(m['local_solves'])}</div></div>
 <div class="card"><div class="k">LLM calls</div><div class="v">{esc(m['llm_calls'])}</div></div>
 <div class="card"><div class="k">Tokens saved</div><div class="v">{esc(m['tokens_saved'])}</div></div>
 <div class="card"><div class="k">Saved %</div><div class="v">{esc(m['saved_pct'])}%</div></div>
 <div class="card"><div class="k">Provider cache</div><div class="v">{esc(m['upstream_cached_tokens'])}</div></div>
 <div class="card"><div class="k">Fallbacks</div><div class="v">{esc(m['fallbacks'])}</div></div>
 <div class="card"><div class="k">Cache entries</div><div class="v">{esc(c['entries'])}</div></div>
 <div class="card"><div class="k">Hit rate</div><div class="v">{esc(c['hit_rate'])}</div></div>
</div>
<table>
<tr><th>Time</th><th>Route</th><th>Model</th><th>Tokens</th><th>Saved</th><th>Status</th><th>Latency</th><th>Prov.cache</th><th>Fallback</th></tr>
{rows}
</table>
</body></html>"""
        body = doc.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

        started = time.perf_counter()
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
            self._record_receipt(
                payload, result,
                route=result.decision.route, status=200,
                ms=(time.perf_counter() - started) * 1000,
            )
            return

        # 2) Forward compacted payload upstream.
        self._forward(
            payload, result, protocol=protocol, endpoint=endpoint,
            started=started,
        )

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
        started: Optional[float] = None,
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

        # Model fallback: plan-gated catalogs reject valid-looking model ids
        # with 400 model_not_supported (Copilot: 48 picker models, most 400).
        # Retry once with the configured fallback instead of passing the wall
        # straight to the client.
        attempts: List[Tuple[str, Dict[str, Any]]] = [(url, forwarded)]
        fb = (self.config.fallback_model or "").strip()
        requested_model = str(payload.get("model", ""))
        if fb and requested_model != fb:
            fb_url = url
            if protocol == "gemini":
                tail = (
                    ":streamGenerateContent?alt=sse"
                    if endpoint == "generate_content_stream"
                    else ":generateContent"
                )
                fb_url = self.config.gemini_upstream.rstrip("/") + f"/v1beta/models/{fb}{tail}"
            attempts.append((fb_url, {**forwarded, "model": fb}))

        upstream_resp = None
        fallback_note = ""
        last_status, last_detail = 502, "upstream unreachable"
        for i, (attempt_url, attempt_payload) in enumerate(attempts):
            req = urllib.request.Request(
                attempt_url, data=json.dumps(attempt_payload).encode("utf-8"),
                headers=headers, method="POST",
            )
            try:
                upstream_resp = urllib.request.urlopen(req, timeout=self.config.request_timeout)
                if i == 1:
                    fallback_note = f"{requested_model} -> {fb}"
                    self.pipeline.metrics.record_fallback()
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                last_status, last_detail = exc.code, detail
                if i == 0 and len(attempts) > 1 and _is_model_rejection(exc.code, detail):
                    continue  # this model is gated/unknown — try the fallback
                break
            except (urllib.error.URLError, OSError) as exc:
                last_status, last_detail = 502, f"upstream unreachable: {exc}"
                break

        if upstream_resp is None:
            try:
                err = json.loads(last_detail)
            except ValueError:
                err = {"error": {"message": last_detail[:500], "type": "upstream_error"}}
            self._send_json(last_status, err)
            self._record_receipt(
                payload, result, route="llm", status=last_status,
                ms=(time.perf_counter() - started) * 1000 if started else 0.0,
                fallback=fallback_note,
            )
            return

        extra = self._route_headers(result)
        if fallback_note:
            extra["X-Optimizer-Fallback"] = fallback_note

        if payload.get("stream") or endpoint == "generate_content_stream":
            self._relay_stream(upstream_resp, result, protocol=protocol, extra=extra)
            self._record_receipt(
                payload, result, route="llm", status=200,
                ms=(time.perf_counter() - started) * 1000 if started else 0.0,
                fallback=fallback_note,
            )
            return

        with upstream_resp:
            body = upstream_resp.read()
        try:
            parsed = json.loads(body.decode("utf-8"))
        except ValueError:
            self._send_json(502, {"error": {"message": "upstream returned non-JSON", "type": "upstream_error"}})
            self._record_receipt(
                payload, result, route="llm", status=502,
                ms=(time.perf_counter() - started) * 1000 if started else 0.0,
                fallback=fallback_note,
            )
            return

        self.pipeline.record_llm(result, parsed, protocol=protocol)
        # Provider prompt-cache: the upstream's own savings, disclosed not claimed.
        cached = upstream_cached_tokens(parsed, protocol=protocol)
        if cached:
            extra["X-Optimizer-Upstream-Cached-Tokens"] = str(cached)
        self._send_json(200, parsed, extra=extra)
        self._record_receipt(
            payload, result, route="llm", status=200,
            ms=(time.perf_counter() - started) * 1000 if started else 0.0,
            upstream_cached=cached, fallback=fallback_note,
        )

    def _relay_stream(
        self, upstream_resp: Any, result: PipelineResult, *, protocol: str = "openai",
        extra: Optional[Dict[str, str]] = None,
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        for key, value in {**self._route_headers(result), **(extra or {})}.items():
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
            # Usage frames (when the upstream sends them) carry the provider's
            # prompt-cache count — surfaced in metrics even though headers are
            # already on the wire.
            usage = _extract_streamed_usage(buffered, protocol)
            cached = usage_cached_tokens(usage, protocol=protocol)
            if cached:
                self.pipeline.metrics.record_upstream_cached(cached)
            # Cache the *answer text*, never the SSE framing.
            answer = _extract_streamed_text(buffered, protocol)
            if answer:
                self.pipeline.cache.set(result.cache_key, answer)


def _is_model_rejection(status: int, detail: str) -> bool:
    """True when upstream rejected the *model id* (retryable via fallback).

    Copilot returns 400 ``model_not_supported`` for plan-gated ids; OpenAI
    returns 404 ``model_not_found`` for unknown ones. Payload-shape errors
    (missing messages, bad temperature) must NOT trigger a fallback retry —
    only model-identity failures do.
    """
    if status not in (400, 404):
        return False
    if "model_not_supported" in detail or "model_not_found" in detail:
        return True
    lowered = detail.lower()
    return (
        "model" in lowered
        and ("does not exist" in lowered or "unsupported model" in lowered
             or "invalid model" in lowered or "no such model" in lowered)
    )


def _extract_streamed_usage(buffered: str, protocol: str) -> Dict[str, Any]:
    """Last usage object seen in relayed SSE frames (empty if none arrived).

    OpenAI sends usage only in the final chunk when ``stream_options.
    include_usage`` is set; Anthropic puts input usage in ``message_start``
    and output usage in ``message_delta``; Gemini repeats usageMetadata per
    frame. Taking the last-seen (merged for Anthropic) object covers all
    three without caring which convention the upstream followed.
    """
    usage: Dict[str, Any] = {}
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
            meta = obj.get("usageMetadata")
            if isinstance(meta, dict):
                usage = meta
        elif protocol == "anthropic":
            if obj.get("type") == "message_start":
                u = (obj.get("message") or {}).get("usage")
                if isinstance(u, dict):
                    usage.update(u)
            elif isinstance(obj.get("usage"), dict):
                usage.update(obj["usage"])
        elif isinstance(obj.get("usage"), dict):
            usage.update(obj["usage"])
    return usage


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
        # Rolling per-request receipts, shared across handler instances.
        self.receipts: Deque[Dict[str, Any]] = deque(maxlen=self.config.receipt_window)

        outer = self

        class Handler(_Handler):
            pipeline = outer.pipeline
            config = outer.config
            receipts = outer.receipts

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
    parser.add_argument("--cache-db", default=None, help="sqlite file for a persistent edge cache")
    parser.add_argument("--fallback-model", default=None, help="retry once with this model on model_not_supported")
    parser.add_argument("--log-requests", action="store_true", help="print one receipt line per request")
    args = parser.parse_args(argv)
    try:
        upstream, extra_headers, fallback_headers = resolve_upstream_flags(args)
    except ValueError as exc:
        parser.error(str(exc))

    pipeline = OptimizationPipeline.with_cdn(
        args.cdn,
        namespace=args.namespace,
        confidence_threshold=args.threshold,
        cache_db=args.cache_db,
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
            fallback_model=args.fallback_model,
            log_requests=args.log_requests,
        ),
        pipeline,
    )
    print(f"token-optimizer gateway on {proxy.base_url} -> {upstream}", flush=True)
    print(f"  cdn endpoint : {args.cdn or 'local LRU only'}", flush=True)
    print(f"  cache db     : {args.cache_db or 'memory only'}", flush=True)
    print(f"  fallback     : {args.fallback_model or 'none'}", flush=True)
    print(f"  jev threshold: {args.threshold}", flush=True)
    print(f"  dashboard    : http://{args.host}:{args.port}/dashboard", flush=True)
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        proxy.shutdown()


if __name__ == "__main__":
    main()
