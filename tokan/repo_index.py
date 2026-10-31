"""Repo structural index — the ".md mapper" (integration pattern 3).

One scan produces a directory-grouped map with top-level symbols, truncated to
a byte budget. Used by the ``token-optimizer index`` CLI and the MCP server's
``repo_index`` tool so agents skip per-session folder rediscovery.

M5 additions (all opt-in, all deterministic):

* ``SymbolCache`` — on-disk ``(mtime_ns, size) -> symbols/identifiers`` so an
  unchanged file is never re-read on a rebuild (``build_index(..., cache=)``).
* ``RepoGraph`` + ``build_graph()`` — top-level symbol definitions, cross-file
  references, Python import edges and test->source relationships, so agents can
  ask "who defines X / who uses it / which test covers it" instead of grepping.

Respects the root ``.gitignore`` (common pattern forms; ``!`` negations are
not supported) so a committed map never references local-only build output.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

__all__ = [
    "DEFAULT_IGNORE",
    "RepoGraph",
    "SymbolCache",
    "build_graph",
    "build_index",
    "collect_identifiers",
    "collect_symbols",
    "symbols_of",
    "walk",
]

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
    r"^(?:def|class|function|func)\s+(\w+)|^export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+(\w+)",
    re.M,
)

#: Whole-file identifier extraction for reference edges (language-agnostic:
#: snake_case and CamelCase names both survive as one token).
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: A file with more unique identifiers than this is treated as generated/
#: minified: too big to cache and low-value for reference edges. Documented
#: omission, not a silent truncation of a good set.
IDENTIFIER_CAP = 5000

#: Cache schema version — a mismatch discards the cache instead of guessing.
_CACHE_VERSION = 1

#: Sentinel for "field not computed this run" (vs ``None`` = computed, absent).
_NOT_STORED = object()

#: (kind, pattern) kinds produced by _gitignore_rules:
#:   "any"    — bare name/glob ("setup.sh", "*.log") — matches at any depth
#:   "dir"    — trailing-slash dir name ("build/") — matches any depth
#:   "anchor" — root-anchored path ("/dist")
#:   "path"   — path with interior slash ("docs/generated") — root-relative
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


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def symbols_of(text: str, limit: int = 12) -> List[str]:
    """Top-level symbol names in ``text`` (the pure, cacheable half)."""
    found: List[str] = []
    for match in _TOP_SYMBOL.finditer(text):
        name = next((g for g in match.groups() if g), None)
        if name:
            found.append(name)
        if len(found) >= limit:
            break
    return found


def collect_identifiers(text: str) -> Optional[Set[str]]:
    """Every identifier-shaped token in ``text``; ``None`` when over the cap.

    Reference edges are exact-word matches, so this set is all a later run
    needs to recompute references without re-reading the file. Over-cap files
    (generated/minified) store nothing and are omitted from reference edges.
    """
    ids = set(_IDENTIFIER_RE.findall(text))
    return ids if len(ids) <= IDENTIFIER_CAP else None


def collect_symbols(path: Path, limit: int = 12) -> List[str]:
    return symbols_of(_read_text(path), limit)


class SymbolCache:
    """On-disk ``(mtime_ns, size) -> symbols/identifiers/imports`` cache.

    A rebuild only re-reads files whose mtime or size changed; everything
    else reuses the stored extraction, so repeated ``index``/``repo_graph``
    runs skip the read+parse cost. JSON on disk so separate CLI/MCP
    invocations share it.

    Every failure is fail-open: an unreadable/corrupt/foreign cache file is
    treated as empty (a slow build, never a wrong one), and a failed save
    still returns the build. The cache stores *derived* data only — never
    anything that changes what a fresh, cacheless build would produce.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else None
        self._files: Dict[str, Dict[str, object]] = {}
        self._seen: Set[str] = set()
        self.hits = 0
        self.misses = 0
        self.load()

    def load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._files = {}
            return
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION or not isinstance(files, dict):
            self._files = {}  # wrong version or shape: discard rather than guess
            return
        self._files = files

    def save(self, *, keep: Optional[Iterable[str]] = None) -> None:
        """Persist, pruning entries for files no longer walked (deleted/ignored).

        ``keep`` is the caller's full walked set — entries outside it are
        stale. Without it, falls back to files touched this run (a build that
        only *reads* some entries must not prune the rest).
        """
        if self.path is None:
            return
        allowed = set(keep) if keep is not None else self._seen
        pruned = {k: v for k, v in self._files.items() if k in allowed}
        self._files = pruned
        payload = {"version": _CACHE_VERSION, "files": pruned}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.path)  # atomic swap: never a torn cache file
        except OSError:
            pass  # fail-open: a missing cache only costs the next rebuild

    def get(self, relative: str, path: Path) -> Optional[Dict[str, object]]:
        """Fresh entry (both mtime_ns and size match) or ``None`` to re-read."""
        try:
            st = path.stat()
        except OSError:
            return None
        entry = self._files.get(relative)
        if not isinstance(entry, dict):
            self.misses += 1
            return None
        if entry.get("mtime") != st.st_mtime_ns or entry.get("size") != st.st_size:
            self.misses += 1
            return None
        symbols = entry.get("symbols")
        if not isinstance(symbols, list):
            self.misses += 1
            return None
        self._seen.add(relative)
        self.hits += 1
        return entry

    def put(
        self,
        relative: str,
        path: Path,
        symbols: List[str],
        *,
        identifiers: object = _NOT_STORED,
        imports: object = _NOT_STORED,
    ) -> None:
        """Record this run's extraction for ``relative`` (overwrites stale data).

        ``identifiers=None`` means "computed but over the cap" and is stored
        as JSON null so a warm build never re-reads a known-huge file;
        ``_NOT_STORED`` (default) means "not computed this run" and omits the
        key entirely.
        """
        try:
            st = path.stat()
        except OSError:
            return
        rec: Dict[str, object] = {
            "mtime": st.st_mtime_ns,
            "size": st.st_size,
            "symbols": list(symbols),
        }
        if identifiers is not _NOT_STORED:
            rec["identifiers"] = (
                sorted(identifiers) if isinstance(identifiers, (set, frozenset, list, tuple)) else None  # type: ignore[arg-type]
            )
        if imports is not _NOT_STORED:
            rec["imports"] = list(imports)  # type: ignore[arg-type]
        self._files[relative] = rec
        self._seen.add(relative)

    @property
    def entry_count(self) -> int:
        return len(self._files)


