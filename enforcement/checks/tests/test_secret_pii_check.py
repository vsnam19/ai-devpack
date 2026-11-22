"""Unit tests for enforcement.checks.secret_pii_check."""

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

from enforcement.checks import secret_pii_check  # noqa: E402


def _init_git_repo(repo_dir: pathlib.Path) -> None:
    """Initialize a temporary git repository."""
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


class TestSecretPiiCheck(unittest.TestCase):
    """Verify secret and external-pattern detection and non-echoing output."""

    def setUp(self) -> None:
        """Create temporary repo and external directory for pattern files."""
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self._tmp.name)
        self.repo_dir = self.tmp_path / "repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self.external_dir = self.tmp_path / "external"
        self.external_dir.mkdir(parents=True, exist_ok=True)
        _init_git_repo(self.repo_dir)

    def tearDown(self) -> None:
        """Clean up temporary workspace."""
        self._tmp.cleanup()

    def _run_scanner(self, args: list[str]) -> tuple[int, str, str]:
        """Run secret_pii_check.main and capture (exit_code, stdout, stderr)."""
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            code = secret_pii_check.main(args)
        return code, out_buf.getvalue(), err_buf.getvalue()

    def test_happy_path_clean_files_and_ai_attribution_allowed(self) -> None:
        """Clean source files (including AI attribution notes) pass with exit code 0."""
        sample = self.repo_dir / "module.cpp"
        sample.write_text(
            "// Generated with assistance from an AI model\n"
            "int add(int a, int b) { return a + b; }\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "add", "module.cpp"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        code, out, _ = self._run_scanner(["--repo", str(self.repo_dir), "--staged"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "")

    def test_detects_private_key_header_without_echoing_secret(self) -> None:
        """Private key headers are blocked and the key material is never echoed."""
        hdr = "-----BEGIN " + "RSA PRIVATE KEY-----"
        secret_body = "MIIEpAIBAAKCAQEA7V123456789SECRETKEYMATERIAL"
        key_file = self.repo_dir / "id_rsa.pem"
        key_file.write_text(f"{hdr}\n{secret_body}\n", encoding="utf-8")

        code, out, _ = self._run_scanner(
            ["--repo", str(self.repo_dir), str(key_file)]
        )
        self.assertEqual(code, 1)
        self.assertIn("id_rsa.pem:1:", out)
        self.assertNotIn(hdr, out)
        self.assertNotIn(secret_body, out)

    def test_detects_aws_access_key_without_echoing_secret(self) -> None:
        """AWS-style access keys are detected on --staged files without echoing the key."""
        fake_aws_key = "AKIA" + "IOSFODNN7EXAMPLE"
        cfg_file = self.repo_dir / "config.ini"
        cfg_file.write_text(f"[default]\nkey = {fake_aws_key}\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "add", "config.ini"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        code, out, _ = self._run_scanner(["--repo", str(self.repo_dir), "--staged"])
        self.assertEqual(code, 1)
        self.assertIn("config.ini:2:", out)
        self.assertNotIn(fake_aws_key, out)

    def test_detects_generic_password_assignment_without_echoing(self) -> None:
        """Generic password assignments are detected without echoing the secret value."""
        secret_val = "SuperSecretValue987654"
        pwd_key = "pass" + "word"
        env_file = self.repo_dir / "service.conf"
        env_file.write_text(f"# config\n{pwd_key}={secret_val}\n", encoding="utf-8")

        code, out, _ = self._run_scanner(
            ["--repo", str(self.repo_dir), str(env_file)]
        )
        self.assertEqual(code, 1)
        self.assertIn("service.conf:2:", out)
        self.assertNotIn(secret_val, out)

    def test_external_patterns_file_outside_repo_blocks_custom_pii(self) -> None:
        """Custom regexes in an external --patterns file block matching PII without echoing it."""
        patterns_file = self.external_dir / "pii_patterns.txt"
        forbidden_token = "CONFIDENTIAL_CODENAME_X99"
        patterns_file.write_text(f"# Custom forbidden patterns\n{forbidden_token}\n", encoding="utf-8")

        doc_file = self.repo_dir / "notes.md"
        doc_file.write_text(f"Line 1\nProject uses {forbidden_token} internally.\n", encoding="utf-8")

        code, out, _ = self._run_scanner(
            [
                "--repo",
                str(self.repo_dir),
                "--patterns",
                str(patterns_file),
                str(doc_file),
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("notes.md:2:", out)
        self.assertNotIn(forbidden_token, out)


    def _git(self, *args: str) -> None:
        """Run a git command inside the temporary repository."""
        subprocess.run(
            ["git", "-C", str(self.repo_dir), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_staged_secret_detected_after_working_tree_removal(self) -> None:
        """A secret staged then deleted from the working tree is still detected with --staged."""
        target = self.repo_dir / "conf.txt"
        target.write_text("line\npassword = \"hunter2hunter2\"\n", encoding="utf-8")
        self._git("add", "conf.txt")
        target.unlink()
        code, out, _ = self._run_scanner(["--repo", str(self.repo_dir), "--staged"])
        self.assertEqual(code, 1)
        self.assertIn("conf.txt:2:", out)
        self.assertNotIn("hunter2hunter2", out)

    def test_staged_ignores_working_tree_only_secret(self) -> None:
        """A secret added only to the working tree is not reported in --staged mode."""
        target = self.repo_dir / "conf.txt"
        target.write_text("clean = 1\n", encoding="utf-8")
        self._git("add", "conf.txt")
        target.write_text("clean = 1\npassword = \"hunter2hunter2\"\n", encoding="utf-8")
        code, out, _ = self._run_scanner(["--repo", str(self.repo_dir), "--staged"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
