"""Persistent (cross-restart) tier for :class:`EdgeCache`, stdlib sqlite3.

The in-memory LRU dies with the process — every gateway restart wiped every
cache entry, so repeat-savings only accumulated within one process lifetime.
This store plugs into the existing ``EdgeCache(store=...)`` hook: entries
written by one gateway run are served by the next one.

Design notes:

* TTL is enforced here with **wall-clock** time (``time.time``), not the
  caller's monotonic clock — monotonic values are meaningless across
  process restarts and reboots, so persisting them would either resurrect
  stale entries forever or expire fresh ones instantly.
  ``EdgeCache._store_get`` also checks its monotonic clock against the
  stored expiry; wall-clock expiries (~1.7e9) always read as "not yet
  expired" to a monotonic clock (~1e5), so that second check is a harmless
  no-op — expiry authority lives here.
* WAL mode + one connection guarded by a lock: the gateway serves requests
  on many threads and every cache fill lands in this table.
* Values are JSON (the cache stores plain answer text today, but the cache
  is generic); unknown/corrupt rows miss instead of raising — a broken
  cache file must never take the gateway down.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import MutableMapping
from typing import Any, Dict, Iterator, List, Optional

__all__ = ["SqliteStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache (expires);
"""


class SqliteStore(MutableMapping):
    """``MutableMapping`` cache backend backed by a sqlite file.

    Implements exactly the surface :class:`EdgeCache` uses
    (``get``/``__setitem__``/``pop`` plus the Mapping protocol for
    introspection), with per-entry wall-clock TTL.
    """

    def __init__(self, path: str, *, ttl_seconds: int = 3600) -> None:
        self.path = os.path.expanduser(path)
        self.ttl_seconds = ttl_seconds
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            # Drop whatever outlived its TTL on a previous run.
            self._db.execute("DELETE FROM cache WHERE expires < ?", (time.time(),))
            self._db.commit()

    # ---------------------------------------------------------- Mapping ABC
    def __getitem__(self, key: str) -> Dict[str, Any]:
        with self._lock:
            row = self._db.execute(
                "SELECT value, expires FROM cache WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                raise KeyError(key)
            value_text, expires = row
            if time.time() >= expires:  # TTL enforced on every read
                self._db.execute("DELETE FROM cache WHERE key = ?", (key,))
                self._db.commit()
                raise KeyError(key)
        try:
            value = json.loads(value_text)
        except ValueError:
            # Corrupt row: treat as absent, drop it, never raise. Delete
            # directly — MutableMapping.pop routes back through __getitem__,
            # which would recurse on the same corrupt row.
            with self._lock:
                self._db.execute("DELETE FROM cache WHERE key = ?", (key,))
                self._db.commit()
            raise KeyError(key)
        return {"value": value, "expires": expires}

    def __setitem__(self, key: str, entry: Dict[str, Any]) -> None:
        # The caller's "expires" comes from a monotonic clock; convert to a
        # wall-clock TTL so entries stay correct across restarts/reboots.
        try:
            value = json.dumps(entry.get("value"), ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return  # unserializable values simply don't persist
        with self._lock:
            self._db.execute(
                "INSERT INTO cache (key, value, expires) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "expires = excluded.expires",
                (key, value, time.time() + self.ttl_seconds),
            )
            self._db.commit()

    def __delitem__(self, key: str) -> None:
        with self._lock:
            cur = self._db.execute("DELETE FROM cache WHERE key = ?", (key,))
            self._db.commit()
        if cur.rowcount == 0:
            raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            rows: List[str] = [
                r[0]
                for r in self._db.execute(
                    "SELECT key FROM cache WHERE expires >= ?", (time.time(),)
                ).fetchall()
            ]
        return iter(rows)

    def __len__(self) -> int:
        with self._lock:
            n = self._db.execute(
                "SELECT COUNT(*) FROM cache WHERE expires >= ?", (time.time(),)
            ).fetchone()[0]
        return int(n)

    # ---------------------------------------------------------- maintenance
    def prune(self) -> int:
        """Delete expired rows now; return how many went away."""
        with self._lock:
            cur = self._db.execute("DELETE FROM cache WHERE expires < ?", (time.time(),))
            self._db.commit()
        return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __repr__(self) -> str:
        return f"SqliteStore(path={self.path!r}, entries={len(self)})"
