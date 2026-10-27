#!/usr/bin/env python3
"""A/B benchmark on a REAL repository — the same VS Code Copilot traffic, with
and without Tokan, plus a validation of Tokan's own reported numbers.

Why this exists
---------------
Generic benchmarks prove the pipeline works; this proves it on *your* repo.
Every payload is built from real artifacts of ``--repo`` (an open source file,
``git diff --stat``, a real unified diff, the real file tree, the real agent
docs) and sent with VS Code's Copilot identity headers, twice:

  DIRECT : POST api.githubcopilot.com/chat/completions
           <- byte-for-byte what VS Code sends without Tokan
  VIA    : POST 127.0.0.1:<port>/v1/chat/completions
           <- through a FRESH Tokan gateway (empty cache)

Billed tokens per leg come from the upstream ``usage`` block both legs return
— measured, never estimated. Cache/local legs never reach upstream, so their
0 is measured too.

Scenarios include the rows that save **nothing** (clean code review) because
an honest benchmark shows where savings do NOT come from, as well as the rows
that do: noisy agent turns, real diffs, exact repeats, and Doc Governor Q&A
over the repo's own docs (naive full-doc resend vs ``/v1/doc/ask``).

Validation
----------
At the end the harness cross-checks the gateway's *claims* (receipt lines from
``--log-requests`` + ``/metrics`` totals) against the provider's *bills*
(``usage.prompt_tokens`` on each leg) — the dashboard is verified, not trusted.

Usage
-----
    token-optimizer serve --copilot --api-key "$(gh auth token)" \
        --port 4040 --log-requests > /tmp/tokan-bench.log 2>&1 &
    python3 benchmarks/repo_ab.py --repo /path/to/repo \
        --gateway http://127.0.0.1:4040 --gateway-log /tmp/tokan-bench.log

Stdlib only. Exits 2 if the gateway or gh auth is missing; 0 otherwise
(it reports; it does not gate CI).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

#: Exactly the headers VS Code's Copilot Chat sends.
COPILOT_IDENTITY = {
    "copilot-integration-id": "vscode-chat",
    "editor-version": "1.106.0",
    "editor-plugin-version": "copilot-chat/0.30.0",
}


# --------------------------------------------------------------------- helpers
def gh_token() -> str:
    return subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, check=True
    ).stdout.strip()


def post(url, payload, token=None, timeout=180):
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


def get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def prompt_of(body) -> int:
    if not body:
        return -1
    usage = body.get("usage") or {}
    return int(usage.get("prompt_tokens", 0))


def completion_of(body) -> int:
    if not body:
        return -1
    usage = body.get("usage") or {}
    return int(usage.get("completion_tokens", 0))


def answer_text(body) -> str:
    if not body:
        return ""
    try:
        return body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    return out.stdout


# --------------------------------------------------------------- real payloads
def build_payloads(repo: Path):
    """Payloads grounded in real artifacts of ``repo`` (VS Code wire format)."""
    # Real open file — a VS Code "review this" turn.
    wire = repo / "frontend/extensions/vscode/src/providers/wire.ts"
    file_text = wire.read_text() if wire.exists() else "// file missing"

    # Real git artifacts.
    diff_stat = git(repo, "diff", "--stat", "HEAD~2").strip()
    git_log = git(repo, "log", "--oneline", "-30").strip()
    uni_diff = git(repo, "diff", "HEAD~3", "--", "backend/src/services/prReview.ts")
    tree = subprocess.run(
        ["find", "backend", "-type", "f"],
        cwd=str(repo), capture_output=True, text=True,
    ).stdout.strip()

    system = {
        "role": "system",
        "content": "You are an expert software engineer working in a repository.",
    }
    review_task = {
        "role": "user",
        "content": "Review this file for bugs and unsafe patterns:\n\n```typescript\n"
        + file_text
        + "\n```",
    }
    diff_turn = [
        system,
        {
            "role": "user",
            "content": "Here is the branch status before I start:\n\n"
            + "```text\n" + diff_stat + "\n```\n\n"
            + "Recent history:\n```text\n" + git_log + "\n```\n\n"
            + "Backend files:\n```text\n" + tree + "\n```\n\n"
            + "What should I review first?",
        },
    ]
    uni_turn = [
        system,
        {
            "role": "user",
            "content": "Explain this diff and flag anything risky:\n\n```diff\n"
            + uni_diff
            + "\n```",
        },
    ]

    # A 4-turn session where history is resent every turn (the agent default).
    turns = [system, review_task]
    session = []
    for i, followup in enumerate(
        [
            "Summarize the main responsibilities of this module.",
            "Where is authentication handled in this file?",
            "Suggest one concrete refactor, keep it small.",
        ],
        1,
    ):
        turns = turns + [
            {"role": "assistant", "content": f"(answer {i}: concise summary of the module)"},
            {"role": "user", "content": followup},
        ]
        session.append(list(turns))

    return [
        ("open file review (clean TS)", [system, review_task], "prompt"),
        ("agent turn: diff stat + log + file tree", diff_turn, "prompt"),
        ("real unified diff (prReview.ts)", uni_turn, "prompt"),
        *[ (f"session turn {i} (history resent)", t, "prompt")
           for i, t in enumerate(session, 1) ],
        ("exact repeat of last turn", session[-1], "repeat"),
    ]


# --------------------------------------------------------------- doc fixture
def build_docset(repo: Path):
    """The docs an agent actually loads for this repo (AGENTS/CLAUDE/instructions)."""
    names = [
        "AGENTS.md", "CLAUDE.md", "instructions.md",
        "README.md", "ARCHITECTURE.md", "ARCHITECTURE_2026.md",
    ]
    parts = []
    for name in names:
        p = repo / name
        if p.exists():
            parts.append(f"===== {name} =====\n{p.read_text()}")
    return "\n\n".join(parts)


#: (question, gold groups — every group must match, any alternative inside a
#: group counts; all facts below are present in the ingested repo docs)
DOC_QUESTIONS = [
    (
        "Which repo config file is canonical, and which one is the legacy "
        "read-only fallback?",
        [".prsniffer.yaml", ".mergegod.yaml"],
    ),
    (
        "What port does the dev API run on, and which Node version is required?",
        ["5050", ["node 22", "node.js 22"]],
    ),
    (
        "Name one existing service that must be extended instead of building "
        "a parallel one.",
        [[
            "spendauthority", "prreview", "changelystack", "riskscoring",
            "riskqueue", "mcpserver", "aiplatforms", "billing",
        ]],
    ),
]


def grade(answer: str, groups) -> bool:
    lowered = answer.lower()
    for group in groups:
        alts = (group,) if isinstance(group, str) else tuple(group)
        if not any(str(alt).lower() in lowered for alt in alts):
            return False
    return True


def squash(text: str) -> str:
    return " ".join((text or "").split())[:150]


# --------------------------------------------------------------- log receipts
RECEIPT_RE = re.compile(
    r"^\S+\s+(?P<path>\S+)\s+route=(?P<route>\S+)\s+status=(?P<status>\d+)"
    r".*?(?P<orig>\d+)->(?P<sent>\d+) tok \((?P<pct>[\d.]+)%\)"
)


def parse_receipts(log_path):
    rows = []
    try:
        for line in Path(log_path).read_text(errors="replace").splitlines():
            m = RECEIPT_RE.search(line)
            if m:
                rows.append({**m.groupdict(), "raw": line})
    except OSError:
        pass
    return rows


# ------------------------------------------------------------------- printing
def fmt(n) -> str:
    return f"{n:,}" if isinstance(n, int) and n >= 0 else str(n)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="path to a real git repository")
    ap.add_argument("--gateway", default="http://127.0.0.1:4040")
    ap.add_argument(
        "--upstream", default="https://api.githubcopilot.com/chat/completions"
    )
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument(
        "--gateway-log", default="",
        help="the --log-requests stdout file, for claim-vs-bill validation",
    )
    ap.add_argument("--skip-doc", action="store_true", help="skip the Doc Governor legs")
    args = ap.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    if not (repo / ".git").exists():
        print(f"{repo} is not a git repository", file=sys.stderr)
        return 2
    try:
        token = gh_token()
    except Exception:
        print("gh auth token unavailable — run `gh auth login` first", file=sys.stderr)
        return 2
    try:
        get(f"{args.gateway}/metrics")
    except Exception:
        print(
            f"gateway not reachable at {args.gateway} — start a FRESH one:\n"
            f'  token-optimizer serve --copilot --api-key "$(gh auth token)" '
            f"--port {args.gateway.rsplit(':', 1)[-1]} --log-requests",
            file=sys.stderr,
        )
        return 2

    print(f"repo   : {repo}")
    print(f"model  : {args.model}   DIRECT (no Tokan) vs VIA {args.gateway} (fresh cache)")
    print("identity: VS Code Copilot headers (copilot-integration-id: vscode-chat)\n")

    payloads = build_payloads(repo)

    # ---------------------------------------------------------- payload A/B
    rows = []
    tot_direct = tot_via = 0
    tot_direct_p = tot_via_p = 0
    failures = 0
    for idx, (name, msgs, kind) in enumerate(payloads, 1):
        raw = {"model": args.model, "messages": msgs, "max_tokens": 96}

        d_body, _, d_ms, d_err = post(args.upstream, raw, token=token)
        v_body, v_hdr, v_ms, v_err = post(
            f"{args.gateway}/v1/chat/completions", raw
        )
        d_p, v_p = prompt_of(d_body), prompt_of(v_body)
        d_c, v_c = completion_of(d_body), completion_of(v_body)
        route = v_hdr.get("X-Optimizer-Route", "-")

        if d_err or v_err:
            failures += 1
        direct_billed = max(d_p, 0) + max(d_c, 0)
        via_billed = max(v_p, 0) + max(v_c, 0)
        # Cache/repeat legs bill 0 on the via side — that IS the saving.
        pct = (
            f"{100.0 * (d_p - v_p) / d_p:.1f}%"
            if d_p > 0 and v_p >= 0
            else "-"
        )
        rows.append(
            {
                "n": idx, "name": name, "direct_p": d_p, "via_p": v_p,
                "direct_c": d_c, "via_c": v_c, "route": route,
                "d_ms": d_ms, "v_ms": v_ms, "pct": pct,
                "d_err": d_err, "v_err": v_err, "kind": kind,
            }
        )
        tot_direct += direct_billed
        tot_via += via_billed
        tot_direct_p += max(d_p, 0)
        tot_via_p += max(v_p, 0)
        err = f"  !! direct={d_err} via={v_err}" if (d_err or v_err) else ""
        print(
            f"[{idx}] {name}\n"
            f"     prompt  direct {fmt(d_p):>7}  via {fmt(v_p):>7}   "
            f"({pct} less)   route={route}   "
            f"latency {d_ms:.0f}ms -> {v_ms:.0f}ms{err}"
        )

    # ------------------------------------------------------- Doc Governor A/B
    doc_rows = []
    if not args.skip_doc:
        doc_text = build_docset(repo)
        print(f"\n--- Doc Governor: {len(doc_text):,} chars of repo docs ---")
        ing, _, _, ing_err = post(
            f"{args.gateway}/v1/ingest", {"text": doc_text}
        )
        if ing_err:
            print(f"     ingest failed: {ing_err}")
            failures += 1
        else:
            doc_id = ing["doc_id"]
            print(
                f"     ingested {doc_id}: {ing['chunks']} chunks, "
                f"{ing['total_tokens']:,} tok (summary {ing['summary_tokens']:,} tok)"
            )
            for qi, (question, golds) in enumerate(DOC_QUESTIONS, 1):
                # Leg 1: naive full-doc resend, direct (no Tokan at all).
                full_payload = {
                    "model": args.model,
                    "messages": [
                        {"role": "user", "content": doc_text + "\n\nQuestion: " + question}
                    ],
                    "max_tokens": 220,
                }
                f_body, _, f_ms, f_err = post(args.upstream, full_payload, token=token)
                # Leg 2: same full-doc payload through the normal gateway path.
                g_body, _, g_ms, g_err = post(
                    f"{args.gateway}/v1/chat/completions", full_payload
                )
                # Leg 3: Doc Governor — ingest once, ask with retrieval.
                d_body, d_hdr, d_ms, d_err = post(
                    f"{args.gateway}/v1/doc/ask",
                    {"doc_id": doc_id, "question": question,
                     "model": args.model, "top_k": 3},
                )
                f_p, g_p, d_p = prompt_of(f_body), prompt_of(g_body), prompt_of(d_body)
                f_ans, d_ans = answer_text(f_body), answer_text(d_body)
                f_ok = grade(f_ans, golds)
                d_ok = grade(d_ans, golds)
                opt = (d_body or {}).get("optimizer") or {}
                err = ""
                if f_err or g_err or d_err:
                    failures += 1
                    err = f"  !! full={f_err} gw={g_err} doc={d_err}"
                pct = (
                    f"{100.0 * (f_p - d_p) / f_p:.1f}%"
                    if f_p > 0 and d_p >= 0 else "-"
                )
                doc_rows.append(
                    {"q": question, "full_p": f_p, "gw_p": g_p, "doc_p": d_p,
                     "f_ok": f_ok, "d_ok": d_ok, "avoided": opt.get("tokens_avoided", 0),
                     "f_ms": f_ms, "g_ms": g_ms, "d_ms": d_ms, "err": err}
                )
                print(
                    f"  Q{qi} prompt: full-doc {fmt(f_p):>6} | via gateway {fmt(g_p):>6} "
                    f"| doc-ask {fmt(d_p):>5}  ({pct} less)   "
                    f"gold: full {'yes' if f_ok else 'NO'} / doc {'yes' if d_ok else 'NO'}{err}"
                )
                print(f"      full: {squash(f_ans)}")
                print(f"      doc : {squash(d_ans)}")

    # ------------------------------------------------------- claimed vs billed
    print("\n" + "=" * 78)
    print("VALIDATION — what the gateway claims vs what the provider billed")
    print("=" * 78)
    metrics = get(f"{args.gateway}/metrics")
    session = metrics.get("session", {})

    # 1) Per-leg: gateway receipt original/sent vs provider prompt counts.
    receipts = parse_receipts(args.gateway_log) if args.gateway_log else []
    llm_receipts = [r for r in receipts if r["route"] in ("llm", "cache")]
    print(f"\nreceipts parsed from log: {len(llm_receipts)}")
    if receipts:
        print(f"{'path':<22} {'claimed orig->sent':>20} {'saved%':>7}")
        for r in receipts[-8:]:
            print(
                f"{r['path']:<22} {r['orig'] + '->' + r['sent']:>20} "
                f"{r['pct'] + '%':>7}"
            )

    # 2) Session totals: claimed vs measured.
    measured_direct_p = sum(max(r["direct_p"], 0) for r in rows)
    measured_via_p = sum(max(r["via_p"], 0) for r in rows)
    claimed_saved = int(session.get("tokens_saved", 0))
    measured_prompt_saved = measured_direct_p - measured_via_p
    print(f"\n{'metric':<34} {'claimed (gateway)':>18} {'measured (provider)':>20}")
    print("-" * 74)
    print(
        f"{'payload A/B prompt tokens':<34} "
        f"{fmt(claimed_saved) + ' (tokens_saved)':>18} "
        f"{fmt(measured_prompt_saved) + ' (direct-via)':>20}"
    )
    if doc_rows:
        claimed_doc = int(session.get("doc_tokens_avoided", 0))
        measured_doc = sum(
            max(r["full_p"] - r["doc_p"], 0) for r in doc_rows if r["full_p"] > 0
        )
        print(
            f"{'doc Q&A avoided tokens':<34} "
            f"{fmt(claimed_doc) + ' (doc_avoided)':>18} "
            f"{fmt(measured_doc) + ' (full-doc-docask)':>20}"
        )
    print(
        f"{'cache hits / llm calls':<34} "
        f"{str(session.get('cache_hits', 0)) + '/' + str(session.get('llm_calls', 0)):>18} "
        f"{'':>20}"
    )
    print(
        f"{'prefix reuse / provider cache':<34} "
        f"{fmt(int(session.get('prefix_reused_tokens', 0))) + ' / ' + fmt(int(session.get('upstream_cached_tokens', 0))):>18}"
    )

    # ------------------------------------------------------------- summary
    print("\n" + "=" * 78)
    print("SUMMARY (prompt tokens only — completion is the model's output, not context)")
    print("=" * 78)
    print(f"{'scenario':<40} {'direct':>9} {'via tokan':>10} {'saved':>8}")
    print("-" * 70)
    for r in rows:
        saved = (
            f"{100.0 * (r['direct_p'] - r['via_p']) / r['direct_p']:.1f}%"
            if r["direct_p"] > 0 else "-"
        )
        print(
            f"{r['name'][:40]:<40} {fmt(r['direct_p']):>9} {fmt(r['via_p']):>10} "
            f"{saved:>8}  [{r['route']}]"
        )
    if doc_rows:
        for i, r in enumerate(doc_rows, 1):
            saved = (
                f"{100.0 * (r['full_p'] - r['doc_p']) / r['full_p']:.1f}%"
                if r["full_p"] > 0 else "-"
            )
            print(
                f"{'doc Q&A ' + str(i) + ': ' + r['q'][:31]:<40} "
                f"{fmt(r['full_p']):>9} {fmt(r['doc_p']):>10} {saved:>8}  [doc]"
            )
    print("-" * 70)
    all_direct = measured_direct_p + sum(max(r["full_p"], 0) for r in doc_rows)
    all_via = measured_via_p + sum(max(r["doc_p"], 0) for r in doc_rows)
    if all_direct:
        print(
            f"{'TOTAL':<40} {fmt(all_direct):>9} {fmt(all_via):>10} "
            f"{100.0 * (all_direct - all_via) / all_direct:.1f}%"
        )

    print("\nRead honestly:")
    print(" • Clean-code rows are expected to save ~0% — there is no waste in")
    print("   clean source; Tokan reports the truth rather than inventing a %.")
    print(" • Session/history rows measure what the *provider's* prefix cache")
    print("   discounts (surfaced as prefix reuse), which Tokan keeps firing.")
    print(" • Exact repeats and Doc Q&A are Tokan's own wins (cache / retrieval).")
    print(" • The VALIDATION block above compares the dashboard's claims to the")
    print("   provider's bills — if they disagree, trust the provider row.")
    if failures:
        print(f"\n{failures} leg(s) errored — see '!!' markers above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
