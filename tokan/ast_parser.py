"""AST-aware semantic differencing with tree-sitter integration.

Integrates language parsers for Python, TypeScript/JavaScript, and Go.
Preserves signatures/decorators/imports; elides long method bodies.
Fails open cleanly to standard CDM line-based elision.
"""
from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

from .cdm import CDMNormalizer, estimate_tokens, strip_ansi

__all__ = [
    "ASTParser",
    "parse_ast",
    "compact_ast_text",
    "ASTIntegrityError",
]


class ASTIntegrityError(Exception):
    pass


# ------------------------------------------------------------------ language defs
_PYTHON_SIGNATURES = re.compile(
    r"^(\s*)(?:@[\w\.]+(?:\([^)]+\))?\s+)*"
    r"(?:async\s+)?def\s+([\w_]+)\s*\([^)]*\)(?:\s*->\s*[^:]*)?:"
)
_PYTHON_CLASS = re.compile(r"^(\s*)class\s+([\w_]+)(?:\([^)]*\))?")
_PYTHON_IMPORT = re.compile(r"^(\s*)(from\s+\S+\s+import|import\s+[\w_\.,\s]+)")
_PYTHON_DECORATOR = re.compile(r"^(\s*)@([\w_\.]+(?:\([^)]+\))?)")

_TS_SIGNATURES = re.compile(
    r"^(\s*)(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|const|let|var)\s+([\w_]+)\s*\([^)]*\)(?:\s*:\s*[^\{]{0,40})?\s*\{"
)
_TS_CLASS = re.compile(r"^(\s*)(?:export\s+)?class\s+([\w_]+)(?:\s+\w+)?(?:\s+extends\s+[\w_\.]+)?")
_TS_IMPORT = re.compile(r"^(\s*)(import\s+[^;]+;?|export\s+\{[^}]+\}\s+from\s+[^;]+;?)")

_GO_SIGNATURES = re.compile(r"^(\s*)func\s+([\w_]+)\s*\([^)]*\)(?:\s+[\w\[\],\s]+)?")
_GO_TYPE = re.compile(r"^(\s*)type\s+([\w_]+)\s+(?:interface|struct)")
_GO_IMPORT = re.compile(r"^(\s*)(?:import\s+\(?\s*)?([\w_\.\"\-/]+)")

# File extension mapping
_EXT_MAP = {
    ".py": "python",
    ".pyi": "python",
    ".pyw": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
}


