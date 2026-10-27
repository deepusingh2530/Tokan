# Repository Map

Generated map of `Default Project` — structural index only; read files on demand.
## ./

- `AGENTS.md`
- `CONTRIBUTING.md` — MyAgentAdapter
- `README.md` — symbol_lookup, SQLiteStore, my_transport, MyCustomAgent, CustomJevRouter
- `setup_github.sh`

## benchmarks/

- `ab_copilot.py` — gh_token, post, billed, make_payloads, run_leg, main
- `bench_gateway.py` — record, pct, timed, free_port, FakeUpstream, _BenchServer, refactor_payload, unique_payload, local_payload, noisy_blob, post_json, get_json
- `doc_qa.py` — contract, tokens_of, baseline_messages, run_offline, _post, _graded, run_live, main
- `live_prefix.py` — _post, _conv, main
- `prefix_cache.py` — _tokenize, _make_cache_upstream, _noisy_tool_output, _conversation, _post, _run_mode, _sum, main
- `repo_ab.py` — gh_token, post, get, prompt_of, completion_of, answer_text, git, build_payloads, build_docset, grade, squash, parse_receipts
- `scenarios.py` — metrics, post, msg_level, run

## examples/

- `build_repo_index.py` — walk, rel, collect_symbols, build_index, main
- `real_session_replay.py` — ensure_requests_clone, read_files, mcp_example, FakeLLM, _Server, free_port, session_example, router_example, main
- `run_proxy.py` — main
- `wrap_agent.py` — fake_transport, main

## tests/

- `fake_upstream.py` — Handler
- `test_adapters.py` — TestRegistry, TestGatewayInfo, TestSnippets, TestDetection
- `test_anthropic_proxy.py` — FakeAnthropicUpstream, AnthropicProxyFixture, TestAnthropicWire, TestStreamedTextExtraction
- `test_cache.py` — TestLocalCache, _EdgeHandler, TestRemoteTier
- `test_cdm.py` — TestStripAnsi, TestFingerprint, TestToolElision, TestConsecutiveDedup, TestFileTree, TestCompactText, TestEstimate
- `test_copilot_proxy.py` — refactor_payload, RecordingUpstream, RecordingFixture, TestHeaderPassthrough, TestUpstreamFlags
- `test_doc_governor.py` — _contract, _question, _post, _post_status, _get, _oracle_upstream, _make_server, _run, TestChunking, TestRetrieval, TestSummary, TestDocumentStore
- `test_gemini_proxy.py` — FakeGeminiUpstream, TestGeminiProxy
- `test_jev.py` — TestJevRouter
- `test_mcp.py` — call, TestHandshake, TestTools, TestStdioLoop
- `test_pipeline_and_proxy.py` — _UpstreamServer, FakeUpstream, UpstreamFixture, TestPipeline, TestSDKClient, TestProxyGateway
- `test_repo_index.py` — TestGitignoreAwareWalk
- `test_session_governor.py` — _post, _get, _tokenize, _make_server, _run, _cache_upstream, _conv, TestPrefixStability, TestSessionTracker, TestGovernorMetrics, TestPipelineIntegration, TestGovernorEndToEnd
- `test_tier1_v030.py` — _post, _get, _ChatUpstream, _make_server, _run, TestSqliteStore, TestReceiptsAndDashboard, _CacheUpstream, TestUpstreamPromptCache, _GatedUpstream, TestModelFallback, TestDiffCompaction

## tokan/

- `__init__.py`
- `__main__.py`
- `cli.py` — _gateway_info, cmd_list, cmd_setup, cmd_serve, _build_pipeline, cmd_mcp, cmd_index, cmd_stats, build_parser, main
- `client.py` — ChatResponse, OptimizedAgentClient
- `doc.py` — _terms, chunk_text, TfidfIndex, summarize, Document, full_resend_tokens, assemble_messages, DocumentStore
- `mcp.py` — MCPServer, _version, serve_stdio, main
- `metrics.py` — SessionMetrics
- `pipeline.py` — LocalExecutor, usage_cached_tokens, upstream_cached_tokens, _default_handlers, PipelineResult, OptimizationPipeline
- `repo_index.py` — _gitignore_rules, _is_ignored, walk, collect_symbols, build_index
- `session.py` — PrefixReuse, SessionTracker

## tokan/adapters/

- `__init__.py`
- `base.py` — GatewayInfo, ConfigSnippet, AgentAdapter
- `builtin.py` — ClaudeCodeAdapter, CodexAdapter, AiderAdapter, CursorAdapter, ClineAdapter, RooCodeAdapter, ContinueAdapter, OpenCodeAdapter, GeminiCLIAdapter, ZedAdapter, WindsurfAdapter, GitHubCopilotAdapter
- `registry.py` — register, _load_entry_points, all_adapters, get, detect_all

## tokan/cache/

- `__init__.py`
- `edge_cache.py` — CDNConfig, EdgeCache
- `sqlite_store.py` — SqliteStore

## tokan/cdm/

- `__init__.py`
- `normalizer.py` — estimate_tokens, strip_ansi, CanonicalRecord, CDMNormalizer

## tokan/jev/

- `__init__.py`
- `router.py` — JevDecision, _sample, JevRouter

## tokan/proxy/

- `__init__.py`
- `__main__.py`
- `interceptor.py` — resolve_upstream_flags, add_upstream_flags, ProxyConfig, _Handler, _is_model_rejection, _extract_streamed_usage, _extract_streamed_text, _GatewayServer, TokenOptimizerProxy, main

## Docs
- `AGENTS.md`
- `CONTRIBUTING.md`
- `README.md`

<!-- 53/64 tracked files, 4440 bytes -->
