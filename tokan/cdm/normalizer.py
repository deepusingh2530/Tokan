"""CDM (Canonical Data Model) normalizer.

Compiles raw, noisy agent payloads (chat messages, tool dumps, file trees)
into a rigid, compact, *deterministic* canonical form that is:

  * cheaper to tokenize (ANSI stripped, tool spam elided, whitespace collapsed)
  * safe to hash for cache lookups (formatting noise does not change the digest)

This module is the front door of the pipeline: everything downstream
(edge cache, JeV router, proxy) keys off :meth:`CDMNormalizer.fingerprint`.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

__all__ = [
    "CanonicalRecord",
    "CDMNormalizer",
    "estimate_tokens",
    "strip_ansi",
]

# CSI sequences plus OSC terminal escapes (screenshots/tool output noise).
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_WHITESPACE_RUN_RE = re.compile(r"\s+")
# A unified-diff hunk header: "@@ -a[,b] +c[,d] @@". Anchored per line so we
# only elide inside real diffs, never in prose that happens to contain "@@ -".
_DIFF_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


def estimate_tokens(text: str) -> int:
    """Cheap ~chars/4 token estimate.

    Prototype heuristic only; swap in a real BPE counter (tiktoken /
    anthropic count_tokens) before trusting absolute numbers. Relative
    savings math stays valid either way.
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def strip_ansi(text: str) -> str:
    """Remove ANSI/OSC escape sequences (terminal colors, hyperlinks)."""
    return _ANSI_RE.sub("", text)


@dataclass(frozen=True)
class CanonicalRecord:
    """Result of compiling a payload into the Canonical Data Model."""

    digest: str
    canonical: str
    original_tokens: int
    canonical_tokens: int

    @property
    def saved_tokens(self) -> int:
        return max(0, self.original_tokens - self.canonical_tokens)

    @property
    def saved_pct(self) -> float:
        if not self.original_tokens:
            return 0.0
        return round(100.0 * self.saved_tokens / self.original_tokens, 2)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "digest": self.digest,
            "original_tokens": self.original_tokens,
            "canonical_tokens": self.canonical_tokens,
            "saved_tokens": self.saved_tokens,
            "saved_pct": self.saved_pct,
        }