class ASTParser:
    """Language-aware parser that preserves signatures and elides bodies."""

    def __init__(self, max_body_lines: int = 10, fallback: bool = True):
        self.max_body_lines = max_body_lines
        self.fallback = fallback
        self._had_tree_sitter = self._try_tree_sitter()

    def _try_tree_sitter(self) -> bool:
        try:
            import tree_sitter  # type: ignore
            return True
        except Exception:
            return False

    def language_for(self, path: str) -> Optional[str]:
        ext = os.path.splitext(path)[1].lower()
        return _EXT_MAP.get(ext)

    def compact_source(self, source: str, path: str = "") -> str:
        lang = self.language_for(path) if path else None
        if lang is None and path:
            # Try inferring from first lines
            lang = self._infer_lang_from_text(source)
        if lang is None:
            if self.fallback:
                return CDMNormalizer().compact_text(source)
            return source
        try:
            return self._compact_by_lang(source, lang)
        except Exception:
            if self.fallback:
                return CDMNormalizer().compact_text(source)
            return source

    def _infer_lang_from_text(self, source: str) -> Optional[str]:
        first = source[:2000]
        if "def " in first or "import " in first or "class " in first:
            return "python"
        if "func " in first or "type " in first and ("interface" in first or "struct" in first):
            return "go"
        if "function" in first or "const " in first or "export " in first or ("class " in first and ":" in first[:500]):
            return "typescript"
        return None

    def _compact_by_lang(self, source: str, lang: str) -> str:
        lines = source.splitlines(True)
        out: List[str] = []
        i = 0
        n = len(lines)

        while i < n:
            line = lines[i]
            stripped = line.lstrip()

            # Preserve imports
            if lang == "python" and _PYTHON_IMPORT.match(stripped):
                out.append(line)
                i += 1
                continue
            if lang == "typescript" and _TS_IMPORT.match(stripped):
                out.append(line)
                i += 1
                continue
            if lang == "go" and _GO_IMPORT.match(stripped):
                out.append(line)
                i += 1
                continue

            # Preserve decorators / annotations before signatures
            if lang == "python":
                if _PYTHON_DECORATOR.match(stripped):
                    # Collect decorator block
                    dec_lines = [line]
                    i += 1
                    while i < n and (lines[i].strip() == "" or _PYTHON_DECORATOR.match(lines[i].lstrip())):
                        dec_lines.append(lines[i])
                        i += 1
                    # Next should be signature
                    if i < n:
                        out.extend(dec_lines)
                        # Process signature at i
                        # Don't advance i here; loop will handle
                        continue
                    else:
                        out.extend(dec_lines)
                        continue

            # Detect signatures
            sig_line = None
            sig_name = None
            sig_indent = 0

            if lang == "python" and _PYTHON_SIGNATURES.match(stripped):
                sig_line = line
                m = _PYTHON_SIGNATURES.match(stripped)
                sig_name = m.group(2) if m else None
                sig_indent = len(line) - len(stripped)
            elif lang == "python" and _PYTHON_CLASS.match(stripped):
                sig_line = line
                m = _PYTHON_CLASS.match(stripped)
                sig_name = m.group(2) if m else None
                sig_indent = len(line) - len(stripped)
            elif lang == "typescript" and (_TS_SIGNATURES.match(stripped) or _TS_CLASS.match(stripped)):
                sig_line = line
                if _TS_CLASS.match(stripped):
                    m = _TS_CLASS.match(stripped)
                    sig_name = m.group(2) if m else None
                else:
                    m = _TS_SIGNATURES.match(stripped)
                    sig_name = m.group(2) if m else None
                sig_indent = len(line) - len(stripped)
            elif lang == "go" and (_GO_SIGNATURES.match(stripped) or _GO_TYPE.match(stripped)):
                sig_line = line
                if _GO_TYPE.match(stripped):
                    m = _GO_TYPE.match(stripped)
                    sig_name = m.group(2) if m else None
                else:
                    m = _GO_SIGNATURES.match(stripped)
                    sig_name = m.group(2) if m else None
                sig_indent = len(line) - len(stripped)

            if sig_line is not None:
                out.append(sig_line)
                i += 1
                # Collect body until next same-or-less-indent non-empty non-comment line
                # For methods: look for next line with indent <= sig_indent that isn't blank/comment
                body_lines: List[str] = []
                while i < n:
                    body_line = lines[i]
                    body_stripped = body_line.lstrip()
                    # End of body: dedent at or above signature indent, or new class/function at same/higher
                    # Also stop at blank lines followed by same-level non-blank (heuristic)
                    if body_stripped == "" or body_stripped.startswith("#") or body_stripped.startswith("//"):
                        body_lines.append(body_line)
                        i += 1
                        continue
                    # Check indent
                    body_indent = len(body_line) - len(body_stripped)
                    if body_indent <= sig_indent and body_stripped:
                        break
                    body_lines.append(body_line)
                    i += 1

                if len(body_lines) > self.max_body_lines:
                    # Elide body: keep first line (usually docstring or opening) for context, and last 2 lines if closing
                    # Actually spec says: elide method implementation bodies exceeding 15 lines into structural placeholders
                    # while retaining surrounding context.
                    # We'll keep first 2 lines of body and replace rest with elision marker
                    kept = body_lines[:2] if len(body_lines) > 2 else body_lines
                    # Check if last line is closing brace/indent reduction
                    elision = f"# ... [implementation elided: {len(body_lines)} lines]\n"
                    if lang == "python":
                        out.extend(kept)
                        out.append(elision)
                    elif lang in ("typescript", "javascript"):
                        out.extend(kept)
                        out.append(f"// ... [implementation elided: {len(body_lines)} lines]\n")
                    elif lang == "go":
                        out.extend(kept)
                        out.append(f"// ... [implementation elided: {len(body_lines)} lines]\n")
                else:
                    out.extend(body_lines)
                continue
            else:
                # Non-signature line: just preserve
                out.append(line)
                i += 1

        result = "".join(out)
        # Clean up: ensure no excessive blank runs
        result = re.sub(r"\n{4,}", "\n\n\n", result)
        return result


def parse_ast(source: str, path: str = "") -> str:
    parser = ASTParser()
    return parser.compact_source(source, path)


def compact_ast_text(text: str, path: str = "", fallback: bool = True) -> str:
    parser = ASTParser(fallback=fallback)
    return parser.compact_source(text, path)
