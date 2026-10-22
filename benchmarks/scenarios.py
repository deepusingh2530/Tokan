#!/usr/bin/env python3
"""Multi-scenario battery for Tokan against the real Copilot upstream.

Reports *message-level* savings (the text actually forwarded, both sides
counted identically) so JSON-framing overhead is never misread as compaction.
Pipeline ``original->sent`` is recorded separately for the receipts/dashboard.

Requires a gateway already serving the Copilot upstream (default ``:4098``),
started with a fallback model so S8 can exercise the retry:

    token-optimizer serve --copilot --api-key "$(gh auth token)" \
        --port 4098 --fallback-model gpt-4o-mini --cache-db /tmp/tokan-scenarios.db

Run from the repo root (the diff scenarios read git history, and ``tokan``
must be importable):

    python3 benchmarks/scenarios.py

Raw results are written next to this file as ``benchmarks/scenario-results.json``.
The dashboard at ``http://127.0.0.1:4098/dashboard`` shows each request's
latency split into Tokan overhead vs time blocked on the provider.
"""
import json, os, subprocess, time, urllib.request, urllib.error
from tokan.pipeline import OptimizationPipeline
from tokan.cdm import CDMNormalizer, estimate_tokens

GW = "http://127.0.0.1:4098"
MODEL = "gpt-4o-mini"
R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenario-results.json")
pipe, norm = OptimizationPipeline(), CDMNormalizer()
results = []


def metrics():
    with urllib.request.urlopen(f"{GW}/metrics", timeout=5) as r:
        return json.loads(r.read())["session"]


def post(payload, path="/v1/chat/completions"):
    req = urllib.request.Request(
        GW + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, dict(r.headers), r.read(), (time.perf_counter() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(), (time.perf_counter() - t0) * 1000


def msg_level(orig_text, fwd_text):
    o = estimate_tokens(orig_text)
    return o, estimate_tokens(fwd_text), (round(100 * (1 - estimate_tokens(fwd_text) / o), 1) if o else 0.0)


def run(name, note, content, model=MODEL, path="/v1/chat/completions"):
    payload = {"model": model, "messages": [{"role": "user", "content": content}]}
    prep = pipe.prepare(payload)
    fwd = prep.messages[0]["content"] if prep.messages else content
    o, s, pct = msg_level(content, fwd)
    before = metrics()
    status, hdrs, raw, ms = post(payload, path)
    after = metrics()
    try:
        parsed = json.loads(raw)
        reply = parsed["choices"][0]["message"].get("content", "")[:55]
        usage = parsed.get("usage", {})
        err = None
    except Exception:
        reply, usage, err = "", {}, raw[:120].decode("utf-8", "replace")
    row = {
        "scenario": name, "note": note, "status": status, "error": err,
        "msg_orig": o, "msg_sent": s, "msg_pct": pct,
        "pipe_orig": prep.record.original_tokens, "pipe_sent": prep.record.canonical_tokens,
        "route": hdrs.get("X-Optimizer-Route"), "fallback": hdrs.get("X-Optimizer-Fallback"),
        "prov_cache": hdrs.get("X-Optimizer-Upstream-Cached-Tokens"),
        "usage_prompt": usage.get("prompt_tokens"),
        "usage_cached": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
        "ms": round(ms, 1), "reply": reply,
    }
    results.append(row)
    print(f"[{status}] {name}: msg {o}->{s} tok ({pct}%) route={row['route']} "
          f"{row['ms']}ms fb={row['fallback']}", flush=True)
    return row


# ------------------------------------------------------------------ payloads
noise = "\n".join(["\x1b[32m$ npm test\x1b[0m"] +
                  [f"\x1b[90m[{i:04d}]\x1b[0m progress step {i} ......... ok" for i in range(120)] +
                  ["", "", "", "", "  PASS  all tests green  ", "", "", ""])
noise_msg = "Test output:\n```\n" + noise + "\n```\nDid the tests pass?"

bigfile = subprocess.run(["git", "show", "HEAD~12:tokan/cdm/normalizer.py"],
                         capture_output=True, text=True, cwd=R).stdout
body = "\n".join(" " + l for l in bigfile.splitlines())
whole_diff = (f"Review this patch:\n\n```diff\n@@ -1,{len(bigfile.splitlines())} +1,{len(bigfile.splitlines())} @@\n"
              "-def old_name():\n+def new_name():\n" + body + "\n```\nWhat changed?")

real_diff = subprocess.run(["git", "diff", "-U3", "HEAD~2..HEAD"],
                           capture_output=True, text=True, cwd=R).stdout
real_diff_msg = "Summarize this patch:\n```diff\n" + real_diff + "\n```"

# oversized but rescuable: slightly over the 12,288 cap, head/tail elision rescues it
rescue = "\n".join(f"\x1b[33mWARN\x1b[0m module_{i%40}: retry {i} after backoff "
                   f"{'.'.join('x'*8 for _ in range(4))}" for i in range(260))
rescue_msg = "Build log:\n```\n" + rescue + "\n```\nSummarize the warnings."

# oversized and NOT rescuable: change-dominated diff already far over cap
big_diff = subprocess.run(["git", "diff", "-U3", "HEAD~12..HEAD"],
                          capture_output=True, text=True, cwd=R).stdout
big_diff_msg = "Summarize this patch:\n```diff\n" + big_diff + "\n```"

# ------------------------------------------------------------------ scenarios
run("S1 fresh chat (clean text)", "No noise to strip - expect ~0% compaction",
    "Reply with the single word: pong")
run("S2 repeat (cache hit)", "Identical to S1 - served from cache",
    "Reply with the single word: pong")
run("S2b unique clean", "Distinct clean text, must be route=llm",
    "Answer with one word only: what color is the sky on mars?")
run("S3 noisy tool output", "ANSI escapes + 120 progress lines + blank runs", noise_msg)
run("S4 whole-file diff", "Large file echoed with a 2-line edit", whole_diff)
run("S5 real git diff", "Change-dominated repo diff - expect ~0%", real_diff_msg)
run("S6 oversized (rescuable)", "Slightly over 12,288 cap - head/tail elision rescues", rescue_msg)
run("S7 oversized diff (too big)", "Change-dominated diff far over cap - honest NOT-rescued case", big_diff_msg)
run("S8 model fallback", "Nonexistent model id -> one fallback retry",
    "say hello there", model="definitely-not-a-real-model-xyz")
run("S9 streaming", "SSE relay", "Count from 1 to 3, one number per line")
run("S10 anthropic wire", "Anthropic /v1/messages protocol",
    "Reply with the single word: bonjour", path="/v1/messages")

with open(OUT, "w") as f:
    json.dump(results, f, indent=2)
print("\n=== summary ===")
for r in results:
    print(f"{r['scenario']:30} {r['status']}  msg {r['msg_orig']:>6}->{r['msg_sent']:<6} {r['msg_pct']:>5}%  {r['ms']:>7}ms")
