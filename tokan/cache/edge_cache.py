"""Edge cache: semantic hashing of prompts/code blocks across sessions.

Two layers:
  * local LRU (in-process, zero latency)
  * optional remote edge endpoint (the CDN tier) addressed as
    ``{endpoint}/{namespace}/{digest}`` — plain HTTP, fail-open by default so
    a cache outage never blocks an agent run.

Keys come from ``CDMNormalizer.fingerprint()`` (canonical, noise-free digests),
so two agents that saw the same file with different terminal colors or line
endings still share one cache entry.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, MutableMapping, Optional

__all__ = ["CDNConfig", "EdgeCache"]


@dataclass(frozen=True)
class CDNConfig:
    endpoint: Optional[str] = None  # e.g. "https://edge.mytokenoptimizer.com/v1"
    namespace: str = "default"      # per-project / per-agent isolation
    ttl_seconds: int = 3600
    fail_open: bool = True          # remote errors -> treat as miss
    timeout_seconds: float = 1.5


class EdgeCache:
    """Two-tier (local LRU + remote edge) semantic cache."""

    def __init__(
        self,
        config: Optional[CDNConfig] = None,
        max_entries: int = 2048,
        store: Optional[MutableMapping[str, Any]] = None,
        clock=time.monotonic,
    ) -> None:
        self.config = config or CDNConfig()
        self._local: "OrderedDict[str, Any]" = OrderedDict()
        self._store = store  # optional pluggable backing store (disk, sqlite...)
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.remote_hits = 0

    # ------------------------------------------------------------- local ops
    def _lru_get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._local.get(key)
            if entry is None:
                return None
            value, expires = entry
            if self._clock() >= expires:  # TTL enforced on every read
                self._local.pop(key, None)
                return None
            self._local.move_to_end(key)
            self.hits += 1
            return value

    def _lru_put(self, key: str, value: Any) -> None:
        with self._lock:
            self._local[key] = (value, self._clock() + self.config.ttl_seconds)
            self._local.move_to_end(key)
            while len(self._local) > self._max:
                self._local.popitem(last=False)

    def _store_get(self, key: str) -> Optional[Any]:
        """Read the backing store (if any), honoring its TTL record."""
        if self._store is None:
            return None
        entry = self._store.get(key)
        if not isinstance(entry, dict) or "value" not in entry:
            return None
        if self._clock() >= float(entry.get("expires", 0)):
            self._store.pop(key, None)
            return None
        return entry["value"]

    # ------------------------------------------------------------- public API
    @staticmethod
    def digest(text: str) -> str:
        """Stable 64-hex digest of canonical text."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def semantic_digest(text: str, *, ngram: int = 5) -> str:
        """Whitespace/punctuation-insensitive digest over character n-grams.

        Near-identical inputs ("refactor foo" vs "Refactor  foo!") collide,
        which is what makes the cache *semantic* rather than byte-exact.
        """
        compact = "".join(ch for ch in text.lower() if ch.isalnum() or ch.isspace())
        tokens = compact.split()
        grams = [" ".join(tokens[i : i + ngram]) for i in range(max(1, len(tokens) - ngram + 1))]
        basis = "\n".join(grams) if grams else compact
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Optional[Any]:
        """Tier 1: local LRU -> Tier 2: remote edge / backing store."""
        value = self._lru_get(key)
        if value is not None:
            return value

        remote_value = self._remote_get(key) if self.config.endpoint else None
        if remote_value is not None:
            with self._lock:
                self.remote_hits += 1
        else:
            remote_value = self._store_get(key)

        if remote_value is None:
            with self._lock:
                self.misses += 1
            return None
        with self._lock:
            self.hits += 1
        self._lru_put(key, remote_value)
        return remote_value

    def set(self, key: str, value: Any) -> None:
        self._lru_put(key, value)
        if self._store is not None:
            self._store[key] = {"value": value, "expires": self._clock() + self.config.ttl_seconds}
        if self.config.endpoint:
            self._remote_put(key, value)

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "remote_hits": self.remote_hits,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
            "entries": len(self._local),
            "endpoint": self.config.endpoint,
            "namespace": self.config.namespace,
        }

    def reset_stats(self) -> None:
        self.hits = self.misses = self.remote_hits = 0

    # --------------------------------------------------------------- remote
    def _url(self, key: str) -> str:
        base = (self.config.endpoint or "").rstrip("/")
        return f"{base}/cache/{self.config.namespace}/{key}"

    def _remote_get(self, key: str) -> Optional[Any]:
        try:
            req = urllib.request.Request(self._url(key), method="GET")
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return payload.get("value")
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            if not self.config.fail_open:
                raise RuntimeError(f"edge cache unavailable: {exc}") from exc
            return None

    def _remote_put(self, key: str, value: Any) -> None:
        try:
            body = json.dumps({"value": value, "ttl": self.config.ttl_seconds}).encode("utf-8")
            req = urllib.request.Request(
                self._url(key),
                data=body,
                method="PUT",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds):
                pass
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            if not self.config.fail_open:
                raise RuntimeError(f"edge cache unavailable: {exc}") from exc
