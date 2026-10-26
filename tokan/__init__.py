"""token-optimizer: token-frugality middleware for AI coding agents.

Three integration patterns, one pipeline (CDM -> edge cache -> JeV):

  1. Gateway proxy   - point the agent's base URL at it (zero code changes)
  2. SDK client      - wrap the agent loop in Python
  3. Repo mapper     - see ``examples/build_repo_index.py`` for the .md mapper
"""
from .cache import CDNConfig, EdgeCache
from .cdm import CanonicalRecord, CDMNormalizer, estimate_tokens
from .client import ChatResponse, OptimizedAgentClient
from .jev import JevDecision, JevRouter
from .metrics import SessionMetrics
from .pipeline import LocalExecutor, OptimizationPipeline, PipelineResult

__version__ = "0.3.1"

__all__ = [
    "CDNConfig",
    "CanonicalRecord",
    "CDMNormalizer",
    "ChatResponse",
    "EdgeCache",
    "JevDecision",
    "JevRouter",
    "LocalExecutor",
    "OptimizationPipeline",
    "OptimizedAgentClient",
    "PipelineResult",
    "SessionMetrics",
    "estimate_tokens",
    "__version__",
]
