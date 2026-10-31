# Tokan Architecture

Status: living document. Milestones M1–M5 landed; later milestones referenced by phase.

Tokan is a provider-neutral token-optimization gateway for AI coding agents. It sits
between agents (Copilot BYOK, Codex, Aider, Gemini CLI, any OpenAI/Anthropic/Gemini
wire client) and upstream LLM providers, minimizing tokens sent upstream while
preserving answer quality and full recoverability of the original context.

## 1. Current assessment (as of M5)

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
| Document intelligence | `tokan/doc.py`: ingest-once, chunk, TF-IDF retrieve, extractive summary, honest "doc tokens avoided" accounting; M5 adds idf-weighted coverage detection (`Document.coverage`) with low-coverage abstention disclosure | production (lexical only, in-memory) |
| Repository intelligence | `tokan/repo_index.py`: gitignore-aware walk, regex top-level symbols, budgeted markdown map; M5 adds incremental `SymbolCache` (mtime+size keyed) and `RepoGraph` (definitions/references/Python-import/test edges) | production (incremental, best-effort regex/import heuristics) |
| Evidence retriever | `tokan/evidence.py` (M5): ranked units across ingested docs (TF-IDF cosine) + repo graph (documented name/path heuristic), labeled per-store scores, bounded excerpts | production (docs via HTTP+MCP; repo graph via MCP/CLI only) |
| Metrics/receipts/dashboard | `tokan/metrics.py` + `/metrics`, `/receipts`, `/dashboard`, `X-Optimizer-*` headers, per-request receipts with token-accurate honest accounting | production (per-process) |
| MCP tools | `tokan/mcp.py`: 9 stdio tools (optimize, repo_index, repo_graph, evidence, cache, metrics, doc) | production |
| Deploy | `deploy/azure/main.bicep` (ACA + Redis + Key Vault), Dockerfile | production |
| Conversation persistence | `tokan/memory.py` (M1): append-only SQLite `ConversationStore` + deterministic `ConversationState`, observe-only behind `--conversation-db` | production (fail-open) |
| Context manifest | `ContextManifest` per request: route, tokens, digest, protected/compressed/omitted, canonical + policy + governor stats | production |
| Context governor / budget | `tokan/governor.py` (M4): per-model windows, output reserve, utility-class shedding of `messages` wires under `--governor` (opt-in) | production (opt-in) |
| Semantic answer cache | `SemanticAnswerCache` (M4): factual-only, model-scoped, high-threshold fuzzy reuse under `--semantic-cache` (opt-in) | production (opt-in) |
| Quality gate / progressive expansion | none | **missing** |
| Authentication / tenant isolation | none — gateway is open; single-tenant cache scope | **missing** (tracked, phase 6) |

Known correctness gaps discovered during assessment:

- `EdgeCache` LRU is properly locked (verified); its hit/miss counters raced
  benignly, and `SessionMetrics` counters had no lock — both fixed in M2
  (thread-safe counters, tested under 8-thread hammering).
- `SessionMetrics` counters have no lock (same thread-safety concern) — M2.

## 2. Gap analysis against the target architecture

Numbering follows the target module list in the master plan.

