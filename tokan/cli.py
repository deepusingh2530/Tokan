"""Unified CLI: ``token-optimizer <command>`` (also ``python -m tokan``).

    token-optimizer list                 # adapters + what's installed here
    token-optimizer setup claude-code    # copy-paste wiring for one agent
    token-optimizer setup codex --json   # machine-readable snippets
    token-optimizer serve --port 4040    # run the gateway (both wire protocols)
    token-optimizer mcp                  # MCP stdio server for an agent
    token-optimizer index . --out AGENTS.md
    token-optimizer stats --port 4040    # pull /metrics from a running gateway
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .adapters import GatewayInfo, all_adapters, detect_all, get


def _gateway_info(args: argparse.Namespace) -> GatewayInfo:
    exe = sys.executable or "python3"
    return GatewayInfo.for_gateway(
        args.host,
        args.port,
        mcp_command=(exe, "-m", "tokan", "mcp"),
    )


# --------------------------------------------------------------------- list
def cmd_list(args: argparse.Namespace) -> int:
    rows = detect_all()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{'ID':<20} {'PROTOCOLS':<24} {'HERE':<8} NAME")
    print("─" * 78)
    for row in rows:
        mark = "✓" if row["detected"] else "·"
        protos = "+".join(row["protocols"])
        print(f"{row['id']:<20} {protos:<24} {mark:<8} {row['name']}")
    installed = [r for r in rows if r["detected"]]
    print("─" * 78)
    print(f"{len(installed)}/{len(rows)} detected on this machine.")
    if installed:
        print("Next: token-optimizer setup <id>   (e.g. "
              + f"token-optimizer setup {installed[0]['id']})")
    return 0


# -------------------------------------------------------------------- setup
def cmd_setup(args: argparse.Namespace) -> int:
    adapter = get(args.agent)
    if adapter is None:
        known = ", ".join(a.id for a in all_adapters())
        print(f"unknown agent '{args.agent}'. Known: {known}", file=sys.stderr)
        return 2
    gw = _gateway_info(args)
    snippets = adapter.all_snippets(gw, include_mcp=not args.no_mcp)

    if args.json:
        print(
            json.dumps(
                [
                    {"kind": s.kind, "target": s.target, "body": s.body, "note": s.note}
                    for s in snippets
                ],
                indent=2,
            )
        )
        return 0

    how = adapter.detect()
    print(f"\n{adapter.display_name}  [{'+'.join(adapter.protocols)}]")
    print(f"  {adapter.summary}")
    print(f"  detected: {how or 'not found on this machine (snippets still valid)'}\n")
    for snippet in snippets:
        print(snippet.render())
        print()
    if "openai" in adapter.protocols:
        print(f"Gateway OpenAI base URL : {gw.openai_base_url}")
    if "anthropic" in adapter.protocols:
        print(f"Gateway Anthropic root  : {gw.anthropic_base_url}")
    if adapter.supports_mcp:
        print(f"MCP command             : {' '.join(gw.mcp_command)}")
    print("\nStart the gateway first:  token-optimizer serve")
    return 0


# -------------------------------------------------------------------- serve
def cmd_serve(args: argparse.Namespace) -> int:
    from .proxy.interceptor import ProxyConfig, TokenOptimizerProxy, resolve_upstream_flags

    try:
        upstream, extra_headers, fallback_headers = resolve_upstream_flags(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    pipeline = _build_pipeline(args)
    config = ProxyConfig(
        upstream_base=upstream,
        api_key=args.api_key,
        anthropic_upstream=args.anthropic_upstream,
        anthropic_api_key=args.anthropic_api_key,
        host=args.host,
        port=args.port,
        gemini_upstream=args.gemini_upstream,
        gemini_api_key=args.gemini_api_key,
        extra_headers=extra_headers,
        fallback_headers=fallback_headers,
        fallback_model=getattr(args, "fallback_model", None),
        log_requests=getattr(args, "log_requests", False),
        prompt_cache_key=getattr(args, "prompt_cache_key", False),
        conversation_db=getattr(args, "conversation_db", None),
    )
    proxy = TokenOptimizerProxy(config, pipeline).start()
    gw = _gateway_info(args)

    print(f"token-optimizer gateway  {gw.origin}", flush=True)
    print(f"  openai wire   : {gw.openai_base_url}/chat/completions -> {upstream}", flush=True)
    print(f"  anthropic wire: {gw.anthropic_base_url}/v1/messages   -> {args.anthropic_upstream}", flush=True)
    print(f"  gemini wire   : {gw.origin}/v1beta/models/*/generateContent -> {args.gemini_upstream}", flush=True)
    print(f"  mcp command   : {' '.join(gw.mcp_command)}", flush=True)
    print(f"  edge cache    : {args.cdn or 'local LRU only'}"
          + (f" + sqlite({getattr(args, 'cache_db', None)})" if getattr(args, "cache_db", None) else ""), flush=True)
    print(f"  fallback model: {getattr(args, 'fallback_model', None) or 'none'}", flush=True)
    print(f"  jev threshold : {args.threshold}", flush=True)
    print(f"  metrics       : {gw.origin}/metrics", flush=True)
    print(f"  dashboard     : {gw.origin}/dashboard", flush=True)
    print("\nWire an agent:  token-optimizer setup <agent>", flush=True)

    import time

    try:
        while True:
            time.sleep(5)
            print(f"\r{pipeline.metrics.report()}", end="", flush=True)
    except KeyboardInterrupt:
        print(f"\n\nFinal: {pipeline.metrics.report()}")
        proxy.shutdown()
    return 0


def _build_pipeline(args: argparse.Namespace):
    from .pipeline import OptimizationPipeline

    pipeline = OptimizationPipeline.with_cdn(
        getattr(args, "cdn", None),
        namespace=getattr(args, "namespace", "default"),
        confidence_threshold=getattr(args, "threshold", 0.88),
        price_per_million_tokens=getattr(args, "price", 2.50),
        cache_db=getattr(args, "cache_db", None),
    )
    # Context Governor (M4): opt-in. Only ever acts when a request would
    # overflow the model's context window — see tokan/governor.py.
    if getattr(args, "governor", False):
        from .governor import ContextGovernor

        pipeline.governor = ContextGovernor(
            context_window=getattr(args, "context_window", None),
            output_reserve=getattr(args, "output_reserve", 4096),
            recent_window=getattr(args, "recent_window", 6),
        )
    # Semantic answer cache (M4): opt-in, conservative. Stores answers in
    # memory only — a restart clears it, which is the safe direction.
    if getattr(args, "semantic_cache", False):
        from .governor import SemanticAnswerCache

        pipeline.semantic_cache = SemanticAnswerCache(
            enabled=True,
            threshold=getattr(args, "semantic_threshold", 0.97),
        )
    return pipeline


# ---------------------------------------------------------------------- mcp
def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp import main as mcp_main

    extra: List[str] = []
    if args.cdn:
        extra += ["--cdn", args.cdn]
    if args.namespace:
        extra += ["--namespace", args.namespace]
    return mcp_main(extra)


# -------------------------------------------------------------------- index
def cmd_index(args: argparse.Namespace) -> int:
    from pathlib import Path

    from .repo_index import DEFAULT_IGNORE, SymbolCache, build_graph, build_index

    root = Path(args.root).resolve()
    ignore = DEFAULT_IGNORE | set(args.ignore or [])
    cache = SymbolCache(root / args.cache) if args.cache else None
    content = build_index(root, ignore, args.max_bytes, cache=cache)
    if args.out == "-":
        print(content)
    else:
        out_path = root / args.out
        out_path.write_text(content, encoding="utf-8")
        extra = ""
        if cache is not None:
            extra = f", cache {cache.hits} hit/{cache.misses} miss"
        print(f"wrote {out_path} ({len(content.encode())} bytes, budget {args.max_bytes}{extra})")
    if args.graph_out:
        import json

        graph = build_graph(root, ignore, cache=cache)
        graph_path = root / args.graph_out
        graph_path.write_text(json.dumps(graph.to_json(), indent=2), encoding="utf-8")
        stats = graph.stats()
        print(
            f"wrote {graph_path} ({stats['symbols']} symbols, "
            f"{stats['reference_edges']} refs, {stats['import_edges']} imports, "
            f"{stats['test_edges']} test edges)"
        )
    return 0


# -------------------------------------------------------------------- stats
def cmd_stats(args: argparse.Namespace) -> int:
    import urllib.request

    origin = f"http://{args.host}:{args.port}"
    try:
        with urllib.request.urlopen(f"{origin}/metrics", timeout=3) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        print(f"gateway unreachable at {origin}: {exc}", file=sys.stderr)
        print("Start it with: token-optimizer serve", file=sys.stderr)
        return 1
    session = data.get("session", {})
    cache = data.get("cache", {})
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    print(f"requests      : {session.get('requests', 0)}")
    print(f"  cache hits  : {session.get('cache_hits', 0)}")
    print(f"  local solves: {session.get('local_solves', 0)}")
    print(f"  llm calls   : {session.get('llm_calls', 0)}")
    print(f"tokens saved  : {session.get('tokens_saved', 0)} ({session.get('saved_pct', 0)}%)")
    print(f"cost saved    : ${session.get('cost_saved_usd', 0)}")
    print(f"cache hit rate: {cache.get('hit_rate', 0)} ({cache.get('entries', 0)} entries)")
    ratio = session.get("provider_cache_ratio", 0) or 0
    print(f"provider cache: {session.get('upstream_cached_tokens', 0)} cached tokens "
          f"({ratio:.0%} of prompt, upstream's own)")
    print(f"prefix reuse  : {session.get('prefix_reused_tokens', 0)} tokens "
          f"(base the provider discounts; kept stable by Tokan)")
    if session.get("doc_questions"):
        print(f"doc Q&A       : {session.get('doc_questions', 0)} questions over "
              f"{session.get('doc_ingests', 0)} doc(s), avoided "
              f"{session.get('doc_tokens_avoided', 0)} tokens "
              f"({session.get('doc_avoided_pct', 0)}% vs full-doc resend)")
    print(f"fallbacks     : {session.get('fallbacks', 0)}")
    return 0


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="token-optimizer",
        description="Token-frugality middleware for AI coding agents.",
    )
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="list agent adapters and local detection")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_setup = sub.add_parser("setup", help="print wiring instructions for one agent")
    p_setup.add_argument("agent", help="adapter id (see `token-optimizer list`)")
    p_setup.add_argument("--host", default="127.0.0.1")
    p_setup.add_argument("--port", type=int, default=4040)
    p_setup.add_argument("--json", action="store_true", help="machine-readable snippets")
    p_setup.add_argument("--no-mcp", action="store_true", help="omit the MCP snippet")
    p_setup.set_defaults(func=cmd_setup)

    def add_gateway_flags(p: argparse.ArgumentParser) -> None:
        from .proxy.interceptor import add_upstream_flags

        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=4040)
        p.add_argument("--upstream", default="https://api.openai.com/v1")
        p.add_argument("--api-key", default=None, help="OpenAI-compatible upstream key")
        p.add_argument("--anthropic-upstream", default="https://api.anthropic.com")
        p.add_argument("--anthropic-api-key", default=None, help="Anthropic upstream key")
        p.add_argument("--gemini-upstream", default="https://generativelanguage.googleapis.com")
        p.add_argument("--gemini-api-key", default=None, help="Gemini upstream key")
        add_upstream_flags(p)
        p.add_argument("--cdn", default=None, help="remote edge-cache endpoint")
        p.add_argument("--namespace", default="default")
        p.add_argument("--threshold", type=float, default=0.88)
        p.add_argument("--price", type=float, default=2.50, help="USD per 1M tokens")
        p.add_argument("--cache-db", default=None,
                       help="sqlite file for a persistent edge cache (survives restarts)")
        p.add_argument("--fallback-model", default=None,
                       help="retry once with this model on model_not_supported/model_not_found")
        p.add_argument("--log-requests", action="store_true",
                       help="print one receipt line per request (route, tokens, latency)")
        p.add_argument("--prompt-cache-key", action="store_true",
                       help="inject a stable prompt_cache_key (session id) into OpenAI-wire "
                            "payloads so the provider routes the conversation to one prompt "
                            "cache (support varies by provider; off by default)")
        p.add_argument("--conversation-db", default=None,
                       help="sqlite file for the append-only conversation store "
                            "(observe-only memory: records events + manifests, never serves them)")
        p.add_argument("--governor", action="store_true",
                       help="Context Governor: when a request would overflow the model's "
                            "context window, shed low-value context (never instructions, "
                            "tool schemas or the user's ask) instead of forwarding a payload "
                            "the provider would reject with a 400. Off by default.")
        p.add_argument("--context-window", type=int, default=None,
                       help="override the built-in per-model context window (tokens); "
                            "set when your deployment's real limit differs from the table")
        p.add_argument("--output-reserve", type=int, default=4096,
                       help="completion tokens to reserve before admitting input "
                            "(a request's own max_tokens wins; default 4096)")
        p.add_argument("--recent-window", type=int, default=6,
                       help="trailing messages treated as the high-value working set (default 6)")
        p.add_argument("--semantic-cache", action="store_true",
                       help="Semantic answer cache: reuse a near-identical previous answer "
                            "for self-contained factual questions. Conservative + opt-in: "
                            "only matches above --semantic-threshold are served.")
        p.add_argument("--semantic-threshold", type=float, default=0.97,
                       help="risk threshold for semantic reuse (default 0.97 = near-paraphrase only)")

    p_serve = sub.add_parser("serve", help="run the optimization gateway")
    add_gateway_flags(p_serve)
    p_serve.set_defaults(func=cmd_serve)

    p_mcp = sub.add_parser("mcp", help="run the MCP server on stdio")
    p_mcp.add_argument("--cdn", default=None)
    p_mcp.add_argument("--namespace", default="default")
    p_mcp.set_defaults(func=cmd_mcp)

    p_index = sub.add_parser("index", help="build a compact repo map (.md mapper)")
    p_index.add_argument("root", nargs="?", default=".")
    p_index.add_argument("--out", default="AGENTS.md", help="'-' prints to stdout")
    p_index.add_argument("--max-bytes", type=int, default=8192)
    p_index.add_argument("--ignore", action="append", default=[])
    p_index.add_argument(
        "--cache", default=None, metavar="PATH",
        help="incremental symbol cache (relative to root): unchanged files are "
             "not re-read on rebuilds (e.g. .git/tokan-index.json)",
    )
    p_index.add_argument(
        "--graph-out", default=None, metavar="PATH",
        help="also write the relationship graph (definitions/references/imports "
             "tests) as JSON, relative to root (e.g. .git/tokan-graph.json)",
    )
    p_index.set_defaults(func=cmd_index)

    p_stats = sub.add_parser("stats", help="metrics from a running gateway")
    p_stats.add_argument("--host", default="127.0.0.1")
    p_stats.add_argument("--port", type=int, default=4040)
    p_stats.add_argument("--json", action="store_true")
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
