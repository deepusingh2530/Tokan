"""Built-in adapters for the major AI coding agents.

Config shapes verified against each agent's documentation (Oct 2026):

========================  ================================================
agent                     wiring
========================  ================================================
Claude Code               ``ANTHROPIC_BASE_URL`` (Anthropic wire) + MCP
Codex CLI                 ``~/.codex/config.toml`` custom provider,
                          ``wire_api = "chat"`` (its default is Responses)
Aider                     ``OPENAI_API_BASE`` / ``--openai-api-base``
Cursor                    OpenAI base-URL override + ``.cursor/mcp.json``
Cline                     "OpenAI Compatible" provider base URL
Roo Code                  "OpenAI Compatible" provider base URL
Continue                  ``~/.continue/config.yaml`` ``baseURL``
OpenCode (V2)             ``providers`` + ``mcp.servers`` (opencode.json)
Gemini CLI                ``GOOGLE_GEMINI_BASE_URL`` (Gemini wire) + MCP
Zed                       ``context_servers`` MCP
Windsurf                  ``mcp_config.json``
========================  ================================================

Honest scope notes:
  * The gateway speaks OpenAI chat-completions and Anthropic Messages.
    Gemini CLI's ``GOOGLE_GEMINI_BASE_URL`` expects *Gemini* wire, which the
    gateway does not translate yet — its adapter wires MCP only and says so.
  * Codex defaults to the Responses API; its snippet registers a chat-wire
    provider so requests match what the gateway speaks.
"""
from __future__ import annotations

import json
from typing import List

from .base import AgentAdapter, ConfigSnippet, GatewayInfo
from .registry import register

__all__ = [
    "ClaudeCodeAdapter", "CodexAdapter", "AiderAdapter", "CursorAdapter",
    "ClineAdapter", "RooCodeAdapter", "ContinueAdapter", "OpenCodeAdapter",
    "GeminiCLIAdapter", "ZedAdapter", "WindsurfAdapter",
    "OpenAIGenericAdapter", "AnthropicGenericAdapter",
    "register_builtin_adapters",
]


@register
class ClaudeCodeAdapter(AgentAdapter):
    id = "claude-code"
    display_name = "Claude Code"
    protocols = ("anthropic", "mcp")
    executables = ("claude",)
    config_paths = ("~/.claude", ".claude")
    summary = "Anthropic-native gateway passthrough via ANTHROPIC_BASE_URL."

    def snippets(self, gw: GatewayInfo) -> List[ConfigSnippet]:
        import os

        if os.name == "nt":
            env_body = (
                f"set ANTHROPIC_BASE_URL={gw.anthropic_base_url}\n"
                "rem keep your existing key; the gateway forwards it upstream\n"
                "rem set ANTHROPIC_AUTH_TOKEN=<key> if unauthenticated"
            )
        else:
            env_body = (
                f"export ANTHROPIC_BASE_URL={gw.anthropic_base_url}\n"
                "# keep your existing key; the gateway forwards it upstream\n"
                "# export ANTHROPIC_AUTH_TOKEN=<key>  # only if unauthenticated"
            )
        settings = json.dumps(
            {"env": {"ANTHROPIC_BASE_URL": gw.anthropic_base_url}}, indent=2
        )
        return [
            self._snippet("env", "shell (~/.zshrc, ~/.bashrc)", env_body,
                          "Claude Code appends /v1/messages itself — point at the gateway root."),
            self._snippet("file", "~/.claude/settings.json (env block, alternative)",
                          settings, "Or set it in managed settings; shell env is read at launch."),
        ]

    def mcp_target(self) -> str:
        return ".mcp.json (project) or ~/.claude.json (user)"


