"""Compression policy: map typed content to explicit L0–L4 levels, with authority protection.

CDM (``tokan.cdm``) supplies the *mechanisms* (``strip_ansi``, diff compaction,
long-line elision, tool head/tail truncation). This module supplies the *policy*
that decides which mechanism is allowed to touch which piece of content:

- Every content element gets a :class:`ContentClass` (``system_instruction``,
  ``tool_result``, ``source_code``, ...) via :func:`classify`.
- Every class carries an :class:`Authority` tier. Authoritative content —
  system/developer instructions, tool schemas, the user's own constraints —
  may never be compressed beyond **L1** (lossless normalization: ANSI strip,
  trailing-whitespace trim, blank-run collapse). Nothing lossy ever touches an
  instruction the model must obey.
- :class:`CompressionLevel` names the L0–L4 ladder explicitly so a decision is
  legible in logs and manifests instead of being an emergent property of which
  code path ran.

The policy is *pure and deterministic*: same input → same decision, no clocks,
no I/O. The forward-time compaction sites (chat/anthropic message list, the
Gemini per-part ``contents`` wire, the Responses ``input`` items) consult
:meth:`CompressionPolicy.level_for` before compacting, so authority is enforced
at the single choke point where bytes are about to change.

Crucially, the **digest basis is untouched**: cache fingerprints are computed
from ``fingerprint_view()`` + ``scrub_for_digest()``, independent of this
policy. Adopting the policy can therefore never invalidate a cache key or
change what the Session Governor measures as prefix reuse — only the bytes
actually forwarded upstream become more conservative for authoritative content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Dict, Mapping, Optional, Tuple

__all__ = [
    "ContentClass",
    "Authority",
    "CompressionLevel",
    "ContentDecision",
    "CompressionPolicy",
    "classify",
    "classify_part",
]


class ContentClass(str):
    """Stable string constants for content classes (JSON-friendly in manifests)."""

    SYSTEM_INSTRUCTION = "system_instruction"
    DEVELOPER_INSTRUCTION = "developer_instruction"
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SOURCE_CODE = "source_code"
    CONVERSATION_HISTORY = "conversation_history"
    REASONING = "reasoning"
    IMAGE = "image"
    UNKNOWN = "unknown"


class Authority(IntEnum):
    """How much a piece of content constrains the answer — drives the level cap."""

    CRITICAL = 3    # instructions the model must obey verbatim (system/developer)
    HIGH = 2        # the user's own ask + tool schemas (structure is load-bearing)
    NORMAL = 1      # ordinary conversation content
    LOW = 0         # re-derivable noise (reasoning scratchpads, duplicate dumps)


class CompressionLevel(IntEnum):
    """Explicit L0–L4 compression ladder (the policy vocabulary)."""

    L0_NONE = 0              # bytes untouched
    L1_LOSSLESS = 1          # ANSI strip, trailing WS, blank runs (byte-equal meaning)
    L2_STRUCTURED = 2        # + long-line elision, deterministic diff compaction
    L3_REDUNDANCY = 3        # + head/tail tool truncation, exact-repeat collapse
    L4_AGGRESSIVE = 4        # + semantic/summary replacement (reserved; unused today)


# Authority floor per class: the *maximum* level ever allowed for that class.
# This is the authority-protection contract, stated once and enforced by
# ``CompressionPolicy.level_for``. L1 for instructions/tools is deliberate:
# lossless normalization cannot change meaning, so it is always safe.
_AUTHORITY_CAP: Dict[str, Authority] = {
    ContentClass.SYSTEM_INSTRUCTION: Authority.CRITICAL,
    ContentClass.DEVELOPER_INSTRUCTION: Authority.CRITICAL,
    ContentClass.TOOL_CALL: Authority.HIGH,
    ContentClass.USER_MESSAGE: Authority.HIGH,
    ContentClass.ASSISTANT_MESSAGE: Authority.NORMAL,
    ContentClass.TOOL_RESULT: Authority.NORMAL,
    ContentClass.SOURCE_CODE: Authority.NORMAL,
    ContentClass.CONVERSATION_HISTORY: Authority.NORMAL,
    ContentClass.REASONING: Authority.LOW,
    ContentClass.IMAGE: Authority.CRITICAL,   # never text-compacted
    ContentClass.UNKNOWN: Authority.NORMAL,
}

# Authority tier → the highest compression level that tier permits. This is the
# authority-protection contract from the master prompt: system/developer
# instructions, tool schemas and the user's own constraints never compress
# beyond L1 (lossless: ANSI strip, trailing WS, blank runs — meaning-safe by
# construction). Only NORMAL/LOW content may reach L3 (tool head/tail
# truncation, exact-repeat collapse). L2 (structured: long-line elision, diff
# compaction) is reachable only when a caller explicitly requests it for
# NORMAL/LOW content.
_TIER_MAX_LEVEL: Dict[Authority, CompressionLevel] = {
    Authority.CRITICAL: CompressionLevel.L1_LOSSLESS,
    Authority.HIGH: CompressionLevel.L1_LOSSLESS,
    Authority.NORMAL: CompressionLevel.L3_REDUNDANCY,
    Authority.LOW: CompressionLevel.L3_REDUNDANCY,
}


@dataclass(frozen=True)
class ContentDecision:
    """Why a given content element may be compacted to a given level."""

    cls: str
    authority: Authority
    level: CompressionLevel
    capped: bool          # True when the authority cap lowered the requested level
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "class": self.cls,
            "authority": self.authority.name,
            "level": int(self.level),
            "level_name": self.level.name,
            "capped": self.capped,
            "reason": self.reason,
        }


# ------------------------------------------------------------------ classify
_HEADING_RE = re.compile(r"^\s*(#{1,6}\s|\s*(?:class|def|function)\s+\w+|"
                         r"public\s+\w+|private\s+\w+|package\s+|import\s+|"
                         r"#include\b|using\s+\w+;)", re.MULTILINE)
_DIFF_MARK_RE = re.compile(r"^(diff --git|\+\+\+ |--- |@@ .* @@|index [0-9a-f]{7})", re.MULTILINE)
_ERROR_MARK_RE = re.compile(r"(Traceback \(most recent call last\)|^\s*at .*\(.*:\d+\)$|"
                            r"\berror\b.*\bException\b|^\s*ERROR\b)", re.MULTILINE | re.IGNORECASE)
_SENTENCE_RE = re.compile(r"[.!?]\s|\n")


def classify(
    text: str,
    *,
    role: str = "",
    tool: bool = False,
) -> Tuple[str, Authority]:
    """Classify one text blob + its role into ``(content_class, authority)``.

    Purely deterministic and local: no model calls, no heuristics that depend on
    request order. Role wins when present (the wire already told us what this
    is); otherwise light structural signals pick the class. Tool *results* and
    tool *call arguments* are distinguished by ``tool`` (the callers know).

    ``classify`` is intentionally conservative — a mis-class that stays within
    the same authority tier is harmless (same level cap), and the caps are what
    actually protect authoritative content.
    """
    if role in ("system",):
        return ContentClass.SYSTEM_INSTRUCTION, Authority.CRITICAL
    if role in ("developer",):
        return ContentClass.DEVELOPER_INSTRUCTION, Authority.CRITICAL
    if role == "tool":
        return ContentClass.TOOL_RESULT, Authority.NORMAL
    if role == "assistant":
        # An assistant turn that is mostly code or a diff is still NORMAL; we
        # do not raise authority for it (the model wrote it, not the user).
        return ContentClass.ASSISTANT_MESSAGE, Authority.NORMAL
    if role == "user":
        return _classify_user(text)
    # No role (part-level / raw blob): sniff structure.
    if tool:
        return ContentClass.TOOL_RESULT, Authority.NORMAL
    if _CODE_SIGNALS(text):
        return ContentClass.SOURCE_CODE, Authority.NORMAL
    return ContentClass.USER_MESSAGE, Authority.HIGH


def classify_part(part_kind: str) -> Tuple[str, Authority]:
    """Classify a canonical ``ContentPart`` kind (M2 typed parts)."""
    if part_kind == "tool_result":
        return ContentClass.TOOL_RESULT, Authority.NORMAL
    if part_kind == "tool_call":
        return ContentClass.TOOL_CALL, Authority.HIGH
    if part_kind == "image":
        return ContentClass.IMAGE, Authority.CRITICAL
    if part_kind == "text":
        return ContentClass.USER_MESSAGE, Authority.HIGH
    return ContentClass.UNKNOWN, Authority.NORMAL


def _classify_user(text: str) -> Tuple[str, Authority]:
    """Split the user's own content: constraints stay HIGH, code/history NORMAL.

    A user turn carrying a code block or diff is mostly re-derivable context
    (NORMAL); a plain prose ask is the user's constraint (HIGH). We only *lower*
    authority when the text is unmistakably code/diff/error output — the safe
    direction, because HIGH caps at L1 and NORMAL caps at L3: lowering authority
    permits more compression, but only for content that is already clearly
    re-derivable (code, tool dumps), never for a prose constraint. The guard is
    density-based (sentence breaks per line), so a short code snippet whose
    numeric literals look sentence-like still classifies as code.
    """
    lines = max(1, len(text.splitlines()))
    prose_density = len(_SENTENCE_RE.findall(text)) / lines
    if _CODE_SIGNALS(text) and prose_density < 1.5:
        return ContentClass.SOURCE_CODE, Authority.NORMAL
    if _ERROR_MARK_RE.search(text) and prose_density < 1.5:
        return ContentClass.TOOL_RESULT, Authority.NORMAL
    return ContentClass.USER_MESSAGE, Authority.HIGH


def _CODE_SIGNALS(text: str) -> bool:
    lines = text.splitlines() or [text]
    hits = sum(1 for ln in lines if _HEADING_RE.match(ln) or _DIFF_MARK_RE.match(ln))
    # Either a clear majority of lines look like code, or the snippet is short
    # and every line but one does. Indented continuations count when at least
    # one def/class/import/diff anchor is present, so a two-function snippet
    # reads as code even though only its def lines match the anchor regex.
    if hits >= 3 or (hits >= 1 and len(lines) <= hits + 1):
        return True
    indented = sum(1 for ln in lines if ln.startswith((" ", "\t")) and ln.strip())
    return hits >= 1 and hits + indented >= 3


# -------------------------------------------------------------------- policy
@dataclass(frozen=True)
class CompressionPolicy:
    """Decide the max compression level for a piece of content.

    ``base_level`` is the level the caller *would* apply without policy (e.g. L3
    for tool results, L2 for ordinary text). ``level_for`` returns the effective
    level after the authority cap, plus a decision record explaining itself.

    ``strict_authority`` (default True) enforces the caps. Set it False only in
    tests that want to observe the uncapped request; the gateway always runs
    with the caps on.
    """

    base_level: CompressionLevel = CompressionLevel.L3_REDUNDANCY
    strict_authority: bool = True

    def level_for(
        self,
        text: str = "",
        *,
        role: str = "",
        tool: bool = False,
        base_level: Optional[CompressionLevel] = None,
        part_kind: str = "",
    ) -> ContentDecision:
        """Effective level + decision for one content element.

        Precedence: an explicit ``part_kind`` (typed M2 parts) classifies
        directly; otherwise ``(text, role, tool)`` classifies. The authority cap
        for the resulting class then lowers ``base_level`` when needed.
        """
        if part_kind:
            cls, authority = classify_part(part_kind)
        else:
            cls, authority = classify(text, role=role, tool=tool)
        requested = self.base_level if base_level is None else base_level
        cap = _TIER_MAX_LEVEL[authority]
        effective = min(requested, cap)
        capped = effective < requested
        if capped and not self.strict_authority:
            effective = requested
            capped = False
        reason = (
            f"{cls} is {authority.name}; capped at {cap.name}"
            if capped
            else f"{cls} is {authority.name}; {requested.name} permitted"
        )
        return ContentDecision(
            cls=cls,
            authority=authority,
            level=effective,
            capped=capped,
            reason=reason,
        )

    # ----------------------------------------------------- wire-level helpers
    def apply_to_text(
        self,
        text: str,
        *,
        role: str = "",
        tool: bool = False,
        base_level: CompressionLevel = CompressionLevel.L3_REDUNDANCY,
        compact: Any = None,
    ) -> Tuple[str, ContentDecision]:
        """Compact ``text`` via the caller's ``compact(text, level=...)`` callable.

        ``compact`` is injected (usually ``CDMNormalizer.compact_text``) so the
        policy stays mechanism-free. When the decision is L1 the caller must
        apply only lossless normalization; the policy signals that by returning
        the level, and ``compact`` is expected to honor it. Returns
        ``(possibly_compacted_text, decision)``.
        """
        decision = self.level_for(text, role=role, tool=tool, base_level=base_level)
        if compact is None or decision.level <= CompressionLevel.L0_NONE:
            return text, decision
        return compact(text, level=decision.level, tool=tool), decision
