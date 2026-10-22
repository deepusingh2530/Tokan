"""Pattern 1: run the transparent gateway and point any agent at it.

    python examples/run_proxy.py --port 4040 --upstream https://api.openai.com/v1

Then, without touching agent code:

    # OpenAI-compatible agents (Aider, Codex CLI, Cursor, custom loops):
    export OPENAI_BASE_URL=http://127.0.0.1:4040/v1
    export OPENAI_API_KEY=sk-...        # read by the proxy, not the agent

    # Claude Code with an OpenAI-compatible relay, or wire the same
    # pipeline behind your Anthropic base URL.

Verify:
    curl -s http://127.0.0.1:4040/healthz
    curl -s http://127.0.0.1:4040/metrics | python3 -m json.tool
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tokan import OptimizationPipeline
from tokan.proxy import ProxyConfig, TokenOptimizerProxy


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4040)
    parser.add_argument("--upstream", default="https://api.openai.com/v1")
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"))
    parser.add_argument("--cdn", default=None, help="remote edge endpoint, e.g. https://edge.example.com/v1")
    parser.add_argument("--namespace", default="my-project")
    parser.add_argument("--threshold", type=float, default=0.88, help="JeV confidence threshold")
    parser.add_argument("--price", type=float, default=2.50, help="USD per 1M tokens (for ROI report)")
    args = parser.parse_args()

    pipeline = OptimizationPipeline.with_cdn(
        args.cdn,
        namespace=args.namespace,
        confidence_threshold=args.threshold,
        price_per_million_tokens=args.price,
    )
    proxy = TokenOptimizerProxy(
        ProxyConfig(
            upstream_base=args.upstream,
            api_key=args.api_key,
            host=args.host,
            port=args.port,
        ),
        pipeline,
    ).start()

    print(f"  gateway    : {proxy.base_url}")
    print(f"  upstream   : {args.upstream}")
    print(f"  edge cache : {args.cdn or 'local LRU (pass --cdn for the remote tier)'}")
    print(f"  JeV thresh : {args.threshold}")
    print(f"  metrics    : http://{args.host}:{args.port}/metrics")
    print("\nSet OPENAI_BASE_URL to the gateway URL in your agent, Ctrl-C to stop.")

    import time

    try:
        while True:
            time.sleep(5)
            print(f"\r{pipeline.metrics.report()}", end="", flush=True)
    except KeyboardInterrupt:
        print(f"\n\nFinal: {pipeline.metrics.report()}")
        proxy.shutdown()


if __name__ == "__main__":
    main()
