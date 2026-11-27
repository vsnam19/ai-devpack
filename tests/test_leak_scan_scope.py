"""Tests for the scan scope of the leak test: only committable files are scanned.

Each test builds a throwaway git repository in a temporary directory, so no file in the real
work tree is touched. Forbidden words are assembled from the fragments of the leak test module
so that this file does not match itself.
"""

import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

_LEAK_MODULE_PATH = Path(__file__).with_name("test_no_project_leaks.py")
_spec = importlib.util.spec_from_file_location("test_no_project_leaks", _LEAK_MODULE_PATH)
leak = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(leak)

HOME_PATH = leak.FORBIDDEN[0] + "x"  # a host path prefix followed by a user name
PROJECT_WORD = "t" + "cu"  # the short project name, lower case


def _git(root: Path, *args: str) -> None:
    """Run git inside ``root`` with the caller's GIT_* overrides removed; raise on failure."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


def _write(root: Path, rel: str, text: str) -> None:
    """Create ``root/rel`` with ``text``, creating parent directories as needed."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class CommittableScopeTest(unittest.TestCase):
    """The leak scan reads tracked and untracked-not-ignored files and nothing else."""

    def setUp(self):
        """Create a temporary git repository with a global excludes file configured."""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "repo"
        self.root.mkdir()
        self.global_ignore = Path(self._tmp.name) / "global_ignore"
        self.global_ignore.write_text(".vscode/\n")
        _git(self.root, "init", "-q")
        _git(self.root, "config", "core.excludesFile", str(self.global_ignore))

    def test_tracked_and_untracked_symlinks(self):
        word = PROJECT_WORD
        (self.root / f"{word}.link").symlink_to("missing")
        _git(self.root, "add", f"{word}.link")
        (self.root / "dir").mkdir()
        (self.root / "dir" / word).mkdir()
        (self.root / "directory.link").symlink_to(f"dir/{word}", target_is_directory=True)
        self.assertCountEqual(
            [f"{word}.link: (file name) {word}", f"directory.link: (link target) {word}"],
            leak.find_leaks(self.root),
        )

    def test_ignored_file_with_host_path_is_not_reported(self):
        """A file matched by a global ignore rule is outside the scan, even with a host path."""
        _write(self.root, ".vscode/settings.json", f'{{"path": "{HOME_PATH}"}}\n')
        self.assertEqual([], leak.find_leaks(self.root))

    def test_untracked_non_ignored_file_with_host_path_is_reported(self):
        """An untracked file that is not ignored is scanned and its leak is reported."""
        _write(self.root, "notes.md", f"see {HOME_PATH}\n")
        leaks = leak.find_leaks(self.root)
        self.assertEqual(1, len(leaks))
        self.assertTrue(leaks[0].startswith("notes.md:1:"), leaks)

    def test_tracked_file_with_host_path_is_reported(self):
        """A tracked file is scanned, whether or not an ignore rule would cover its path."""
        _write(self.root, "tracked.md", f"see {HOME_PATH}\n")
        _git(self.root, "add", "tracked.md")
        leaks = leak.find_leaks(self.root)
        self.assertEqual(1, len(leaks))
        self.assertTrue(leaks[0].startswith("tracked.md:1:"), leaks)

    def test_tracked_file_deleted_from_disk_is_skipped(self):
        """A path listed by git but missing on disk does not crash the scan."""
        _write(self.root, "gone.md", f"see {HOME_PATH}\n")
        _git(self.root, "add", "gone.md")
        (self.root / "gone.md").unlink()
        self.assertEqual([], leak.find_leaks(self.root))

    def test_project_word_inside_base64_is_not_reported(self):
        """A project word embedded inside a longer alphanumeric run is not a match."""
        _write(self.root, "blob.txt", "abc" + PROJECT_WORD.capitalize() + "Xyz\nQ2ytCu01gZ\n")
        self.assertEqual([], leak.find_leaks(self.root))

    def test_project_word_as_separate_word_is_reported(self):
        """The project word surrounded by spaces is a match (case-insensitive)."""
        _write(self.root, "word.txt", " " + PROJECT_WORD.upper() + " \n")
        leaks = leak.find_leaks(self.root)
        self.assertEqual(1, len(leaks))
        self.assertTrue(leaks[0].startswith("word.txt:1:"), leaks)

    def test_proxy_spellings_in_text_and_file_names(self):
        """All proxy spellings and camel case match in file contents and file names."""
        variants = ["tls" + separator + "proxy" for separator in ("", "_", "-")]
        variants.append("Tls" + "Proxy")
        for word in variants:
            with self.subTest(word=word), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _write(root, "notes.txt", f"see {word} here\n")
                _write(root, f"{word}.txt", "clean\n")
                self.assertCountEqual(
                    [f"notes.txt:1: {word}", f"{word}.txt: (file name) {word}"],
                    leak.find_leaks(root),
                )

    def test_words_containing_only_one_proxy_fragment_are_ignored(self):
        """Unrelated words containing only one proxy fragment are not flagged."""
        _write(self.root, "dtls-handshake.txt", "Use dtls and proxypass.\n")
        _write(self.root, "proxypass.txt", "The proxy supports tls.\n")
        self.assertEqual([], leak.find_leaks(self.root))

    def test_host_path_is_substring_match(self):
        """The host-path pattern matches without word boundaries."""
        self.assertIsNotNone(leak.PATTERN.search("prefix" + HOME_PATH))


class FallbackWalkTest(unittest.TestCase):
    """Outside a git work tree the leak scan walks the directory and says so."""

    def test_walk_scans_dangling_targets_without_reading_external_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "archive"
            root.mkdir()
            _write(parent, "outside.txt", PROJECT_WORD)
            (root / "external.link").symlink_to("../outside.txt")
            (root / "dangling.link").symlink_to(f"../{PROJECT_WORD}/missing.txt")
            self.assertEqual(
                [f"dangling.link: (link target) {PROJECT_WORD}"], leak.find_leaks(root),
            )

    def test_plain_directory_is_walked_with_notice(self):
        """A non-git directory is scanned and a notice is printed to stderr."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "plain.md", f"see {HOME_PATH}\n")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                leaks = leak.find_leaks(root)
        self.assertEqual(1, len(leaks))
        self.assertIn("not a git work tree", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
