#!/usr/bin/env python3
"""A/B: the same Copilot session with and without Tokan.

Every payload is sent twice from the same machine, same model, same Copilot
subscription:

  DIRECT : POST api.githubcopilot.com/chat/completions
           <- exactly what VS Code does without Tokan (gh OAuth token +
              Copilot identity headers)
  TOKAN  : POST 127.0.0.1:<port>/v1/chat/completions
           <- via a FRESH gateway process (empty cache), same upstream +
              credential underneath

Billed tokens per leg come from the upstream ``usage`` block both legs
return. Cache/local legs never reach upstream, so they bill 0 — that is the
measured savings, not an estimate.

Scenarios mirror real agent sessions: clean context, bloat-heavy tool
output (two sizes: one the provider accepts, one the provider REJECTS at
its prompt-token limit), a deterministic lookup (local), resent history, and
an exact repeat (cache). Run against a fresh gateway so first encounters
measure compaction, not cache:

    token-optimizer serve --copilot --api-key "$(gh auth token)" --port 4041
    python benchmarks/ab_copilot.py --gateway http://127.0.0.1:4041

Stdlib only. Exits non-zero if any Tokan leg fails.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

COPILOT_IDENTITY = {
    "copilot-integration-id": "vscode-chat",
    "editor-version": "1.106.0",
    "editor-plugin-version": "copilot-chat/0.30.0",
}


def gh_token() -> str:
    return subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, check=True
    ).stdout.strip()


def post(url, payload, token=None, timeout=120):
    headers = {"Content-Type": "application/json", **COPILOT_IDENTITY}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ms = (time.perf_counter() - t0) * 1000
            return json.loads(body), dict(resp.headers), ms, None
    except urllib.error.HTTPError as exc:
        ms = (time.perf_counter() - t0) * 1000
        detail = exc.read()[:300].decode(errors="replace")
        return None, {}, ms, f"HTTP {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001
        ms = (time.perf_counter() - t0) * 1000
        return None, {}, ms, str(exc)


def billed(body) -> int:
    if not body:
        return -1  # upstream error marker
    usage = body.get("usage") or {}
    return int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))


# ---------------------------------------------------------------- payloads
def make_payloads():
    code_dump = "\n".join(
        f"def handler_{i}(request):  # generated endpoint #{i}\n"
        f"    return jsonify(ok=True, n={i})"
        for i in range(60)
    )
    ansi = "\x1b[31m{}\x1b[0m"

    def error_dump(lines):
        out = []
        for i in range(lines):
            out.append(
                ansi.format(
                    f"ERROR  tests/test_api_{i}.py::test_flow failed — "
                    f"assert 200 == 500, traceback above    "
                )
            )
            out += ["", "", ""]
        return "\n".join(out)

    system = {
        "role": "system",
        "content": "You are an expert software engineer working in a repository.",
    }
    task = {
        "role": "user",
        "content": "I want to add request retries with exponential backoff to the "
        "HTTP client. Here is the current module for context:\n\n```python\n"
        + code_dump
        + "\n```",
    }
    followup = [
        system,
        task,
        {
            "role": "assistant",
            "content": "Use a Retry-After-aware backoff wrapper around send().",
        },
        {"role": "user", "content": "Show me the final wrapper function."},
    ]
    return [
        ("task + clean code context", [system, task]),
        (
            "bloat-heavy turn, tool output (fits upstream limit)",
            [
                system,
                task,
                {
                    "role": "user",
                    "content": "The test run failed, full output:\n\n"
                    + error_dump(180),
                },
            ],
        ),
        (
            "deterministic lookup",
            [
                system,
                {
                    "role": "user",
                    "content": "What is on line number 42? context: alpha\nbeta\ngamma",
                },
            ],
        ),
        ("follow-up question (history resent)", followup),
        ("exact repeat of previous turn", followup),
        (
            "oversized turn — provider REJECTS raw payload",
            [
                system,
                task,
                {
                    "role": "user",
                    "content": "The full test run output follows:\n\n"
                    + error_dump(350),
                },
            ],
        ),
    ]


def run_leg(url, model, token, msgs):
    payload = {"model": model, "messages": msgs, "max_tokens": 48}
    body, headers, ms, err = post(url, payload, token=token)
    route = headers.get("X-Optimizer-Route", "-")
    return body, route, ms, err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://127.0.0.1:4041")
    ap.add_argument(
        "--upstream", default="https://api.githubcopilot.com/chat/completions"
    )
    ap.add_argument("--model", default="gpt-4o-mini")
    args = ap.parse_args()

    try:
        token = gh_token()
    except Exception:
        print("gh auth token unavailable — run `gh auth login` first", file=sys.stderr)
        return 2
    try:
        urllib.request.urlopen(f"{args.gateway}/metrics", timeout=3)
    except Exception:
        print(
            f"gateway not reachable at {args.gateway} — start a FRESH one with:\n"
            f'  token-optimizer serve --copilot --api-key "$(gh auth token)" '
            f"--port {args.gateway.rsplit(':', 1)[-1]}",
            file=sys.stderr,
        )
        return 2

    print(f"model: {args.model}   direct (no Tokan) vs {args.gateway} (fresh cache)\n")
    rows = []
    tot_d = tot_t = 0
    failures = 0
    for idx, (name, msgs) in enumerate(make_payloads(), 1):
        d_body, _, d_ms, d_err = run_leg(args.upstream, args.model, token, msgs)
        t_body, route, t_ms, t_err = run_leg(
            f"{args.gateway}/v1/chat/completions", args.model, None, msgs
        )
        if t_err is not None:
            print(f"[{idx}] {name}\n  DIRECT ok/err: {d_err}\n  TOKAN  ERROR: {t_err}")
            failures += 1
            continue

        d = billed(d_body)
        t = 0 if route in ("cache", "local") else billed(t_body)

        if d < 0 and "model_max_prompt_tokens_exceeded" in (d_err or ""):
            # Provider rejected the raw payload; Tokan served it after compaction.
            print(
                f"[{idx}] {name}\n"
                f"  DIRECT: {d_err.splitlines()[0][:110]}\n"
                f"  TOKAN : 200 OK via `{route}` — {t} tokens billed after compaction"
            )
            rows.append((idx, name, "rejected", t, "—", route, d_ms, t_ms))
            continue
        if d < 0:
            print(f"[{idx}] {name}\n  DIRECT ERROR: {d_err}\n  TOKAN route: {route}")
            failures += 1
            continue

        pct = (1 - t / d) * 100 if d else 0.0
        tot_d += d
        tot_t += t
        rows.append((idx, name, d, t, pct, route, d_ms, t_ms))
        print(
            f"[{idx}] {name:<48} direct {d:>6} tok → tokan {t:>6} tok "
            f"({pct:5.1f}% less) | route {route:<6} | {d_ms:6.0f}ms vs {t_ms:6.0f}ms"
        )

    if not rows:
        print("no successful comparisons — aborting", file=sys.stderr)
        return 1

    print("\n" + "=" * 104)
    print(
        f"{'SESSION TOTAL (rows that both legs completed)':<50} direct {tot_d:>6} tok "
        f"→ tokan {tot_t:>6} tok ({(1 - tot_t / tot_d) * 100:5.1f}% less)"
    )
    print("same model, same subscription — savings = compaction + local + cache\n")

    print("---- markdown ----")
    print(
        "| # | scenario | direct (no Tokan) | via Tokan | saved | route "
        "| direct ms | Tokan ms |"
    )
    print("|---|---|---:|---:|---:|---|---:|---:|")
    for idx, name, d, t, pct, route, d_ms, t_ms in rows:
        d_cell = f"**rejected**" if d == "rejected" else str(d)
        t_cell = f"{t} (200 OK)" if d == "rejected" else str(t)
        pct_cell = "request fixed" if d == "rejected" else f"{pct:.1f}%"
        print(
            f"| {idx} | {name} | {d_cell} | {t_cell} | {pct_cell} | `{route}` "
            f"| {d_ms:.0f} | {t_ms:.0f} |"
        )
    print(
        f"| | **total** | **{tot_d}** | **{tot_t}** | "
        f"**{(1 - tot_t / tot_d) * 100:.1f}%** | | | |"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
