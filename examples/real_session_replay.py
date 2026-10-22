#!/usr/bin/env python3
"""Real-repo example: run tokan against actual project content.

Three workflows, all using the INSTALLED tokan package and a live
``token-optimizer serve`` gateway, with real source files from psf/requests
as the context payloads (cloned on first run if missing):

  1. MCP stdio  — repo_index + optimize_context on real files
  2. Gateway    — a multi-turn coding-agent session replayed end-to-end
                  (compaction per turn, cache hits, deterministic local
                  answers) against a fake upstream standing in for the LLM
  3. JeV router — realistic agent queries classified local vs llm

    python examples/real_session_replay.py [--requests-root DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_CLONE = os.path.join(
    tempfile.gettempdir(), "tokan-examples", "requests"
)


def ensure_requests_clone(root):
    if os.path.isfile(os.path.join(root, "src", "requests", "adapters.py")):
        return
    os.makedirs(os.path.dirname(root), exist_ok=True)
    print(f"cloning psf/requests (shallow) -> {root}")
    subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet", "https://github.com/psf/requests", root],
        check=True,
    )


def read_files(root, rels):
    out = {}
    for rel in rels:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            out[rel] = fh.read()
    return out


# ------------------------------------------------------------------ 1. MCP
def mcp_example(cli, repo_root):
    print("\n--- 1. MCP stdio: repo_index + optimize_context on real files ---")
    files = read_files(repo_root, ["src/requests/adapters.py"])
    mcp = subprocess.Popen(
        [cli, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )

    def rpc(obj):
        mcp.stdin.write(json.dumps(obj) + "\n")
        mcp.stdin.flush()
        return json.loads(mcp.stdout.readline())

    rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "real-example", "version": "0"}}})
    mcp.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    mcp.stdin.flush()

    r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "repo_index", "arguments": {"root": repo_root}}})
    text = r["result"]["content"][0]["text"]
    print(f"repo_index: {len(text)} chars for {repo_root}")
    for line in text.splitlines()[:6]:
        print("   ", line)

    payload = [
        {"role": "system", "content": "You are an expert Python engineer."},
        {"role": "user", "content": "Add brotli content-encoding support. "
         "Here is requests/adapters.py for context:\n\n" + files["src/requests/adapters.py"]},
    ]
    t0 = time.time()
    r = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "optimize_context", "arguments": {"messages": payload}}})
    dt = (time.time() - t0) * 1000
    text = r["result"]["content"][0]["text"]
    print(f"\noptimize_context on a real {len(files['src/requests/adapters.py']):,}-char "
          f"file ({dt:.0f}ms):")
    print("   " + text.replace("\n", "\n   ")[:600])
    mcp.terminate()


# ------------------------------------------------------- 2. gateway session
class FakeLLM(BaseHTTPRequestHandler):
    """Stands in for the real upstream LLM (no API key needed)."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        body = json.dumps({
            "id": "cmpl-real", "object": "chat.completion", "model": "fake-llm",
            "choices": [{"index": 0, "message": {"role": "assistant",
                         "content": "I would add a brotli decoder branch in "
                                    "HTTPAdapter.send and set Accept-Encoding."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10000, "completion_tokens": 25, "total_tokens": 10025},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def session_example(cli, repo_root):
    print("\n--- 2. Live gateway: multi-turn agent session on real code ---")
    import tokan
    from tokan import OptimizationPipeline

    files = read_files(repo_root, [
        "src/requests/adapters.py", "src/requests/sessions.py",
        "src/requests/models.py", "src/requests/api.py",
    ])
    index_map = subprocess.run(
        [cli, "index", repo_root, "--out", "-"], capture_output=True, text=True, check=True
    ).stdout

    up = _Server(("127.0.0.1", 0), FakeLLM)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    port = free_port()
    gw = subprocess.Popen(
        [cli, "serve", "--port", str(port), "--host", "127.0.0.1",
         "--upstream", f"http://127.0.0.1:{up.server_address[1]}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{base}/healthz", timeout=2)
            break
        except Exception:
            time.sleep(0.1)
    print(f"gateway {base} -> fake LLM; package {tokan.__version__}")

    # A realistic session: each turn RESENDS the full history, as agents do.
    history = [
        {"role": "system", "content": "You are an expert Python engineer."},
        {"role": "user", "content": "I want to add brotli content-encoding "
         "support to requests. Repo map:\n\n" + index_map},
        {"role": "assistant", "content": "Let me look at the adapters."},
        {"role": "user", "content": "Reading the relevant files:\n\n"
         + files["src/requests/adapters.py"] + "\n\n" + files["src/requests/sessions.py"]},
    ]
    pipeline = OptimizationPipeline.with_cdn()
    chat = f"{base}/v1/chat/completions"

    def send(msgs):
        req = urllib.request.Request(
            chat, data=json.dumps({"model": "gpt-4.1", "messages": msgs}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            json.loads(resp.read())
            return dict(resp.headers)

    turns = [
        ("turn 1: task + repo map", history[:2]),
        ("turn 2: + real adapters.py & sessions.py dumps", history),
        ("turn 3: + follow-up question (history resent)", history + [
            {"role": "user", "content": "Now, where exactly should the decoder "
             "branch go? What is on line number 42 of adapters.py? context:\n"
             + files["src/requests/adapters.py"][:2000]}]),
    ]
    print(f"\n{'turn':<44} {'route':>7} {'raw tokens':>11} {'canonical':>10} "
          f"{'saved':>8} {'ms':>7}")
    for name, msgs in turns:
        t0 = time.time()
        headers = send(msgs)
        ms = (time.time() - t0) * 1000
        rec = pipeline.normalizer.fingerprint(msgs, {})
        saved = 1 - rec.canonical_tokens / max(1, rec.original_tokens)
        print(f"{name:<44} {headers.get('X-Optimizer-Route', '?'):>7} "
              f"{rec.original_tokens:>11,} {rec.canonical_tokens:>10,} "
              f"{saved * 100:>7.1f}% {ms:>7.1f}")

    # Exact resend of turn 2 -> semantic cache serves it, LLM never runs.
    t0 = time.time()
    headers = send(history)
    ms = (time.time() - t0) * 1000
    print(f"{'turn 4: exact resend of turn 2 (cache probe)':<44} "
          f"{headers.get('X-Optimizer-Route', '?'):>7} {'':>11} {'':>10} {'':>8} {ms:>7.1f}")

    with urllib.request.urlopen(f"{base}/metrics", timeout=5) as resp:
        metrics = json.loads(resp.read())
    session, cache = metrics.get("session", {}), metrics.get("cache", {})
    print(f"\nsession: {session.get('requests')} requests | "
          f"{session.get('cache_hits')} cache hits | "
          f"{session.get('local_solves')} local solves | "
          f"{session.get('llm_calls')} LLM calls")
    print(f"tokens saved: {session.get('tokens_saved', 0):,} "
          f"({session.get('saved_pct', 0)}%)  cache: {cache}")
    gw.terminate()
    up.shutdown()


# ---------------------------------------------------------- 3. JeV battery
def router_example(repo_root):
    print("\n--- 3. JeV router: realistic queries on real file content ---")
    from tokan import OptimizationPipeline

    snippet = "\n".join(
        read_files(repo_root, ["src/requests/adapters.py"])["src/requests/adapters.py"]
        .splitlines()[:15]
    )
    pipeline = OptimizationPipeline.with_cdn()
    queries = [
        "What is on line number 8 of the paste? context:\n" + snippet,
        "How many occurrences of 'class' are in the paste? context:\n" + snippet,
        "list all the imports",
        "Is 2 * 34 larger than the session timeout?",
        "Refactor adapters.py to support HTTP/2 and explain the trade-offs step by step.",
        "Debug this: my session hangs after a ConnectionError. Walk me through the retry loop.",
        "What's the best way to add brotli support? Compare the options.",
        "Plan a migration from requests to httpx across the codebase.",
    ]
    print(f"{'query':<52} {'route':>5} {'conf':>5}  signals")
    counts = {"local": 0, "llm": 0}
    for q in queries:
        r = pipeline.prepare({"model": "gpt-4.1", "messages": [{"role": "user", "content": q}]})
        counts[r.route] = counts.get(r.route, 0) + 1
        shown = q.splitlines()[0]
        sigs = ",".join(r.decision.signals)
        print(f"{shown[:51]:<52} {r.route:>5} {r.decision.confidence:>5.2f}  {sigs}")
    print(f"-> {counts['local']} served locally (0 tokens), {counts['llm']} forwarded")
    print("   note: a local-leaning query still forwards unless a deterministic")
    print("   executor actually answered it (no_executor_answer -> llm), so the")
    print("   gateway never fabricates an answer.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests-root", default=DEFAULT_CLONE)
    ap.add_argument("--cli", default="token-optimizer")
    args = ap.parse_args()

    ensure_requests_clone(args.requests_root)
    import tokan
    print(f"tokan {tokan.__version__} from {tokan.__file__}")
    mcp_example(args.cli, args.requests_root)
    session_example(args.cli, args.requests_root)
    router_example(args.requests_root)


if __name__ == "__main__":
    main()
