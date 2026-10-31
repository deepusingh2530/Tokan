# Changelog

All notable changes to Tokan will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-10-31

First stable release. **Compatibility statement:** the provider wires
(OpenAI / Anthropic / Gemini), the receipt schema (additive-only keys),
`/metrics` and the CLI flags are stable surfaces from 1.0.0 — breaking
changes will bump the major version. 456 tests pass.

The milestone train M1–M6 (stacked PRs #6–#12, built on the
`feature/enterprise-hybrid` base):

- **M1 — Conversation memory foundation** (`tokan/memory.py`, PR #7):
  append-only SQLite `ConversationStore`, deterministic `ConversationState`
  extraction (no model calls), `ContextManifest` receipts; observe-only
  behind `--conversation-db`, fail-open when storage is unavailable.
- **M2 — Canonical request model** (`tokan/canonical.py`, PR #8): typed
  `CanonicalRequest` with per-protocol round-trip; request `rid` on every
  receipt; digest parity across OpenAI / Anthropic / Gemini / Responses
  wires as a hard contract.
- **M3 — Content classification + compression policy** (`tokan/policy.py`,
  PR #9): typed content classes, authority tiers, L0–L4 compression levels;
  system/developer instructions, tool schemas and the user's ask are capped
  lossless — runtime features never change `record.digest`.
- **M4 — Context Governor + Token Budget Controller** (`tokan/governor.py`,
  PR #10): per-model windows, output reserve, utility-class shedding under
  opt-in `--governor` (sheds counted in a separate ledger, never in
  `tokens_saved`); conservative factual-only `--semantic-cache`.
- **M5 — Repository + document intelligence v2** (PR #12): incremental
  `SymbolCache` + `RepoGraph`, cross-store `EvidenceRetriever`, idf-weighted
  `Document.coverage` with low-coverage abstention disclosure; doc Q&A
  benchmark −87.8% prompt tokens with 8/8 gold-fact recall (labeled
  synthetic) and 3/3 answer parity live.
- **M6 — Hardening: inbound auth, secret hygiene, quality gate, routing**
  (this release):
  - **Inbound authentication:** `--api-keys` / `TOKAN_API_KEYS`; clients
    present `Authorization: Bearer <key>` or `X-Optimizer-Api-Key: <key>`;
    constant-time comparison, `/healthz` stays open for probes, 401 with
    `WWW-Authenticate` and the key never echoed, failures counted
    (`auth_failures`). A non-loopback bind without keys **refuses to
    start** unless `--allow-insecure` acknowledges it (fail-closed).
  - **Secret hygiene** (`tokan/secret.py`): detect-and-disclose by default —
    ten *documented* credential formats (precision over recall), bytes
    untouched, findings reported on receipts / metrics / dashboard.
    Opt-in `--redact-secrets` replaces each finding with
    `[REDACTED:<type>]` before canonicalization, the cache digest and the
    upstream call, so no stored artifact can contain a secret; two payloads
    differing only in secret material fingerprint to **one digest** (cache
    keys never encode secrets). Scan capped at 2 MB per string value.
  - **Quality gate:** `--doc-fallback-full` — when coverage says the
    document likely cannot answer, send the **full document** instead of
    weak excerpts (wrapped so CDM long-line protection cannot truncate it),
    disclosed as `doc_fallback: "full_doc"` with honest accounting: savings
    drop, that is the price of the guarantee.
  - **Routing wire-up:** `--task-router` classifies each request into
    tier_0/1/2 and discloses it (receipt `tier`, `/metrics` `tiers`,
    `router_requests`); the model is rewritten **only** with an explicit
    `--tier1-model` — never defaulted, so BYOK/plan-gated flows keep working.
  - 35 new tests (`tests/test_secret.py`, `tests/test_gateway_auth.py`).

### Added
- **Session Governor (Phase 1) — stable-prefix engineering for provider prompt
  caches.** Coding agents resend their full history every turn; providers
  discount a repeated *prefix* (often ~0.1×) but only if it is byte-identical.
  Tokan now guarantees its compaction is deterministic (so the shared prefix
  stays byte-stable and the provider's cache keeps firing) and *measures* the
  reused prefix:
  - `SessionTracker` (`tokan/session.py`) derives a per-conversation session key
    (or reads an `X-Optimizer-Session` header) and computes the longest common
    prefix — in tokens — with the previous turn. That reused prefix is the base
    the provider discounts.
  - Per-request **prefix reuse** and **provider cache ratio** on receipts, the
    console (`--log-requests`), `/metrics`, `stats`, and new dashboard tiles
    (`Cache ratio`, `Prefix reuse`) plus a per-row `Prefix` column.
  - **Honesty contract:** the reused prefix and provider cache ratio are the
    *provider's* billing discount, which Tokan *enables* by keeping the prefix
    stable — surfaced, never counted as Tokan's compaction savings.
  - **`--prompt-cache-key`** (opt-in): injects a stable `prompt_cache_key` into
    OpenAI-wire payloads so the provider routes the conversation to one cache.
  - Benchmarks: `benchmarks/prefix_cache.py` (fake prefix-caching upstream —
    −27.3% full-price tokens/turn with the cache ratio unchanged) and
    `benchmarks/live_prefix.py` (real Copilot — measured prefix reuse climbing
    with real `cached_tokens` hits). 17 new tests in
    `tests/test_session_governor.py`.
- **Doc Governor (Phase 2) — ingest once, answer many.** Document Q&A normally
  resends the entire document with every question. Tokan now serves
  `POST /v1/ingest` (chunk + TF-IDF index + extractive summary → `doc_id`) and
  `POST /v1/doc/ask` (retrieve top-k excerpts, assemble
  `[summary + excerpts + question]`, forward as a standard OpenAI-wire payload
  through the **normal pipeline** — cache, receipts, fallback, sessions all
  reused; a repeated question is a 0-token cache hit):
  - `tokan/doc.py`: sentence/paragraph-aware chunking with overlap (hard
    windows for oversized blobs), stdlib TF-IDF cosine retrieval (deterministic,
    no dependencies), IDF-salience extractive summary, bounded thread-safe LRU
    `DocumentStore` (content-addressed ids), prompt assembly + full-resend
    baseline math.
  - **Doc metrics honesty:** savings reported as `doc_tokens_avoided` against
    the naive full-document-resend baseline in dedicated counters
    (`doc_ingests`, `doc_questions`, `doc_full_tokens`, `doc_sent_tokens`,
    `doc_chunks_used`, `doc_avoided_pct`) — never mixed into `tokens_saved`,
    which stays compaction-only. Doc receipts use the full-doc baseline as
    `original_tokens`, so the dashboard's Tokens/Saved columns tell the truth
    for doc rows (plus a `doc` marker), with new **Docs / Doc Q&A / Doc
    avoided** tiles, `X-Optimizer-Doc-Id` / `X-Optimizer-Doc-Avoided-Tokens`
    headers, an `optimizer` block in response bodies, and `doc=… avoided=…`
    receipt lines.
  - **MCP:** `doc_ingest` + `doc_search` tools so coding agents can index a
    file once and retrieve excerpts per turn instead of re-reading it.
  - Benchmarks: `benchmarks/doc_qa.py` — offline (8-question gold-fact
    contract: −87.8% prompt tokens, recall@1 8/8) and `--live` (real Copilot,
    full-doc vs doc-ask side by side: −85.8% prompt tokens, answer parity 3/3
    vs 3/3). 37 new tests in `tests/test_doc_governor.py`, including an
    extractive-oracle upstream that can only answer if the gold fact actually
    reached the prompt.
- **Real-repository A/B benchmark (`benchmarks/repo_ab.py`)** — every payload
  built from an actual codebase (open source file, real `git diff --stat` +
  `git log` + file tree, a real unified diff, a resent-history session, an
  exact repeat, and Doc Q&A over the repo's own agent docs with gold-fact
  grading), sent twice with VS Code's Copilot identity headers — direct vs
  through a fresh gateway — plus a **VALIDATION block that checks the
  gateway's claims (receipts + `/metrics`) against the provider's bills**.
  Measured on a real 25 MB TypeScript repo: clean rows 0.0% (kept in,
  honestly), exact repeat 100%, Doc Q&A 60.6% with 3/3 answer parity,
  session total 28.7% (54,915 → 39,144 prompt tokens). Running it caught two
  of the defects fixed below.

### Fixed
- **Session savings booked on the provider's token basis.** `tokens_saved`
  subtracted upstream-billed tokens from the local chars/4 estimate of the
  raw payload, so tokenizer disagreement was booked as savings — the live
  A/B caught a 2× over-claim (11,009 claimed vs 5,403 billed). LLM legs now
  project the raw estimate onto what the provider actually billed (raw/sent
  ratio), so a clean payload claims exactly 0, and completion tokens (model
  output) no longer dilute the counter. Same basis applied to streamed
  responses when usage frames arrive. Re-measured: claims within 10.6% of
  provider bills (the gap is the cache counterfactual, which has no bill and
  stays an estimate), doc counters within 1.2%. 1 new regression test.
- **Doc summary no longer truncated on the wire.** The extractive summary
  was built as one long line, which the CDM normalizer's long-line
  protection (`max_line_chars=2000`) silently elided — for long documents
  ~2/3 of the summary never reached the provider, making doc prompts look
  cheaper than they should while quietly losing context. `summarize()` now
  wraps at 120 chars (no token cost — space becomes newline), doc prompts
  carry the whole summary, and the real-repo A/B re-measures Doc Q&A at
  58–63% with 3/3 answer parity on both legs. 1 new end-to-end regression
  test (183 tests total).

## [0.3.1] - 2026-10-26

### Added
- **Latency split (dashboard + receipts)** — every receipt now records
  `upstream_ms` (time actually blocked waiting on the provider to generate the
  answer) alongside end-to-end `ms`, and derives Tokan's own overhead as
  `ms - upstream_ms`. The dashboard's Latency column renders both
  (`1474ms (+1.1 tokan, 1473ms upstream)`), and `--log-requests` prints the
  same split, so a slow model generation is never misread as gateway slowness.
  Cache and local routes report `upstream_ms=0` — their entire (sub-30 ms)
  latency is Tokan's own, which is exactly the overhead number that matters.

## [0.3.0] - 2026-10-23

Five quality-of-life and correctness improvements, all stdlib-only.

### Added
- **Persistent cache (`--cache-db`)** — `SqliteStore`, a WAL-mode sqlite
  backend behind the existing `EdgeCache` store hook. Entries written by one
  gateway run are served by the next, so repeat-savings survive restarts
  instead of dying with the process. TTL is enforced with wall-clock time
  (monotonic clocks are meaningless across a restart); a corrupt or truncated
  row reads as a cache miss rather than raising.
- **Live dashboard (`/dashboard`)** — a single auto-refreshing HTML page
  showing session savings, cache hit rate, provider prompt-cache hits, and a
  rolling table of the last 40 requests (route, model, tokens in→out, latency,
  fallback). Human-facing; `/metrics` stays the machine interface.
- **Per-request receipts (`--log-requests`)** — one console line per request:
  route, status, model, tokens in→out, savings %, latency, plus any provider
  cache or fallback event. Every request that passes through the gateway is
  also recorded in the dashboard's rolling window, so savings are visible per
  request rather than only as a session aggregate.
- **Model fallback (`--fallback-model`)** — when the upstream rejects a model
  id with `model_not_supported` / `model_not_found` (Copilot plan-gates most
  picker ids; OpenAI 404s unknown ones), the gateway retries once with the
  configured fallback instead of surfacing the wall. Disclosed, not silent:
  the response carries `X-Optimizer-Fallback: <requested> -> <used>` and the
  event is counted in `/metrics`. Payload-shape errors (400 for a missing
  `messages` array) do **not** trigger a retry — only model-identity failures
  do, so a genuinely malformed request is never masked.

### Changed
- **Diff-aware compaction in CDM** — the normalizer detects unified diffs and
  elides runs of *unchanged context* lines longer than `2 × diff_context`
  (default `diff_context=3`), keeping the head, tail, and every `+`/`-`
  change line verbatim behind an explicit marker. Scope is honest and measured:
  a `git diff` of this repo's own history is change-dominated (~79% change
  lines) and saves ~0%, while a whole-file diff (an agent echoing a large file
  with a small edit) drops 96.8% of its tokens. Change lines are never lost.
- **Upstream prompt-cache surfacing** — the gateway now reads the provider's
  own cached-token count (OpenAI/Copilot `prompt_tokens_details.cached_tokens`,
  Anthropic `cache_read_input_tokens`, Gemini `usageMetadata.cachedContentTokenCount`)
  from both buffered and streamed responses. It surfaces the number as
  `X-Optimizer-Upstream-Cached-Tokens` and counts it in `/metrics`, disclosed
  as the *provider's* savings — never claimed as Tokan's.

## [0.2.3] - 2026-10-23

### Added
- **`benchmarks/ab_copilot.py`** — live A/B harness: the same session sent
  direct to `api.githubcopilot.com` (exactly what VS Code does without
  Tokan) and through a fresh-cache gateway on the same Copilot subscription.
  Billed tokens come from the upstream `usage` block on both legs, so
  cache/local savings are measured, not estimated. Live results (both in
  README): gpt-4o-mini session 11,999 → 8,609 tokens (28.3% less) with one
  provider-rejected oversized payload (400 `model_max_prompt_tokens_exceeded`)
  returning 200 OK through Tokan after compaction; gpt-4.1 session 27.5%
  less.

### Fixed
- **README benchmark section presented fabricated numbers** — the v0.1.0
  per-agent table (67.1% average reduction, "Cost Saved" column implying
  $1,545–3,002 per million tokens) predated the benchmark harness and no
  script in the repo produces it. Replaced with harness-measured results:
  `bench_gateway.py` 15-check performance table, shape-dependent savings
  from the live Copilot A/B, and verified cache behavior. The "98% hit rate
  (semantic digest)" claim was also wrong: whitespace variants hit 100%
  deterministically via the scrubbed digest, and the n-gram semantic
  digest is only used by the MCP server, not the gateway serving path.
  Headline claims updated from "53–78% / 70–85%" to measured ranges.
- **README architecture diagram** now matches the code: JeV threshold
  direction (local confidence ≥ 0.88 → "local"), Local Executor scope
  (line lookups built in; count/AST/symbol handlers pluggable), CDM
  whitespace handling (blank runs + trailing space; full collapse only on
  the digest basis), and the scope-keyed digest fact added.

## [0.2.2] - 2026-10-22

### Fixed
- **Gateway dropped connections on simultaneous bursts** — the gateway
  server inherited the stdlib listen backlog of 5, so when many agents
  connected at the same instant SYNs were dropped: measured ~246
  "connection reset by peer" + 109 upstream-fetch failures per 480
  requests at ~2.4k conn/s (benchmarks/bench_gateway.py scenario B6).
  The server now listens with a backlog of 128: 480/480 requests succeed
  at ~1.9k req/s offered load. Found by the benchmark harness.

### Added
- Regression test: a 30-way concurrent burst through the gateway must
  lose zero connections

## [0.2.1] - 2026-10-22

### Fixed
- **JeV routing DoS on huge pasted payloads** — the file-path feature regex
  (`(?:[\w.-]+/)+…`) backtracked exponentially on long slash-free lines: a
  single 2MB log/code paste stalled routing for ~19s (measured ~3.9s after
  CDM compaction). The regex now uses bounded segments, and all feature
  scans (query patterns + blob signals) run on a bounded head+tail sample —
  routing is ~750× faster on that payload (~25ms blob scan locally) and
  stays O(1) in payload size. Found by the
  new `benchmarks/bench_gateway.py` harness (B4 scenario, 2MB payload).

### Added
- **`benchmarks/bench_gateway.py`** — stdlib-only performance harness: real
  `token-optimizer serve` subprocess against a fake upstream with simulated
  LLM latency; covers CDM compaction speed/savings, EdgeCache lookup at 50k
  entries, gateway miss overhead / cache-hit / local-answer / 2MB-body /
  SSE-relay / 40-way-concurrency latency and throughput, gateway RSS, MCP
  stdio round-trip, and CLI startup — each checked against explicit targets,
  non-zero exit on failure (CI-ready)
- Regression tests: pathological 2MB blob must route in <250ms; path
  detection still works within the scan cap

## [0.2.0] - 2026-10-22

### Added

#### Distribution
- **PyPI**: `pip install tokan` / `pipx install tokan` — zero-dependency pure
  wheel, published by a tag-triggered GitHub Actions workflow using OIDC
  trusted publishing (no API tokens stored in GitHub)
- **CI matrix**: GitHub Actions tests on Python 3.9–3.14 on every push/PR
- **Single-file release asset**: `tokan.pyz` zipapp attached to every GitHub
  Release — runs on any machine with Python 3.9+, no pip required

#### GitHub Copilot support
- **`github-copilot` adapter** — Copilot CLI BYOK provider env vars, VS Code
  ``overrideProxyUrl`` settings snippet (with the Business/Enterprise plan
  caveat), gateway serve command, and ``~/.copilot/mcp-config.json`` MCP entry
  (``copilot mcp add`` one-shot in the snippet note)
- **Credential passthrough** — client auth (BYOK keys, Copilot OAuth tokens)
  and identity headers (``Copilot-Integration-Id``, ``Editor-Version``,
  ``Editor-Plugin-Version``, ``Openai-Organization``, ``Conversation-ID``,
  ``User-Agent``, ``X-Request-Id``, ``x-api-key``, ``x-goog-api-key``) are
  forwarded upstream unchanged unless a gateway key is configured for that
  wire — the gateway key always wins and drops the client credential
- **``fallback_headers``** on ``ProxyConfig`` — applied only where the client
  sent no value (stand-in Copilot identity headers for BYOK clients)
- **``--copilot [PLAN]``** serve flag — points the OpenAI wire at a Copilot
  plan's API origin (``individual`` | ``business`` | ``enterprise``) and
  installs fallback identity headers
- **``--extra-header "Key: Value"``** serve flag (repeatable) — explicit
  upstream headers that override anything the client sent
- **``GET /models`` + ``GET /v1/models`` passthrough** — model pickers keep
  working; the upstream catalog is relayed with the same auth rules

#### Gemini wire passthrough (completes the 0.1.0 preview)
- **``generateContent`` forwarding** — ``contents`` text parts are compacted
  in place (new public ``CDMNormalizer.compact_text``); non-text parts
  (``inline_data``, ``function_call``) and the ``generationConfig`` /
  ``systemInstruction`` envelope pass through verbatim
- **``streamGenerateContent`` SSE** — classified, relayed frame-by-frame,
  and cache-served locally in Gemini SSE shape; extracted answer text (never
  SSE framing) fills the cache
- **``--gemini-upstream`` / ``--gemini-api-key``** serve flags; client
  ``x-goog-api-key`` passes through when no gateway key is set
- **Gemini CLI adapter upgraded** to openai+mcp — ``GOOGLE_GEMINI_BASE_URL``
  passthrough is real now, replacing the roadmap note

### Fixed
- **MCP launch command referenced the renamed module** — ``GatewayInfo``
  defaulted to ``python -m token_optimizer mcp`` (``ModuleNotFoundError``);
  now ``python -m tokan mcp`` in defaults, CLI banner, and every snippet
- **Entry-point group renamed** ``token_optimizer.adapters`` →
  ``tokan.adapters`` to match the published ``pyproject.toml`` docs
- **Repo index now respects the root ``.gitignore``** — committed ``AGENTS.md``
  maps no longer reference local-only build output; ``!`` negations are
  unsupported by design (a map that re-includes an ignored path is worse
  than one that omits it)

## [0.1.0] - 2026-10-22

### Added

#### Core Pipeline
- **Canonical Data Model (CDM)** normalizer with noise-insensitive fingerprinting
  - ANSI escape code stripping
  - Whitespace collapsing and long-line elision
  - Tool output spam reduction (head/tail preservation)
  - Structured content preservation (Anthropic tool_result blocks, image parts)
  - Deterministic SHA-256 digest with scope-aware keys
- **Edge Cache** with two-tier architecture
  - Local LRU with configurable max_entries and TTL enforcement
  - Optional remote tier via HTTP PUT/GET to CDN endpoint
  - Fail-open by default (dead remote → local miss, never outage)
  - Semantic digest for whitespace-insensitive matching
- **JeV Router** (Jevons Paradox Engine)
  - System-1 decision engine with confidence threshold (default 0.88)
  - Regex feature extraction over query and payload shape
  - Deterministic answer detection to prevent fabricated responses
- **Local Executor** for zero-token deterministic answers
  - Line number lookups
  - Exact count queries
  - Pluggable handler system

#### Wire Protocols
- **OpenAI Chat Completions** (`/v1/chat/completions`)
  - Full request/response passthrough
  - Streaming relay with SSE framing
- **Anthropic Messages** (`/v1/messages`)
  - System prompt folding into cache digest scope
  - Content block preservation
  - Local `count_tokens` endpoint (`/v1/messages/count_tokens`)
  - Streaming relay with Anthropic SSE events
- **Gemini GenerateContent** (`/v1beta/models/*/generateContent`)
  - Content-to-messages conversion for cache digesting
  - GenerationConfig passthrough
  - Full response shaping

#### Integration Patterns
1. **Transparent Gateway Proxy**
   - Auto protocol detection from path
   - Response headers with optimization diagnostics (`X-Optimizer-Route`, `X-Optimizer-Saved-Tokens`, etc.)
   - `/healthz` and `/metrics` endpoints
   - Streaming support for all protocols
2. **In-Process SDK Client** (`OptimizedAgentClient`)
   - Injectable transport for testing
   - Session metrics and ROI reporting
   - Support for all three protocols
3. **MCP Server** (stdio)
   - 5 optimization tools: `optimize_context`, `repo_index`, `cache_lookup`, `cache_store`, `session_metrics`
   - JSON-RPC 2.0 compliant (protocol version 2025-06-18)
   - Auto-discoverable via MCP client registration

#### Agent Adapters (13 built-in)
- **Claude Code**: `ANTHROPIC_BASE_URL` env var, `.mcp.json`
- **Codex CLI**: `~/.codex/config.toml` with `wire_api = "chat"`, `[mcp_servers]` TOML
- **Aider**: `OPENAI_API_BASE` env / `~/.aider.conf.yml`
- **Cursor**: Models settings base URL, `.cursor/mcp.json`
- **Cline**: "OpenAI Compatible" provider, `cline_mcp_settings.json`
- **Roo Code**: "OpenAI Compatible" provider, `mcp_settings.json`
- **Continue**: `~/.continue/config.yaml` `baseURL`, `mcpServers` YAML
- **OpenCode**: `providers.baseURL` override, `mcp.servers` (V2 format)
- **Gemini CLI**: `GOOGLE_GEMINI_BASE_URL` env var, `.gemini/settings.json`
- **Zed**: `context_servers` MCP
- **Windsurf**: `mcp_config.json`
- **Any OpenAI SDK**: `OPENAI_BASE_URL` env var (catch-all)
- **Any Anthropic SDK**: `ANTHROPIC_BASE_URL` env var (catch-all)

**Extensibility**:
- Entry-point discovery: `[project.entry-points."tokan.adapters"]`
- Runtime registration: `@register` decorator
- Fail-safe: broken plugins never take down the registry

#### CLI
- `token-optimizer list`: Show adapters + local detection
- `token-optimizer setup <agent>`: Print wiring instructions
- `token-optimizer serve [options]`: Run gateway
- `token-optimizer mcp`: Run MCP server on stdio
- `token-optimizer index [root]`: Build repo `.md` mapper
- `token-optimizer stats`: Show metrics from running gateway

#### Repo Indexer
- Byte-budgeted structural map generation
- Directory-grouped file listing with top-level symbols
- Configurable ignore patterns and output size
- Shared by CLI and MCP `repo_index` tool

#### Tests (75 passing)
- Unit tests for all modules
- End-to-end integration tests with fake upstreams
- Protocol-specific tests (OpenAI, Anthropic, Gemini)
- Adapter registry and detection tests
- MCP JSON-RPC dispatch and tool behavior tests

#### Documentation
- Comprehensive README with architecture diagram
- Agent compatibility matrix (13 agents × 3 protocols)
- MCP tool reference
- CLI reference
- API reference for SDK and MCP server
- Configuration guide (TOML, env vars)
- Deployment recipes (systemd, Docker, nginx)
- Troubleshooting guide
- Production security considerations
- Benchmark results

### Changed
- Renamed project from `token-optimizer` to `Tokan`
- Package directory renamed from `token_optimizer` to `tokan`
- CLI command remains `token-optimizer` for backward compatibility

### Fixed
- `rstrip("/v1")` edge case eating trailing digits from ephemeral ports (use `origin` property)
- Streamed SSE framing leaking into cache (parse answer text before caching)
- `detect_all()` not honoring fail-safe promise (broken adapter detection errors surfaced)
- `register()` returning instance instead of class from decorator (breaks `ClassName()` calls)

---

## [Unreleased] (planned)

### Planned
- OpenAI Responses API (`/v1/responses`) support
- Real BPE token counting (replace ~chars/4 heuristic)
- Persistent cache backends (SQLite, Redis, S3)
- Performance optimizations (faster digests, streaming compaction)
