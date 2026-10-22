"""Agent adapter plugins: built-ins + third-party entry-point discovery."""
from .base import AgentAdapter, ConfigSnippet, GatewayInfo
from .registry import (
    ENTRY_POINT_GROUP,
    all_adapters,
    detect_all,
    get,
    register,
)

# Importing the module runs its @register decorators.
from . import builtin as _builtin  # noqa: F401  (side effect: registration)

__all__ = [
    "AgentAdapter",
    "ConfigSnippet",
    "GatewayInfo",
    "ENTRY_POINT_GROUP",
    "all_adapters",
    "detect_all",
    "get",
    "register",
]
