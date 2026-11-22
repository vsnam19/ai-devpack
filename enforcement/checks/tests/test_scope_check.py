"""Unit tests for enforcement.checks.scope_check."""

from __future__ import annotations

import contextlib
import io
import pathlib
import subprocess
import sys
import tempfile
import unittest

_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from enforcement.checks import scope_check  # noqa: E402


def _setup_repo(repo_dir: pathlib.Path) -> None:
    """Create a temporary git repo with an initial commit."""
    subprocess.run(["git", "init", str(repo_dir)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "test-user"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "test@local.invalid"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    (repo_dir / "src").mkdir(parents=True, exist_ok=True)
    (repo_dir / "docs").mkdir(parents=True, exist_ok=True)
    (repo_dir / "src" / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (repo_dir / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo_dir), "add", "."],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", "initial"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class TestScopeCheck(unittest.TestCase):
    """Verify allowed-glob scope checking against a temporary git repository."""

    def setUp(self) -> None:
        """Create an isolated temporary git repository."""
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_dir = pathlib.Path(self._tmp.name) / "repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        _setup_repo(self.repo_dir)

    def tearDown(self) -> None:
        """Clean up temporary directory."""
        self._tmp.cleanup()

    def _run_scope(self, args: list[str]) -> tuple[int, str, str]:
        """Run scope_check.main and capture (exit_code, stdout, stderr)."""
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            code = scope_check.main(args)
        return code, out_buf.getvalue(), err_buf.getvalue()

    def test_happy_path_all_changes_within_allowed_globs(self) -> None:
        """Changes matching allowed globs succeed with exit code 0."""
        (self.repo_dir / "src" / "main.cpp").write_text("int main() { return 1; }\n", encoding="utf-8")
        (self.repo_dir / "src" / "sub").mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "src" / "sub" / "helper.hpp").write_text("#pragma once\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "add", "."],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "commit", "-m", "update src"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        code, out, _ = self._run_scope(
            [
                "--repo",
                str(self.repo_dir),
                "--base",
                "HEAD~1",
                "--allow",
                "src/**",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("SCOPE_OK", out)

    def test_change_outside_allowed_globs_fails(self) -> None:
        """Touching a file outside --allow globs returns exit code 1 and prints SCOPE_VIOLATION."""
        (self.repo_dir / "docs" / "guide.md").write_text("# Updated\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "add", "."],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "commit", "-m", "update docs"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        code, out, _ = self._run_scope(
            [
                "--repo",
                str(self.repo_dir),
                "--base",
                "HEAD~1",
                "--allow",
                "src/**",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("SCOPE_VIOLATION: docs/guide.md", out)

    def test_invalid_base_ref_fails_closed(self) -> None:
        """An unknown git base reference fails closed with exit code 1."""
        code, _, err = self._run_scope(
            [
                "--repo",
                str(self.repo_dir),
                "--base",
                "nonexistent-branch-ref-999",
                "--allow",
                "src/**",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("SCOPE_ERROR", err)


if __name__ == "__main__":
    unittest.main()