class CDMNormalizer:
    """Normalizes agent traffic into the Canonical Data Model.

    Parameters
    ----------
    tool_head / tool_tail:
        Lines of a tool output kept head/tail when truncating. The middle is
        replaced by an explicit elision marker so the model still sees shape.
    max_line_chars:
        Single lines longer than this (minified bundles, base64 blobs) are
        elided to a marker + tail; the tail keeps trailing file paths intact.
    """

    ELISION_MARKER = "... [cdm: {n} lines elided] ..."
    DIFF_ELISION_MARKER = "... [cdm: {n} unchanged diff lines elided] ..."

    def __init__(
        self,
        tool_head: int = 40,
        tool_tail: int = 20,
        max_line_chars: int = 2000,
        diff_context: int = 3,
    ) -> None:
        self.tool_head = tool_head
        self.tool_tail = tool_tail
        self.max_line_chars = max_line_chars
        # Lines of unchanged context kept around each change in a diff hunk.
        self.diff_context = max(0, diff_context)

    # ------------------------------------------------------------------ text
    def _compact_diff(self, text: str) -> str:
        """Elide runs of unchanged context lines inside unified diffs.

        Coding agents ship git diffs constantly, and a multi-file diff is
        mostly space-prefixed context around a handful of ``+``/``-`` changes.
        Changes, hunk headers, and file headers are always kept verbatim; long
        runs of pure context keep their head and tail and elide the middle
        behind an explicit marker (same never-silently-delete contract as
        tool-output elision). The model still sees the shape and every change.
        """
        if "@@ -" not in text:  # fast path: not diff-shaped
            return text
        lines = text.splitlines()
        out: List[str] = []
        run: List[str] = []
        in_hunk = False
        keep = self.diff_context

        def flush() -> None:
            if not run:
                return
            if len(run) > 2 * keep:
                omitted = len(run) - 2 * keep
                out.extend(run[:keep])
                out.append(self.DIFF_ELISION_MARKER.format(n=omitted))
                out.extend(run[-keep:])
            else:
                out.extend(run)
            run.clear()

        for line in lines:
            if _DIFF_HUNK_RE.match(line):
                flush()
                in_hunk = True
                out.append(line)
            elif not in_hunk:
                out.append(line)
            elif line.startswith((" ", "\t")) or line == "":
                # Context inside a hunk (blank line = stripped context line).
                run.append(line)
            elif line.startswith(("+", "-", "\\")):
                flush()
                out.append(line)
            else:
                # Non-diff line: the hunk (and this diff section) ended.
                flush()
                in_hunk = False
                out.append(line)
        flush()
        return "\n".join(out)

    def _tidy(self, text: str) -> str:
        text = strip_ansi(text)
        text = self._compact_diff(text)
        text = _TRAILING_WS_RE.sub("", text)
        text = _BLANK_RUN_RE.sub("\n\n", text)
        return text.strip()

    def _elide_long_line(self, line: str) -> str:
        if len(line) <= self.max_line_chars:
            return line
        keep = self.max_line_chars
        return f"{line[:keep]}... [cdm: {len(line) - keep} chars elided]"

    def _compact_tool_output(self, text: str) -> str:
        lines = text.splitlines()
        if len(lines) > self.tool_head + self.tool_tail:
            head = lines[: self.tool_head]
            tail = lines[-self.tool_tail :]
            omitted = len(lines) - len(head) - len(tail)
            marker = self.ELISION_MARKER.format(n=omitted)
            lines = head + [marker] + tail
        return "\n".join(self._elide_long_line(line) for line in lines)

    def compact_text(self, text: str) -> str:
        """Noise-strip one text blob (ANSI, trailing WS, blank runs, elision).

        Public so wire adapters can compact payloads that don't arrive as
        message lists — e.g. the Gemini API's per-part ``contents`` wire.
        """
        text = self._tidy(text)
        return "\n".join(self._elide_long_line(line) for line in text.splitlines())

    # -------------------------------------------------------------- messages
    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):  # Anthropic-style content blocks
            parts = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(str(block.get("text") or block.get("content") or json.dumps(block, sort_keys=True)))
                else:
                    parts.append(str(block))
            return "\n".join(parts)
        if content is None:
            return ""
        if isinstance(content, dict):
            return json.dumps(content, sort_keys=True, ensure_ascii=False)
        return str(content)

    def normalize_messages(
        self, messages: Sequence[Mapping[str, Any]], *, scrub: bool = False
    ) -> List[Dict[str, Any]]:
        """Produce compact, deterministic messages (see class docstring).

        ``scrub=True`` additionally collapses *all* whitespace runs to single
        spaces. That variant is only used to derive cache digests — the
        forwarded payload keeps real structure (code indentation stays intact).

        Structured content (Anthropic ``tool_result`` blocks, image parts,
        multimodal arrays) is preserved verbatim when forwarding and only
        projected to text for digesting — flattening it would corrupt the
        upstream request.
        """
        out: List[Dict[str, Any]] = []
        prev_key: Optional[str] = None
        for msg in messages:
            role = str(msg.get("role", "unknown"))
            content = msg.get("content")

            if isinstance(content, str) or content is None:
                text = self._content_to_text(content)
                text = (
                    self._compact_tool_output(text) if role == "tool" else self._tidy(text)
                )
                text = "\n".join(self._elide_long_line(line) for line in text.splitlines())
                if scrub:
                    text = _WHITESPACE_RUN_RE.sub(" ", text).strip()
                entry: Dict[str, Any] = {"role": role, "content": text}
            elif scrub:
                projected = _WHITESPACE_RUN_RE.sub(
                    " ", self._tidy(self._content_to_text(content))
                ).strip()
                entry = {"role": role, "content": projected}
            else:
                entry = {"role": role, "content": content}

            for key in ("name", "tool_call_id"):
                if msg.get(key):
                    entry[key] = msg[key]
            if msg.get("tool_calls"):
                entry["tool_calls"] = msg["tool_calls"]

            # Compare on the full entry so structured content dedups correctly.
            dedup_key = json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str)
            if dedup_key == prev_key:  # drop consecutive exact duplicates
                continue
            out.append(entry)
            prev_key = dedup_key
        return out

    def canonical_json(
        self,
        messages: Sequence[Mapping[str, Any]],
        scope: Optional[Mapping[str, Any]] = None,
        *,
        scrub: bool = False,
    ) -> str:
        """Deterministic serialization: sorted keys, no incidental whitespace."""
        payload: Dict[str, Any] = {"messages": self.normalize_messages(messages, scrub=scrub)}
        if scope:
            payload["scope"] = {k: v for k, v in sorted(scope.items()) if v is not None}
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def canonical_message_strings(self, messages: Sequence[Mapping[str, Any]]) -> List[str]:
        """Deterministic per-message serialization, one string per message.

        The Session Governor keys prefix reuse off these strings: because they
        are byte-stable, a growing conversation's shared prefix serializes
        identically every turn, which is exactly the condition a provider's
        prompt cache needs to hit. (A non-deterministic compactor would break
        that and silently cost the discount.)
        """
        normalized = self.normalize_messages(messages)
        return [
            json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
            for m in normalized
        ]

    def fingerprint(
        self,
        messages: Sequence[Mapping[str, Any]],
        scope: Optional[Mapping[str, Any]] = None,
    ) -> CanonicalRecord:
        """Digest + before/after token accounting for a chat payload.

        ``scope`` holds request parameters that change the answer (model,
        temperature, ...) so a cached gpt-4 reply is never served to gpt-3.5.
        """
        original_blob = json.dumps(
            {"messages": list(messages), "scope": dict(scope or {})},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        canonical = self.canonical_json(messages, scope)
        # Digest derives from the whitespace-scrubbed form so padding, line
        # breaks, and terminal escapes never fragment the cache.
        basis = self.canonical_json(messages, scope, scrub=True)
        return CanonicalRecord(
            digest=hashlib.sha256(basis.encode("utf-8")).hexdigest(),
            canonical=canonical,
            original_tokens=estimate_tokens(original_blob),
            canonical_tokens=estimate_tokens(canonical),
        )

    # ------------------------------------------------------------ file trees
    def compact_file_tree(self, paths: Iterable[str]) -> str:
        """Group a flat path listing by directory: fewer tokens, same info.

        ``["src/a.py", "src/b.py", "tests/t.py"]`` becomes
        ``[tree 3 files] src/:a.py,b.py | tests/:t.py``
        """
        unique = sorted({p.strip() for p in paths if p and p.strip()})
        groups: Dict[str, List[str]] = defaultdict(list)
        for path in unique:
            directory, _, filename = path.rpartition("/")
            groups[directory].append(filename)
        parts = [
            (f"{dir_}/:" if dir_ else "") + ",".join(names)
            for dir_, names in sorted(groups.items())
        ]
        return f"[tree {len(unique)} files] " + " | ".join(parts)
