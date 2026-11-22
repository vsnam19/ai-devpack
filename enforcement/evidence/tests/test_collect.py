"""Unit tests for enforcement.evidence.collect and git pre-commit hooks."""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from enforcement.evidence import collect  # noqa: E402


def _init_repo_with_two_commits(repo_dir: pathlib.Path) -> str:
    """Create a temp git repo with 2 commits and return HEAD SHA."""
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
    (repo_dir / "file.cpp").write_text("int x = 1;\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo_dir), "add", "file.cpp"],
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
    (repo_dir / "file.cpp").write_text("int x = 2;\nint y = 3;\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-am", "second"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    res = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return res.stdout.strip()


class TestEvidenceCollect(unittest.TestCase):
    """Test evidence bundle creation with and without git provenance notes."""

    def setUp(self) -> None:
        """Create temporary repo, receipt file, and review file."""
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self._tmp.name)
        self.repo_dir = self.tmp_path / "repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self.head_sha = _init_repo_with_two_commits(self.repo_dir)

        self.receipt_file = self.tmp_path / "gate_receipt.json"
        self.receipt_file.write_text(
            json.dumps(
                {
                    "gate_status": "GATE_OK",
                    "checks": [{"name": "unit", "status": "PASS", "exit_code": 0, "duration_s": 0.1}],
                }
            ),
            encoding="utf-8",
        )
        self.review_file = self.tmp_path / "review.md"
        self.review_file.write_text(
            "# Review Findings\nNo blocking issues found.\n\nVERDICT: APPROVE\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        """Clean up temporary workspace."""
        self._tmp.cleanup()

    def _run_collect(self, args: list[str]) -> tuple[int, str, str]:
        """Run collect.main and capture (exit_code, stdout, stderr)."""
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            code = collect.main(args)
        return code, out_buf.getvalue(), err_buf.getvalue()

    def test_collect_bundles_receipt_verdict_diffstat_and_provenance_note(self) -> None:
        """When ai-provenance note is attached to HEAD, it is bundled into evidence/<change-id>.json."""
        prov_text = '{"model":"sonnet","prompt_sha256":"abc123"}'
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "notes", "--ref", "ai-provenance", "add", "-m", prov_text, "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        code, out, _ = self._run_collect(
            [
                "--repo",
                str(self.repo_dir),
                "--change-id",
                "CHG-101",
                "--receipt",
                str(self.receipt_file),
                "--review",
                str(self.review_file),
                "--base",
                "HEAD~1",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("EVIDENCE_OK", out)

        bundle_path = self.repo_dir / "evidence" / "CHG-101.json"
        self.assertTrue(bundle_path.is_file())
        data = json.loads(bundle_path.read_text(encoding="utf-8"))
        self.assertEqual(data["change_id"], "CHG-101")
        self.assertEqual(data["git_head"], self.head_sha)
        self.assertEqual(data["review_verdict"], "APPROVE")
        self.assertEqual(data["gate_receipt"]["gate_status"], "GATE_OK")
        self.assertIn("file.cpp", data["diff_stat"])
        self.assertEqual(data["provenance_note"], prov_text)

    def test_collect_succeeds_without_provenance_note(self) -> None:
        """When no ai-provenance git note exists on HEAD, provenance_note is None and exit is 0."""
        code, _, _ = self._run_collect(
            [
                "--repo",
                str(self.repo_dir),
                "--change-id",
                "CHG-102",
                "--receipt",
                str(self.receipt_file),
                "--review",
                str(self.review_file),
                "--base",
                "HEAD~1",
            ]
        )
        self.assertEqual(code, 0)
        bundle_path = self.repo_dir / "evidence" / "CHG-102.json"
        data = json.loads(bundle_path.read_text(encoding="utf-8"))
        self.assertIsNone(data["provenance_note"])
        self.assertEqual(data["review_verdict"], "APPROVE")

    def test_collect_fails_when_review_missing_verdict_line(self) -> None:
        """If the review file does not end with a VERDICT: line, collect fails closed with exit 1."""
        self.review_file.write_text("# Incomplete review without verdict\n", encoding="utf-8")
        code, _, err = self._run_collect(
            [
                "--repo",
                str(self.repo_dir),
                "--change-id",
                "CHG-103",
                "--receipt",
                str(self.receipt_file),
                "--review",
                str(self.review_file),
                "--base",
                "HEAD~1",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("EVIDENCE_ERROR", err)

    def test_pre_commit_hook_install_and_execution(self) -> None:
        """Installing pre-commit hook via install.sh blocks staged secrets and allows clean commits."""
        install_script = pathlib.Path(_REPO_ROOT) / "enforcement" / "hooks" / "install.sh"
        res = subprocess.run(
            ["bash", str(install_script), "--repo", str(self.repo_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr)

        hook_path = self.repo_dir / ".git" / "hooks" / "pre-commit"
        self.assertTrue(hook_path.exists())

        clean_proc = subprocess.run(
            ["bash", str(hook_path)],
            cwd=str(self.repo_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(clean_proc.returncode, 0)

        pwd_key = "pass" + "word"
        secret_file = self.repo_dir / "leak.conf"
        secret_file.write_text(f"{pwd_key}=MyHiddenPass123\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "add", "leak.conf"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        block_proc = subprocess.run(
            ["bash", str(hook_path)],
            cwd=str(self.repo_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(block_proc.returncode, 1)
        self.assertIn("leak.conf:1:", block_proc.stdout)
        self.assertNotIn("MyHiddenPass123", block_proc.stdout)


if __name__ == "__main__":
    unittest.main()
