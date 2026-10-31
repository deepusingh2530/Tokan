# Tokan Architecture

Status: living document. Milestone M1 landed; later milestones referenced by phase.

Tokan is a provider-neutral token-optimization gateway for AI coding agents. It sits
between agents (Copilot BYOK, Codex, Aider, Gemini CLI, any OpenAI/Anthropic/Gemini
wire client) and upstream LLM providers, minimizing tokens sent upstream while
preserving answer quality and full recoverability of the original context.

## 1. Current assessment (as of M1)

What exists and works today, verified by tests and live traffic:

| Area | Implementation | State |
|---|---|---|
| Protocol adapters | OpenAI chat, OpenAI Responses, Anthropic Messages, Gemini generateContent (+ SSE for all) in `tokan/proxy/interceptor.py` | production |
| Normalization / compaction (CDM) | `tokan/cdm/normalizer.py`: canonical fingerprint, ANSI strip, whitespace collapse, tool-output elision, consecutive dedup, file-tree compaction, line elision, chars/4 estimate | production |
| Fingerprint de-volatilization | `tokan/pipeline.py::devolatile` + `<current_datetime>` canonicalization: rotating client-injected ids/timestamps scrubbed from the digest only (wire bytes untouched) | production |
| Exact answer cache | `tokan/cache/edge_cache.py` (local LRU → remote CDN → optional SQLite), digest-keyed, TTL, fail-open remote | production |
| Provider prompt-cache affinity | `--prompt-cache-key` injects a stable session-derived key | production |
| Deterministic routing (JeV) | `tokan/jev/router.py`: regex-heuristic local/llm decision; local requires a registered executor answer (never fabricated) | production (binary; no tiers) |
| Deterministic executors | `LocalExecutor` with a single `line_lookup` handler | minimal |
| Session/prefix measurement | `tokan/session.py`: per-session prefix reuse (honest, disclosed-not-claimed) | production (in-memory) |
| Document intelligence | `tokan/doc.py`: ingest-once, chunk, TF-IDF retrieve, extractive summary, honest "doc tokens avoided" accounting | production (lexical only, in-memory) |
| Repository intelligence | `tokan/repo_index.py`: gitignore-aware walk, regex top-level symbols, budgeted markdown map | basic (full rebuild, regex-only) |
| Metrics/receipts/dashboard | `tokan/metrics.py` + `/metrics`, `/receipts`, `/dashboard`, `X-Optimizer-*` headers, per-request receipts with token-accurate honest accounting | production (per-process) |
| MCP tools | `tokan/mcp.py`: 7 stdio tools (optimize, repo_index, cache, metrics, doc) | production |
| Deploy | `deploy/azure/main.bicep` (ACA + Redis + Key Vault), Dockerfile | production |
| Conversation persistence | none — history lives only in agent clients | **missing** |
| Context manifest | partial — receipts carry tokens/route/digest but no evidence ledger | **partial** |
| Context governor / budget | none — compaction applied uniformly; no context-limit awareness | **missing** |
| Quality gate / progressive expansion | none | **missing** |
| Authentication / tenant isolation | none — gateway is open; single-tenant cache scope | **missing** (tracked, phase 6) |

Known correctness gaps discovered during assessment:

- `EdgeCache` LRU is properly locked (verified); only its hit/miss counters
  race benignly. `SessionMetrics` counters have no lock — M2.
- `SessionMetrics` counters have no lock (same thread-safety concern) — M2.

## 2. Gap analysis against the target architecture

Numbering follows the target module list in the master plan.

| # | Target module | Exists as | Gap |
|---|---|---|---|
| 1 | Protocol adapters | interceptor path-dispatch + `prepare()` | no canonical model; per-protocol ad-hoc projection; no reconstruction guarantee |
| 2 | Auth/scope guard | — | no inbound auth, no tenant/user scope (M6) |
| 3 | Canonical request model | implicit `messages` list | no typed model, no extension container (M2) |
| 4 | Content classifier | heuristics inside CDM | no typed content classes/authority/fidelity (M3) |
| 5 | Secret protector | none | secret detection/redaction (M6) |
| 6 | Conversation store | — | **M1 (this milestone)** |
| 7 | Conversation state extractor | — | **M1 (deterministic subset)** |
| 8 | Context Governor | CDM compaction, applied uniformly | utility-per-token selection, required-context protection (M4) |
| 9 | Repository intelligence | repo_index map | symbols/calls/tests graph, incremental (M5) |
| 10 | Document intelligence | doc.py TF-IDF | semantic retrieval, coverage detection (M5) |
| 11 | Relevance/dependency retriever | doc TF-IDF only | cross-store retrieval (M4–M5) |
| 12 | Token Budget Controller | none | context limits, output reservation, per-model limits (M4) |
| 13 | CDM normalization | production | explicit L0–L4 levels + policy engine (M3) |
| 14 | Exact cache | production | scope keys lack tenant; key includes only model/temp/top_p/user (M2 scope work) |
| 15 | Prefix-cache optimizer | `--prompt-cache-key` + stable ordering | formal prompt-ordering module (M4) |
| 16 | Semantic cache | `semantic_digest` (ngram) unused for answers | conservative answer reuse w/ risk threshold (M4) |
| 17 | Intermediate-artifact cache | none | summaries/embeddings/index caches (M3–M5) |
| 18 | JeV decision engine | binary local/llm | multi-route policy + tiers (M6) |
| 19 | Deterministic executors | line_lookup only | executor plugin API + suite (M6) |
| 20 | Model router | single `--fallback-model` retry | capability tiers, chains, budget (M6) |
| 21 | Prompt assembler | inline per-protocol | governor-driven assembly (M4) |
| 22 | Quality gate | — | pre/post deterministic checks (M6) |
| 23 | Progressive fallback | — | context expansion loop (M6) |
| 24 | Memory update pipeline | cache fill only | M1 begins it (events); responses wired M2 |
| 25 | Optimization receipts | receipts + headers | manifest evidence ledger (M1 skeleton) |
| 26 | Metrics/tracing/eval | metrics + dashboard + benchmark scripts | OTel, locked counters, CI eval harness (M2–M6) |

