"""Conversation memory: append-only canonical history + structured state.

Recoverability over deletion: optimization reduces what is *transmitted* to an
LLM, never what is *retained*. The full canonical conversation lives here as an
immutable append-only event log; compaction, omission and caching decisions only
shape the upstream view.

Three pieces:

- :class:`ConversationStore` — per-conversation append-only event log (stdlib
  SQLite, WAL, thread-safe). Every event carries a content digest; ``verify``
  detects any post-hoc mutation. Retention is policy, applied by ``prune``.
- :func:`extract_state` — deterministic (rule-based, no model calls) extraction
  of concise structured state from events: objective, explicit constraints,
  files under discussion, open questions. Conflicts resolve latest-wins while
  both facts stay retrievable from the log.
- :class:`ContextManifest` — per-request evidence ledger (what was included,
  compressed, protected; which route/model/cache decisions fired). M1 records
  what the gateway genuinely knows; the ledger grows when the Context Governor
  lands.

All storage failures are the caller's to fail open from: nothing in this module
raises into a request path by design of its gateway wiring (observe-only).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

__all__ = [
    "ConversationEvent",
    "ConversationStore",
    "ConversationState",
    "extract_state",
    "ContextManifest",
    "content_digest",
    "POLICY_VERSION",
]

#: Bumped when compression/policy behavior changes; recorded in manifests so
#: cached decisions can be traced to the policy that made them.
POLICY_VERSION = "cdm-1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_events (
    tenant       TEXT NOT NULL,
    conversation TEXT NOT NULL,
    user         TEXT NOT NULL DEFAULT '',
    version      INTEGER NOT NULL,
    id           TEXT NOT NULL,
    ts           REAL NOT NULL,
    source       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    digest       TEXT NOT NULL,
    content      TEXT NOT NULL,
    PRIMARY KEY (tenant, conversation, id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conv_version
    ON conversation_events (tenant, conversation, version);
"""


