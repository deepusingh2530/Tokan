"""Repo index: .gitignore-aware walk, map assembly, incremental cache, graph."""
import json
import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import tokan.repo_index as ri
from tokan.repo_index import (
    RepoGraph,
    SymbolCache,
    build_graph,
    build_index,
    collect_identifiers,
    walk,
)


def _fixture_repo(root: Path) -> None:
    """A tiny repo exercising imports, relative imports, tests, non-Python."""
    (root / "lib").mkdir()
    (root / "lib" / "__init__.py").write_text("# pkg\n", encoding="utf-8")
    (root / "lib" / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (root / "lib" / "core.py").write_text(
        "import os\n"
        "from lib.util import helper\n"
        "from . import util\n"
        "def run():\n"
        "    return helper() + util.n\n",
        encoding="utf-8",
    )
    (root / "lib" / "other.py").write_text("helperX = 5\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_core.py").write_text(
        "from lib.core import run\n"
        "def test_run():\n"
        "    run()\n",
        encoding="utf-8",
    )
    (root / "widget.go").write_text("package main\n\nfunc Widget() {}\n", encoding="utf-8")
    (root / "widget_test.go").write_text(
        "package main\n\nfunc TestWidget() { Widget() }\n", encoding="utf-8"
    )
    (root / "notes.txt").write_text("not code\n", encoding="utf-8")


class TestGitignoreAwareWalk(unittest.TestCase):
    def test_gitignored_files_and_dirs_are_skipped(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".gitignore").write_text(
                "setup.sh\n*.log\nbuild/\n/generated\n", encoding="utf-8"
            )
            (root / "main.py").write_text("def entrypoint():\n    pass\n")
            (root / "setup.sh").write_text("echo hi\n")
            (root / "debug.log").write_text("noise\n")
            (root / "build").mkdir()
            (root / "build" / "artifact.py").write_text("def hidden():\n    pass\n")
            (root / "generated").mkdir()
            (root / "generated" / "schema.py").write_text("def gen():\n    pass\n")
            files = {p.relative_to(root).as_posix() for p in walk(root, ignore=set())}
        self.assertEqual(files, {"main.py"})

    def test_gitignore_matches_at_any_depth(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".gitignore").write_text("*.log\nsecrets/\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("def app():\n    pass\n")
            (root / "src" / "debug.log").write_text("x")
            (root / "src" / "secrets").mkdir()
            (root / "src" / "secrets" / "key.py").write_text("KEY = 1\n")
            files = {p.relative_to(root).as_posix() for p in walk(root, ignore=set())}
        self.assertEqual(files, {"src/app.py"})

    def test_file_named_like_dir_rule_is_kept(self):
        # "build/" is a directory rule — a plain FILE named build must survive.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".gitignore").write_text("build/\n", encoding="utf-8")
            (root / "build").write_text("#!/bin/sh\n")  # file, not dir
            (root / "keep.py").write_text("def keep():\n    pass\n")
            files = {p.name for p in walk(root, ignore=set())}
        self.assertEqual(files, {"build", "keep.py"})

    def test_build_index_omits_gitignored_files(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".gitignore").write_text("helper.sh\n", encoding="utf-8")
            (root / "helper.sh").write_text("echo\n")
            (root / "app.py").write_text("def run():\n    pass\n")
            out = build_index(root, ignore=set(), max_bytes=8192)
        self.assertIn("`app.py`", out)
        self.assertNotIn("helper.sh", out)

    def test_no_gitignore_still_walks(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("def a():\n    pass\n")
            files = {p.name for p in walk(root, ignore=set())}
        self.assertEqual(files, {"a.py"})


class TestSymbolCache(unittest.TestCase):
    def _build(self, root, cache):
        return build_index(root, ignore=set(), max_bytes=8192, cache=cache)

    def test_warm_build_reads_nothing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_repo(root)
            cache = SymbolCache(root / ".cache.json")
            self._build(root, cache)                       # cold: every file read
            warm = SymbolCache(root / ".cache.json")
            with mock.patch.object(ri, "_read_text", wraps=ri._read_text) as spy:
                self._build(root, warm)
            self.assertEqual(spy.call_count, 0, "warm build must not re-read files")
            self.assertGreater(warm.hits, 0)
            self.assertEqual(warm.misses, 0)

    def test_warm_output_identical_to_cacheless(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_repo(root)
            plain = build_index(root, ignore=set(), max_bytes=8192)
            cache = SymbolCache(root / ".cache.json")
            cached = self._build(root, cache)
            warm = self._build(root, SymbolCache(root / ".cache.json"))
        self.assertEqual(plain, cached)
        self.assertEqual(cached, warm)

    def test_changed_file_invalidates_only_that_entry(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_repo(root)
            self._build(root, SymbolCache(root / ".cache.json"))
            target = root / "lib" / "util.py"
            target.write_text("def helper():\n    return 1\n\ndef brand_new():\n    pass\n",
                              encoding="utf-8")
            os.utime(target, ns=(1, 1))  # force a distinct mtime
            warm = SymbolCache(root / ".cache.json")
            out = self._build(root, warm)
            self.assertIn("brand_new", out)
            self.assertEqual(warm.misses, 1)
            self.assertGreater(warm.hits, 0)

    def test_deleted_file_is_pruned_from_cache(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_repo(root)
            self._build(root, SymbolCache(root / ".cache.json"))
            (root / "widget.go").unlink()
            cache = SymbolCache(root / ".cache.json")
            self._build(root, cache)
            self.assertNotIn("widget.go", cache._files)

    def test_corrupt_cache_fails_open(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_repo(root)
            (root / ".cache.json").write_text("{not json", encoding="utf-8")
            cache = SymbolCache(root / ".cache.json")
            out = self._build(root, cache)  # must not raise
            self.assertEqual(out, build_index(root, ignore=set(), max_bytes=8192))

    def test_wrong_cache_version_is_discarded(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_repo(root)
            (root / ".cache.json").write_text(
                json.dumps({"version": 99, "files": {"lib/util.py": {"symbols": ["stale"]}}}),
                encoding="utf-8",
            )
            cache = SymbolCache(root / ".cache.json")
            out = self._build(root, cache)
            self.assertIn("helper", out)   # fresh extraction, not the stale entry
            self.assertEqual(cache.hits, 0)  # the stale entry never matches
            self.assertGreater(cache.misses, 0)

    def test_unwritable_cache_location_fails_open(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_repo(root)
            blocker = root / "afile.txt"
            blocker.write_text("i am a file, not a dir\n", encoding="utf-8")
            cache = SymbolCache(blocker / "nested" / "cache.json")
            out = self._build(root, cache)  # save() must swallow the OSError
            self.assertEqual(out, build_index(root, ignore=set(), max_bytes=8192))

    def test_graph_build_warm_reads_nothing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_repo(root)
            build_graph(root, ignore=set(), cache=SymbolCache(root / ".cache.json"))
            warm = SymbolCache(root / ".cache.json")
            with mock.patch.object(ri, "_read_text", wraps=ri._read_text) as spy:
                graph = build_graph(root, ignore=set(), cache=warm)
            self.assertEqual(spy.call_count, 0, "warm graph build must not re-read files")
            self.assertGreater(warm.hits, 0)
            self.assertGreater(len(graph.definitions), 0)


class TestRepoGraph(unittest.TestCase):
    def _graph(self, root) -> RepoGraph:
        return build_graph(root, ignore=set())

    def test_definitions_references_and_whole_word_matching(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_repo(root)
            g = self._graph(root)
            self.assertEqual(g.definitions["helper"], "lib/util.py")
            self.assertEqual(g.definitions["run"], "lib/core.py")
            # definer never lists itself as a referencing file
            self.assertNotIn("lib/util.py", g.references.get("helper", []))
            # whole-word only: lib/other.py has helperX, not helper
            self.assertNotIn("lib/other.py", g.references.get("helper", []))
            # importers/mentioners do count
            self.assertIn("lib/core.py", g.references.get("helper", []))

    def test_source_definition_wins_over_test_definition(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src.py").write_text("def shared():\n    pass\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_src.py").write_text(
                "def shared():\n    pass\n", encoding="utf-8"
            )
            g = self._graph(root)
            self.assertEqual(g.definitions["shared"], "src.py")

    def test_python_import_edges_absolute_and_relative(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_repo(root)
            g = self._graph(root)
            self.assertEqual(g.imports["lib/core.py"], ["lib/util.py"])
            # stdlib must not produce an edge
            self.assertNotIn("os.py", json.dumps(g.imports))
            self.assertEqual(g.imports["tests/test_core.py"], ["lib/core.py"])

    def test_test_edges_python_via_imports_non_python_via_symbols(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_repo(root)
            g = self._graph(root)
            # python: precise import edge
            self.assertEqual(g.tests["lib/core.py"], ["tests/test_core.py"])
            # go: symbol mention fallback (Widget used by widget_test.go)
            self.assertEqual(g.tests["widget.go"], ["widget_test.go"])
            # a test file is never mapped as another test's "source"
            self.assertNotIn("tests/test_core.py", g.tests)

    def test_build_is_deterministic_and_round_trips(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_repo(root)
            g1 = self._graph(root)
            g2 = self._graph(root)
            self.assertEqual(g1.to_json(), g2.to_json())
            self.assertEqual(RepoGraph.from_json(g1.to_json()).to_json(), g1.to_json())

    def test_over_cap_files_are_omitted_not_partial(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_repo(root)
            with mock.patch.object(ri, "IDENTIFIER_CAP", 3):
                self.assertIsNone(collect_identifiers("a b c d"))
                g = self._graph(root)
                # every file exceeds the tiny cap -> no reference edges at all,
                # rather than a silently partial picture
                self.assertEqual(g.references, {})
                self.assertGreater(len(g.definitions), 0)

    def test_graph_stats_match_payload(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_repo(root)
            g = self._graph(root)
            stats = g.stats()
            self.assertEqual(stats["symbols"], len(g.definitions))
            self.assertEqual(
                stats["reference_edges"], sum(len(v) for v in g.references.values())
            )
            self.assertEqual(stats["test_edges"], sum(len(v) for v in g.tests.values()))


class TestIndexCLI(unittest.TestCase):
    def _run(self, argv):
        from tokan.cli import main

        buf = StringIO()
        with redirect_stdout(buf):
            rc = main(argv)
        return rc, buf.getvalue()

    def test_cache_and_graph_out_flags(self):
        from tokan.cli import build_parser

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_repo(root)
            argv = [
                "index", str(root), "--out", "-",
                "--cache", ".git/tokan-index.json",
                "--graph-out", ".git/tokan-graph.json",
            ]
            rc, out = self._run(argv)
            self.assertEqual(rc, 0)
            self.assertIn("`util.py`", out)
            cache_file = root / ".git" / "tokan-index.json"
            graph_file = root / ".git" / "tokan-graph.json"
            self.assertTrue(cache_file.exists())
            self.assertTrue(graph_file.exists())
            graph = json.loads(graph_file.read_text(encoding="utf-8"))
            self.assertEqual(graph["definitions"]["helper"], "lib/util.py")
            # second run: cache is warm (all files hit)
            cache = SymbolCache(cache_file)
            build_index(root, set(), 8192, cache=cache)
            self.assertGreater(cache.hits, 0)
            self.assertEqual(cache.misses, 0)
            # parser defaults: flags are opt-in
            args = build_parser().parse_args(["index"])
            self.assertIsNone(args.cache)
            self.assertIsNone(args.graph_out)


if __name__ == "__main__":
    unittest.main()
