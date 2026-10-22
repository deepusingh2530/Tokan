# Changelog

All notable changes to Tokan will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