def _canon(obj: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace — stable across processes."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def content_digest(content: Mapping[str, Any]) -> str:
    """Digest of an event payload — the exact basis the store writes.

    External callers (the gateway observer) must use this to compare against
    stored digests; any other JSON encoding (different separators, key order)
    produces a different digest and defeats dedupe.
    """
    return hashlib.sha256(_canon(content).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConversationEvent:
    """One immutable fact in a conversation's history."""

    tenant: str
    conversation: str
    user: str
    version: int
    id: str
    ts: float
    source: str  # user | assistant | tool | system | optimization
    kind: str  # message | manifest | state
    digest: str
    content: Dict[str, Any]

    def as_dict(self, *, include_content: bool = True) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "tenant": self.tenant,
            "conversation": self.conversation,
            "user": self.user,
            "version": self.version,
            "id": self.id,
            "ts": self.ts,
            "source": self.source,
            "kind": self.kind,
            "digest": self.digest,
        }
        if include_content:
            out["content"] = self.content
        return out


class ConversationStore:
    """Append-only, per-conversation event log backed by a single SQLite file.

    Thread-safe (one lock around writes; the connection is shared). ``tenant``
    scopes every read and write — two tenants can use the same file without
    visibility into each other. There are deliberately no update APIs; the only
    removal path is :meth:`prune` (retention policy).
    """

    def __init__(
        self,
        path: str,
        *,
        tenant: str = "default",
        user: str = "",
        retention_seconds: Optional[float] = None,
    ) -> None:
        self.path = path
        self.tenant = str(tenant)
        self.user = str(user)
        self.retention_seconds = retention_seconds
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- write ---------------------------------------------------------------

    def append(
        self,
        conversation_id: str,
        *,
        source: str,
        kind: str,
        content: Mapping[str, Any],
        user: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> ConversationEvent:
        """Append one event; assigns the next per-conversation version."""
        conversation_id = str(conversation_id)
        body = dict(content)
        digest = content_digest(body)
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM conversation_events "
                "WHERE tenant = ? AND conversation = ?",
                (self.tenant, conversation_id),
            ).fetchone()
            version = int(row[0]) + 1
            event_id = hashlib.sha256(
                f"{self.tenant}|{conversation_id}|{version}|{digest}".encode("utf-8")
            ).hexdigest()[:32]
            event = ConversationEvent(
                tenant=self.tenant,
                conversation=conversation_id,
                user=self.user if user is None else str(user),
                version=version,
                id=event_id,
                ts=time.time() if ts is None else float(ts),
                source=str(source),
                kind=str(kind),
                digest=digest,
                content=body,
            )
            self._conn.execute(
                "INSERT INTO conversation_events "
                "(tenant, conversation, user, version, id, ts, source, kind, digest, content) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.tenant,
                    event.conversation,
                    event.user,
                    event.version,
                    event.id,
                    event.ts,
                    event.source,
                    event.kind,
                    event.digest,
                    _canon(body),
                ),
            )
            self._conn.commit()
        return event

    # -- read ----------------------------------------------------------------

    def events(
        self,
        conversation_id: str,
        *,
        since_version: int = 0,
        verify: bool = False,
    ) -> List[ConversationEvent]:
        """All events for a conversation, oldest first.

        With ``verify=True`` every event's stored digest is recomputed; a
        mismatch raises ``ValueError`` naming the tampered event.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT tenant, conversation, user, version, id, ts, source, kind, "
                "       digest, content "
                "FROM conversation_events "
                "WHERE tenant = ? AND conversation = ? AND version > ? "
                "ORDER BY version",
                (self.tenant, str(conversation_id), int(since_version)),
            ).fetchall()
        events = [
            ConversationEvent(
                tenant=r[0], conversation=r[1], user=r[2], version=r[3], id=r[4],
                ts=r[5], source=r[6], kind=r[7], digest=r[8],
                content=json.loads(r[9]),
            )
            for r in rows
        ]
        if verify:
            for ev in events:
                recomputed = hashlib.sha256(_canon(ev.content).encode("utf-8")).hexdigest()
                if recomputed != ev.digest:
                    raise ValueError(
                        f"integrity failure: event v{ev.version} ({ev.id}) content mutated"
                    )
        return events

    def last(self, conversation_id: str) -> Optional[ConversationEvent]:
        """Most recent event, or None for an unseen conversation."""
        return self._last_where(conversation_id, kind=None)

    def last_message(
        self, conversation_id: str, *, source: Optional[str] = None
    ) -> Optional[ConversationEvent]:
        """Most recent ``kind='message'`` event, optionally by source role."""
        return self._last_where(conversation_id, kind="message", source=source)

    def _last_where(
        self, conversation_id: str, *, kind: Optional[str], source: Optional[str] = None
    ) -> Optional[ConversationEvent]:
        query = (
            "SELECT tenant, conversation, user, version, id, ts, source, kind, "
            "       digest, content "
            "FROM conversation_events "
            "WHERE tenant = ? AND conversation = ?"
        )
        params: List[Any] = [self.tenant, str(conversation_id)]
        if kind is not None:
            query += " AND kind = ?"
            params.append(kind)
        if source is not None:
            query += " AND source = ?"
            params.append(source)
        query += " ORDER BY version DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        if row is None:
            return None
        return ConversationEvent(
            tenant=row[0], conversation=row[1], user=row[2], version=row[3], id=row[4],
            ts=row[5], source=row[6], kind=row[7], digest=row[8],
            content=json.loads(row[9]),
        )

    def conversations(self) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT conversation FROM conversation_events "
                "WHERE tenant = ? ORDER BY conversation",
                (self.tenant,),
            ).fetchall()
        return [r[0] for r in rows]

    def verify(self, conversation_id: str) -> int:
        """Recompute every digest; return the number of corrupted events."""
        bad = 0
        for ev in self.events(conversation_id):
            if hashlib.sha256(_canon(ev.content).encode("utf-8")).hexdigest() != ev.digest:
                bad += 1
        return bad

    # -- policy --------------------------------------------------------------

    def prune(self) -> int:
        """Drop events older than the retention cutoff; return count removed.

        Without a configured retention nothing is ever removed.
        """
        if not self.retention_seconds:
            return 0
        cutoff = time.time() - float(self.retention_seconds)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM conversation_events WHERE tenant = ? AND ts < ?",
                (self.tenant, cutoff),
            )
            self._conn.commit()
        return int(cur.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --------------------------------------------------------------------------
# Structured conversation state (deterministic extraction)
# --------------------------------------------------------------------------

#: Markers that make a user sentence an explicit constraint (authority: the
#: preserve-verbatim class — the governor may never compress these away).
_CONSTRAINT_RE = re.compile(
    r"\b(must|shall|never|always|do not|don't|dont|ensure|keep|preserve|required|only)\b",
    re.IGNORECASE,
)
_FILE_RE = re.compile(r"(?:[\w.\-]{1,100}/){1,6}[\w.\-]{1,100}\.\w{1,5}")
_MAX_CONSTRAINTS = 20
_MAX_FILES = 50
_MAX_QUESTIONS = 10
_OBJECTIVE_CHARS = 400


@dataclass
class ConversationState:
    """Concise structured state derived from the event log.

    Versioned by the last event incorporated; conflicting facts resolve
    latest-wins *without* overwriting history (both remain in the log).
    """

    conversation: str
    version: int = 0
    objective: str = ""
    constraints: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)
    updated: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "conversation": self.conversation,
            "version": self.version,
            "objective": self.objective,
            "constraints": list(self.constraints),
            "files": list(self.files),
            "open_questions": list(self.open_questions),
            "updated": self.updated,
        }


def _event_text(ev: ConversationEvent) -> str:
    content = ev.content
    for key in ("text", "objective", "value"):
        val = content.get(key)
        if isinstance(val, str):
            return val
    return ""


def extract_state(events: Sequence[ConversationEvent], conversation_id: str = "") -> ConversationState:
    """Deterministically derive structured state from a conversation's events.

    Rules (all unit-tested, no model calls):

    - ``objective``: latest user-message text, truncated.
    - ``constraints``: sentences of user messages carrying constraint markers.
    - ``files``: distinct file paths anywhere in the history, most recent last.
    - ``open_questions``: question lines in the latest user message.
    """
    conv = conversation_id or (events[-1].conversation if events else "")
    state = ConversationState(conversation=conv)
    if not events:
        return state
    state.version = events[-1].version
    state.updated = events[-1].ts

    constraints: List[str] = []
    files: List[str] = []
    latest_user = ""
    for ev in events:
        if ev.kind != "message":
            continue
        text = _event_text(ev)
        if not text:
            continue
        if ev.source == "user":
            latest_user = text
            for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
                sentence = sentence.strip()
                if (
                    sentence
                    and len(sentence) <= 240
                    and _CONSTRAINT_RE.search(sentence)
                    and sentence not in constraints
                ):
                    constraints.append(sentence)
        for match in _FILE_RE.findall(text):
            if match not in files:
                files.append(match)

    state.objective = latest_user[:_OBJECTIVE_CHARS]
    state.constraints = constraints[:_MAX_CONSTRAINTS]
    state.files = files[-_MAX_FILES:]
    if latest_user:
        state.open_questions = [
            line.strip()
            for line in latest_user.splitlines()
            if line.strip().endswith("?")
        ][:_MAX_QUESTIONS]
    return state


# --------------------------------------------------------------------------
# Context manifest (per-request evidence ledger)
# --------------------------------------------------------------------------


@dataclass
class ContextManifest:
    """What actually happened to one request's context.

    M1 records what the gateway genuinely knows today (route, tokens, cache,
    scrubbing, protected system messages). ``omitted`` is populated by the
    Context Governor (M4) when it sheds context to fit a model's window —
    it lists the message indices withheld from the provider, so an omission
    is always auditable after the fact.
    """

    rid: str
    conversation: str
    ts: float
    protocol: str = ""
    route: str = ""
    status: int = 0
    model: str = ""
    digest: str = ""
    original_tokens: int = 0
    optimized_tokens: int = 0
    cache_hit: bool = False
    volatile_scrubbed: int = 0
    prefix_reused: int = 0
    protected: Dict[str, int] = field(default_factory=dict)
    included: Dict[str, int] = field(default_factory=dict)
    compressed: Dict[str, int] = field(default_factory=dict)
    omitted: List[str] = field(default_factory=list)
    #: Canonical-model view of the request (messages/tools/parts/extension
    #: counts) — filled once the canonical parser is wired into the observer.
    canonical: Dict[str, int] = field(default_factory=dict)
    policy: Dict[str, Any] = field(
        default_factory=lambda: {"version": POLICY_VERSION, "levels": ["L1", "L2"]}
    )
    #: Context Governor budget decision (M4): window, reserve, what was shed.
    #: Empty dict when the governor is off or the request fit its budget.
    governor: Dict[str, Any] = field(default_factory=dict)
    risk: str = "low"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rid": self.rid,
            "conversation": self.conversation,
            "ts": self.ts,
            "protocol": self.protocol,
            "route": self.route,
            "status": self.status,
            "model": self.model,
            "digest": self.digest,
            "original_tokens": self.original_tokens,
            "optimized_tokens": self.optimized_tokens,
            "cache_hit": self.cache_hit,
            "volatile_scrubbed": self.volatile_scrubbed,
            "prefix_reused": self.prefix_reused,
            "protected": dict(self.protected),
            "included": dict(self.included),
            "compressed": dict(self.compressed),
            "omitted": list(self.omitted),
            "canonical": dict(self.canonical),
            "policy": dict(self.policy),
            "governor": dict(self.governor),
            "risk": self.risk,
        }
