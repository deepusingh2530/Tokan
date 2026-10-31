# Tokan — Complete Usage Guide

A detailed, end-to-end manual for installing, running, and getting value out of
Tokan. Written so **anyone** can go from zero to a working, savings-reporting
gateway without reading the source.

> **What Tokan is.** A local, stdlib-only **token-optimization gateway** for AI
> coding agents. You point your agent at Tokan instead of the provider; Tokan
> sits in the middle and, on every request, does one of three things:
>
> | Route | When | What the client gets | Upstream call? |
> |---|---|---|---|
> | **`cache`** | The *exact* payload was seen before | The previously cached answer | **None** (served locally, ~1 ms) |
> | **`local`** | The question is deterministically answerable (a lookup / simple transform the JeV router is confident about) | An answer computed on your machine | **None** (~tens of ms) |
> | **`llm`** | Otherwise | A compacted payload sent to the provider, answer relayed back | **Yes** |
>
> On the `llm` route Tokan **compacts** the outgoing context (strips ANSI,
> collapses whitespace, elides huge noise, compacts diffs) so you send fewer
> tokens — and it **reports exactly what it did** per request. It never invents
> savings: clean text correctly shows ~0%.

Tokan speaks three wire protocols simultaneously, so almost any agent works:

- **OpenAI** — `http://127.0.0.1:4040/v1/chat/completions`
- **Anthropic** — `http://127.0.0.1:4040/v1/messages`
- **Gemini** — `http://127.0.0.1:4040/v1beta/models/<model>:generateContent`

It can front **OpenAI**, **Anthropic**, **Gemini**, a **custom OpenAI-compatible**
endpoint, or your **GitHub Copilot subscription** (no separate API key needed).

---

## Table of contents

