"""Repo structural index — the ".md mapper" (integration pattern 3).

One scan produces a directory-grouped map with top-level symbols, truncated to
a byte budget. Used by the ``token-optimizer index`` CLI and the MCP server's
``repo_index`` tool so agents skip per-session folder rediscovery.

Respects the root ``.gitignore`` (common pattern forms; ``!`` negations are
not supported) so a committed map never references local-only build output.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

__all__ = ["DEFAULT_IGNORE", "build_index", "collect_symbols", "walk"]

DEFAULT_IGNORE: Set[str] = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", "coverage",
    ".next", ".turbo", "target", ".idea", ".vscode", ".cline", ".roo",
}

_CODE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".c",
    ".h", ".cpp", ".hpp", ".rb", ".php", ".swift", ".scala", ".sh", ".sql",
}

_DOC_NAME = re.compile(r"^(readme|agents|claude|contributing|architect(ure)?)", re.I)

_TOP_SYMBOL = re.compile(
    r"^(?:def|class|function)\s+(\w+)|^export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+(\w+)",
    re.M,
)

# (kind, pattern) kinds produced by _gitignore_rules:
#   "any"    — bare name/glob ("setup.sh", "*.log") — matches at any depth
#   "dir"    — trailing-slash dir name ("build/") — matches any depth
#   "anchor" — root-anchored path ("/dist")
#   "path"   — path with interior slash ("docs/generated") — root-relative
GitRule = Tuple[str, str]


def _gitignore_rules(root: Path) -> List[GitRule]:
    """Parse the root ``.gitignore`` into matchable rules.

    Covers the pattern forms real repos use (comments, blanks, bare names,
    globs, directory suffixes, anchored paths). Negation (``!keep``) is
    deliberately unsupported: a map that re-includes an ignored path is
    worse than one that omits it.
    """
    rules: List[GitRule] = []
    try:
        text = (Path(root) / ".gitignore").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rules
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if line.startswith("/"):
            rules.append(("anchor", line.lstrip("/")))
        elif line.endswith("/"):
            rules.append(("dir", line.rstrip("/")))
        elif "/" in line:
            rules.append(("path", line.rstrip("/")))
        else:
            rules.append(("any", line))
    return rules


def _is_ignored(relative: str, is_dir: bool, rules: Iterable[GitRule]) -> bool:
    parts = Path(relative).parts
    name = parts[-1] if parts else relative
    for kind, pattern in rules:
        if kind == "any":
            if fnmatch.fnmatch(name, pattern):
                return True
        elif kind == "dir":
            # Trailing-slash rules match a directory name at any depth —
            # but the final component only counts when it really is a dir.
            for i, part in enumerate(parts):
                if not fnmatch.fnmatch(part, pattern):
                    continue
                if i == len(parts) - 1 and not is_dir:
                    continue
                return True
        elif kind == "anchor":
            if fnmatch.fnmatch(relative, pattern):
                return True
        elif kind == "path":
            if relative == pattern or relative.startswith(pattern + "/"):
                return True
    return False


def walk(root: Path, ignore: Iterable[str], rules: Optional[Iterable[GitRule]] = None) -> List[Path]:
    """Walk ``root`` for indexable files, skipping ``ignore`` and gitignored paths.

    ``rules`` defaults to parsing ``root/.gitignore``; pass an explicit empty
    list to disable gitignore filtering.
    """
    root = Path(root)
    rules = list(_gitignore_rules(root) if rules is None else rules)
    files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in ignore
            and not d.startswith(".")
            and not _is_ignored(str((Path(dirpath) / d).relative_to(root)), True, rules)
        ]
        for name in filenames:
            if name.startswith(".") and name not in {".env.example"}:
                continue
            if _is_ignored(str((Path(dirpath) / name).relative_to(root)), False, rules):
                continue
            files.append(Path(dirpath) / name)
    return files


def collect_symbols(path: Path, limit: int = 12) -> List[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: List[str] = []
    for match in _TOP_SYMBOL.finditer(text):
        name = next((g for g in match.groups() if g), None)
        if name:
            found.append(name)
        if len(found) >= limit:
            break
    return found


def build_index(root: Path, ignore: Iterable[str], max_bytes: int) -> str:
    """Build the structural map, dropping lowest-priority directories until it fits."""
    root = Path(root)
    files = walk(root, ignore, _gitignore_rules(root))
    by_dir: Dict[str, List[Path]] = {}
    docs: List[str] = []
    for path in files:
        relative = str(path.relative_to(root))
        if path.suffix.lower() in _CODE_EXT or _DOC_NAME.match(path.name):
            by_dir.setdefault(str(Path(relative).parent), []).append(path)
        if _DOC_NAME.match(path.name):
            docs.append(relative)

    header = (
        f"# Repository Map\n\nGenerated map of `{root.name}` — structural index only; read files on demand.\n"
    )

    blocks: List[str] = []
    for directory in sorted(by_dir):
        entry = [f"## {directory}/", ""]
        for path in sorted(by_dir[directory]):
            symbols = collect_symbols(path)
            suffix = f" — {', '.join(symbols)}" if symbols else ""
            entry.append(f"- `{path.name}`{suffix}")
        blocks.append("\n".join(entry))

    def assemble(blocks_: List[str]) -> str:
        body = "\n\n".join(blocks_)
        if docs:
            body += "\n\n## Docs\n" + "\n".join(f"- `{d}`" for d in sorted(docs))
        return header + body + "\n"

    body = assemble(blocks)
    while len(body.encode()) > max_bytes and blocks:
        blocks.pop()
        body = assemble(blocks)

    total, shown = len(files), sum(len(v) for v in by_dir.values())
    body += f"\n<!-- {shown}/{total} tracked files, {len(body.encode())} bytes -->\n"
    return body