def build_index(
    root: Path, ignore: Iterable[str], max_bytes: int, *, cache: Optional[SymbolCache] = None
) -> str:
    """Build the structural map, dropping lowest-priority directories until it fits.

    With ``cache``, files whose ``(mtime_ns, size)`` are unchanged reuse their
    stored symbols instead of being re-read; the cache is saved on return.
    """
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
            relative = str(path.relative_to(root))
            if cache is not None:
                hit = cache.get(relative, path)
                if hit is not None:
                    symbols = list(hit["symbols"])  # type: ignore[arg-type]
                else:
                    text = _read_text(path)
                    symbols = symbols_of(text)
                    identifiers = (
                        collect_identifiers(text) if path.suffix.lower() in _CODE_EXT else None
                    )
                    cache.put(relative, path, symbols, identifiers=identifiers)
            else:
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
    if cache is not None:
        cache.save(keep=[str(p.relative_to(root)) for p in files])
    return body


# ---------------------------------------------------- relationship graph (M5)
#: Test-file shapes: ``test_*.py``, ``*_test.go``, ``x.test.ts``, ``x.spec.js``,
#: plus anything under a ``tests/``/``__tests__/`` directory.
_TEST_NAME = re.compile(r"(?:^test_[\w.-]+$|_test\.[A-Za-z0-9]+$|\.(?:test|spec)\.[A-Za-z0-9]+$)", re.I)
_TEST_DIR = {"tests", "test", "__tests__", "spec"}

_PY_FROM = re.compile(r"^from\s+(\.*)([A-Za-z_][\w.]*)?\s+import\s+(.*)$")
_IDENT_OK = re.compile(r"[A-Za-z_][\w.]*$")


def _is_test(relative: str) -> bool:
    parts = Path(relative).parts
    if any(p in _TEST_DIR for p in parts[:-1]):
        return True
    # search(), not match(): the suffix forms (foo_test.go, x.test.ts) anchor
    # at the END of the name, not the start.
    return bool(_TEST_NAME.search(parts[-1]))


