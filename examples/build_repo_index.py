"""Pattern 3: the .md repo mapper — one scan, then <8 KB of context per session.

Scan a repository once and emit a structural map (AGENTS.md / CLAUDE.md style)
that an agent loads at session start instead of re-walking the tree with
`ls -R` / glob tool calls on every turn.

    python examples/build_repo_index.py . --out AGENTS.md
    python examples/build_repo_index.py . --out CLAUDE.md --max-bytes 8192
"""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import Iterable, List, Tuple

DEFAULT_IGNORE = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", "coverage",
    ".next", ".turbo", "target", ".idea", ".vscode",
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


def rel(root: Path, path: Path) -> str:
    return str(path.relative_to(root))


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
    files = walk(root, ignore)
    by_dir: dict = {}
    docs: List[str] = []
    for path in files:
        r = rel(root, path)
        if path.suffix.lower() in _CODE_EXT or _DOC_NAME.match(path.name):
            by_dir.setdefault(str(Path(r).parent), []).append(path)
        if _DOC_NAME.match(path.name):
            docs.append(r)

    lines: List[str] = ["# Repository Map", "", f"Generated map of `{root.name}` — structural index only; read files on demand.", ""]

    # Budget-aware: emit densest directories first, drop the tail if too big.
    blocks: List[str] = []
    for directory in sorted(by_dir):
        paths = sorted(by_dir[directory])
        entry = [f"## {directory}/", ""]
        for path in paths:
            rel_path = rel(root, path)
            symbols = collect_symbols(path)
            suffix = f" — {', '.join(symbols)}" if symbols else ""
            entry.append(f"- `{path.name}`{suffix}")
        blocks.append("\n".join(entry))

    body = "\n\n".join(blocks)
    if docs:
        body += "\n\n## Docs\n" + "\n".join(f"- `{d}`" for d in sorted(docs))

    header = "\n".join(lines)
    while len((header + body).encode()) > max_bytes and blocks:
        blocks.pop()  # drop lowest-priority (lexically last) directory
        body = "\n\n".join(blocks)
        if docs:
            body += "\n\n## Docs\n" + "\n".join(f"- `{d}`" for d in sorted(docs))

    out = header + body + "\n"
    total, shown = len(files), sum(len(v) for v in by_dir.values())
    out += f"\n<!-- {shown}/{total} tracked files, {len(out)} bytes -->\n"
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--out", default="AGENTS.md")
    parser.add_argument("--max-bytes", type=int, default=8192, help="budget; map is truncated to fit")
    parser.add_argument("--ignore", action="append", default=[], help="extra dir names to skip")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    ignore = DEFAULT_IGNORE | set(args.ignore)
    content = build_index(root, ignore, args.max_bytes)
    out_path = root / args.out
    out_path.write_text(content, encoding="utf-8")
    print(f"wrote {out_path} ({len(content.encode())} bytes, budget {args.max_bytes})")


if __name__ == "__main__":
    main()
