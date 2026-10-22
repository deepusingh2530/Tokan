"""Repo index: .gitignore-aware walk and map assembly."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tokan.repo_index import build_index, walk


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


if __name__ == "__main__":
    unittest.main()