| # | Target module | Exists as | Gap |
|---|---|---|---|
| 1 | Protocol adapters | interceptor path-dispatch + `prepare()` | no canonical model; per-protocol ad-hoc projection; no reconstruction guarantee |
| 2 | Auth/scope guard | — | no inbound auth, no tenant/user scope (M6) |
| 3 | Canonical request model | `tokan/canonical.py` (M2) | pipeline adoption in M3 (governor consumes `fingerprint_view`) |
| 4 | Content classifier | `tokan/policy.py` (M3) | typed classes + authority tiers; user/system capped L1 |
| 5 | Secret protector | none | secret detection/redaction (M6) |
| 6 | Conversation store | `tokan/memory.py` (M1) | done |
| 7 | Conversation state extractor | `ConversationState` (M1, deterministic) | done |
| 8 | Context Governor | `tokan/governor.py` (M4) | progressive expansion loop (M6); gemini/responses shedding (M6) |
| 9 | Repository intelligence | `SymbolCache` + `RepoGraph` (`tokan/repo_index.py`, M5): incremental index, definitions/references/imports/test edges, `index --cache/--graph-out`, MCP `repo_graph` | done (regex/import heuristics, documented) |
| 10 | Document intelligence | doc.py TF-IDF + `Document.coverage` (M5): idf-weighted coverage detection, low-coverage prompt caveat + disclosure, `doc_low_coverage` counter | done (auto-fallback deferred to M6) |
| 11 | Relevance/dependency retriever | `EvidenceRetriever` (`tokan/evidence.py`, M5): cross-store ranked units, per-store scores, `POST /v1/evidence` (docs) + MCP `evidence` (docs+repo) | done |
| 12 | Token Budget Controller | `TokenBudget` + `context_window_for` (M4) | done (operator override via `--context-window`) |
| 13 | CDM normalization | production | explicit L0–L4 levels + policy engine (`tokan/policy.py`, M3) |
| 14 | Exact cache | production | scope keys lack tenant; key includes only model/temp/top_p/user (M2 scope work) |
| 15 | Prefix-cache optimizer | `--prompt-cache-key` + stable ordering + session measurement | formal prompt-ordering module (deferred; measurement honest in `session.py`) |
| 16 | Semantic cache | `SemanticAnswerCache` (M4, opt-in conservative) | done (factual-only, model-scoped, 0.97 threshold, TTL+LRU) |
| 17 | Intermediate-artifact cache | none | summaries/embeddings/index caches (M3–M5) |
| 18 | JeV decision engine | binary local/llm | multi-route policy + tiers (M6) |
| 19 | Deterministic executors | line_lookup only | executor plugin API + suite (M6) |
| 20 | Model router | single `--fallback-model` retry | capability tiers, chains, budget (M6) |
| 21 | Prompt assembler | inline per-protocol + governor-driven `messages` shedding (M4) | gemini/responses governor assembly (M6) |
| 22 | Quality gate | — | pre/post deterministic checks (M6) |
| 23 | Progressive fallback | — | context expansion loop (M6) |
| 24 | Memory update pipeline | cache fill only | M1 begins it (events); responses wired M2 |
| 25 | Optimization receipts | receipts + headers + `ContextManifest` ledger (M1/M4) | done |
| 26 | Metrics/tracing/eval | metrics + dashboard + benchmark scripts | OTel, locked counters, CI eval harness (M2–M6) |

## 3. Phased plan

Small, independently testable milestones. Each lands with tests + measurements.

- **M1 (done): Conversation memory foundation** — append-only `ConversationStore`
  (SQLite), deterministic `ConversationState` extraction, `ContextManifest`
  skeleton, observe-only gateway wiring behind `--conversation-db`. Zero
  behavior change when the flag is off (parity-tested).
- **M2 (done): Canonical request model** — `tokan/canonical.py`: typed
  `CanonicalRequest` for all four wires with structured content preserved
  (images/tool calls/tool results never flattened), extension container for
  unmodeled provider fields, byte-equal `to_payload()` reconstruction
  (round-trip tested), and `fingerprint_view()` that reproduces
  `pipeline.prepare()`'s digest exactly (parity-tested; cache keys stable
  when M3 adopts it). Receipts gain request ids unconditionally (additive
  key); `SessionMetrics` and `EdgeCache` counters are thread-safe; manifests
  record canonical-model stats on live traffic.
- **M2: Canonical request model** — typed `CanonicalRequest` with extension
  container and lossless reconstruction for all four protocols; receipts gain
  request ids unconditionally; `SessionMetrics` locking. The store starts
  recording assistant responses on streaming paths too. (Done — streaming
  responses were already recorded in M1; M2 added the canonical model,
  unconditional rid, and counter locking.)
- **M3 (done): Content classification + compression policy engine** —
  `tokan/policy.py`: `ContentClass`/`Authority`/`CompressionLevel` (explicit
  L0–L4), deterministic `classify()`, and a `CompressionPolicy` bound into the
  wire-forward choke points. Authority protection enforced at the mechanism:
  system/developer instructions, tool schemas and the user's own constraints
  cap at **L1 lossless** (ANSI/whitespace only — no diff compaction, no
  long-line elision, no truncation); only NORMAL/LOW content reaches L3. The
  cache digest is provably policy-independent (asserted for all four
  protocols), so adopting the policy can never change a cache key.