1. [Requirements](#1-requirements)
2. [Install](#2-install)
3. [Quick start — 5 minutes](#3-quick-start--5-minutes)
4. [How it works (request lifecycle)](#4-how-it-works-request-lifecycle)
5. [Running the gateway (`serve`) in detail](#5-running-the-gateway-serve-in-detail)
6. [Wiring your agent](#6-wiring-your-agent)
7. [MCP integration](#7-mcp-integration)
8. [Observability: dashboard, metrics, stats, receipts](#8-observability-dashboard-metrics-stats-receipts)
9. [Persistent cache & model fallback](#9-persistent-cache--model-fallback)
10. [The repo indexer (`index`)](#10-the-repo-indexer-index)
11. [Realistic expectations — what actually saves tokens](#11-realistic-expectations--what-actually-saves-tokens)
12. [Troubleshooting](#12-troubleshooting)
13. [Full command reference](#13-full-command-reference)
14. [FAQ](#14-faq)

---

## 1. Requirements

- **Python 3.9+** (3.9–3.14 supported; the test suite runs on both ends).
- **No third-party dependencies** — Tokan is pure standard library. Nothing to
  audit, nothing to vendor.
- An **upstream** to front:
  - a **GitHub Copilot** subscription (uses your existing `gh` login — no API
    key), **or**
  - an **OpenAI / Anthropic / Gemini** API key, **or**
  - any **OpenAI-compatible** endpoint (Ollama, vLLM, LiteLLM, LM Studio, etc.).
- An **AI coding agent** to point at the gateway (Claude Code, Codex, Cursor,
  VS Code GitHub Copilot, Cline, Roo Code, Continue, OpenCode, Gemini CLI, Zed,
  Windsurf, Aider, …). Tokan is agent-agnostic.

Everything runs **on your machine**, bound to `127.0.0.1` by default. Your code
and prompts never leave your computer except to reach the provider you already
use.

---

## 2. Install

Pick **one**. pipx is recommended for the CLI because it keeps Tokan isolated
from your system Python.

### Option A — pipx (recommended)

```bash
pipx install tokan
# provides the `token-optimizer` command
```

Already installed? Upgrade with:

```bash
pipx upgrade tokan
```

### Option B — pip (into a virtualenv)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install tokan
```

### Option C — run from source (no install)

```bash
git clone https://github.com/deepusingh2530/Tokan.git
cd Tokan
python3 -m tokan --help        # `python3 -m tokan` == `token-optimizer`
```

### Option D — single-file zipapp (no package manager at all)

Every release ships a self-contained `tokan.pyz` on the
[GitHub Releases page](https://github.com/deepusingh2530/Tokan/releases):

```bash
curl -fsSL -o tokan.pyz \
  https://github.com/deepusingh2530/Tokan/releases/latest/download/tokan.pyz
python3 tokan.pyz --help
```

### Verify the install

```bash
token-optimizer --help
python3 -c "import tokan; print(tokan.__version__)"
```

> There is no `--version` flag; use the `import tokan` line above to print the
> version.

---

## 3. Quick start — 5 minutes

The fastest path that produces visible savings is the **GitHub Copilot** flow,
because it needs no API key. Substitute an OpenAI key if you prefer (shown at
the end).

### Step 1 — Start the gateway

Open a terminal and run:

```bash
token-optimizer serve --copilot --api-key "$(gh auth token)"
```

`gh auth token` prints your existing GitHub OAuth token (run `gh auth login`
first if `gh` isn't authenticated). You'll see a banner:

```
token-optimizer gateway  http://127.0.0.1:4040
  openai wire   : http://127.0.0.1:4040/v1/chat/completions -> https://api.githubcopilot.com
  anthropic wire: http://127.0.0.1:4040/v1/messages   -> https://api.anthropic.com
  gemini wire   : http://127.0.0.1:4040/v1beta/models/*/generateContent -> ...
  edge cache    : local LRU only
  metrics       : http://127.0.0.1:4040/metrics
  dashboard     : http://127.0.0.1:4040/dashboard

Wire an agent:  token-optimizer setup <agent>
```

Leave this terminal running — it's the gateway.

### Step 2 — Sanity-check it

In a second terminal:

```bash
curl -s http://127.0.0.1:4040/healthz
# {"ok": true, "service": "token-optimizer"}

curl -s http://127.0.0.1:4040/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"say pong"}]}'
# a normal chat-completion JSON response, relayed from Copilot
```

If the curl above returns a reply, the gateway is live and fronting Copilot.

### Step 3 — Point an agent at it

Run the built-in helper for your agent and follow the printed snippet:

```bash
token-optimizer setup github-copilot   # for VS Code / Copilot Chat
token-optimizer setup claude-code      # for Claude Code
token-optimizer setup codex            # for Codex CLI
# ... see §6 for every agent
```

The helper prints **exact** environment variables / config files to edit. For
the VS Code GitHub Copilot case, for example, you set:

```
"github.copilot.advanced": {
  "debug.overrideProxyUrl": "http://127.0.0.1:4040/v1"
}
```

### Step 4 — Use your agent, then watch savings

Use the agent normally. Then open the live dashboard:

```
http://127.0.0.1:4040/dashboard
```

or print a session summary:

```bash
token-optimizer stats --port 4040
```

Every request shows its **route** (`cache` / `local` / `llm`), tokens in→out,
savings %, and the **latency split** (Tokan overhead vs. time blocked on the
provider — see §8).

### Using an OpenAI key instead of Copilot

```bash
token-optimizer serve --api-key "$OPENAI_API_KEY"
# upstream defaults to https://api.openai.com/v1
```

For a custom OpenAI-compatible server (e.g. Ollama):

```bash
token-optimizer serve --upstream http://127.0.0.1:11434/v1 --api-key ollama
```

---

## 4. How it works (request lifecycle)

For every request that arrives at the gateway, Tokan runs this pipeline:

```
        agent ──▶ gateway (:4040)
                     │
             1. normalize payload  (Canonical Data Model)
                • strip ANSI escapes
                • collapse whitespace / blank runs
                • elide enormous noisy blocks (head+tail kept)
                • compact diffs (keep every +/- change line)
                     │
             2. cache probe  ── exact payload digest ──▶ HIT? serve cached answer, STOP (route=cache)
                     │
             3. JeV router  ── confident it's answerable locally? ──▶ compute locally (route=local)
                     │
             4. compact + forward to provider  (route=llm)
                • send the compacted payload
                • relay the answer back
                • record usage / provider prompt-cache hits
                     │
             5. receipt: route, tokens in→out, savings %,
                latency split (tokan ms vs upstream ms)
```

Key ideas:

- **Canonical Data Model (CDM).** Requests from different agents are normalized
  into one canonical shape before compaction, so optimization logic is written
  once. Text is compacted **losslessly where it matters** — change lines in a
  diff are always preserved.
- **Message-level accounting.** Published savings figures tokenize the *text
  actually forwarded* on both sides identically. This means clean prose honestly
  reports ~0% instead of a fake number derived from JSON minification.
- **The cache is exact-match.** The key is a digest of the whole payload, so a
  *changed* context is never served a stale answer. A growing multi-turn
  conversation is therefore a deliberate miss each time.
- **Provider prompt-cache is disclosed, not claimed.** If the upstream itself
  caches a prompt prefix, Tokan surfaces it as
  `X-Optimizer-Upstream-Cached-Tokens` and on the dashboard's `PROV.CACHE`
  column — it is the provider's savings, not Tokan's.

---

## 5. Running the gateway (`serve`) in detail

`token-optimizer serve` is the main command. All flags:

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address. Keep local unless you intend to share on a LAN. |
| `--port` | `4040` | Bind port. |
| `--upstream` | `https://api.openai.com/v1` | OpenAI-compatible upstream base URL. |
| `--api-key` | — | Key for the OpenAI-compatible upstream (also used for `--copilot` token). |
| `--anthropic-upstream` | `https://api.anthropic.com` | Anthropic-native upstream. |
| `--anthropic-api-key` | — | Anthropic key (if fronting real Anthropic). |
| `--gemini-upstream` | `https://generativelanguage.googleapis.com` | Gemini-native upstream. |
| `--gemini-api-key` | — | Gemini key. |
| `--copilot [PLAN]` | off | Front a **GitHub Copilot** plan as the OpenAI upstream. `PLAN` ∈ `individual` (default) \| `business` \| `enterprise`. |
| `--extra-header "K: V"` | — | Add/override an upstream header (repeatable), e.g. `"Copilot-Integration-Id: vscode-chat"`. |
| `--cdn ENDPOINT` | — | Remote edge-cache endpoint (shared team cache). |
| `--namespace NAME` | `default` | Cache namespace — isolate or share caches per team/project. |
| `--threshold 0.88` | `0.88` | JeV router confidence threshold for answering locally. |
| `--price 2.50` | `2.50` | USD per 1M tokens (for the "cost saved" figure). |
| `--cache-db PATH` | — | SQLite file for a **persistent** cache that survives restarts. |
| `--fallback-model MODEL` | — | Retry once with this model when upstream rejects the requested one (`model_not_supported` / `model_not_found`). |
| `--log-requests` | off | Print one **receipt line** per request to the console. |
| `--prompt-cache-key` | off | Inject a stable `prompt_cache_key` (the session id) into OpenAI-wire payloads so the provider routes the conversation to one prompt cache. Provider support varies; off by default. |
| `--conversation-db PATH` | — | SQLite file for the **append-only conversation store** (observe-only memory: records message events + context manifests per turn, never serves them). Storage failures fail open — the gateway serves on regardless. |
| `--governor` | off | **Context Governor**: when a request would overflow the model's context window, shed low-value context (oldest optional turns first — never system/developer instructions, tool exchanges or your latest ask) instead of forwarding a payload the provider would reject with a 400. Under budget it measures only and touches nothing. Off by default. |
| `--context-window N` | model table | Override the built-in per-model context window (tokens). Set this when your deployment's real limit differs from the table. |
| `--output-reserve N` | `4096` | Completion tokens to reserve before admitting input (a request's own `max_tokens` wins per request). |
| `--recent-window N` | `6` | Trailing messages treated as the high-value working set the governor protects longest. |
| `--semantic-cache` | off | **Semantic answer cache**: reuse a near-identical previous answer for self-contained factual questions (paraphrases of "What is CI?"). Conservative + opt-in: edits, file paths, code and imperatives are never cached; answers are scoped per model. Semantic hits are booked as `semantic`, not `cache`, so the exact cache's hit rate stays honest. |
| `--semantic-threshold 0.97` | `0.97` | Risk threshold for semantic reuse (0.97 = near-paraphrase only). Raise it to be more conservative, lower it with care. |

### Provider recipes

**GitHub Copilot (individual plan, your existing login):**
```bash
token-optimizer serve --copilot --api-key "$(gh auth token)"
```

**GitHub Copilot (business / enterprise):**
```bash
token-optimizer serve --copilot business --api-key "$(gh auth token)"
token-optimizer serve --copilot enterprise --api-key "$(gh auth token)"
```

**OpenAI:**
```bash
token-optimizer serve --api-key "$OPENAI_API_KEY"
```

**Anthropic (Claude models via the Anthropic wire):**
```bash
token-optimizer serve --anthropic-api-key "$ANTHROPIC_API_KEY"
# point Claude Code at ANTHROPIC_BASE_URL=http://127.0.0.1:4040
```

**Gemini:**
```bash
token-optimizer serve --gemini-api-key "$GEMINI_API_KEY"
```

**Custom OpenAI-compatible (Ollama / vLLM / LM Studio):**
```bash
token-optimizer serve --upstream http://127.0.0.1:11434/v1 --api-key ollama
```

**Full-featured (persistent cache, fallback, receipts):**
```bash
token-optimizer serve --copilot --api-key "$(gh auth token)" \
  --cache-db ~/.tokan/cache.db \
  --fallback-model gpt-4o-mini \
  --log-requests
```

### HTTP endpoints the gateway serves

| Path | Purpose |
|---|---|
| `POST /v1/chat/completions` | OpenAI wire |
| `POST /v1/messages` | Anthropic wire |
| `POST /v1beta/models/<m>:generateContent` (and `:streamGenerateContent`) | Gemini wire |
| `POST /v1/ingest` | **Doc Governor:** index a document once → `doc_id` |
| `POST /v1/doc/ask` | **Doc Governor:** ask an ingested document a question |
| `POST /v1/evidence` | **Evidence retriever:** ranked doc chunks for a query (docs only — repo evidence stays behind MCP/CLI) |
| `GET /healthz` (alias `/health`) | Liveness probe |
| `GET /models` (alias `/v1/models`) | Model-list passthrough (for agent pickers) |
| `GET /metrics` | Machine-readable session + cache metrics (JSON) |
| `GET /dashboard` | Human-readable live HTML dashboard |

### Document Q&A — `/v1/ingest` + `/v1/doc/ask`

The **Doc Governor** removes the classic document-Q&A waste: re-sending the
whole document with every question. Ingest once, ask many:

```bash
# 1) Ingest the document once (chunking + TF-IDF index + summary happen here)
curl -s http://127.0.0.1:4040/v1/ingest \
  -H 'Content-Type: application/json' \
  -d '{"text": "<full document text>", "metadata": {"source": "contract.pdf"}}'
```

```json
{
  "doc_id": "d_9d10a9d672e6d9e8",
  "chunks": 33,
  "total_tokens": 7858,
  "summary_tokens": 158,
  "summary": "…extractive summary of the most salient sentences…",
  "metadata": {"source": "contract.pdf"},
  "next": "POST /v1/doc/ask {\"doc_id\": \"...\", \"question\": \"...\", \"model\": \"...\"}"
}
```

`doc_id` defaults to a content hash (re-ingesting identical text returns the
same id); pass `doc_id` explicitly to override. `text` (or `document`) is
required — empty/missing text is a 400 with an explicit message.

```bash
# 2) Ask questions — as many as you like
curl -s http://127.0.0.1:4040/v1/doc/ask \
  -H 'Content-Type: application/json' \
  -d '{"doc_id": "d_9d10a9d672e6d9e8",
       "question": "What is the cap on liability?",
       "model": "gpt-4o-mini", "top_k": 3}'
```

The gateway assembles `[extractive summary + top-k retrieved excerpts +
question]` as a standard OpenAI-wire payload and runs it through the **normal
pipeline** — cache, receipts, model fallback, prefix/session tracking all apply.
A repeated question is a cache hit: **0 upstream tokens**. The response is an
ordinary `chat.completion` plus a disclosed `optimizer` block:

```json
{
  "choices": [{"message": {"role": "assistant", "content": "…"}}],
  "usage": {"prompt_tokens": 1086, "completion_tokens": 40},
  "optimizer": {
    "mode": "doc",
    "doc_id": "d_9d10a9d672e6d9e8",
    "route": "llm",
    "chunks_used": 3,
    "retrieved_chunks": [18, 17, 12],
    "full_doc_tokens": 7866,
    "sent_tokens": 1086,
    "tokens_avoided": 6780,
    "avoided_pct": 86.2,
    "coverage": 0.629,
    "low_coverage": false
  }
}
```

- `full_doc_tokens` is the naive baseline (whole document + question);
  `sent_tokens` is what Tokan actually forwarded; the difference is **doc
  tokens avoided** — reported separately from compaction savings.
- `coverage` (M5) is the idf-weighted fraction of the question's content
  vocabulary the document contains. Below **0.35** (`low_coverage: true`)
  the prompt sent upstream carries a strengthened abstention instruction
  ("this document appears not to cover this topic…") and the console line,
  receipt, dashboard card and `doc_low_coverage` metric all disclose the
  flag. The route does **not** change: no fabricated local "cannot find it"
  answer — the model still decides, with a fair warning.
- Headers: `X-Optimizer-Doc-Id`, `X-Optimizer-Doc-Avoided-Tokens` (these carry
  the economics for `stream: true` responses too).
- `question` may also be supplied OpenAI-style as `messages[]` (the last user
  turn is taken as the question). `top_k` clamps to 1–10. `temperature`,
  `top_p`, `max_tokens`, `user`, and `stream` pass through.
- Errors are explicit: unknown `doc_id` → **404** (the store is a bounded
  in-memory LRU, per gateway process — re-ingest after a restart); missing
  `question` / `model` / `doc_id` → **400** naming the field.

### Evidence retrieval — `POST /v1/evidence`

One ranked list of evidence units (bounded excerpts with token counts) for a
question — without calling a model. Useful for "show me what you'd send"
debugging, or for agents assembling their own prompts:

```bash
curl -s http://127.0.0.1:4040/v1/evidence \
  -H 'Content-Type: application/json' \
  -d '{"query": "What is the cap on liability?", "top_k": 5}'
```

```json
{
  "query": "What is the cap on liability?",
  "units": [
    {"source": "doc", "key": "d_9d10a9… • chunk 17/42", "score": 0.61,
     "tokens": 96, "title": "doc d_9d10a9… • chunk 17/42", "text": "…"}
  ],
  "count": 1,
  "tokens": 96,
  "scopes": ["doc"],
  "note": "repo-symbol evidence is served by the MCP 'evidence' tool (and CLI), not over unauthenticated HTTP"
}
```

- **Scope boundary:** this endpoint searches **ingested documents only** —
  unauthenticated HTTP never walks your filesystem. Repo-graph evidence
  (symbols, references, paths) is available through the MCP `evidence` tool
  and the CLI, which run with agent-local trust.
- Scores are store-native (TF-IDF cosine for documents) and labeled per unit;
  cross-store score calibration is **not** claimed. Optional `doc_ids` list
  restricts the search.

---

## 6. Wiring your agent

Two commands cover every agent:

```bash
token-optimizer list              # adapters + which are detected on this machine
token-optimizer setup <agent-id>  # exact wiring for one agent
```

`list` shows all adapters and marks the ones it found installed:

```
ID                   PROTOCOLS                HERE     NAME
──────────────────────────────────────────────────────────────────────
aider                openai                   ·        Aider
claude-code          anthropic+mcp            ✓        Claude Code
codex                openai+mcp               ✓        Codex CLI
cursor               openai+mcp               ✓        Cursor
github-copilot       openai+mcp               ✓        GitHub Copilot
...
```

`setup <id>` prints copy-pasteable snippets (env vars, config files, MCP
blocks). Add `--json` for machine-readable output, `--no-mcp` to omit the MCP
block, and `--host`/`--port` if your gateway isn't on `127.0.0.1:4040`.

Below are the concrete snippets for the most common agents (as printed by
`setup`; your exact values may differ — always trust the live `setup` output).

### Claude Code (`claude-code`) — Anthropic wire

Set the base URL to the gateway root (Claude Code appends `/v1/messages`):

```bash
# ~/.zshrc / ~/.bashrc
export ANTHROPIC_BASE_URL=http://127.0.0.1:4040
# keep your existing Anthropic key; the gateway forwards it upstream
```

Or in `~/.claude/settings.json`:

```json
{ "env": { "ANTHROPIC_BASE_URL": "http://127.0.0.1:4040" } }
```

Then add the MCP block (see §7) to `~/.claude.json` or a project `.mcp.json`.

### Codex CLI (`codex`) — OpenAI chat wire

`~/.codex/config.toml` (root keys must precede TOML tables):

```toml
model = "gpt-4o"
model_provider = "token-optimizer"

[model_providers.token-optimizer]
name = "Token Optimizer Gateway"
base_url = "http://127.0.0.1:4040/v1"
env_key = "OPENAI_API_KEY"
wire_api = "chat"    # the gateway speaks chat, not Responses
```

### GitHub Copilot in VS Code (`github-copilot`)

In VS Code settings, override the Copilot proxy URL to the gateway:

```json
"github.copilot.advanced": {
  "debug.overrideProxyUrl": "http://127.0.0.1:4040/v1"
}
```

VS Code supplies its own Copilot token; the gateway forwards it. For Copilot
Chat's CLI/provider mode you can instead point
`COPILOT_PROVIDER_BASE_URL=http://127.0.0.1:4040/v1`.

### Cursor (`cursor`), Cline (`cline`), Roo Code (`roo-code`), Continue (`continue`)

Each uses an OpenAI-compatible "custom / override" provider setting. Set the
base URL to `http://127.0.0.1:4040/v1` and use any/your real key. Run
`token-optimizer setup cursor` (etc.) for the exact field names and JSON.

### OpenCode (`opencode`) — OpenAI + Anthropic + MCP

OpenCode supports both wires; point either/both base URLs at the gateway and
add the MCP server for the optimization tools.

### Gemini CLI (`gemini-cli`)

Point the CLI at the gateway's Gemini endpoint via its base-URL env var:

```bash
export GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:4040
# pass a key via GEMINI_API_KEY, or let the gateway supply one with --gemini-api-key
```

### Zed (`zed`), Windsurf (`windsurf`) — MCP-first

These are wired primarily through the **MCP** server (§7). Run
`token-optimizer setup zed` / `setup windsurf` for the config path.

### Aider (`aider`), generic Anthropic (`anthropic-generic`), generic OpenAI (`openai-generic`)

- **Aider:** `aider --openai-api-base http://127.0.0.1:4040/v1` (plus key).
- **Generic Anthropic:** set `ANTHROPIC_BASE_URL=http://127.0.0.1:4040`.
- **Generic OpenAI:** set `OPENAI_BASE_URL=http://127.0.0.1:4040/v1`.

---

## 7. MCP integration

Beyond proxying, Tokan exposes an **MCP (Model Context Protocol) stdio server**
so agents can call its optimization tools directly.

**Tools exposed:**

| Tool | What it does |
|---|---|
| `optimize_context` | CDM-compact a payload and report tokens saved |
| `repo_index` | Structural repo map within a byte budget |
| `repo_graph` | Definitions / references / imports / test-edge graph within a byte budget (`truncated` flag) |
| `cache_lookup` | Semantic cache probe — "is this context already known?" |
| `cache_store` | Store a context in the cache |
| `session_metrics` | Live savings / ROI counters |
| `doc_ingest` | Index a long document once → `doc_id` + summary |
| `doc_search` | Summary + top-k excerpts for a question (vs re-reading the file) + coverage score |
| `evidence` | Ranked evidence units across docs **and** the repo graph — pass `root` to include symbol/path evidence |

Doc Q&A tools disclose `coverage` / `low_coverage`: below 0.35 the document
likely cannot answer the question — look elsewhere rather than guessing.

**Start it manually** (for testing):

```bash
token-optimizer mcp
```

**Register it with an agent.** `token-optimizer setup <agent>` prints the block;
for Claude Code (`~/.claude.json` or project `.mcp.json`):

```json
{
  "mcpServers": {
    "token-optimizer": {
      "command": "token-optimizer",
      "args": ["mcp"]
    }
  }
}
```

(The `command` is whatever launches Tokan on your machine — the `setup` helper
prints the absolute Python path it detected.)

---

## 8. Observability: dashboard, metrics, stats, receipts

Tokan makes savings **visible and auditable**, per request — not just a session
total.

### Live dashboard — `http://127.0.0.1:4040/dashboard`

An auto-refreshing HTML page showing:

- **Counters:** requests, cache hits, local solves, LLM calls, tokens saved,
  saved %, provider cache, prefix reuse, **Docs / Doc Q&A / Doc avoided**,
  fallbacks, cache entries, hit rate.
- **A rolling table of recent requests:** time, route, model, tokens in→out,
  saved % (rows from `/v1/doc/ask` carry a `doc` marker — their savings are
  measured against the full-document resend), status, **latency (tokan /
  upstream)**, provider cache, fallback.

The **latency split** is the important one for trust. Each row shows total
end-to-end latency and, in parentheses, how much was Tokan's own overhead vs.
time blocked on the provider generating:

```
llm     42→28    33.33%  200  1474ms (+1.1 tokan, 1473ms upstream)
cache   41→0    100.00%  200  0.39ms (+0.39 tokan)
local   33787→0 100.00%  200  29.74ms (+29.74 tokan)
```

Read it as: **the seconds you see are the model generating, not the gateway.**
Tokan adds ~1 ms on an LLM-route miss and sub-millisecond on a cache hit; a
`local` or `cache` route has *no* upstream at all, so its entire (tiny) latency
is Tokan's.

### Metrics — `http://127.0.0.1:4040/metrics`

Machine-readable JSON (`session` + `cache` counters). Use this for monitoring.

### `stats` command

```bash
token-optimizer stats --port 4040        # human-readable
token-optimizer stats --port 4040 --json # raw JSON
```

Example output:

```
requests      : 9
  cache hits  : 1
  local solves: 1
  llm calls   : 7
tokens saved  : 36517 (81.51%)
cost saved    : $0.09
cache hit rate: 0.09 (7 entries)
provider cache: 7552 cached tokens (31% of prompt, upstream's own)
prefix reuse  : 24891 tokens (base the provider discounts; kept stable by Tokan)
doc Q&A       : 4 questions over 1 doc(s), avoided 28410 tokens (87% vs full-doc resend)
fallbacks     : 1
```

### Session Governor — prefix reuse & provider cache ratio

Two numbers explain the *economics* of a multi-turn session (distinct from
Tokan's compaction savings, and never double-counted):

- **prefix reuse** — tokens that repeated the previous turn's prefix. This is
  the base the provider's prompt cache discounts. Tokan keeps that prefix
  byte-stable so the discount actually fires.
- **provider cache ratio** (`cached ÷ prompt`) — the share of upstream prompt
  tokens the provider served from its own cache, read from the response.

On the dashboard these appear as **Cache ratio** and **Prefix reuse** tiles plus
a per-row **Prefix** column; in `stats` and `/metrics` they are
`provider_cache_ratio` and `prefix_reused_tokens`. An agent can name its
conversation with an `X-Optimizer-Session: <id>` header (otherwise the key is
derived from the message root), and `--prompt-cache-key` can inject a stable
cache key. See the README's *Session Governor* section for the measurements.

### Doc Governor — doc tokens avoided

Document Q&A economics are tracked in their **own counters** (`/metrics`
`session` block, `stats` line, dashboard **Docs / Doc Q&A / Doc avoided**
tiles):

| Field | Meaning |
|---|---|
| `doc_ingests` / `doc_ingested_tokens` | documents stored / tokens stored |
| `doc_questions` | `/v1/doc/ask` requests served |
| `doc_full_tokens` | naive baseline: full document + question, per question |
| `doc_sent_tokens` | what Tokan actually forwarded (0 on cache hits) |
| `doc_tokens_avoided` | `doc_full_tokens − doc_sent_tokens` |
| `doc_avoided_pct` | avoided ÷ full, in percent |
| `doc_chunks_used` | retrieved excerpts sent (retrieval quality signal) |
| `doc_low_coverage` | doc asks whose question outran the document (M5 quality signal — not a saving) |
| `evidence_searches` / `evidence_units` | `/v1/evidence` + MCP `evidence` calls / units returned (M5) |

These are deliberately **not** mixed into `tokens_saved`: compaction savings
and retrieval avoidance have different baselines, and one number per baseline
is what stays defensible. Provider prompt-cache discounts remain the
provider's, surfaced only.

### Context Governor — budget sheds (`--governor`)

When the governor is on, budget decisions are booked in their **own counters**
(`/metrics`, `stats`, dashboard **Governor sheds / Shed tokens / Overflows**
tiles):

| Field | Meaning |
|---|---|
| `governor_sheds` | requests where context was shed to fit the model's window |
| `governor_shed_tokens` | estimated tokens withheld from the provider |
| `governor_overflows` | requests whose *required* context alone exceeded the window (forwarded unchanged — the provider decides) |

Shed tokens are an **estimate** of what we withheld and are deliberately
**not** counted in `tokens_saved` — shedding is risk, not provenance: we can't
claim the provider would have accepted the un-shed payload. The receipt and
manifest carry the full plan (window, reserve, kept/shed indices) for audit.

### What `tokens_saved` is measured on

Session `tokens_saved` is booked on the **provider's token basis**: the raw
payload's local chars/4 estimate is projected by what the provider actually
billed for the sent payload, so tokenizer disagreement is never booked as
savings — a clean payload claims exactly 0 — and completion tokens (model
output) are excluded, since only context counts as saved. `benchmarks/repo_ab.py`
cross-checks these claims against real Copilot bills (measured: claims within
10.6% of billed — the gap is the unbillable cache counterfactual, which stays
an estimate — and doc counters within 1.2%).

### Receipts — `--log-requests`

Start the gateway with `--log-requests` to get one console line per request:

```
03:55:34 /v1/chat/completions route=llm status=200 model=gpt-4o-mini 41->27 tok (34.15%) 1553.56ms [tokan +1.43ms, upstream 1552.12ms] prefix_reuse=1390 upstream_cache=1664 (87%)
12:04:11 /v1/doc/ask route=llm status=200 model=gpt-4o-mini 7866->1086 tok (86.2%) 1421.07ms [tokan +1.9ms, upstream 1419.2ms] doc=d_9d10a9d672e6d9e8 avoided=6780tok
```

Each receipt carries: route, status, model, tokens in→out, savings %, total
latency, the **tokan/upstream latency split**, plus **prefix reuse**, the
**provider cache ratio** (`upstream_cache=… (…%)`), any fallback event, and —
for doc asks — the `doc=<id> avoided=<n>tok` line where `original_tokens` is
the full-document baseline.

---

## 9. Persistent cache & model fallback

### Persistent cache — `--cache-db PATH`

By default the cache is an in-process LRU that dies with the gateway. Point it
at a SQLite file to make repeat-savings **survive restarts**:

```bash
token-optimizer serve --copilot --api-key "$(gh auth token)" --cache-db ~/.tokan/cache.db
```

- WAL-mode SQLite; a corrupt/truncated row is treated as a **miss**, never a crash.
- TTL uses wall-clock time (meaningful across restarts).
- The dashboard `CACHE ENTRIES` count and `stats` reflect the stored entries.

### Model fallback — `--fallback-model MODEL`

Copilot plans gate many model ids (and OpenAI 404s unknown ones). When the
upstream rejects a request with `model_not_supported` / `model_not_found`,
Tokan retries **once** with the fallback model and **discloses** it:

```bash
token-optimizer serve --copilot --api-key "$(gh auth token)" --fallback-model gpt-4o-mini
```

The retry shows up as:
- header `X-Optimizer-Fallback: <requested> -> <fallback>`
- a `FALLBACK` cell on the dashboard
- an incremented `fallbacks` counter in `stats` / `/metrics`

This turns a hard 400/404 into a working answer while being transparent that a
different model was used.

---

## 10. The repo indexer (`index`)

Generate a compact structural map of a repository — useful as an `AGENTS.md` /
context file so agents don't waste tokens walking the tree:

```bash
token-optimizer index .                    # writes ./AGENTS.md (8 KB budget)
token-optimizer index . --out -            # print to stdout
token-optimizer index . --max-bytes 20000  # bigger budget
token-optimizer index . --ignore dist --ignore vendor   # extra ignores
token-optimizer index . --cache .tokan/index-cache.json # incremental rebuilds
token-optimizer index . --graph-out graph.json          # dump the repo graph (M5)
```

It walks the tree (honoring `.gitignore` + built-in ignores), collects a
symbol index, and emits a size-bounded Markdown map.

**Incremental builds (`--cache`)** keep a JSON symbol cache keyed by file
`(mtime, size)`: a warm rebuild re-reads nothing (verified by read-count
instrumentation) — typically ~2× faster cold-vs-warm on mid-size repos, and
the gap widens with repo size since only the walk repeats. A corrupt or
stale cache fails **open**: it's ignored and rebuilt.

**`--graph-out`** additionally computes the M5 `RepoGraph` — definitions,
whole-word cross-file references, Python import edges and test→source edges —
and writes it as JSON. The same graph serves the MCP `repo_graph` and
`evidence` tools. Import resolution is a line-anchored best effort (documented
heuristics, not an AST): re-exports and dynamic imports are not followed.

---

## 11. Realistic expectations — what actually saves tokens

Tokan's core promise is **honesty**: it reports real numbers, including the
cases where there is nothing to save. Here's what to expect, based on the
committed measurement battery (`benchmarks/scenarios.py`, results in
`benchmarks/scenario-results.json`):

**Where Tokan saves a lot:**

| Scenario | Savings | Why |
|---|---:|---|
| Whole-file diff echoed with a small edit | **~96%** | Diff compaction keeps every `+`/`-` change line and elides unchanged context. |
| Noisy tool output (ANSI + hundreds of log lines) | **~20%** | ANSI stripped, blank runs collapsed, huge noise elided (head+tail kept). |
| Oversized-but-rescuable payload | **~11%** | Enough noise elided to slip back under the provider's prompt cap. |
| Exact repeat of a previous request | **100%** | Served from cache — zero upstream tokens. |
| Deterministically answerable question | **100%** (route `local`) | Answered on your machine, no LLM call. |
| Repeated document questions via `/v1/doc/ask` | **~86–88%** | Only summary + retrieved excerpts travel, not the whole document (measured, with recall@k reported alongside). |

**Where Tokan honestly saves ~0%:**

- **Clean prose / normal chat.** There is no waste to strip, so it reports 0%
  rather than manufacturing a number from JSON minification.
- **A real, change-dominated `git diff` already over the prompt cap.** The
  changes *are* the payload; there's nothing safe to elide, so the provider
  rejects it (400). Tokan shows this boundary rather than hiding it.
- **A growing multi-turn conversation.** The cache is exact-match, so each new
  turn is a deliberate miss. Repeated-prefix savings in that case come from the
  **provider's** prompt cache (surfaced as `PROV.CACHE`, never claimed by Tokan).

**Latency.** The end-to-end time you observe is dominated by the model
generating. Tokan's own overhead — measured on the dashboard's split — is
single-digit milliseconds on a miss and sub-millisecond on a hit.

The full 11-scenario table (with the honest negatives) is in the
[README](../README.md#live-scenario-tests-v030-dashboard--receipts).

---

## 12. Troubleshooting

**`gateway unreachable at 127.0.0.1:4040` (from `stats`)**
The gateway isn't running. Start it with `token-optimizer serve ...`. Confirm
with `curl -s http://127.0.0.1:4040/healthz`.

**`Address already in use` on start**
Another process holds the port. Find/stop it, or use a different port:
```bash
lsof -ti:4040            # what's using 4040
token-optimizer serve --port 4041 ...
```
Then pass the matching `--port` to `setup`/`stats`.

**400 `model_max_prompt_tokens_exceeded`**
The payload is over the provider's prompt cap (Copilot `gpt-4o-mini` caps at
12,288 tokens) and had nothing safe to elide (e.g. a change-dominated diff).
This is an upstream limit, not a Tokan bug. See §11.

**400/404/422 on a model id**
The plan gates that model. Add `--fallback-model <supported-model>` so Tokan
retries once and discloses it (§9).

**401 on the Anthropic wire**
The gateway is fronting Copilot (a Copilot OAuth token), which it will not
forward to `api.anthropic.com`. To use real Claude models, start the gateway
with `--anthropic-api-key` instead of `--copilot`.

**`token-optimizer: command not found`**
The pipx bin dir isn't on your `PATH`. Either use the absolute path
(`~/.local/bin/token-optimizer`) or add it to `PATH`, or invoke
`python3 -m tokan`.

**Changes I made aren't taking effect (running from source)**
A previously-installed version may be shadowing the repo. Run from a neutral
directory, or reinstall:
```bash
pipx upgrade tokan
# or run the repo directly:
python3 -m tokan serve ...
```

**Everything shows 0% savings**
Likely you're sending clean text, which is correct — see §11. Try a noisy tool
output or a whole-file diff to see compaction, and a repeated question to see a
cache hit.

**Slow responses**
Check the dashboard latency split (§8). If `upstream` is large, that's the
model generating — not Tokan. Tokan's `tokan` component should stay in single-digit
milliseconds.

---

## 13. Full command reference

```
token-optimizer <command>

  list                          List agent adapters + local detection
      --json

  setup <agent>                 Print wiring instructions for one agent
      --host HOST               (default 127.0.0.1)
      --port PORT               (default 4040)
      --json                    Machine-readable snippets
      --no-mcp                  Omit the MCP snippet

  serve                         Run the optimization gateway
      --host HOST               (default 127.0.0.1)
      --port PORT               (default 4040)
      --upstream URL            OpenAI-compatible upstream
      --api-key KEY             OpenAI-compatible / Copilot token
      --anthropic-upstream URL  (default https://api.anthropic.com)
      --anthropic-api-key KEY
      --gemini-upstream URL     (default https://generativelanguage.googleapis.com)
      --gemini-api-key KEY
      --copilot [PLAN]          individual | business | enterprise
      --extra-header "K: V"     Repeatable upstream header
      --cdn ENDPOINT            Remote edge-cache endpoint
      --namespace NAME          (default default)
      --threshold FLOAT         (default 0.88)
      --price FLOAT             USD per 1M tokens (default 2.50)
      --cache-db PATH           Persistent SQLite cache
      --fallback-model MODEL    Retry once on model_not_supported/not_found
      --log-requests            One receipt line per request
      --prompt-cache-key        Inject a stable prompt_cache_key (OpenAI wire)

  mcp                           Run the MCP server on stdio
      --cdn ENDPOINT
      --namespace NAME

  index [ROOT]                  Build a compact repo map (default ROOT .)
      --out FILE                (default AGENTS.md; '-' = stdout)
      --max-bytes N             (default 8192)
      --ignore PATTERN          Repeatable extra ignore
      --cache PATH              Incremental symbol cache (relative to root)
      --graph-out PATH          Also write the repo graph as JSON

  stats                         Metrics from a running gateway
      --host HOST               (default 127.0.0.1)
      --port PORT               (default 4040)
      --json
```

`python3 -m tokan <command>` is equivalent to `token-optimizer <command>`.

---

## 14. FAQ

**Do I need an API key?**
Not if you use GitHub Copilot — `--copilot --api-key "$(gh auth token)"` reuses
your existing subscription. For OpenAI/Anthropic/Gemini you supply that
provider's key.

**Does my data leave my machine?**
Only to the upstream provider you configure — exactly as if your agent talked
to it directly. Tokan itself binds to `127.0.0.1` and adds no telemetry.

**Is it safe to share on a LAN / with my team?**
It's designed for local use. If you bind `--host 0.0.0.0`, put it behind your
own auth/network controls. Use `--namespace` and a shared `--cdn`/`--cache-db`
deliberately if you *want* a shared team cache.

**Which agents are supported?**
Any that speak OpenAI, Anthropic, or Gemini wires, or MCP. Built-in adapters:
Aider, Claude Code, Codex, Cursor, Cline, Roo Code, Continue, OpenCode, Gemini
CLI, Zed, Windsurf, GitHub Copilot, plus generic OpenAI/Anthropic. See
`token-optimizer list`.

**Will it change model outputs?**
On the `llm` route the *context* sent to the model is compacted (noise removed,
diffs trimmed), which can change a long-context answer the same way trimming
context normally would. Change lines in diffs are always preserved. Cache and
local routes return previously-computed or locally-computed answers with no
model call.

**What is the "Session Governor" / prefix reuse? Is that Tokan's savings?**
No — and the distinction matters. Providers discount a repeated prompt *prefix*,
but only if it's byte-identical. Tokan keeps the prefix byte-stable so that
discount fires, and *measures* it (`prefix reuse`, `provider cache ratio`). Those
tokens are the **provider's** billing discount, surfaced for visibility; Tokan's
own savings are the compaction numbers (`tokens saved`). The two are reported
separately and never double-counted. See the README's *Session Governor* section.

**How do I report a bug or contribute?**
<https://github.com/deepusingh2530/Tokan> — issues and PRs welcome. The test
suite is stdlib `unittest`: `python3 -m unittest discover -s tests`.

---

*Tokan is MIT-licensed, stdlib-only, and developed by Deepu Singh
<dksingh1130@gmail.com>. Source: <https://github.com/deepusingh2530/Tokan>.*
