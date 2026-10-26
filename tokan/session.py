"""Session Governor: stable-prefix engineering for provider prompt caches.

Why this exists
---------------
Coding agents resend the full conversation history on every turn. Modern
providers (OpenAI, Anthropic, Gemini) auto-cache a repeated prompt prefix and
bill those cached tokens at a steep discount -- but only if the prefix is
byte-identical to the previous request. A compactor that serializes
non-deterministically (varied key order, stray whitespace, an injected
timestamp) silently shatters that cache and the discount disappears.

Tokan already compacts payloads; this module makes compaction prefix-safe and
measurable:

* CDMNormalizer.canonical_message_strings gives a deterministic per-message
  serialization, so a growing conversation's shared prefix is byte-stable
  across turns.
* SessionTracker remembers the last canonical payload per session and computes
  the longest common prefix (in tokens) with the new one. That reused prefix is
  exactly what the provider will cache and bill at a discount.

Honesty contract: the reused prefix is the provider's billing discount, which
Tokan *enables* by keeping the prefix stable -- Tokan does not invent it and
never counts it as compaction savings. /metrics and the dashboard keep the two
apart: compaction savings (Tokan's) vs. prefix reuse / provider cache ratio
(the discount Tokan captured).
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .cdm import estimate_tokens

__all__ = ["PrefixReuse", "SessionTracker", "SESSION_HEADER"]

# Header an agent can send to name its conversation explicitly.
SESSION_HEADER = "X-Optimizer-Session"


@dataclass(frozen=True)
class PrefixReuse:
    """How much of a request repeats the previous turn's canonical prefix."""

    session_key: str
    reused_messages: int   # leading canonical messages identical to last turn
    reused_tokens: int     # tokens inside that reused prefix
    total_tokens: int      # total canonical tokens this turn
    is_new_session: bool   # first turn seen for this key

    @property
    def reuse_pct(self) -> float:
        if not self.total_tokens:
            return 0.0
        return round(100.0 * self.reused_tokens / self.total_tokens, 2)

    @property
    def fresh_tokens(self) -> int:
        """Tokens outside the reused prefix -- the expensive, full-price part."""
        return max(0, self.total_tokens - self.reused_tokens)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "session_key": self.session_key,
            "reused_messages": self.reused_messages,
            "reused_tokens": self.reused_tokens,
            "total_tokens": self.total_tokens,
            "reuse_pct": self.reuse_pct,
            "fresh_tokens": self.fresh_tokens,
            "is_new_session": self.is_new_session,
        }


class SessionTracker:
    """Tracks per-session canonical payloads and measures prefix reuse.

    Bounded (LRU) and thread-safe -- a gateway serves many concurrent agents.
    """

    def __init__(self, max_sessions: int = 1024) -> None:
        self.max_sessions = max(1, int(max_sessions))
        self._last: "OrderedDict[str, List[str]]" = OrderedDict()
        self._lock = threading.Lock()

    # ------------------------------------------------------------- keying
    @staticmethod
    def derive_key(messages: Sequence[Mapping[str, Any]]) -> str:
        """A stable conversation identity from the message root.

        Uses the first system/developer message plus the first user message, so
        every turn of the same conversation shares a key while distinct
        conversations (different setup or opener) do not. No agent cooperation
        required; an explicit X-Optimizer-Session header wins when present.
        """
        parts: List[str] = []

        def canon(m: Mapping[str, Any]) -> str:
            return json.dumps(
                {"role": m.get("role"), "content": m.get("content")},
                sort_keys=True, ensure_ascii=False, default=str,
            )

        for m in messages:
            if str(m.get("role", "")).lower() in ("system", "developer"):
                parts.append(canon(m))
                break
        for m in messages:
            if str(m.get("role", "")).lower() == "user":
                parts.append(canon(m))
                break
        if not parts and messages:
            parts.append(canon(messages[0]))
        basis = " ".join(parts)
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------ observe
    def observe(
        self,
        messages: Sequence[Mapping[str, Any]],
        msg_strings: Sequence[str],
        *,
        explicit_key: Optional[str] = None,
    ) -> PrefixReuse:
        """Record this turn's canonical payload and measure prefix reuse.

        msg_strings is the deterministic per-message serialization (see
        CDMNormalizer.canonical_message_strings). Returns the longest common
        prefix shared with the previous turn in the same session.
        """
        key = (explicit_key or "").strip() or self.derive_key(messages)
        strings = list(msg_strings)
        total_tokens = sum(estimate_tokens(s) for s in strings)

        with self._lock:
            last = self._last.get(key)
            reused_messages = 0
            if last is not None:
                n = min(len(last), len(strings))
                i = 0
                while i < n and last[i] == strings[i]:
                    i += 1
                reused_messages = i
            # Remember this turn (LRU: move-to-front, evict oldest beyond cap).
            self._last[key] = strings
            self._last.move_to_end(key)
            while len(self._last) > self.max_sessions:
                self._last.popitem(last=False)

        reused_tokens = sum(estimate_tokens(s) for s in strings[:reused_messages])
        return PrefixReuse(
            session_key=key,
            reused_messages=reused_messages,
            reused_tokens=reused_tokens,
            total_tokens=total_tokens,
            is_new_session=last is None,
        )

    # -------------------------------------------------------------- introspect
    def __len__(self) -> int:
        with self._lock:
            return len(self._last)

    def clear(self) -> None:
        with self._lock:
            self._last.clear()