@register
class CodexAdapter(AgentAdapter):
    id = "codex"
    display_name = "Codex CLI"
    protocols = ("openai", "mcp")
    executables = ("codex",)
    config_paths = ("~/.codex/config.toml",)
    summary = "Custom chat-wire provider in ~/.codex/config.toml."

    def snippets(self, gw: GatewayInfo) -> List[ConfigSnippet]:
        toml = (
            "# ~/.codex/config.toml\n"
            "# Root keys must precede tables (TOML rule).\n"
            'model = "gpt-4o"\n'
            'model_provider = "token-optimizer"\n'
            "\n"
            "[model_providers.token-optimizer]\n"
            'name = "Token Optimizer Gateway"\n'
            f'base_url = "{gw.openai_base_url}"\n'
            'env_key = "OPENAI_API_KEY"\n'
            '# Codex defaults to the Responses wire; the gateway speaks chat.\n'
            'wire_api = "chat"\n'
        )
        env = (
            "# Alternative for the built-in provider only\n"
            "# (still Responses wire — prefer the provider block above):\n"
            f"export OPENAI_BASE_URL={gw.openai_base_url}"
        )
        return [
            self._snippet("file", "~/.codex/config.toml", toml),
            self._snippet("env", "shell", env),
        ]

    def mcp_target(self) -> str:
        return "~/.codex/config.toml ([mcp_servers] table)"

    def mcp_snippet(self, gw: GatewayInfo, *, target: str = "") -> ConfigSnippet:
        toml = (
            "# ~/.codex/config.toml\n"
            "[mcp_servers.token-optimizer]\n"
            f'command = "{gw.mcp_command[0]}"\n'
            f"args = [{', '.join(json.dumps(a) for a in gw.mcp_command[1:])}]\n"
        )
        return self._snippet("file", self.mcp_target(), toml)


@register
class AiderAdapter(AgentAdapter):
    id = "aider"
    display_name = "Aider"
    protocols = ("openai",)
    executables = ("aider",)
    config_paths = ("~/.aider.conf.yml", ".aider.conf.yml")
    summary = "OPENAI_API_BASE env var or aider.conf.yml openai-api-base."

    def snippets(self, gw: GatewayInfo) -> List[ConfigSnippet]:
        conf = (
            "# ~/.aider.conf.yml\n"
            f"openai-api-base: {gw.openai_base_url}\n"
        )
        env = (
            f"export OPENAI_API_BASE={gw.openai_base_url}\n"
            "# or per-run: aider --openai-api-base " + gw.openai_base_url
        )
        return [
            self._snippet("file", "~/.aider.conf.yml", conf),
            self._snippet("env", "shell", env),
        ]


@register
class CursorAdapter(AgentAdapter):
    id = "cursor"
    display_name = "Cursor"
    protocols = ("openai", "mcp")
    executables = ("cursor",)
    config_paths = ("~/.cursor", ".cursor")
    summary = "OpenAI base-URL override in Models settings + .cursor/mcp.json."

    def snippets(self, gw: GatewayInfo) -> List[ConfigSnippet]:
        ui = (
            "1. Cursor Settings → Models\n"
            "2. Under an OpenAI-compatible provider, set Base URL to:\n"
            f"   {gw.openai_base_url}\n"
            "3. Keep your API key — the gateway forwards it upstream.\n"
            "\n"
            "If your Cursor build exposes a generic \"Custom\" provider,\n"
            "use that instead; field labels move between releases."
        )
        return [self._snippet("ui", "Cursor Settings → Models", ui)]

    def mcp_target(self) -> str:
        return ".cursor/mcp.json (project) or ~/.cursor/mcp.json (global)"


@register
class ClineAdapter(AgentAdapter):
    id = "cline"
    display_name = "Cline"
    protocols = ("openai", "mcp")
    config_paths = ("~/.cline", ".cline", "~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev")
    summary = '"OpenAI Compatible" provider with the gateway base URL.'

    def snippets(self, gw: GatewayInfo) -> List[ConfigSnippet]:
        ui = (
            "1. Cline panel → Settings (gear) → API Provider\n"
            '2. Choose "OpenAI Compatible"\n'
            f"3. Base URL: {gw.openai_base_url}\n"
            "4. API Key: your existing key (forwarded upstream)\n"
            "5. Model: any model your upstream account serves"
        )
        return [self._snippet("ui", "Cline Settings → API Provider", ui)]

    def mcp_target(self) -> str:
        return "cline_mcp_settings.json (Cline → MCP Servers → View Raw Config)"


@register
class RooCodeAdapter(AgentAdapter):
    id = "roo-code"
    display_name = "Roo Code"
    protocols = ("openai", "mcp")
    executables = ("roo",)
    config_paths = ("~/.roo", ".roo")
    summary = '"OpenAI Compatible" provider with the gateway base URL.'

    def snippets(self, gw: GatewayInfo) -> List[ConfigSnippet]:
        ui = (
            "1. Roo Code Settings → Providers\n"
            '2. Choose "OpenAI Compatible"\n'
            f"3. Base URL: {gw.openai_base_url}\n"
            "4. API Key: your existing key (forwarded upstream)"
        )
        return [self._snippet("ui", "Roo Code Settings → Providers", ui)]

    def mcp_target(self) -> str:
        return "mcp_settings.json (Roo Code → MCP Servers → View Raw Config)"


