"""Repo-level leak test: no project names, personal data or host paths in the pack.

Scans only the files that could be committed: tracked files plus untracked files that are
not ignored (``git ls-files -z -co --exclude-standard``, which honours global excludes).
Outside a git work tree the scan falls back to a plain directory walk and prints a notice.
Fails when any forbidden pattern (SPEC section 5) appears in a scanned file name or text.
The patterns are assembled from fragments at import time so that this file does not match itself.
"""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Fragments are joined at runtime; the literal forbidden words never appear here.
_FRAGMENTS = [
    ("/ho", "me/"),
    ("vi", "pc"),
    ("t", "cu"),
    ("sa", "525"),
    ("qual", "comm"),
    ("shoulder", "tap"),
    ("nam", "vs"),
    ("tls", "proxy"),
    ("tls_", "proxy"),
    ("tls-", "proxy"),
]
FORBIDDEN = [a + b for a, b in _FRAGMENTS]
SKIP_DIRS = {".git", "__pycache__", ".worktrees"}
_WORD_CHAR = "A-Za-z0-9"


def _build_pattern() -> re.Pattern:
    """Compile the forbidden-pattern alternation.

    Patterns that start with a word character (project names) must match on word boundaries,
    so that a short project name does not match inside random base64. Path-like patterns
    (starting with a slash) are plain substring matches.
    """
    alternatives = []
    for word in FORBIDDEN:
        escaped = re.escape(word)
        if word[0].isalnum():
            escaped = f"(?<![{_WORD_CHAR}]){escaped}(?![{_WORD_CHAR}])"
        alternatives.append(escaped)
    return re.compile("|".join(alternatives), re.IGNORECASE)


PATTERN = _build_pattern()


def _git_env() -> dict:
    """Return the process environment without GIT_* overrides (so the caller's repo is not used)."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Run ``git -C root args`` and return the completed process without raising."""
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        env=_git_env(),
        check=False,
    )


def _committable_files(root: Path):
    """Return the repo-relative paths of tracked and untracked-not-ignored files, or None.

    Returns None when ``root`` is not the top level of a git work tree, so the caller can
    fall back to a directory walk.
    """
    top = _git(root, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return None
    if Path(top.stdout.decode().strip()).resolve() != root.resolve():
        return None
    listing = _git(root, "ls-files", "-z", "-co", "--exclude-standard")
    if listing.returncode != 0:
        return None
    return [Path(p) for p in listing.stdout.decode().split("\0") if p]


def _walk_files(root: Path):
    """Return the repo-relative paths of every regular file and symlink under ``root``, skipping SKIP_DIRS."""
    found = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.is_symlink() or path.is_file():
            found.append(rel)
    return found


def iter_files(root: Path):
    """Yield the repo-relative paths of regular files and symlinks that could be committed under ``root``.

    Uses ``git ls-files -z -co --exclude-standard`` when ``root`` is a git work tree. Otherwise
    prints a notice to stderr and walks the directory, skipping SKIP_DIRS.
    """
    listed = _committable_files(root)
    if listed is None:
        print(f"leak scan: {root} is not a git work tree; falling back to a directory walk",
              file=sys.stderr)
        listed = _walk_files(root)
    for rel in sorted(listed):
        path = root / rel
        if path.is_symlink() or path.is_file():
            yield rel


def find_leaks(root: Path):
    """Return a list of ``relative_path:line: match`` strings for forbidden patterns.

    File names, regular file contents and symlink targets are checked. Undecodable bytes are ignored.
    """
    leaks = []
    for rel in iter_files(root):
        for m in PATTERN.finditer(str(rel)):
            leaks.append(f"{rel}: (file name) {m.group(0)}")
        path = root / rel
        if path.is_symlink():
            for m in PATTERN.finditer(os.readlink(path)):
                leaks.append(f"{rel}: (link target) {m.group(0)}")
            continue
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

    def test_telaf_is_allowed(self):
        self.assertIsNone(PATTERN.search("TelAF apps and telaf-selinux"))

    def test_each_remaining_project_term_is_forbidden(self):
        fragments = (
            ("vi", "pc"), ("t", "cu"), ("sa", "525"), ("qual", "comm"),
            ("shoulder", "tap"), ("nam", "vs"), ("tls", "proxy"),
            ("tls_", "proxy"), ("tls-", "proxy"),
        )
        for left, right in fragments:
            term = (left + right).upper()
            with self.subTest(term=term):
                self.assertIsNotNone(PATTERN.search(f" {term} "))

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
    if len(sys.argv) == 3 and sys.argv[1] == "--scan-tree":
        leaks = find_leaks(Path(sys.argv[2]).resolve())
        for leak in leaks:
            print(leak, file=sys.stderr)
        sys.exit(bool(leaks))
    unittest.main()