- **M4 (done): Context Governor + Token Budget Controller** — `tokan/governor.py`:
  per-model context windows (`context_window_for`, longest family-prefix match,
  operator override via `--context-window`), output reservation, and
  utility-class selection (REQUIRED/HIGH_VALUE/OPTIONAL/REDUNDANT) that sheds
  only when a request would overflow its window. Under budget the governor is
  pure measurement (`applied=False`, bytes + cache key untouched); over budget
  it sheds redundant → optional → high-value, never the system/developer
  instructions, tool exchanges (atomic unit) or the newest user ask; if the
  required context alone overflows it fails open (forward everything, flag
  `overflow`). Shedding binds to the `messages` wire (chat/anthropic); gemini
  and responses measure only and never claim bytes changed. The digest is
  computed before the plan, so shedding can never change a cache key
  (asserted for all four protocols). Conservative semantic answer cache:
  opt-in `--semantic-cache`, factual-only, model-scoped, high risk threshold,
  TTL+LRU; semantic hits route as `semantic`, not `cache`, so the digest
  cache's hit rate stays honest. Measured overhead: +156µs on a 52-message
  conversation (~3µs/message), +30µs on a 10-turn; semantic probe 15µs.
- **M5 (done): Repository + document intelligence v2** — `tokan/repo_index.py`
  gains an incremental `SymbolCache` (JSON on disk, keyed by
  `(mtime_ns, size)`; a warm build re-reads nothing — verified by read-count
  instrumentation; corrupt/stale caches fail open) and `RepoGraph` via
  `build_graph()`: top-level symbol definitions (source files before test
  files, first-definer-wins), whole-word cross-file references (excluding
  every definer), Python import edges (line-anchored parse, longest-prefix
  dotted resolution extended to the first imported name so `from a import b`
  prefers `a/b.py`), and test→source edges (Python via resolved imports —
  precise; non-Python via whole-symbol mention — best effort). Surfaced via
  `index --cache/--graph-out` and the MCP `repo_graph` tool (budgeted JSON
  with a `truncated` flag). `tokan/evidence.py` adds `EvidenceRetriever`:
  one ranked list of evidence units across ingested documents (store-native
  TF-IDF cosine) and the repo graph (documented heuristic
  `best*(0.6+0.4*coverage)` capped 0.99; exact symbol matches intentionally
  outrank moderate chunk cosines; scores are labeled per store and **not**
  cross-calibrated — disclosed on every unit). HTTP `POST /v1/evidence`
  searches ingested docs only (unauthenticated HTTP never walks the
  filesystem; repo evidence lives behind MCP/CLI with agent-local trust).
  Doc coverage detection: `Document.coverage` measures the idf-weighted
  fraction of the question's content vocabulary the document contains
  (stopwords dropped first); below the a-priori `LOW_COVERAGE=0.35` the
  assembled prompt strengthens its abstention instruction and the ask is
  disclosed (`coverage`/`low_coverage` in the response `optimizer` block,
  receipts, console line, dashboard card, `doc_low_coverage` counter, MCP
  `doc_search`). On labeled synthetic fixtures (8 answerable / 8 off-topic
  questions) the split is 0/8 answerable flagged, 8/8 off-topic flagged —
  reported honestly as synthetic, not production precision. The inverted
  reference pass also made uncached graph builds ~9x faster on a 600-file
  corpus (O(total identifiers) instead of O(symbols × files)).
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

Stored as a `kind="manifest"` event (never a response header — wire
compatibility). Receipts carry a request `rid` unconditionally since M2
(additive key, so existing consumers are unaffected).

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
6. **Semantic answer cache is conservative-when-on (updated in M4)** — landed
   as designed: opt-in (`--semantic-cache`, default off), high risk threshold
   (0.97), factual-only task-class gate (edits/file paths/code excluded),
   model-scoped (an answer from one model is never served for another), TTL +
   LRU bounded, memory-only. Semantic hits get their own `semantic` route so
   the digest cache's hit rate stays honest, per the cache-safety rules.

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
