# Repository Map

Generated map of `Default Project` — structural index only; read files on demand.
## ./

- `AGENTS.md`
- `CONTRIBUTING.md` — MyAgentAdapter
- `README.md` — symbol_lookup, SQLiteStore, my_transport, MyCustomAgent, CustomJevRouter

## examples/

- `build_repo_index.py` — walk, rel, collect_symbols, build_index, main
- `run_proxy.py` — main
- `wrap_agent.py` — fake_transport, main

## tests/

- `fake_upstream.py` — Handler
- `test_adapters.py` — TestRegistry, TestGatewayInfo, TestSnippets, TestDetection
- `test_anthropic_proxy.py` — FakeAnthropicUpstream, AnthropicProxyFixture, TestAnthropicWire, TestStreamedTextExtraction
- `test_cache.py` — TestLocalCache, _EdgeHandler, TestRemoteTier
- `test_cdm.py` — TestStripAnsi, TestFingerprint, TestToolElision, TestConsecutiveDedup, TestFileTree, TestCompactText, TestEstimate
- `test_copilot_proxy.py` — refactor_payload, RecordingUpstream, RecordingFixture, TestHeaderPassthrough, TestUpstreamFlags
- `test_gemini_proxy.py` — FakeGeminiUpstream, TestGeminiProxy
- `test_jev.py` — TestJevRouter
- `test_mcp.py` — call, TestHandshake, TestTools, TestStdioLoop
- `test_pipeline_and_proxy.py` — FakeUpstream, UpstreamFixture, TestPipeline, TestSDKClient, TestProxyGateway
- `test_repo_index.py` — TestGitignoreAwareWalk

## tokan/

- `__init__.py`
- `__main__.py`
- `cli.py` — _gateway_info, cmd_list, cmd_setup, cmd_serve, _build_pipeline, cmd_mcp, cmd_index, cmd_stats, build_parser, main
- `client.py` — ChatResponse, OptimizedAgentClient
- `mcp.py` — MCPServer, _version, serve_stdio, main
- `metrics.py` — SessionMetrics
- `pipeline.py` — LocalExecutor, _default_handlers, PipelineResult, OptimizationPipeline
- `repo_index.py` — _gitignore_rules, _is_ignored, walk, collect_symbols, build_index

## tokan/adapters/

- `__init__.py`
- `base.py` — GatewayInfo, ConfigSnippet, AgentAdapter
- `builtin.py` — ClaudeCodeAdapter, CodexAdapter, AiderAdapter, CursorAdapter, ClineAdapter, RooCodeAdapter, ContinueAdapter, OpenCodeAdapter, GeminiCLIAdapter, ZedAdapter, WindsurfAdapter, GitHubCopilotAdapter
- `registry.py` — register, _load_entry_points, all_adapters, get, detect_all

## tokan/cache/

- `__init__.py`
- `edge_cache.py` — CDNConfig, EdgeCache

## tokan/cdm/

- `__init__.py`
- `normalizer.py` — estimate_tokens, strip_ansi, CanonicalRecord, CDMNormalizer

## tokan/jev/

- `__init__.py`
- `router.py` — JevDecision, JevRouter

## tokan/proxy/

- `__init__.py`
- `__main__.py`
- `interceptor.py` — resolve_upstream_flags, add_upstream_flags, ProxyConfig, _Handler, _extract_streamed_text, TokenOptimizerProxy, main

## Docs
- `AGENTS.md`
- `CONTRIBUTING.md`
- `README.md`

<!-- 38/42 tracked files, 2710 bytes -->