@register
class ContinueAdapter(AgentAdapter):
    id = "continue"
    display_name = "Continue"
    protocols = ("openai", "mcp")
    config_paths = ("~/.continue/config.yaml", ".continue")
    summary = "Model entry with baseURL in ~/.continue/config.yaml."

    def snippets(self, gw: GatewayInfo) -> List[ConfigSnippet]:
        yaml = (
            "# ~/.continue/config.yaml\n"
            "models:\n"
            "  - name: GPT-4o via Token Optimizer\n"
            "    provider: openai\n"
            "    model: gpt-4o\n"
            "    apiKey: YOUR_KEY\n"
            f"    baseURL: {gw.openai_base_url}\n"
        )
        return [self._snippet("file", "~/.continue/config.yaml", yaml)]

    def mcp_target(self) -> str:
        return "~/.continue/config.yaml (mcpServers block)"

    def mcp_snippet(self, gw: GatewayInfo, *, target: str = "") -> ConfigSnippet:
        yaml = (
            "# ~/.continue/config.yaml\n"
            "mcpServers:\n"
            "  - name: token-optimizer\n"
            f"    command: {gw.mcp_command[0]}\n"
            f"    args: [{', '.join(gw.mcp_command[1:])}]\n"
        )
        return self._snippet("file", self.mcp_target(), yaml)


@register
class OpenCodeAdapter(AgentAdapter):
    id = "opencode"
    display_name = "OpenCode"
    protocols = ("openai", "anthropic", "mcp")
    executables = ("opencode",)
    config_paths = ("opencode.json", "opencode.jsonc", ".opencode", "~/.config/opencode/opencode.json")
    summary = "providers.baseURL override + mcp.servers in opencode.json(c)."

    def snippets(self, gw: GatewayInfo) -> List[ConfigSnippet]:
        provider_jsonc = (
            '{\n'
            '  "$schema": "https://opencode.ai/config.json",\n'
            '  "providers": {\n'
            '    "tokenoptimizer": {\n'
            '      "name": "Token Optimizer",\n'
            '      "env": ["OPENAI_API_KEY"],\n'
            '      "package": "@opencode/ai/providers/openai-compatible",\n'
            f'      "settings": {{ "baseURL": "{gw.openai_base_url}" }},\n'
            '      "models": { "gpt-4o": {} }\n'
            "    }\n"
            "  }\n"
            "}\n"
        )
        endpoint_override = (
            '{\n'
            '  "$schema": "https://opencode.ai/config.json",\n'
            '  "providers": {\n'
            '    "anthropic": {\n'
            f'      "settings": {{ "baseURL": "{gw.anthropic_base_url}" }}\n'
            "    }\n"
            "  }\n"
            "}\n"
        )
        return [
            self._snippet("file", "opencode.json (custom OpenAI-compatible provider)",
                          provider_jsonc, "Select as: tokenoptimizer/gpt-4o in /models."),
            self._snippet("file", "opencode.json (route built-in Anthropic through gateway)",
                          endpoint_override,
                          "Simpler if you only use Anthropic-catalog models."),
        ]

    def mcp_target(self) -> str:
        return "opencode.json → mcp.servers (or `opencode mcp add`)"

    def mcp_snippet(self, gw: GatewayInfo, *, target: str = "") -> ConfigSnippet:
        import sys

        cmd = list(gw.mcp_command)
        if cmd[0] in ("python3", "python"):
            cmd[0] = sys.executable or cmd[0]
        jsonc = (
            '{\n'
            '  "$schema": "https://opencode.ai/config.json",\n'
            '  "mcp": {\n'
            '    "servers": {\n'
            '      "token-optimizer": {\n'
            '        "type": "local",\n'
            f'        "command": {json.dumps(cmd)}\n'
            "      }\n"
            "    }\n"
            "  }\n"
            "}\n"
        )
        return self._snippet("file", self.mcp_target(), jsonc,
                             "Or: opencode mcp add token-optimizer --global -- "
                             + " ".join(cmd))


