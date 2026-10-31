# Repository Map

Generated map of `Tokan` — structural index only; read files on demand.
## ./

- `AGENTS.md`
- `CONTRIBUTING.md` — MyAgentAdapter
- `README.md` — symbol_lookup, SQLiteStore, my_transport, MyCustomAgent, CustomJevRouter

## benchmarks/

- `ab_copilot.py` — gh_token, post, billed, make_payloads, run_leg, main
- `bench_enterprise.py` — bench
- `bench_gateway.py` — record, pct, timed, free_port, FakeUpstream, _BenchServer, refactor_payload, unique_payload, local_payload, noisy_blob, post_json, get_json
- `doc_qa.py` — contract, tokens_of, baseline_messages, run_offline, _post, _graded, run_live, main
- `live_prefix.py` — _post, _conv, main
- `prefix_cache.py` — _tokenize, _make_cache_upstream, _noisy_tool_output, _conversation, _post, _run_mode, _sum, main
- `repo_ab.py` — gh_token, post, get, prompt_of, completion_of, answer_text, git, build_payloads, build_docset, grade, squash, parse_receipts
- `scenarios.py` — metrics, post, msg_level, run
- `test_enterprise_hybrid.py` — _workspace_settings, _mcp_config, TestEnterpriseHybrid

## docs/

- `ARCHITECTURE.md`

## examples/

- `build_repo_index.py` — walk, rel, collect_symbols, build_index, main
- `real_session_replay.py` — ensure_requests_clone, read_files, mcp_example, FakeLLM, _Server, free_port, session_example, router_example, main
- `run_proxy.py` — main
- `wrap_agent.py` — fake_transport, main

## extensions/vscode-tokan/

- `README.md`
- `build.sh`

## extensions/vscode-tokan/out/

- `extension.js` — gatewayBase, authHeaders, activate, deactivate

## extensions/vscode-tokan/src/

- `extension.ts` — gatewayBase, authHeaders, activate, deactivate

## tests/

- `fake_upstream.py` — Handler
- `test_adapters.py` — TestRegistry, TestGatewayInfo, TestSnippets, TestDetection
- `test_anthropic_proxy.py` — FakeAnthropicUpstream, AnthropicProxyFixture, TestAnthropicWire, TestStreamedTextExtraction
- `test_cache.py` — TestLocalCache, _EdgeHandler, TestRemoteTier
- `test_canonical.py` — TestRoundTrip, TestDigestParity, TestStructuredPreservation, TestExtensions, TestManifestStats
- `test_cdm.py` — TestStripAnsi, TestFingerprint, TestToolElision, TestConsecutiveDedup, TestFileTree, TestCompactText, TestEstimate
- `test_conversation_observer.py` — _Upstream, _EchoUpstream, _chat, _wait_messages, TestConversationObserver
- `test_copilot_proxy.py` — refactor_payload, RecordingUpstream, RecordingFixture, TestHeaderPassthrough, TestUpstreamFlags
- `test_devolatile.py` — _payload, TestDevolatile, TestDigestStability
- `test_doc_governor.py` — _contract, _question, _post, _post_status, _get, _oracle_upstream, _make_server, _run, TestChunking, TestRetrieval, TestSummary, TestDocumentStore
- `test_enterprise_layers.py` — Config, simple_function, long_method, Processor, AppComponent, TestASTIntegrity, TestASTFallback, TestEmbeddingCache, TestRoutingAccuracy, TestEnterpriseIntegration
- `test_evidence.py` — _fixture_repo, TestRepoMatchScore, TestEvidenceDocOnly, TestEvidenceCrossStore
- `test_gateway_auth.py` — _post, _post_status, _get, _get_status, _EchoUpstream, _run, _chat_payload, TestEnsureBindSafety, TestInboundAuth, TestSecretPosture, _doc_config, TestDocQualityGate, TestTaskRouter, TestUpstreamKeyFromEnv
- `test_gemini_proxy.py` — FakeGeminiUpstream, TestGeminiProxy
- `test_governor.py` — _Upstream, _conv, TestTokenBudget, TestContextWindowRegistry, TestClassification, TestSelection, TestDigestParity, TestWireBinding, TestSemanticCache, TestSemanticPipelineBinding, TestForwardPathE2E, TestMetricsAccounting
- `test_jev.py` — TestJevRouter
- `test_mcp.py` — call, TestHandshake, TestTools, TestStdioLoop
- `test_memory.py` — _StoreCase, TestConversationStore, TestStateExtraction, TestManifest, TestLastPayloadMessage
- `test_pipeline_and_proxy.py` — _UpstreamServer, FakeUpstream, UpstreamFixture, TestPipeline, TestSDKClient, TestProxyGateway
- `test_policy.py` — _Upstream, _decision, TestClassification, TestPolicyLevels, TestAuthorityProtection, TestDigestIndependence, TestLevelAwareCompactText, TestForwardPathAuthority
- `test_repo_index.py` — _fixture_repo, TestGitignoreAwareWalk, TestSymbolCache, TestRepoGraph, TestIndexCLI
- `test_responses_route.py` — _UpstreamServer, responses_payload, TestResponsesProjection, TestStreamedResponsesExtraction, _FakeResponsesUpstream, _make_handler, TestResponsesGateway
- `test_secret.py` — TestDetectionPrecision, TestRedaction, TestDigestHygiene, TestPatternSet
- `test_session_governor.py` — _post, _get, _wait_receipts, _tokenize, _make_server, _run, _cache_upstream, _conv, TestPrefixStability, TestSessionTracker, TestGovernorMetrics, TestPipelineIntegration
- `test_tier1_v030.py` — _post, _get, _ChatUpstream, _make_server, _run, TestSqliteStore, TestReceiptsAndDashboard, _CacheUpstream, TestUpstreamPromptCache, _GatedUpstream, TestModelFallback, TestDiffCompaction

