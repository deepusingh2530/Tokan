"""Secret detection (M6): find credential-shaped strings before they travel.

Two modes, both honest about what they do:

* **Detect-and-disclose** (default): every request's payload is scanned; the
  gateway reports what it saw (receipt, console, ``/metrics``, dashboard) so
  an operator learns "you are sending secrets to your provider" — but the
  bytes are forwarded untouched. Nothing is mutated by default.

* **Redact** (opt-in, ``--redact-secrets``): each detected secret is replaced
  with a typed placeholder ``[REDACTED:<type>]`` **before** anything leaves
  the machine — before canonicalization, before the cache digest, before the
  upstream call. The provider never receives the secret, and no digest or
  stored artifact ever contains it. Redaction is deterministic: two payloads
  that differ only in secret material fingerprint identically, so the cache
  key can never encode the secret.

Precision over recall. Every pattern matches a *documented* token format
(prefix + length), never generic entropy — a false accusation on someone's
source code is worse than missing an exotic credential format. Findings are
advisory; with redaction off nothing changes on the wire.

Scanning is capped (``SCAN_LIMIT`` bytes per string value) so a pathological
multi-megabyte paste cannot turn the gateway into a regex amplifier.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

__all__ = [
    "SecretFinding",
    "SECRET_PATTERNS",
    "SCAN_LIMIT",
    "detect_secrets",
    "redact_text",
    "redact_payload",
    "count_payload_secrets",
]

#: Bytes scanned per string value; longer values scan the head only.
SCAN_LIMIT = 2_000_000

#: (type, compiled pattern) — documented credential formats. The OpenAI
#: pattern explicitly excludes ``sk-ant-`` so Anthropic keys classify under
#: their own type instead of colliding with the broader ``sk-`` shape.
SECRET_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,250}|github_pat_[A-Za-z0-9_]{20,250})\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-(?!ant-)(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("private_key_block", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
)


@dataclass(frozen=True)
class SecretFinding:
    """One credential-shaped span: its type and ``[start, end)`` offsets."""

    type: str
    start: int
    end: int


def detect_secrets(text: str, *, scan_limit: int = SCAN_LIMIT) -> List[SecretFinding]:
    """Every credential-shaped span in ``text``, sorted by position.

    When multiple patterns match the same span, all are reported (callers
    that redact keep the first and drop the overlaps); callers that only
    count should de-overlap so one secret is never counted twice.
    """
    if not text:
        return []
    scan = text if len(text) <= scan_limit else text[:scan_limit]
    findings: List[SecretFinding] = []
    for name, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(scan):
            findings.append(SecretFinding(name, match.start(), match.end()))
    findings.sort(key=lambda f: (f.start, f.end, f.type))
    return findings


def redact_text(
    text: str, *, scan_limit: int = SCAN_LIMIT
) -> Tuple[str, Dict[str, int]]:
    """Replace every detected secret with ``[REDACTED:<type>]``.

    Deterministic and overlap-safe: when two patterns match the same span,
    the first (by position, then type name) wins and the covered spans are
    skipped — a secret is never replaced twice. Returns the new text and a
    per-type count of what was removed.
    """
    findings = detect_secrets(text, scan_limit=scan_limit)
    if not findings:
        return text, {}
    parts: List[str] = []
    counts: Dict[str, int] = {}
    cursor = 0
    for finding in findings:
        if finding.start < cursor:
            continue  # overlapping match — already covered by a replacement
        parts.append(text[cursor:finding.start])
        parts.append(f"[REDACTED:{finding.type}]")
        counts[finding.type] = counts.get(finding.type, 0) + 1
        cursor = finding.end
    parts.append(text[cursor:])
    return "".join(parts), counts


def count_payload_secrets(
    payload: Any, *, scan_limit: int = SCAN_LIMIT
) -> Dict[str, int]:
    """Per-type count of secrets in a JSON-ish payload (no mutation).

    Counts are non-overlapping (same de-overlap rule as ``redact_text``), so
    ``sum(count_payload_secrets(p).values())`` equals what redaction would
    have replaced.
    """
    counts: Dict[str, int] = {}

    def walk(node: Any) -> None:
        if isinstance(node, str):
            _, found = redact_text(node, scan_limit=scan_limit)
            for name, n in found.items():
                counts[name] = counts.get(name, 0) + n
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(payload)
    return counts


def redact_payload(
    payload: Any, *, scan_limit: int = SCAN_LIMIT
) -> Tuple[Any, Dict[str, int]]:
    """Deep-copy ``payload`` with every detected secret replaced.

    Strings are rebuilt; numbers/bools/None pass through; dicts and lists are
    rebuilt around them (key names are preserved — they are structural, not
    secret material). Returns ``(redacted_payload, counts)``; the original is
    never mutated.
    """
    counts: Dict[str, int] = {}

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            text, found = redact_text(node, scan_limit=scan_limit)
            for name, n in found.items():
                counts[name] = counts.get(name, 0) + n
            return text
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        return node

    return walk(payload), counts