def _py_imports(text: str) -> List[Tuple[int, str, List[str]]]:
    """``(level, module, names)`` per Python import line.

    Line-anchored and best-effort: a string literal whose *line* begins with
    ``import`` inside a docstring could produce an edge, but only to a module
    that really exists in the repo — a false edge still points at real code.
    Multiline ``from x import (`` captures the module (line 1) but not the
    name list, which only matters for the ``from . import name`` submodule case.
    """
    out: List[Tuple[int, str, List[str]]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("from "):
            m = _PY_FROM.match(stripped)
            if not m:
                continue
            level = len(m.group(1))
            module = m.group(2) or ""
            names = re.findall(r"[A-Za-z_]\w*", m.group(3))
            out.append((level, module, names))
        elif stripped.startswith("import "):
            for part in stripped[7:].split(","):
                token = part.strip().split()[0] if part.strip() else ""
                if token and _IDENT_OK.match(token):
                    out.append((0, token, []))
    return out


def _module_map(py_files: Iterable[str]) -> Dict[str, str]:
    """Dotted module name -> relpath (``tokan/policy.py -> tokan.policy``)."""
    modules: Dict[str, str] = {}
    for rel in py_files:
        dotted = rel[:-3].replace("/", ".")
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        modules[dotted] = rel
    return modules


def _longest_prefix(dotted: str, modules: Dict[str, str]) -> Optional[str]:
    """Shortest-code path for ``a.b.c``: exact hit wins, else ``a.b``, ``a``.

    Longest prefix keeps ``from tokan import policy`` -> ``tokan/__init__.py``
    while ``from tokan.policy import classify`` -> ``tokan/policy.py``, and
    turns ``from a import b`` into the ``a/b.py`` edge when ``b`` is a module.
    """
    if not dotted:
        return None
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        hit = modules.get(".".join(parts[:i]))
        if hit is not None:
            return hit
    return None


def _resolve_py_import(
    level: int, module: str, names: List[str], relpath: str, modules: Dict[str, str]
) -> Optional[str]:
    """Resolve one import line to a repo-local file (``None`` = outside repo).

    The dotted candidate extends to the first imported name so
    ``from a import b`` prefers ``a/b.py`` when ``b`` is a module, then falls
    back through the prefixes (``a.b`` -> ``a``) — one longest-prefix lookup
    covers module, package and name imports.
    """
    if level == 0 and not module:
        return None
    if level:
        parts = Path(relpath).parent.parts
        # clamp: a pathological ``from ....`` cannot climb above the repo root
        # (a negative slice would silently wrap around).
        base = list(parts[: max(0, len(parts) - (level - 1))])
    else:
        base = []
    if module:
        dotted = ".".join(base + module.split("."))
        if names:
            dotted += "." + names[0]
    elif names:  # ``from . import x``: x may itself be a submodule
        dotted = ".".join(base + [names[0]])
    else:  # ``from . import *``: the package itself
        dotted = ".".join(base)
    return _longest_prefix(dotted, modules)


@dataclass
class RepoGraph:
    """Cross-file relationships over top-level symbols.

    All four relations are deterministic (sorted, first-definer-wins in
    non-test-then-test order) and built from whole-word matches only:

    * ``definitions`` — symbol -> the file that defines it. Source files are
      processed before test files so a source ``main`` never loses to a test's.
    * ``references``  — symbol -> files mentioning it, excluding every file
      that defines it (re-definition is not a reference).
    * ``imports``     — file -> repo-local files it imports (Python only;
      other languages are covered by ``references``, not import edges).
    * ``tests``       — source file -> test files covering it. Python tests
      use resolved imports (precise); non-Python tests fall back to
      whole-symbol mentions (best effort).
    """

    definitions: Dict[str, str] = field(default_factory=dict)
    references: Dict[str, List[str]] = field(default_factory=dict)
    imports: Dict[str, List[str]] = field(default_factory=dict)
    tests: Dict[str, List[str]] = field(default_factory=dict)

    def stats(self) -> Dict[str, int]:
        return {
            "symbols": len(self.definitions),
            "referenced_symbols": len(self.references),
            "reference_edges": sum(len(v) for v in self.references.values()),
            "files_with_imports": len(self.imports),
            "import_edges": sum(len(v) for v in self.imports.values()),
            "test_edges": sum(len(v) for v in self.tests.values()),
        }

    def to_json(self) -> Dict[str, object]:
        return {
            "definitions": dict(sorted(self.definitions.items())),
            "references": {k: sorted(v) for k, v in sorted(self.references.items())},
            "imports": {k: sorted(v) for k, v in sorted(self.imports.items())},
            "tests": {k: sorted(v) for k, v in sorted(self.tests.items())},
            "stats": self.stats(),
        }

    @classmethod
    def from_json(cls, data: Dict[str, object]) -> "RepoGraph":
        defs_raw = data.get("definitions") or {}
        return cls(
            definitions={str(k): str(v) for k, v in dict(defs_raw).items()},
            references={str(k): [str(x) for x in v] for k, v in dict(data.get("references") or {}).items()},
            imports={str(k): [str(x) for x in v] for k, v in dict(data.get("imports") or {}).items()},
            tests={str(k): [str(x) for x in v] for k, v in dict(data.get("tests") or {}).items()},
        )


def build_graph(root: Path, ignore: Iterable[str], *, cache: Optional[SymbolCache] = None) -> RepoGraph:
    """Build the relationship graph for ``root``.

    With ``cache``, files whose extraction (symbols/identifiers/imports) is
    already fresh are not re-read — a warm graph build on an unchanged repo
    reads nothing. The cache is saved on return.
    """
    root = Path(root)
    files = walk(root, ignore, _gitignore_rules(root))
    code = sorted(
        str(p.relative_to(root)) for p in files if p.suffix.lower() in _CODE_EXT
    )
    py_files = [rel for rel in code if rel.endswith(".py")]
    modules = _module_map(py_files)

    symbols: Dict[str, List[str]] = {}
    identifiers: Dict[str, Optional[Set[str]]] = {}
    py_import_lines: Dict[str, List[Tuple[int, str, List[str]]]] = {}

    for rel in code:
        path = root / rel
        entry = cache.get(rel, path) if cache is not None else None
        cached_symbols: Optional[List[str]] = list(entry["symbols"]) if entry else None  # type: ignore[arg-type]
        has_ids = bool(entry) and "identifiers" in entry  # type: ignore[union-attr]
        cached_ids = entry.get("identifiers") if entry else None
        has_imports = bool(entry) and "imports" in entry  # type: ignore[union-attr]
        cached_imports = entry.get("imports") if entry else None
        is_py = rel.endswith(".py")
        need = (
            cached_symbols is None
            or not has_ids
            or (is_py and not has_imports)
        )
        text: Optional[str] = None
        if need:
            text = _read_text(path)
        symbols[rel] = cached_symbols if cached_symbols is not None else symbols_of(text or "")
        if has_ids:
            identifiers[rel] = set(cached_ids) if isinstance(cached_ids, list) else None  # type: ignore[arg-type]
        elif text is not None:
            identifiers[rel] = collect_identifiers(text)
        else:  # pragma: no cover — need is always True when has_ids is False
            identifiers[rel] = None
        if is_py:
            if has_imports and isinstance(cached_imports, list):
                py_import_lines[rel] = [tuple(x) for x in cached_imports]  # type: ignore[misc]
            else:
                py_import_lines[rel] = _py_imports(text or _read_text(path))
        if cache is not None and need:
            cache.put(
                rel,
                path,
                symbols[rel],
                identifiers=identifiers[rel],
                imports=[list(t) for t in py_import_lines.get(rel, [])] if is_py else _NOT_STORED,
            )

    # definitions: source files first, then tests; sorted within each tier so
    # the first definer is stable across runs.
    definers: Dict[str, Set[str]] = {}
    definitions: Dict[str, str] = {}
    for rel in sorted(code, key=lambda r: (_is_test(r), r)):
        for sym in symbols.get(rel, []):
            definers.setdefault(sym, set()).add(rel)
            definitions.setdefault(sym, rel)

    # references: whole-word mention in a file that does not define it.
    # Inverted pass — iterate each file's identifier SET and keep only those
    # that are repo symbols (O(total identifiers)), not the cross product of
    # every symbol with every file (O(symbols * files), which dominates graph
    # builds on repos bigger than a few hundred files).
    references: Dict[str, List[str]] = {}
    for rel in code:
        ids = identifiers.get(rel)
        if not ids:
            continue
        for sym in ids:
            if sym not in definitions:
                continue
            owners = definers.get(sym)
            if owners is not None and rel in owners:
                continue  # re-definition is not a reference
            references.setdefault(sym, []).append(rel)

    # imports: Python resolution to repo-local files only.
    imports: Dict[str, List[str]] = {}
    for rel in py_files:
        targets: List[str] = []
        for level, module, names in py_import_lines.get(rel, []):
            hit = _resolve_py_import(level, module, names, rel, modules)
            if hit is not None and hit != rel and hit not in targets:
                targets.append(hit)
        if targets:
            imports[rel] = sorted(targets)

    # tests: Python via import edges; other languages via symbol mentions.
    tests: Dict[str, List[str]] = {}
    for rel in code:
        if not _is_test(rel):
            continue
        targets = {t for t in imports.get(rel, []) if not _is_test(t)}
        if not rel.endswith(".py"):
            ids = identifiers.get(rel)
            if ids:
                for sym, definer in definitions.items():
                    if definer != rel and not _is_test(definer) and sym in ids:
                        targets.add(definer)
        if targets:
            for target in sorted(targets):
                tests.setdefault(target, []).append(rel)

    for mapping in (references, imports, tests):
        for key in mapping:
            mapping[key] = sorted(set(mapping[key]))

    graph = RepoGraph(definitions=definitions, references=references, imports=imports, tests=tests)
    if cache is not None:
        cache.save(keep=[str(p.relative_to(root)) for p in files])
    return graph
