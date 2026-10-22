"""MCP server (stdio): plug the optimizer into any MCP-capable agent.

    token-optimizer mcp           # or: python -m token_optimizer mcp

Register it once in the agent (see ``token-optimizer setup <agent> --mcp``)
and the agent gains tools that answer context questions *without* an LLM call:

  * ``optimize_context``  — CDM-compact a payload, report tokens saved
  * ``repo_index``        — structural repo map within a byte budget
  * ``cache_lookup``      — semantic cache probe (is this context already known?)
  * ``cache_store``       — seed the cache from the agent's own knowledge
  * ``session_metrics``   — live savings/ROI counters

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout (MCP stdio).
Protocol version: 2025-06-18.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional, TextIO

from .adapters import GatewayInfo
from .pipeline import OptimizationPipeline
from .repo_index import DEFAULT_IGNORE, build_index

__all__ = ["MCPServer", "serve_stdio"]

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "token-optimizer"

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "optimize_context",
        "description": (
            "Compact a chat payload through the Canonical Data Model and report "
            "before/after token counts plus a semantic cache probe. Never calls "
            "an LLM — use it to shrink context before sending, or to check "
            "whether an answer is already cached."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "messages": {
                    "type": "array",
                    "description": "OpenAI/Anthropic-shaped messages to compact.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["role", "content"],
                    },
                },
                "model": {"type": "string", "description": "Scope key for cache digesting."},
            },
            "required": ["messages"],
        },
    },
    {
        "name": "repo_index",
        "description": (
            "Generate a compact structural map of a repository (directory-grouped "
            "files + top-level symbols) within a byte budget. Load this instead "
            "of re-walking the tree with glob/ls tool calls."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Repo root (defaults to cwd)."},
                "max_bytes": {"type": "integer", "description": "Output budget (default 8192)."},
            },
        },
    },
    {
        "name": "cache_lookup",
        "description": (
            "Probe the semantic edge cache: does this exact or near-identical "
            "context already have a stored answer? Returns the cached text if found."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Canonical text to look up."},
                "semantic": {
                    "type": "boolean",
                    "description": "Use whitespace-insensitive semantic digest (default true).",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "cache_store",
        "description": "Seed the semantic cache with text the agent already knows.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "value": {"type": "string", "description": "Answer/value to store."},
            },
            "required": ["text", "value"],
        },
    },
    {
        "name": "session_metrics",
        "description": "Live token/cost savings for this optimizer process.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class MCPServer:
    """Request handler for one MCP session. I/O-agnostic (unit-testable)."""

    def __init__(self, pipeline: Optional[OptimizationPipeline] = None) -> None:
        self.pipeline = pipeline or OptimizationPipeline()
        self._initialized = False

    # ------------------------------------------------------------- dispatch
    def handle(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Process one JSON-RPC message; return None for notifications."""
        method = message.get("method", "")
        msg_id = message.get("id")
        is_notification = "id" not in message

        if method.startswith("notifications/"):
            if method == "notifications/initialized":
                self._initialized = True
            return None

        if method == "initialize":
            return self._result(msg_id, self._initialize(message.get("params") or {}))
        if method == "ping":
            return self._result(msg_id, {})
        if method == "tools/list":
            return self._result(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            params = message.get("params") or {}
            return self._result(msg_id, self._call_tool(params.get("name"), params.get("arguments") or {}))

        if is_notification:
            return None
        return self._error(msg_id, -32601, f"method not found: {method}")

    @staticmethod
    def _result(msg_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _initialize(params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": _version()},
            "instructions": (
                "Token-optimization tools. Call optimize_context before sending "
                "large payloads, and repo_index instead of walking the tree."
            ),
        }

    # --------------------------------------------------------------- tools
    def _call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            payload = self._dispatch(name, args)
        except Exception as exc:  # tool errors are results, not protocol errors
            return {
                "content": [{"type": "text", "text": f"{name} failed: {exc}"}],
                "isError": True,
            }
        return {"content": [{"type": "text", "text": payload}], "isError": False}

    def _dispatch(self, name: str, args: Dict[str, Any]) -> str:
        if name == "optimize_context":
            messages = args.get("messages") or []
            scope = {"model": args.get("model")} if args.get("model") else None
            record = self.pipeline.normalizer.fingerprint(messages, scope)
            decision = self.pipeline.router.evaluate(messages)
            cached = self.pipeline.cache.get(record.digest)
            return json.dumps(
                {
                    "digest": record.digest,
                    "original_tokens": record.original_tokens,
                    "compact_tokens": record.canonical_tokens,
                    "tokens_saved": record.saved_tokens,
                    "saved_percent": record.saved_pct,
                    "route": "cache" if cached else decision.route,
                    "cached_answer": cached,
                    "signals": list(decision.signals),
                    "compact_payload": json.loads(record.canonical),
                },
                indent=2,
            )

        if name == "repo_index":
            from pathlib import Path

            root = Path(args.get("root") or ".").resolve()
            max_bytes = int(args.get("max_bytes") or 8192)
            return build_index(root, DEFAULT_IGNORE, max_bytes)

        if name == "cache_lookup":
            text = args.get("text", "")
            use_semantic = args.get("semantic", True)
            key = (
                self.pipeline.cache.semantic_digest(text)
                if use_semantic
                else self.pipeline.cache.digest(text)
            )
            value = self.pipeline.cache.get(key)
            return json.dumps({"digest": key, "hit": value is not None, "value": value}, indent=2)

        if name == "cache_store":
            text = args.get("text", "")
            key = self.pipeline.cache.semantic_digest(text)
            self.pipeline.cache.set(key, args.get("value", ""))
            return json.dumps({"stored": True, "digest": key}, indent=2)

        if name == "session_metrics":
            return json.dumps(
                {"session": self.pipeline.metrics.as_dict(), "cache": self.pipeline.cache.stats()},
                indent=2,
            )

        raise ValueError(f"unknown tool: {name}")


def _version() -> str:
    try:
        from . import __version__

        return __version__
    except Exception:  # pragma: no cover
        return "0.0.0"


def serve_stdio(
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    pipeline: Optional[OptimizationPipeline] = None,
) -> None:
    """Blocking stdio loop. Logs go to stderr — stdout is protocol-only."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = MCPServer(pipeline)

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        try:
            response = server.handle(message)
        except Exception as exc:  # pragma: no cover - defensive
            response = {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32603, "message": str(exc)}}
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="token-optimizer mcp", description=__doc__)
    parser.add_argument("--cdn", default=None, help="remote edge-cache endpoint")
    parser.add_argument("--namespace", default="default")
    args = parser.parse_args(argv)

    pipeline = OptimizationPipeline.with_cdn(args.cdn, namespace=args.namespace)
    sys.stderr.write(f"{SERVER_NAME} MCP server on stdio (cdn={args.cdn or 'local'})\n")
    serve_stdio(pipeline=pipeline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
