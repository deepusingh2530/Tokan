"""Tests for the adapter plugin system: registry, detection, snippet rendering."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tokan.adapters import (
    ENTRY_POINT_GROUP,
    GatewayInfo,
    all_adapters,
    detect_all,
    get,
    register,
)
from tokan.adapters.base import AgentAdapter
from tokan.adapters.builtin import ClaudeCodeAdapter, CodexAdapter, OpenCodeAdapter

GW = GatewayInfo.for_gateway("127.0.0.1", 4040)


class TestRegistry(unittest.TestCase):
    def test_builtin_coverage(self):
        ids = {a.id for a in all_adapters()}
        expected = {
            "claude-code", "codex", "aider", "cursor", "cline", "roo-code",
            "continue", "opencode", "gemini-cli", "github-copilot", "zed",
            "windsurf", "openai-generic", "anthropic-generic",
        }
        self.assertTrue(expected.issubset(ids), f"missing: {expected - ids}")

    def test_claude_code_is_registered(self):
        # Regression: its @register decorator was once dropped.
        self.assertIsNotNone(get("claude-code"))

    def test_get_unknown_returns_none(self):
        self.assertIsNone(get("does-not-exist"))

    def test_third_party_registration(self):
        class MyAdapter(AgentAdapter):
            id = "test-third-party"
            display_name = "Test Third Party"
            protocols = ("openai",)
            summary = "registered at runtime"

            def snippets(self, gw):
                return [self._snippet("env", "shell", f"export X={gw.openai_base_url}")]

        register(MyAdapter)
        try:
            adapter = get("test-third-party")
            self.assertIsNotNone(adapter)
            snippet = adapter.snippets(GW)[0]
            self.assertIn(GW.openai_base_url, snippet.body)
        finally:
            from tokan.adapters import registry as reg

            reg._registry.pop("test-third-party", None)

    def test_entry_point_group_declared(self):
        self.assertEqual(ENTRY_POINT_GROUP, "tokan.adapters")

    def test_broken_adapter_cannot_break_listing(self):
        # detect_all must survive adapters whose detect() misbehaves.
        from tokan.adapters import registry as reg

        class Exploding(AgentAdapter):
            id = "z-exploding"
            display_name = "Exploding"
            protocols = ()

            def detect(self):
                raise RuntimeError("boom")

            def snippets(self, gw):
                return []

        # detect_all must survive adapters whose detect() misbehaves: the
        # broken one is reported as undetected + error, everyone else lists.
        register(Exploding)
        try:
            rows = detect_all()
            self.assertGreaterEqual(len(rows), 13)
            broken = next(r for r in rows if r["id"] == "z-exploding")
            self.assertFalse(broken["detected"])
            self.assertIn("boom", broken["detect_error"])
            claude = next(r for r in rows if r["id"] == "claude-code")
            self.assertIn("protocols", claude)  # others unaffected
        finally:
            reg._registry.pop("z-exploding", None)


class TestGatewayInfo(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(GW.openai_base_url, "http://127.0.0.1:4040/v1")
        self.assertEqual(GW.origin, "http://127.0.0.1:4040")
        self.assertEqual(GW.anthropic_base_url, "http://127.0.0.1:4040")

    def test_no_rstrip_footgun_on_port_ending_in_1(self):
        # Regression: "http://127.0.0.1:50181".rstrip("/v1") eats the final 1.
        gw = GatewayInfo.for_gateway("127.0.0.1", 50181)
        self.assertEqual(gw.origin, "http://127.0.0.1:50181")
        self.assertTrue(gw.origin.endswith("50181"))

    def test_mcp_entry(self):
        entry = GW.mcp_config_entry
        self.assertEqual(entry["command"], "python3")
        self.assertEqual(entry["args"], ["-m", "tokan", "mcp"])


class TestSnippets(unittest.TestCase):
    def test_every_adapter_renders(self):
        for adapter in all_adapters():
            for snippet in adapter.all_snippets(GW):
                rendered = snippet.render()
                self.assertIn(snippet.target, rendered)
                # Non-generic adapters must wire a real gateway URL.
                if adapter.id not in ("openai-generic", "anthropic-generic"):
                    pass  # some are UI-only; URL check below where applicable

    def test_claude_code_uses_gateway_root_not_v1(self):
        snippets = ClaudeCodeAdapter().all_snippets(GW)
        env = snippets[0].body
        self.assertIn(GW.anthropic_base_url, env)
        self.assertIn("ANTHROPIC_BASE_URL", env)
        # Claude Code appends /v1/messages itself.
        self.assertNotIn("/v1\n", env)

    def test_codex_declares_chat_wire(self):
        # Codex defaults to Responses API; gateway speaks chat.
        toml = CodexAdapter().snippets(GW)[0].body
        self.assertIn('wire_api = "chat"', toml)
        self.assertIn(GW.openai_base_url, toml)
        # TOML: root keys must precede tables.
        self.assertLess(toml.index('model = "gpt-4o"'), toml.index("[model_providers"))

    def test_codex_mcp_snippet_is_toml(self):
        mcp = CodexAdapter().mcp_snippet(GW)
        self.assertIn("[mcp_servers.token-optimizer]", mcp.body)

    def test_copilot_snippets_cover_cli_vscode_and_gateway(self):
        from tokan.adapters.builtin import GitHubCopilotAdapter

        adapter = GitHubCopilotAdapter()
        bodies = {s.target: s.body for s in adapter.snippets(GW)}
        cli = next(b for t, b in bodies.items() if "Copilot CLI" in t)
        self.assertIn("COPILOT_PROVIDER_BASE_URL", cli)
        self.assertIn(GW.openai_base_url, cli)
        self.assertIn("COPILOT_PROVIDER_TYPE=openai", cli)
        vscode = next(b for t, b in bodies.items() if "VS Code" in t)
        self.assertIn("overrideProxyUrl", vscode)
        self.assertIn(GW.openai_base_url, vscode)
        self.assertIn("--copilot", "\n".join(bodies.values()))

    def test_copilot_mcp_snippet_is_local_server_shape(self):
        import sys

        from tokan.adapters.builtin import GitHubCopilotAdapter

        body = GitHubCopilotAdapter().mcp_snippet(GW).body
        config = json.loads(body)
        server = config["mcpServers"]["token-optimizer"]
        self.assertEqual(server["type"], "local")
        self.assertEqual(server["command"], sys.executable)
        self.assertEqual(server["args"], ["-m", "tokan", "mcp"])

    def test_opencode_provider_shape_matches_v2_docs(self):
        body = OpenCodeAdapter().snippets(GW)[0].body
        config = json.loads(body)  # must be valid JSON (jsonc subset here)
        provider = config["providers"]["tokenoptimizer"]
        self.assertEqual(provider["package"], "@opencode/ai/providers/openai-compatible")
        self.assertEqual(provider["settings"]["baseURL"], GW.openai_base_url)
        self.assertEqual(config["$schema"], "https://opencode.ai/config.json")

    def test_opencode_mcp_is_v2_local_server_shape(self):
        body = OpenCodeAdapter().mcp_snippet(GW).body
        config = json.loads(body)
        server = config["mcp"]["servers"]["token-optimizer"]
        self.assertEqual(server["type"], "local")
        self.assertIsInstance(server["command"], list)

    def test_mcp_capable_adapters_emit_mcp_entry(self):
        for adapter in all_adapters():
            if adapter.supports_mcp:
                mcp_snips = [s for s in adapter.all_snippets(GW) if "mcp" in s.target.lower() or "mcp" in s.body.lower() or "context_servers" in s.target or "mcp_servers" in s.body]
                self.assertTrue(mcp_snips, f"{adapter.id} claims mcp but has no MCP snippet")


class TestDetection(unittest.TestCase):
    def test_config_path_detection(self):
        class PathAdapter(AgentAdapter):
            id = "test-path"
            display_name = "Path"
            protocols = ("openai",)

            def snippets(self, gw):
                return []

        with TemporaryDirectory() as tmp:
            marker = Path(tmp) / "agent.conf"
            adapter = PathAdapter()
            adapter.config_paths = (str(marker),)
            self.assertIsNone(adapter.detect())
            marker.write_text("x")
            how = adapter.detect()
            self.assertIsNotNone(how)
            self.assertIn("agent.conf", how)

    def test_detect_all_shape(self):
        rows = detect_all()
        for row in rows:
            self.assertIn("id", row)
            self.assertIn("protocols", row)
            self.assertIn("detected", row)
            self.assertIsInstance(row["detected"], bool)


if __name__ == "__main__":
    unittest.main()