## tokan/

- `__init__.py`
- `__main__.py`
- `ast_parser.py` — ASTIntegrityError, ASTParser, parse_ast, compact_ast_text
- `canonical.py` — ContentPart, CanonicalMessage, ToolDef, CanonicalRequest, parse, to_payload, _parse_openai_chat, _parse_chat_message, _parse_chat_part, _parse_openai_tool, _tool_to_openai_wire, _to_openai_chat
- `cli.py` — _gateway_info, cmd_list, cmd_setup, cmd_serve, _build_pipeline, cmd_mcp, cmd_index, cmd_stats, build_parser, main
- `client.py` — ChatResponse, OptimizedAgentClient
- `doc.py` — _terms, chunk_text, TfidfIndex, summarize, Document, full_resend_tokens, assemble_messages, DocumentStore
- `evidence.py` — EvidenceUnit, _content_terms, repo_match_score, EvidenceRetriever
- `governor.py` — context_window_for, UtilityClass, ContextUnit, TokenBudget, GovernorPlan, ContextGovernor, SemanticAnswerCache, _text_of, _last_user_index, _is_verbatim_repeat, _shed_key
- `mcp.py` — MCPServer, _version, serve_stdio, main
- `memory.py` — _canon, content_digest, ConversationEvent, ConversationStore, ConversationState, _event_text, extract_state, ContextManifest
- `metrics.py` — SessionMetrics
- `pipeline.py` — LocalExecutor, usage_cached_tokens, responses_input_to_messages, devolatile, scrub_for_digest, upstream_cached_tokens, _default_handlers, PipelineResult, OptimizationPipeline
- `policy.py` — ContentClass, Authority, CompressionLevel, ContentDecision, classify, classify_part, _classify_user, _CODE_SIGNALS, CompressionPolicy
- `repo_index.py` — _gitignore_rules, _is_ignored, walk, _read_text, symbols_of, collect_identifiers, collect_symbols, SymbolCache, build_index, _is_test, _py_imports, _module_map
- `secret.py` — SecretFinding, detect_secrets, redact_text, count_payload_secrets, redact_payload
- `semantic_cache.py` — SemanticCacheHit, _now_mono, _stable_embedding, _cosine, semantic_digest, EmbeddingCache
- `session.py` — PrefixReuse, SessionTracker
- `task_router.py` — AdaptiveTaskRouter

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
- `interceptor.py` — resolve_upstream_flags, add_upstream_flags, ProxyConfig, ensure_bind_safety, _Handler, _last_payload_message, _is_model_rejection, _extract_streamed_usage, _extract_streamed_text, _GatewayServer, TokenOptimizerProxy, main

## Docs
- `AGENTS.md`
- `CONTRIBUTING.md`
- `README.md`
- `docs/ARCHITECTURE.md`
- `extensions/vscode-tokan/README.md`

<!-- 84/99 tracked files, 8066 bytes -->
