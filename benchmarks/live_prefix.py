#!/usr/bin/env python3
"""Live multi-turn prefix-reuse check against the real Copilot upstream.

Starts a Tokan gateway (--copilot) and drives a growing coding conversation,
then prints each turn's prefix reuse (measured by Tokan) and the provider's
real cached_tokens (read from Copilot's usage block). Reproduces the honest
claim that the shared prefix is stable and the provider caches it.

    PYTHONPATH=. python3 benchmarks/live_prefix.py --turns 5
"""
import argparse
import json
import os
import sys
import time
import subprocess
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _post(url, payload):
    req = Request(url, data=json.dumps(payload).encode(),
                  headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    try:
        with urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read()), (time.perf_counter() - t0) * 1000
    except HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), (time.perf_counter() - t0) * 1000


def _conv(turn):
    msgs = [{"role": "system", "content": "You are a build-analysis agent. Be terse."}]
    for i in range(1, turn + 1):
        log = "\n".join(f"[build] step {j}: compiling module_{j%20} ... ok" for j in range(120))
        msgs.append({"role": "user",
                     "content": f"Change {i} build log:\n```\n{log}\n```\nDid it pass?"})
        msgs.append({"role": "assistant", "content": f"Yes, change {i} built cleanly."})
    return msgs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", type=int, default=5)
    ap.add_argument("--port", type=int, default=4099)
    args = ap.parse_args()

    token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
    log = open("/tmp/live-prefix-gw.log", "w")
    gw = subprocess.Popen(
        [sys.executable, "-m", "tokan", "serve", "--copilot", "--api-key", token,
         "--port", str(args.port), "--log-requests",
         "--fallback-model", "gpt-4o-mini"],
        stdout=log, stderr=subprocess.STDOUT,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    time.sleep(4)
    base = f"http://127.0.0.1:{args.port}/v1/chat/completions"
    try:
        print(f"turn │ prompt │ cached │ prefix_reuse │ full_price │ ms")
        print("─────┼────────┼────────┼──────────────┼────────────┼──────")
        for t in range(1, args.turns + 1):
            status, body, ms = _post(base, {"model": "gpt-4o-mini", "messages": _conv(t)})
            u = body.get("usage", {})
            cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
            prompt = u.get("prompt_tokens", 0)
            # prefix reuse from the gateway /metrics
            with urlopen(f"http://127.0.0.1:{args.port}/metrics", timeout=5) as r:
                m = json.loads(r.read())["session"]
            print(f"{t:>4} │ {prompt:>6} │ {cached:>6} │ {m.get('prefix_reused_tokens',0):>12} │ "
                  f"{prompt-cached:>10} │ {ms:.0f}")
        print(f"\nfinal metrics: cache_ratio={m.get('provider_cache_ratio',0):.0%} "
              f"prefix_reused={m.get('prefix_reused_tokens',0)} "
              f"upstream_cached={m.get('upstream_cached_tokens',0)}")
    finally:
        gw.terminate()
        gw.wait(timeout=5)


if __name__ == "__main__":
    main()
