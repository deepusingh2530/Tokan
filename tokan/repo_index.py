"""Repo structural index — the ".md mapper" (integration pattern 3).

One scan produces a directory-grouped map with top-level symbols, truncated to
a byte budget. Used by the ``token-optimizer index`` CLI and the MCP server's
``repo_index`` tool so agents skip per-session folder rediscovery.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Set

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


def walk(root: Path, ignore: Iterable[str]) -> List[Path]:
    files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore and not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") and name not in {".env.example"}:
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
    files = walk(root, ignore)
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