## 3. Phased plan

Small, independently testable milestones. Each lands with tests + measurements.

- **M1 (done): Conversation memory foundation** — append-only `ConversationStore`
  (SQLite), deterministic `ConversationState` extraction, `ContextManifest`
  skeleton, observe-only gateway wiring behind `--conversation-db`. Zero
  behavior change when the flag is off (parity-tested).
- **M2: Canonical request model** — typed `CanonicalRequest` with extension
  container and lossless reconstruction for all four protocols; receipts gain
  request ids unconditionally; `SessionMetrics` locking. The store starts
  recording assistant responses on streaming paths too.
- **M3: Content classification + compression policy engine** — explicit L0–L4
  compression levels bound to content classes; authority protection
  (system/developer/tool schemas never above L1).
- **M4: Context Governor + Token Budget Controller** — per-model context limits,
  output reservation, utility-per-token selection over required/high-value/
  optional/redundant classes; progressive expansion loop; conservative semantic
  answer cache with risk threshold.
- **M5: Repository + document intelligence v2** — incremental symbol index,
  dependency/test relationships, evidence-unit retrieval; doc coverage detection.
- **M6: Quality gate + routing + production hardening** — pre/post-call
  deterministic validation, multi-tier model routing, executor plugin API,
  auth (bearer/tenant scoping), rate limits, OTel, eval harness in CI.

## 4. M1 — interfaces and storage schema

### 4.1 `tokan/memory.py::ConversationStore`

Append-only canonical conversation history (recoverability principle: optimization
reduces what is *transmitted*, never what is *retained*).

```python
ConversationStore(path, *, tenant="default", user="", retention_seconds=None)
    .append(conversation_id, *, source, kind, content, user=None) -> ConversationEvent
    .events(conversation_id, *, since_version=0, verify=False) -> List[ConversationEvent]
    .last(conversation_id) -> Optional[ConversationEvent]
    .verify(conversation_id) -> int          # integrity failures detected
    .prune() -> int                          # retention cutoff enforcement
    .conversations() -> List[str]
    .close()
```

Schema (stdlib sqlite3, WAL, thread-safe via lock):

```sql
CREATE TABLE IF NOT EXISTS conversation_events (
    tenant       TEXT NOT NULL,
    conversation TEXT NOT NULL,
    user         TEXT NOT NULL DEFAULT '',
    version      INTEGER NOT NULL,      -- per-conversation sequence, 1-based
    id           TEXT NOT NULL,         -- sha256(tenant|conversation|version|digest)[:32]
    ts           REAL NOT NULL,
    source       TEXT NOT NULL,         -- user | assistant | tool | system | optimization
    kind         TEXT NOT NULL,         -- message | manifest | state
    digest       TEXT NOT NULL,         -- sha256(canonical content json)
    content      TEXT NOT NULL,         -- canonical json
    PRIMARY KEY (tenant, conversation, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conv_version
    ON conversation_events (tenant, conversation, version);
```

Append-only is enforced by API (no update/delete methods other than `prune`)
plus per-row integrity digests (`verify()` detects any mutation).

### 4.2 `tokan/memory.py::ConversationState` / `extract_state(events)`

Deterministic (rule-based, no model calls) structured state:

- `objective` — text of the latest user message (bounded 400 chars)
- `constraints` — user-message sentences matching constraint markers
  (must/shall/never/always/do not/ensure/keep/preserve), bounded
- `files` — distinct file paths across the conversation, most recent first
- `open_questions` — question lines from the latest user message
- `version` — last incorporated event version; conflicts resolve latest-wins
  while both facts remain retrievable from the event log (never overwritten)

### 4.3 `tokan/memory.py::ContextManifest`

