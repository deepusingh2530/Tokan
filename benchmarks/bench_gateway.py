#!/usr/bin/env python3
"""Performance validation harness for the published tokan package.

Validates the INSTALLED package (pipx/pip venv) end-to-end: a real
``token-optimizer serve`` subprocess in front of a fake upstream with
simulated LLM latency, plus in-process CDM/cache microbenchmarks, an MCP
stdio round-trip, CLI startup, and gateway memory.

    python3 -m venv venv && venv/bin/pip install tokan
    python benchmarks/bench_gateway.py --cli venv/bin/token-optimizer

Scenarios are checked against explicit targets; the process exits non-zero
if any FAIL, so it can run in CI. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ------------------------------------------------------------------ helpers

RESULTS = []  # (name, value, unit, target, ok)


def record(name, value, unit, target, ok):
    RESULTS.append((name, value, unit, target, ok))
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {name:<46} {value:>10.2f} {unit}   (target {target})")


def pct(values, p):
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1)))))
    return ordered[k]


def timed(n, fn):
    """Run fn n times, return list of per-call milliseconds."""
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000.0)
    return out


def free_port():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------- fake upstream

LATENCY = [0.05]  # seconds; mutable per phase


class FakeUpstream(BaseHTTPRequestHandler):
    hits = 0
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        with FakeUpstream.lock:
            FakeUpstream.hits += 1
            n = FakeUpstream.hits
        time.sleep(LATENCY[0])

        if payload.get("stream"):
            frames = []
            for i in range(10):
                chunk = {
                    "id": "cmpl-stream",
                    "object": "chat.completion.chunk",
                    "model": payload.get("model", "fake"),
                    "choices": [
                        {"index": 0, "delta": {"content": f"tok{i} "}, "finish_reason": None}
                    ],
                }
                frames.append(f"data: {json.dumps(chunk)}\n\n")
            frames.append("data: [DONE]\n\n")
            body = "".join(frames).encode()
            ctype = "text/event-stream"
        else:
            body = json.dumps(
                {
                    "id": f"cmpl-{n}",
                    "object": "chat.completion",
                    "model": payload.get("model", "fake"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": f"answer #{n}"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020},
                }
            ).encode()
            ctype = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _BenchServer(ThreadingHTTPServer):
    """Burst-tolerant: listen() reads the backlog at construction, so it
    must be a class attribute (setting it on the handler does nothing)."""

    request_queue_size = 128
    daemon_threads = True


# ---------------------------------------------------------------- payloads

def refactor_payload():
    return {
        "model": "gpt-4.1",
        "messages": [
            {"role": "system", "content": "You are an expert software engineer. " * 40},
            {
                "role": "user",
                "content": "Refactor this monolith and explain trade-offs:\n```python\n"
                + "\n".join(f"def fn_{i}(): return {i}" for i in range(80))
                + "\n```",
            },
        ],
    }


def unique_payload():
    p = refactor_payload()
    p["messages"][1]["content"] += f"\nRun id: {uuid.uuid4().hex}"
    return p


def local_payload():
    return {
        "model": "gpt-4.1",
        "messages": [
            {
                "role": "user",
                "content": "What is on line number 3? context: alpha\nbeta\ngamma",
            }
        ],
    }


def noisy_blob(mib=2):
    unit = (
        "2026-10-22 12:00:00 DEBUG worker processing batch item\n"
        "\x1b[31mERROR\x1b[0m  something failed      \n\n\n\n"
        + "x" * 5000
        + "\n"
    )
    return unit * (int(mib * 1024 * 1024 / len(unit)))


# ------------------------------------------------------------------- client

def post_json(url, payload, headers=None, timeout=30):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        try:
            return json.loads(body), dict(resp.headers)
        except ValueError:  # SSE stream — return raw text for timing
            return body.decode("utf-8", "replace"), dict(resp.headers)


def get_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def wait_http(url, seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            get_json(url, timeout=2)
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    return False


def rss_mb(pid):
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=True
        )
        return int(out.stdout.strip()) / 1024.0
    except Exception:
        return -1.0


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", default="token-optimizer", help="path to the installed CLI")
    ap.add_argument("--keep", action="store_true", help="keep gateway running after bench")
    args = ap.parse_args()

    # Line-buffered stdout: results must survive even if a later phase hangs.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:  # pragma: no cover
        pass

    import tokan

    print(f"\n=== tokan performance validation ===")
    print(f"package : {tokan.__file__}")
    print(f"version : {tokan.__version__}   python {sys.version.split()[0]}")
    in_site = "site-packages" in (tokan.__file__ or "") or "dist-packages" in (tokan.__file__ or "")
    print(f"installed-package mode: {'yes' if in_site else 'NO — running from source tree!'}\n")

    # ---------------------------------------------- A: in-process microbench
    print("--- A. in-process package microbenchmarks ---")
    from tokan.cache import EdgeCache
    from tokan.cdm import estimate_tokens
    from tokan import OptimizationPipeline

    blob = noisy_blob(2)
    messages = [{"role": "user", "content": blob}]
    pipeline = OptimizationPipeline.with_cdn()
    fp = timed(5, lambda: pipeline.normalizer.fingerprint(messages, {}))
    record("A1 CDM fingerprint 2MB payload (mean)", statistics.mean(fp), "ms", "< 200ms", statistics.mean(fp) < 200)
    savings = 1 - (fp_ := pipeline.normalizer.fingerprint(messages, {})).canonical_tokens / max(1, fp_.original_tokens)
    record("A1b token compaction savings on noise", savings * 100, "%", "> 40%", savings > 0.40)
    print(f"       (tokens: {fp_.original_tokens:,} -> {fp_.canonical_tokens:,}; est of raw chars/4: {estimate_tokens(blob):,})")

    cache = EdgeCache(max_entries=100000)
    for i in range(50000):
        cache.set(f"key-{i}", f"value-{i}")
    lookups = timed(10000, lambda: cache.get("key-25000"))
    record("A2 EdgeCache get() @50k entries (mean)", statistics.mean(lookups), "ms", "< 0.1ms", statistics.mean(lookups) < 0.1)

    # ------------------------------------------------ B: gateway subprocess
    print("\n--- B. gateway end-to-end (fake upstream, 50ms simulated LLM) ---")
    upstream = _BenchServer(("127.0.0.1", 0), FakeUpstream)
    up_port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    gw_port = free_port()
    base = f"http://127.0.0.1:{gw_port}"
    gw = subprocess.Popen(
        [args.cli, "serve", "--port", str(gw_port), "--host", "127.0.0.1",
         "--upstream", f"http://127.0.0.1:{up_port}", "--threshold", "0.88"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if not wait_http(f"{base}/healthz"):
        print("gateway failed to start"); return 1

    chat = f"{base}/v1/chat/completions"
    LATENCY[0] = 0.05

    post_json(f"{base}/healthz" if False else chat, refactor_payload())  # warmup

    direct = timed(30, lambda: post_json(f"http://127.0.0.1:{up_port}/chat/completions", refactor_payload()))
    record("B0 direct upstream baseline (50ms sim)", statistics.mean(direct), "ms", "≈50ms", True)

    miss_route = []
    miss = timed(30, lambda: miss_route.append(post_json(chat, unique_payload())[1].get("X-Optimizer-Route")))
    overhead_p95 = pct(miss, 95) - pct(direct, 95)
    record("B1 cache miss overhead vs direct (p95)", overhead_p95, "ms", "< 15ms", overhead_p95 < 15)
    assert all(r == "llm" for r in miss_route), f"unexpected routes: {set(miss_route)}"

    post_json(chat, refactor_payload())  # prime cache
    hit = timed(300, lambda: post_json(chat, refactor_payload()))
    record("B2 cache hit latency (p95)", pct(hit, 95), "ms", "< 5ms", pct(hit, 95) < 5)

    local_route = []
    local = timed(200, lambda: local_route.append(post_json(chat, local_payload())[1].get("X-Optimizer-Route")))
    record("B3 deterministic local answer (p95)", pct(local, 95), "ms", "< 5ms", pct(local, 95) < 5)
    print(f"       (routes seen: {sorted(set(local_route))})")

    big = {"model": "gpt-4.1", "messages": [{"role": "user", "content": "Refactor this:\n" + noisy_blob(2)}]}
    big_t = timed(5, lambda: post_json(chat, big))
    record("B4 2MB payload through gateway (mean)", statistics.mean(big_t), "ms", "< 400ms", statistics.mean(big_t) < 400)

    LATENCY[0] = 0.0
    stream_direct = timed(10, lambda: post_json(f"http://127.0.0.1:{up_port}/chat/completions", {**refactor_payload(), "stream": True}))
    stream_gw = timed(10, lambda: post_json(chat, {**refactor_payload(), "stream": True}))
    stream_added = statistics.mean(stream_gw) - statistics.mean(stream_direct)
    record("B5 SSE relay added latency (mean)", stream_added, "ms", "< 25ms", stream_added < 25)

    LATENCY[0] = 0.01
    errors = [0]
    lat = []
    lock = threading.Lock()

    def worker(i):
        for j in range(12):
            p = refactor_payload() if (i + j) % 2 else unique_payload()
            try:
                t0 = time.perf_counter()
                post_json(chat, p, timeout=60)
                dt = (time.perf_counter() - t0) * 1000.0
                with lock:
                    lat.append(dt)
            except Exception:
                with lock:
                    errors[0] += 1

    n_workers = 40
    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
    for t in threads: t.start()
    for t in threads: t.join()
    wall = time.perf_counter() - t0
    rps = len(lat) / wall
    record("B6 concurrency: 40 threads x 12 req errors", float(errors[0]), "errors", "= 0", errors[0] == 0)
    record("B6b concurrency latency (p95)", pct(lat, 95), "ms", "< 60ms", pct(lat, 95) < 60)
    record("B6c concurrency throughput", rps, "req/s", "> 500", rps > 500)
    print(f"       ({len(lat)} requests in {wall:.2f}s; p50 {pct(lat,50):.1f}ms)")

    mem = rss_mb(gw.pid)
    record("B7 gateway RSS after load", mem, "MB", "< 200MB", 0 < mem < 200)

    # --------------------------------------------------------------- C: MCP
    print("\n--- C. MCP stdio round-trip (installed CLI subprocess) ---")
    mcp = subprocess.Popen([args.cli, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def rpc(obj, timeout=10):
        mcp.stdin.write(json.dumps(obj) + "\n")
        mcp.stdin.flush()
        import select

        ready, _, _ = select.select([mcp.stdout], [], [], timeout)
        if not ready:
            raise RuntimeError("MCP response timeout")
        return json.loads(mcp.stdout.readline())

    init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "bench", "version": "0"}}})
    # Notification: the server correctly sends no response — never read here.
    mcp.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    mcp.stdin.flush()

    def list_tools():
        return rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    rpc_list = timed(20, list_tools)
    names = [t["name"] for t in list_tools()["result"]["tools"]]
    record("C1 MCP tools/list round-trip (p95)", pct(rpc_list, 95), "ms", "< 50ms", pct(rpc_list, 95) < 50)
    print(f"       (tools: {', '.join(names)})")
    mcp.terminate()

    # ------------------------------------------------------------ D: startup
    print("\n--- D. CLI startup ---")
    startup = timed(5, lambda: subprocess.run([args.cli, "--help"], capture_output=True, check=True))
    record("D1 `token-optimizer --help` cold start", statistics.mean(startup), "ms", "< 2000ms", statistics.mean(startup) < 2000)

    if not args.keep:
        gw.terminate(); gw.wait(timeout=5)
    upstream.shutdown(); upstream.server_close()

    # ------------------------------------------------------------- summary
    fails = [r for r in RESULTS if not r[4]]
    print(f"\n=== summary: {len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed ===")
    if fails:
        for name, value, unit, target, _ in fails:
            print(f"  FAILED: {name} = {value:.2f}{unit} (target {target})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
