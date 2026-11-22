"""Repo-level leak test: no project names, personal data or host paths in the pack.

Walks the repository (excluding ``.git``) and fails when any forbidden pattern
(SPEC section 5) appears in a text file. The patterns are assembled from fragments at
import time so that this file does not match itself.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Fragments are joined at runtime; the literal forbidden words never appear here.
_FRAGMENTS = [
    ("/ho", "me/"),
    ("tel", "af"),
    ("vi", "pc"),
    ("t", "cu"),
    ("sa", "525"),
    ("qual", "comm"),
    ("shoulder", "tap"),
    ("nam", "vs"),
]
FORBIDDEN = [a + b for a, b in _FRAGMENTS]
PATTERN = re.compile("|".join(re.escape(p) for p in FORBIDDEN), re.IGNORECASE)
SKIP_DIRS = {".git", "__pycache__", ".worktrees"}


def iter_files(root: Path):
    """Yield every regular file under ``root`` except those inside skipped directories."""
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            yield path


def find_leaks(root: Path):
    """Return a list of ``relative_path:line: match`` strings for forbidden patterns.

    Both file names and file contents are checked. Undecodable bytes are ignored.
    """
    leaks = []
    for path in iter_files(root):
        rel = path.relative_to(root)
        for m in PATTERN.finditer(str(rel)):
            leaks.append(f"{rel}: (file name) {m.group(0)}")
        text = path.read_bytes().decode("utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            m = PATTERN.search(line)
            if m:
                leaks.append(f"{rel}:{lineno}: {m.group(0)}")
    return leaks


class NoProjectLeaksTest(unittest.TestCase):
    """Checks that the repository contains no forbidden project-specific strings."""

    def test_repository_has_no_forbidden_patterns(self):
        """The real repository tree must be free of forbidden patterns."""
        leaks = find_leaks(REPO_ROOT)
        self.assertEqual([], leaks, "forbidden patterns found:\n" + "\n".join(leaks))

    def test_detector_flags_forbidden_text(self):
        """The detector itself must report a planted forbidden word (guards against a dead test)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.md").write_text("ok line\nsee " + FORBIDDEN[1].upper() + " here\n")
            (root / "b.md").write_text("clean\n")
            leaks = find_leaks(root)
        self.assertEqual(1, len(leaks))
        self.assertTrue(leaks[0].startswith("a.md:2:"))

    def test_detector_ignores_git_directory(self):
        """Files below ``.git`` are not scanned."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".git" / "x").write_text(FORBIDDEN[0])
            self.assertEqual([], find_leaks(root))

    def test_this_file_does_not_match_itself(self):
        """The test file must not contain any forbidden pattern literally."""
        self.assertIsNone(PATTERN.search(Path(__file__).read_text()))


if __name__ == "__main__":
    unittest.main()
