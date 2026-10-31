"""MCP server (stdio): plug the optimizer into any MCP-capable agent.

    token-optimizer mcp           # or: python -m tokan mcp

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
from .cdm import estimate_tokens
from .pipeline import OptimizationPipeline
from .repo_index import DEFAULT_IGNORE, build_graph, build_index

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
        "name": "repo_graph",
        "description": (
            "Relationship graph over a repository's top-level symbols: which "
            "file defines each symbol, which files reference it, Python import "
            "edges, and which test files cover each source file. Use instead of "
            "grepping when asked 'where is X defined / used / tested'. Walks "
            "the repo in one pass; output is budgeted and reports truncation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Repo root (defaults to cwd)."},
                "max_bytes": {"type": "integer", "description": "Output budget (default 24000)."},
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
    {
        "name": "evidence",
        "description": (
            "Ranked evidence units across ingested documents AND the repo "
            "graph in one list: the chunks and symbols/paths most relevant to "
            "a question, each with source labels and token counts. Pass 'root' "
            "to include repo-symbol evidence (walks the repo in one pass); "
            "without it, only ingested documents are searched. Use when a "
            "question spans docs and code."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The question."},
                "top_k": {"type": "integer", "description": "Units to return (default 5, max 50)."},
                "doc_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Restrict doc evidence to these ids (default: all).",
                },
                "root": {
                    "type": "string",
                    "description": "Repo root for symbol/path evidence (optional; docs-only without it).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "doc_ingest",
        "description": (
            "Ingest a long document once (chunk + TF-IDF index + extractive "
            "summary) and get back a doc_id. Reuse the doc_id with doc_search "
            "instead of re-reading the whole file on every question — this is "
            "the Doc Governor: ingest once, ask many."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Full document text."},
                "doc_id": {
                    "type": "string",
                    "description": "Optional explicit id (defaults to a content hash).",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "doc_search",
        "description": (
            "Retrieve the summary + top-k relevant excerpts of an ingested "
            "document for a question. Returns far fewer tokens than re-reading "
            "the full document; answer from the excerpts (they include their "
            "chunk numbers for citation). The answer includes a coverage score: "
            "below 0.35 the document likely cannot answer the question — look "
            "elsewhere rather than guessing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "query": {"type": "string", "description": "Question to retrieve for."},
                "top_k": {"type": "integer", "description": "Excerpts to return (default 3, max 10)."},
            },
            "required": ["doc_id", "query"],
        },
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

        if name == "repo_graph":
            from pathlib import Path

            root = Path(args.get("root") or ".").resolve()
            max_bytes = max(1024, int(args.get("max_bytes") or 24000))
            data = build_graph(root, DEFAULT_IGNORE).to_json()
            data["truncated"] = False  # present before sizing so the budget holds
            original = json.loads(json.dumps(data))  # pristine copy to shrink from
            cap: Any = None
            truncated = False
            while len(json.dumps(data, indent=2).encode()) > max_bytes and (cap is None or cap > 1):
                truncated = True
                cap = 256 if cap is None else max(1, cap // 2)
                data["definitions"] = dict(list(original["definitions"].items())[:cap])
                for key in ("references", "imports", "tests"):
                    data[key] = {k: v[:cap] for k, v in list(original[key].items())[:cap]}
            data["truncated"] = truncated
            return json.dumps(data, indent=2)

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

        if name == "doc_ingest":
            text = args.get("text", "")
            doc_id = args.get("doc_id") or None
            doc = self.pipeline.documents.ingest(text, doc_id=doc_id)
            self.pipeline.metrics.record_ingest(doc.chunk_count, doc.total_tokens)
            return json.dumps(
                {
                    "doc_id": doc.doc_id,
                    "chunks": doc.chunk_count,
                    "total_tokens": doc.total_tokens,
                    "summary_tokens": doc.summary_tokens,
                    "summary": doc.summary,
                    "next": f'doc_search {{"doc_id": "{doc.doc_id}", "query": "..."}}',
                },
                indent=2,
            )

        if name == "doc_search":
            doc_id = str(args.get("doc_id") or "")
            query = str(args.get("query") or "")
            if not doc_id or not query:
                raise ValueError("doc_search requires 'doc_id' and 'query'")
            doc = self.pipeline.documents.get(doc_id)
            if doc is None:
                raise ValueError(
                    f"unknown doc_id {doc_id!r} — ingest it first with doc_ingest"
                )
            try:
                top_k = int(args.get("top_k") or 3)
            except (TypeError, ValueError):
                top_k = 3
            top_k = max(1, min(10, top_k))
            chunks, idxs = doc.retrieve(query, top_k=top_k)
            from .doc import LOW_COVERAGE

            coverage = round(doc.coverage(query), 3)  # M5 quality disclosure
            return json.dumps(
                {
                    "doc_id": doc.doc_id,
                    "query": query,
                    "full_document_tokens": doc.total_tokens,
                    "coverage": coverage,
                    "low_coverage": coverage < LOW_COVERAGE,
                    "summary": doc.summary,
                    "summary_tokens": doc.summary_tokens,
                    "excerpts": [
                        {"chunk": i + 1, "text": c}
                        for i, c in zip(idxs, chunks)
                    ],
                    "retrieved_tokens": estimate_tokens(doc.summary) + sum(
                        estimate_tokens(c) for c in chunks
                    ),
                },
                indent=2,
            )

        if name == "evidence":
            from pathlib import Path as _Path

            from .evidence import EvidenceRetriever

            query = str(args.get("query") or "").strip()
            if not query:
                raise ValueError("evidence requires 'query'")
            try:
                top_k = int(args.get("top_k") or 5)
            except (TypeError, ValueError):
                top_k = 5
            top_k = max(1, min(50, top_k))
            doc_ids = args.get("doc_ids")
            if doc_ids is not None and not isinstance(doc_ids, list):
                raise ValueError("'doc_ids' must be a list of doc ids")
            # Repo evidence is opt-in via 'root' (MCP runs with the agent's own
            # trust, same as repo_index; the walk happens once per call).
            graph = None
            root = str(args.get("root") or "").strip()
            if root:
                graph = build_graph(_Path(root).expanduser().resolve(), DEFAULT_IGNORE)
            retriever = EvidenceRetriever(self.pipeline.documents, graph)
            units = retriever.search(query, top_k=top_k, doc_ids=doc_ids)
            self.pipeline.metrics.record_evidence(len(units))
            return json.dumps(
                {
                    "query": query,
                    "units": [u.as_dict() for u in units],
                    "count": len(units),
                    "tokens": sum(u.tokens for u in units),
                    "scopes": ["doc", "repo"] if graph is not None else ["doc"],
                },
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
