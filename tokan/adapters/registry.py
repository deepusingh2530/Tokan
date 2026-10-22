"""Adapter registry: built-ins + third-party entry-point discovery.

Built-in adapters register at import. External packages extend the system
without touching this codebase by declaring the ``token_optimizer.adapters``
entry-point group::

    # pyproject.toml of your agent's plugin package
    [project.entry-points."token_optimizer.adapters"]
    my-agent = "my_pkg.adapter:MyAgentAdapter"

Discovery is lazy and fail-safe: a broken third-party adapter is skipped, never
fatal — one bad plugin must not break every other agent's integration.
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional, Type

from .base import AgentAdapter, ConfigSnippet, GatewayInfo

__all__ = [
    "AgentAdapter",
    "ConfigSnippet",
    "GatewayInfo",
    "register",
    "get",
    "all_adapters",
    "detect_all",
    "ENTRY_POINT_GROUP",
]

ENTRY_POINT_GROUP = "token_optimizer.adapters"

_lock = threading.Lock()
_registry: Dict[str, AgentAdapter] = {}
_discovered = False


def register(adapter: "AgentAdapter | Type[AgentAdapter]"):
    """Register an adapter instance (or class, which is instantiated).

    Returns the *original* object so that ``@register class Foo(...)`` keeps
    ``Foo`` bound to the class, not to a freshly built instance.
    """
    original = adapter
    if isinstance(adapter, type):
        adapter = adapter()
    if not adapter.id:
        raise ValueError(f"adapter {adapter!r} must define a non-empty id")
    with _lock:
        _registry[adapter.id] = adapter
    return original


def _load_entry_points() -> None:
    """Discover third-party adapters; never raise."""
    global _discovered
    with _lock:
        if _discovered:
            return
        _discovered = True
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover
        return
    try:
        eps = entry_points()
        selected = (
            eps.select(group=ENTRY_POINT_GROUP)
            if hasattr(eps, "select")
            else eps.get(ENTRY_POINT_GROUP, [])  # Python 3.9 fallback
        )
    except Exception:
        return
    for ep in selected:
        try:
            obj = ep.load()
            register(obj)
        except Exception:
            # A broken plugin must not take down the registry.
            continue


def all_adapters() -> List[AgentAdapter]:
    """All adapters, built-in and third-party, sorted by display name."""
    _load_entry_points()
    with _lock:
        items = list(_registry.values())
    return sorted(items, key=lambda a: (a.display_name or a.id).lower())


def get(adapter_id: str) -> Optional[AgentAdapter]:
    _load_entry_points()
    with _lock:
        return _registry.get(adapter_id)


def detect_all() -> List[Dict[str, object]]:
    """Rows for ``token-optimizer list``: id, name, protocols, detection.

    A third-party adapter whose ``detect()`` raises is reported as
    undetected with its error noted — one broken plugin must not take down
    the listing for every other agent.
    """
    rows = []
    for adapter in all_adapters():
        try:
            how = adapter.detect()
            error = None
        except Exception as exc:
            how, error = None, str(exc)
        rows.append(
            {
                "id": adapter.id,
                "name": adapter.display_name,
                "protocols": list(adapter.protocols),
                "detected": how is not None,
                "detected_how": how,
                "detect_error": error,
                "summary": adapter.summary,
            }
        )
    return rows