Per-request evidence ledger (skeleton in M1; grows in M3/M4 when the governor
can report real inclusion/omission decisions):

```json
{
  "rid": "…", "conversation": "…", "ts": 1.7e9,
  "protocol": "openai", "route": "llm", "status": 200, "model": "…",
  "digest": "16hex", "original_tokens": 6603, "optimized_tokens": 6587,
  "cache_hit": false, "volatile_scrubbed": 2, "prefix_reused": 0,
  "protected": {"system_messages": 1},
  "included": {"messages": 4}, "compressed": {"devolatilized": 2},
  "omitted": [], "policy": {"version": "cdm-1", "levels": ["L1", "L2"]},
  "risk": "low"
}
```

Stored as a `kind="manifest"` event; surfaced in receipts (`rid` key) only when
the store is enabled, so flag-off behavior stays byte-identical.

### 4.4 Gateway wiring (observe-only)

- `--conversation-db PATH` on `tokan serve` → `ProxyConfig.conversation_db`.
- Observer hooks at the single receipt choke point: last payload message
  (protocol-aware extraction from the *original* payload — the store keeps
  original bytes, never the scrubbed digest view), assistant response text
  where already available, manifest event.
- Conversation id: `X-Optimizer-Session` header when present, else the
  session-derived key already used for prefix measurement.
- **Fail-open**: any store failure (bad path, corrupt file, disk full) logs one
  warning and serving continues unchanged. Storage is never in the correctness
  path.
- Resend dedupe: a request whose last message digest equals the stored last
  event's digest appends no duplicate message event.

## 5. Decisions (ADR summaries)

1. **SQLite append-only log over an external DB** — stdlib-only codebase, zero
   ops burden, single-file backup; the schema ports to Postgres unchanged when
   multi-replica arrives.
2. **Deterministic state extraction only (M1)** — no model calls for state;
   every rule is unit-testable and reproducible. LLM-assisted extraction is
   deferred until deterministic coverage proves insufficient.
3. **Observe-only first** — the gateway's wire behavior, receipts, headers, and
   cache semantics are unchanged until a later milestone turns the governor on.
   Parity is enforced by tests, not convention.
4. **Manifest as stored event, not response header** — manifests are debugging
   and evaluation artifacts; keeping them out of responses preserves wire
   compatibility.
5. **Original bytes in the store** — the digest-scope scrubbing (devolatilization)
   is for cache keys only; the canonical history must remain lossless.
6. **Semantic answer cache stays off (unchanged from earlier decision)** — when
   M4 lands it will be conservative (high threshold, task-class and
   revision match), per the cache-safety rules.

## 6. Security threat model (summary)

Trust boundaries: agent → gateway (currently unauthenticated), gateway →
provider (key injected server-side), gateway → stores (local disk).

M1 mitigations: tenant column in the store schema (isolation from day one),
per-event integrity digests, retention support, no secrets in manifests
(digests and counts only), conversation content never logged unless
`--log-requests`.

Known accepted risks until M6: no inbound authentication (mitigated by bind
address + network placement; ACA deployment is publicly reachable — auth is the
top M6 item); no TLS on the gateway itself (terminate at ACA/ingress);
`/dashboard` and `/receipts` unauthenticated.

## 7. Observability plan

M1: receipts + `/metrics` + `/dashboard` unchanged; manifests persisted for
offline analysis. M2: request ids everywhere, locked counters. M6: OpenTelemetry
traces per stage (prepare → governor → upstream), structured logs, no raw
prompts by default.

## 8. Evaluation methodology

Existing harnesses (`benchmarks/prefix_cache.py`, `scenarios.py`, `doc_qa.py`,
`repo_ab.py`, Copilot A/B) provide token and quality baselines. M4 adds the
quality gate with deterministic checks; M6 runs the workload matrix (long
conversations, debugging, refactoring, tool-heavy loops, conflicting
instructions, cache invalidation) in CI against golden datasets, comparing
baseline vs lossless vs semantic vs aggressive vs progressive-fallback modes.
Claims are made only for measured numbers.

## 9. Test plan (M1)

- `tests/test_memory.py` — store ordering/versions, integrity verification
  (tamper detection), retention, tenant isolation, thread-safety under
  concurrent append; state extraction (objective/constraints/files/questions,
  latest-wins conflict); manifest shape; per-protocol last-message extraction
  (chat str/list content, Responses input items, Anthropic blocks, Gemini parts).
- `tests/test_conversation_observer.py` — end-to-end parity: gateway with store
  enabled returns byte-identical responses/headers; events persisted per turn;
  fail-open on store misconfiguration; resend dedupe.
- Full suite regression (245+ tests) must stay green.

## 10. Rollout

M1 is opt-in (`--conversation-db`); no flag → no change. Enabling it on the
deployed gateway requires only the flag plus a mounted volume; disabling is
instant. Later milestones follow the same pattern: observe-only first, opt-in
activation second, default-on only after measured parity.
