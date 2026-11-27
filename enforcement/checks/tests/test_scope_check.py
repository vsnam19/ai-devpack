"""Unit tests for enforcement.checks.scope_check."""

from __future__ import annotations

import contextlib
import hashlib
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

    def test_pack_artifact_exclusion_requires_exact_untracked_path_and_content(self) -> None:
        artifact = self.repo_dir / "runs" / "one" / "steps" / "02-gate-receipt.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"pack receipt")
        args = [
            "--repo", str(self.repo_dir), "--base", "HEAD", "--allow", "src/**",
            "--pack-artifact", artifact.relative_to(self.repo_dir).as_posix(),
            hashlib.sha256(b"pack receipt").hexdigest(),
        ]
        code, out, _ = self._run_scope(args)
        self.assertEqual(code, 0, out)

        same_name = self.repo_dir / "docs" / artifact.name
        same_name.write_bytes(b"pack receipt")
        (self.repo_dir / "gate_receipt.json").write_bytes(b"pack receipt")
        code, out, _ = self._run_scope(args)
        self.assertEqual(code, 1)
        self.assertIn(f"SCOPE_VIOLATION: docs/{artifact.name}", out)
        self.assertIn("SCOPE_VIOLATION: gate_receipt.json", out)
        self.assertNotIn("SCOPE_VIOLATION: runs/", out)

        artifact.write_bytes(b"agent edit")
        code, out, _ = self._run_scope(args)
        self.assertEqual(code, 1)
        self.assertIn("SCOPE_VIOLATION: runs/", out)

        artifact.write_bytes(b"pack receipt")
        subprocess.run(["git", "add", str(artifact)], cwd=self.repo_dir, check=True)
        code, out, _ = self._run_scope(args)
        self.assertEqual(code, 1)
        self.assertIn("SCOPE_VIOLATION: runs/", out)

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


    def test_untracked_out_of_scope_file_fails(self) -> None:
        """An untracked (not ignored) file outside --allow globs is reported as a violation."""
        (self.repo_dir / "stray.txt").write_text("x\n", encoding="utf-8")
        code, out, _ = self._run_scope(
            ["--repo", str(self.repo_dir), "--base", "HEAD", "--allow", "src/**"]
        )
        self.assertEqual(code, 1)
        self.assertIn("SCOPE_VIOLATION: stray.txt", out)

    def test_untracked_in_scope_file_passes(self) -> None:
        """An untracked file inside the allowed globs passes."""
        (self.repo_dir / "src" / "new_file.txt").write_text("x\n", encoding="utf-8")
        code, out, _ = self._run_scope(
            ["--repo", str(self.repo_dir), "--base", "HEAD", "--allow", "src/**"]
        )
        self.assertEqual(code, 0)
        self.assertIn("SCOPE_OK", out)

    def test_gate_and_lint_config_edits_fail_as_gate_touched_regardless_of_allow(self) -> None:
        """Edits to preflight.toml, .devpack/**, .clang-tidy, or gate config fail as gate-touched even with --allow **."""
        for rel in ("preflight.toml", ".devpack/project.toml", ".clang-tidy", "gate.toml"):
            with self.subTest(rel=rel):
                target = self.repo_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("# modified\n", encoding="utf-8")
                code, out, _ = self._run_scope(
                    ["--repo", str(self.repo_dir), "--base", "HEAD", "--allow", "**"]
                )
                self.assertEqual(1, code)
                self.assertIn("gate-touched", out)
                self.assertIn(rel, out)
                target.unlink()

    def test_refuses_tampered_git_config_fsmonitor_without_executing_command(self) -> None:
        """scope_check refuses to run git when .git/config has core.fsmonitor and does not create marker."""
        with tempfile.TemporaryDirectory() as tmp_marker_dir:
            marker = pathlib.Path(tmp_marker_dir) / "scope_pwned.txt"
            cfg = self.repo_dir / ".git" / "config"
            cfg.write_text(
                cfg.read_text(encoding="utf-8") + f"\n[core]\n\tfsmonitor = touch {marker}\n",
                encoding="utf-8",
            )
            code, _, err = self._run_scope(
                ["--repo", str(self.repo_dir), "--base", "HEAD", "--allow", "src/**"]
            )
            self.assertEqual(1, code)
            self.assertIn("SCOPE_ERROR", err)
            self.assertIn("git_metadata_tampered", err)
            self.assertFalse(marker.exists())


    def test_workspace_selection_exclusions_and_gate_report_all_violations(self) -> None:
        """All changed-path restrictions are reported together, including F1 protections."""
        config = self.repo_dir.parent / "project.toml"
        config.write_text('[workspace]\nroot="src"\nexclude=["src/active/generated/**"]\n[[component]]\nname="active"\npath="active"\n[[component]]\nname="inactive"\npath="inactive"\n[scan]\nselected=["active"]\n', encoding="utf-8")
        paths = ["src/active/generated/auto.cpp", "src/inactive/other.cpp", "docs/outside.md", ".devpack/project.toml"]
        for name in paths:
            path = self.repo_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("changed\n", encoding="utf-8")
        code, out, _ = self._run_scope(["--repo", str(self.repo_dir), "--base", "HEAD", "--allow", "**", "--project-config", str(config)])
        self.assertEqual(code, 1)
        for path in paths:
            self.assertIn(path, out)
        self.assertIn("excluded", out)
        self.assertIn("active components", out)
        self.assertIn("gate-touched", out)


if __name__ == "__main__":
    unittest.main()
