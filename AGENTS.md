# Repository Map

Generated map of `Default Project` — structural index only; read files on demand.
## ./

- `AGENTS.md`
- `README.md`

## examples/

- `build_repo_index.py` — walk, rel, collect_symbols, build_index, main
- `run_proxy.py` — main
- `wrap_agent.py` — fake_transport, main

## tests/

- `fake_upstream.py` — Handler
- `test_adapters.py` — TestRegistry, TestGatewayInfo, TestSnippets, TestDetection
- `test_anthropic_proxy.py` — FakeAnthropicUpstream, AnthropicProxyFixture, TestAnthropicWire, TestStreamedTextExtraction
- `test_cache.py` — TestLocalCache, _EdgeHandler, TestRemoteTier
- `test_cdm.py` — TestStripAnsi, TestFingerprint, TestToolElision, TestConsecutiveDedup, TestFileTree, TestEstimate
- `test_jev.py` — TestJevRouter
- `test_mcp.py` — call, TestHandshake, TestTools, TestStdioLoop
- `test_pipeline_and_proxy.py` — FakeUpstream, UpstreamFixture, TestPipeline, TestSDKClient, TestProxyGateway

## token_optimizer/

- `__init__.py`
- `__main__.py`
- `cli.py` — _gateway_info, cmd_list, cmd_setup, cmd_serve, _build_pipeline, cmd_mcp, cmd_index, cmd_stats, build_parser, main
- `client.py` — ChatResponse, OptimizedAgentClient
- `mcp.py` — MCPServer, _version, serve_stdio, main
- `metrics.py` — SessionMetrics
- `pipeline.py` — LocalExecutor, _default_handlers, PipelineResult, OptimizationPipeline
- `repo_index.py` — walk, collect_symbols, build_index

## token_optimizer/adapters/

- `__init__.py`
- `base.py` — GatewayInfo, ConfigSnippet, AgentAdapter
- `builtin.py` — ClaudeCodeAdapter, CodexAdapter, AiderAdapter, CursorAdapter, ClineAdapter, RooCodeAdapter, ContinueAdapter, OpenCodeAdapter, GeminiCLIAdapter, ZedAdapter, WindsurfAdapter, OpenAIGenericAdapter
- `registry.py` — register, _load_entry_points, all_adapters, get, detect_all

## token_optimizer/cache/

- `__init__.py`
- `edge_cache.py` — CDNConfig, EdgeCache

## token_optimizer/cdm/

- `__init__.py`
- `normalizer.py` — estimate_tokens, strip_ansi, CanonicalRecord, CDMNormalizer

## token_optimizer/jev/

- `__init__.py`
- `router.py` — JevDecision, JevRouter

## token_optimizer/proxy/

- `__init__.py`
- `__main__.py`
- `interceptor.py` — ProxyConfig, _Handler, _extract_streamed_text, TokenOptimizerProxy, main

## Docs
- `AGENTS.md`
- `README.md`

<!-- 34/35 tracked files, 2251 bytes -->