@register
class GeminiCLIAdapter(AgentAdapter):
    id = "gemini-cli"
    display_name = "Gemini CLI"
    protocols = ("mcp",)  # see module docstring: wire passthrough not yet built
    executables = ("gemini",)
    config_paths = ("~/.gemini", ".gemini")
    summary = "MCP tools now; Gemini-wire passthrough is on the roadmap."

    def snippets(self, gw: GatewayInfo) -> List[ConfigSnippet]:
        note = (
            "# GOOGLE_GEMINI_BASE_URL exists but expects Gemini wire\n"
            "# (generateContent), which the gateway does not translate yet.\n"
            "# MCP below gives Gemini CLI the repo index + cache tools today.\n"
            f"# export GOOGLE_GEMINI_BASE_URL={gw.anthropic_base_url}  # roadmap"
        )
        return [self._snippet("env", "shell (roadmap note)", note)]

    def mcp_target(self) -> str:
        return ".gemini/settings.json → mcpServers (or `gemini mcp add`)"

    def mcp_snippet(self, gw: GatewayInfo, *, target: str = "") -> ConfigSnippet:
        entry = json.dumps({"mcpServers": {"token-optimizer": gw.mcp_config_entry}}, indent=2)
        return self._snippet("file", self.mcp_target(), entry)


@register
class ZedAdapter(AgentAdapter):
    id = "zed"
    display_name = "Zed"
    protocols = ("mcp",)
    executables = ("zed",)
    config_paths = ("~/.config/zed/settings.json", "~/Library/Application Support/Zed/settings.json")
    summary = "MCP context server in Zed settings.json."

    def mcp_target(self) -> str:
        return "~/.config/zed/settings.json → context_servers"

    def mcp_snippet(self, gw: GatewayInfo, *, target: str = "") -> ConfigSnippet:
        import sys

        cmd = list(gw.mcp_command)
        if cmd[0] in ("python3", "python"):
            cmd[0] = sys.executable or cmd[0]
        settings = (
            '{\n'
            '  "context_servers": {\n'
            '    "token-optimizer": {\n'
            f'      "command": {json.dumps(cmd)}\n'
            "    }\n"
            "  }\n"
            "}\n"
        )
        return self._snippet("file", self.mcp_target(), settings)

    def snippets(self, gw: GatewayInfo) -> List[ConfigSnippet]:
        return []  # MCP-only; snippet comes from all_snippets()


@register
class WindsurfAdapter(AgentAdapter):
    id = "windsurf"
    display_name = "Windsurf"
    protocols = ("mcp",)
    config_paths = ("~/.codeium/windsurf",)
    summary = "MCP servers in windsurf/mcp_config.json."

    def mcp_target(self) -> str:
        return "~/.codeium/windsurf/mcp_config.json"

    def snippets(self, gw: GatewayInfo) -> List[ConfigSnippet]:
        return []


@register
class OpenAIGenericAdapter(AgentAdapter):
    id = "openai-generic"
    display_name = "Any OpenAI-SDK client"
    protocols = ("openai",)
    summary = "OPENAI_BASE_URL works for OpenAI SDK, LangChain, LiteLLM, etc."

    def snippets(self, gw: GatewayInfo) -> List[ConfigSnippet]:
        env = (
            f"export OPENAI_BASE_URL={gw.openai_base_url}\n"
            "export OPENAI_API_KEY=<your key>  # forwarded upstream\n"
            "\n"
            "# Python SDK:\n"
            "# from openai import OpenAI\n"
            f'# client = OpenAI()  # picks up OPENAI_BASE_URL={gw.openai_base_url}'
        )
        return [self._snippet("env", "shell", env)]

    def detect(self):
        return "generic"  # always listed — it's the catch-all


@register
class AnthropicGenericAdapter(AgentAdapter):
    id = "anthropic-generic"
    display_name = "Any Anthropic-SDK client"
    protocols = ("anthropic",)
    summary = "ANTHROPIC_BASE_URL for Anthropic SDK / Bedrock-style relays."

    def snippets(self, gw: GatewayInfo) -> List[ConfigSnippet]:
        env = (
            f"export ANTHROPIC_BASE_URL={gw.anthropic_base_url}\n"
            "export ANTHROPIC_API_KEY=<your key>  # forwarded upstream\n"
            "\n"
            "# Python SDK:\n"
            "# from anthropic import Anthropic\n"
            f'# client = Anthropic()  # picks up ANTHROPIC_BASE_URL={gw.anthropic_base_url}'
        )
        return [self._snippet("env", "shell", env)]

    def detect(self):
        return "generic"


def register_builtin_adapters() -> None:
    """Idempotent; decorators already registered at import, this guards
    explicit calls from the CLI."""
    # Registration happens via @register at import; nothing to do here beyond
    # ensuring the module has been imported (which importing this module does).
    return None
