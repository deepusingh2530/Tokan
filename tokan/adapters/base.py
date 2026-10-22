"""Adapter plugin interface: how one coding agent plugs into the gateway.

An adapter knows three things about its agent:
  1. which protocols it speaks (``openai`` / ``anthropic`` / ``mcp``),
  2. whether it is installed (binary on PATH, config file present),
  3. how to wire it to a running gateway (copy-pasteable snippets).

Third-party agents plug in via the ``tokan.adapters`` entry-point
group — see :mod:`tokan.adapters.registry`.
"""
from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = ["AgentAdapter", "ConfigSnippet", "GatewayInfo"]


@dataclass(frozen=True)
class GatewayInfo:
    """Endpoints a running gateway exposes; adapters render against this."""

    host: str = "127.0.0.1"
    port: int = 4040
    openai_base_url: str = ""    # e.g. http://127.0.0.1:4040/v1
    anthropic_base_url: str = ""  # e.g. http://127.0.0.1:4040
    mcp_command: Tuple[str, ...] = ("python3", "-m", "tokan", "mcp")

    @classmethod
    def for_gateway(cls, host: str, port: int, mcp_command: Optional[Sequence[str]] = None) -> "GatewayInfo":
        cmd = tuple(mcp_command) if mcp_command else ("python3", "-m", "tokan", "mcp")
        return cls(
            host=host,
            port=port,
            openai_base_url=f"http://{host}:{port}/v1",
            anthropic_base_url=f"http://{host}:{port}",
            mcp_command=cmd,
        )

    @property
    def origin(self) -> str:
        """Gateway root URL (Anthropic-style base, no trailing slash)."""
        return f"http://{self.host}:{self.port}"

    @property
    def mcp_config_entry(self) -> Dict[str, object]:
        """MCP ``mcpServers`` entry (Claude Code / Cursor / Continue shape)."""
        return {"command": self.mcp_command[0], "args": list(self.mcp_command[1:])}


@dataclass(frozen=True)
class ConfigSnippet:
    """One copy-pasteable piece of agent configuration.

    ``kind``: ``env`` (shell export), ``file`` (config file content),
    ``ui`` (in-app steps), or ``command`` (one-shot shell command).
    ``target``: where it goes — a path, a settings panel, or a shell.
    """

    kind: str
    target: str
    body: str
    note: str = ""

    def render(self) -> str:
        header = f"[{self.kind}] {self.target}"
        bar = "─" * max(len(header), 40)
        out = f"{bar}\n{header}\n{bar}\n{self.body.rstrip()}\n"
        if self.note:
            out += f"\n# {self.note}\n"
        return out


class AgentAdapter(ABC):
    """Base class for built-in and third-party agent adapters."""

    #: stable identifier used on the CLI (``token-optimizer setup <id>``)
    id: str = ""
    #: human-readable name for listings
    display_name: str = ""
    #: wire protocols this agent can consume: "openai", "anthropic", "mcp"
    protocols: Tuple[str, ...] = ()
    #: binaries that identify an installed agent (any match counts)
    executables: Tuple[str, ...] = ()
    #: config paths (``~`` expanded) whose presence identifies the agent
    config_paths: Tuple[str, ...] = ()
    #: one-line description shown in ``token-optimizer list``
    summary: str = ""

    # ------------------------------------------------------------ detection
    def detect(self) -> Optional[str]:
        """Return how the agent was detected, or None if not installed."""
        for exe in self.executables:
            if shutil.which(exe):
                return f"binary: {exe}"
        for raw in self.config_paths:
            if Path(raw).expanduser().exists():
                return f"config: {raw}"
        return None

    @property
    def detected(self) -> bool:
        return self.detect() is not None

    # ------------------------------------------------------------- snippets
    @abstractmethod
    def snippets(self, gw: GatewayInfo) -> List[ConfigSnippet]:
        """Config pieces that point this agent at the gateway."""

    # ------------------------------------------------------------------ MCP
    @property
    def supports_mcp(self) -> bool:
        return "mcp" in self.protocols

    def mcp_snippet(self, gw: GatewayInfo, *, target: str = "MCP config") -> ConfigSnippet:
        """Default MCP registration snippet (JSON ``mcpServers`` entry)."""
        import json

        entry = {"mcpServers": {"token-optimizer": gw.mcp_config_entry}}
        return ConfigSnippet(
            kind="file",
            target=target,
            body=json.dumps(entry, indent=2),
            note="Exposes optimize_context, repo_index, cache_lookup, session_metrics tools.",
        )

    def all_snippets(self, gw: GatewayInfo, *, include_mcp: bool = True) -> List[ConfigSnippet]:
        out = list(self.snippets(gw))
        if include_mcp and self.supports_mcp:
            out.append(self.mcp_snippet(gw, target=self.mcp_target()))
        return out

    def mcp_target(self) -> str:
        """Where this agent keeps its MCP server registry."""
        return "MCP config"

    # --------------------------------------------------------------- helper
    @staticmethod
    def _snippet(kind: str, target: str, body: str, note: str = "") -> ConfigSnippet:
        return ConfigSnippet(kind=kind, target=target, body=body, note=note)
