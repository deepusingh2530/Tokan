"""Tests for the MCP stdio server (JSON-RPC dispatch + tool behavior)."""
import json
import unittest
from io import StringIO

from tokan.mcp import MCPServer, PROTOCOL_VERSION, TOOLS, serve_stdio


def call(server, method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    response = server.handle(msg)
    assert response is not None, f"{method} unexpectedly treated as notification"
    return response


class TestHandshake(unittest.TestCase):
    def setUp(self):
        self.server = MCPServer()

    def test_initialize_echoes_protocol(self):
        resp = call(self.server, "initialize", {"protocolVersion": "2025-06-18"})
        result = resp["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "token-optimizer")
        self.assertIn("tools", result["capabilities"])

    def test_initialized_notification_returns_none(self):
        resp = self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertIsNone(resp)

    def test_ping(self):
        self.assertEqual(call(self.server, "ping")["result"], {})

    def test_unknown_method_is_method_not_found(self):
        resp = call(self.server, "bogus/method")
        self.assertEqual(resp["error"]["code"], -32601)

    def test_tools_list_schema(self):
        tools = call(self.server, "tools/list")["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(names, {"optimize_context", "repo_index", "cache_lookup", "cache_store", "session_metrics"})
        for tool in tools:
            self.assertIn("inputSchema", tool)
            self.assertIn("description", tool)
        self.assertEqual(len(TOOLS), 5)


class TestTools(unittest.TestCase):
    def setUp(self):
        self.server = MCPServer()

    def _payload(self, response):
        result = response["result"]
        self.assertFalse(result["isError"], result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])

    def test_optimize_context_reports_savings(self):
        resp = call(
            self.server,
            "tools/call",
            {
                "name": "optimize_context",
                "arguments": {
                    "messages": [
                        {"role": "system", "content": "You are an expert. " * 20},
                        {"role": "user", "content": "Refactor this code and explain why."},
                    ],
                    "model": "gpt-4o",
                },
            },
        )
        data = self._payload(resp)
        self.assertGreater(data["original_tokens"], 0)
        self.assertLessEqual(data["compact_tokens"], data["original_tokens"])
        self.assertIn(data["route"], ("llm", "cache", "local"))

    def test_optimize_context_bad_input_is_tool_error(self):
        resp = call(
            self.server,
            "tools/call",
            {"name": "optimize_context", "arguments": {"messages": "not-a-list"}},
        )
        self.assertTrue(resp["result"]["isError"])

    def test_unknown_tool_is_tool_error_not_rpc_error(self):
        resp = call(self.server, "tools/call", {"name": "nope", "arguments": {}})
        self.assertIn("result", resp)  # not resp["error"]
        self.assertTrue(resp["result"]["isError"])

    def test_repo_index_fits_budget(self):
        resp = call(
            self.server, "tools/call",
            {"name": "repo_index", "arguments": {"root": ".", "max_bytes": 3000}},
        )
        text = resp["result"]["content"][0]["text"]
        self.assertTrue(text.startswith("# Repository Map"))
        self.assertLessEqual(len(text.encode()), 3000 + 200)  # + footer tolerance
        self.assertIn("## ", text)

    def test_cache_roundtrip_with_semantic_noise(self):
        store = call(
            self.server, "tools/call",
            {"name": "cache_store", "arguments": {"text": "what is foo", "value": "foo is a bar"}},
        )
        self.assertTrue(store["result"]["content"][0]["text"].startswith("{"))

        lookup = call(
            self.server, "tools/call",
            {"name": "cache_lookup", "arguments": {"text": "What is   FOO?"}},
        )
        data = json.loads(lookup["result"]["content"][0]["text"])
        self.assertTrue(data["hit"])
        self.assertEqual(data["value"], "foo is a bar")

    def test_cache_lookup_miss(self):
        resp = call(
            self.server, "tools/call",
            {"name": "cache_lookup", "arguments": {"text": "completely unknown query"}},
        )
        data = json.loads(resp["result"]["content"][0]["text"])
        self.assertFalse(data["hit"])

    def test_session_metrics(self):
        resp = call(self.server, "tools/call", {"name": "session_metrics", "arguments": {}})
        data = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("session", data)
        self.assertIn("cache", data)


class TestStdioLoop(unittest.TestCase):
    def test_serve_stdio_roundtrip(self):
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            "not-json-ignored",
            "",
        ]
        stdin = StringIO("\n".join(lines))
        stdout = StringIO()
        serve_stdio(stdin=stdin, stdout=stdout)

        responses = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(responses), 2)  # notification + bad line produce no output
        self.assertEqual(responses[0]["id"], 1)
        self.assertEqual(responses[0]["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.assertEqual(responses[1]["id"], 2)
        self.assertEqual(len(responses[1]["result"]["tools"]), 5)

    def test_tool_exception_becomes_error_result(self):
        server = MCPServer()
        resp = call(server, "tools/call", {"name": "repo_index", "arguments": {"root": "/nonexistent-path-xyz"}})
        # Either tool error (preferred) or a raised path — must not be a protocol error.
        self.assertIn("result", resp)


if __name__ == "__main__":
    unittest.main()
