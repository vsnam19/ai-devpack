"""Unit tests for enforcement.gate.gate covering all fail-closed paths and the happy path."""

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

from enforcement.gate import gate  # noqa: E402


def _init_git_repo(repo_dir: pathlib.Path) -> str:
    """Initialize a temporary git repository with one commit and return HEAD SHA."""
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
    (repo_dir / "README.md").write_text("# temp repo\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo_dir), "add", "README.md"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", "initial commit"],
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


class TestGateRunner(unittest.TestCase):
    """Test suite verifying fail-closed behavior and receipt generation of gate.py."""

    def setUp(self) -> None:
        """Create an isolated temporary directory for each test."""
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self._tmp.name)
        self.repo_dir = self.tmp_path / "repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self.receipt_path = self.tmp_path / "receipt.json"
        self.config_path = self.tmp_path / "gate.toml"

    def tearDown(self) -> None:
        """Clean up the temporary directory."""
        self._tmp.cleanup()

    def _run_gate(self, args: list[str]) -> tuple[int, str]:
        """Invoke gate.main with captured stdout and return (exit_code, stdout_text)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = gate.main(args)
        return code, buf.getvalue()

    def test_happy_path_runs_ordered_checks_and_writes_receipt(self) -> None:
        """All checks pass in order inside repo root, printing GATE_OK and writing full JSON receipt."""
        head_sha = _init_git_repo(self.repo_dir)
        marker_file = self.repo_dir / "step1.txt"
        self.config_path.write_text(
            """
[[check]]
name = "create-marker"
cmd = ["python3", "-c", "import pathlib; pathlib.Path('step1.txt').write_text('ok', encoding='utf-8')"]
timeout_s = 10
required = true

[[check]]
name = "verify-marker"
cmd = ["python3", "-c", "import pathlib, sys; sys.exit(0 if pathlib.Path('step1.txt').read_text() == 'ok' else 1)"]
timeout_s = 10
required = true
""",
            encoding="utf-8",
        )

        code, out = self._run_gate(
            [
                "--config",
                str(self.config_path),
                "--repo",
                str(self.repo_dir),
                "--receipt",
                str(self.receipt_path),
            ]
        )

        self.assertEqual(code, 0)
        self.assertTrue(marker_file.is_file())
        lines = [line.strip() for line in out.strip().splitlines()]
        self.assertEqual(
            lines,
            [
                "PASS create-marker",
                "PASS verify-marker",
                "GATE_OK",
            ],
        )

        self.assertTrue(self.receipt_path.is_file())
        receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["gate_status"], "GATE_OK")
        self.assertIsNone(receipt["first_failed_check"])
        self.assertEqual(receipt["git_head"], head_sha)
        self.assertTrue(receipt["timestamp_utc"].endswith("Z"))
        self.assertEqual(len(receipt["checks"]), 2)
        self.assertEqual(receipt["checks"][0]["name"], "create-marker")
        self.assertEqual(receipt["checks"][0]["status"], "PASS")
        self.assertEqual(receipt["checks"][0]["exit_code"], 0)
        self.assertIsInstance(receipt["checks"][0]["duration_s"], float)
        self.assertEqual(receipt["checks"][1]["name"], "verify-marker")
        self.assertEqual(receipt["checks"][1]["status"], "PASS")
        self.assertEqual(receipt["checks"][1]["exit_code"], 0)

    def test_command_not_found_fails_gate(self) -> None:
        """A non-existent command executable fails the check and outputs GATE_FAIL <check>."""
        self.config_path.write_text(
            """
[[check]]
name = "missing-binary"
cmd = ["nonexistent_command_xyz_98765", "--version"]
timeout_s = 5
required = true
""",
            encoding="utf-8",
        )

        code, out = self._run_gate(
            [
                "--config",
                str(self.config_path),
                "--repo",
                str(self.repo_dir),
                "--receipt",
                str(self.receipt_path),
            ]
        )

        self.assertEqual(code, 1)
        lines = [line.strip() for line in out.strip().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("FAIL missing-binary:"))
        self.assertIn("command not found", lines[0])
        self.assertEqual(lines[1], "GATE_FAIL missing-binary")

        receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["gate_status"], "GATE_FAIL")
        self.assertEqual(receipt["first_failed_check"], "missing-binary")
        self.assertEqual(receipt["checks"][0]["exit_code"], 127)

    def test_nonzero_exit_fails_gate_and_reports_first_failing_check(self) -> None:
        """A check returning non-zero fails the gate and reports the first failing check name."""
        self.config_path.write_text(
            """
[[check]]
name = "passing-first"
cmd = ["python3", "-c", "import sys; sys.exit(0)"]
timeout_s = 5
required = true

[[check]]
name = "failing-second"
cmd = ["python3", "-c", "import sys; sys.exit(3)"]
timeout_s = 5
required = true

[[check]]
name = "failing-third"
cmd = ["python3", "-c", "import sys; sys.exit(2)"]
timeout_s = 5
required = true
""",
            encoding="utf-8",
        )

        code, out = self._run_gate(
            [
                "--config",
                str(self.config_path),
                "--repo",
                str(self.repo_dir),
                "--receipt",
                str(self.receipt_path),
            ]
        )

        self.assertEqual(code, 1)
        lines = [line.strip() for line in out.strip().splitlines()]
        self.assertEqual(lines[0], "PASS passing-first")
        self.assertTrue(lines[1].startswith("FAIL failing-second:"))
        self.assertTrue(lines[2].startswith("FAIL failing-third:"))
        self.assertEqual(lines[3], "GATE_FAIL failing-second")

        receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["first_failed_check"], "failing-second")
        self.assertEqual(receipt["checks"][1]["exit_code"], 3)
        self.assertEqual(receipt["checks"][2]["exit_code"], 2)

    def test_timeout_fails_gate(self) -> None:
        """A check exceeding timeout_s is terminated and fails the gate."""
        self.config_path.write_text(
            """
[[check]]
name = "slow-check"
cmd = ["python3", "-c", "import time; time.sleep(5)"]
timeout_s = 0.2
required = true
""",
            encoding="utf-8",
        )

        code, out = self._run_gate(
            [
                "--config",
                str(self.config_path),
                "--repo",
                str(self.repo_dir),
                "--receipt",
                str(self.receipt_path),
            ]
        )

        self.assertEqual(code, 1)
        lines = [line.strip() for line in out.strip().splitlines()]
        self.assertTrue(lines[0].startswith("FAIL slow-check:"))
        self.assertIn("timeout", lines[0])
        self.assertEqual(lines[1], "GATE_FAIL slow-check")

        receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["checks"][0]["exit_code"], 124)

    def test_missing_config_fails_gate(self) -> None:
        """A missing configuration file fails closed with GATE_FAIL config."""
        missing_path = self.tmp_path / "does_not_exist.toml"
        code, out = self._run_gate(
            [
                "--config",
                str(missing_path),
                "--repo",
                str(self.repo_dir),
                "--receipt",
                str(self.receipt_path),
            ]
        )

        self.assertEqual(code, 1)
        lines = [line.strip() for line in out.strip().splitlines()]
        self.assertTrue(lines[0].startswith("FAIL config:"))
        self.assertEqual(lines[1], "GATE_FAIL config")

    def test_invalid_toml_config_fails_gate(self) -> None:
        """Malformed TOML in the configuration file fails closed."""
        self.config_path.write_text("[[check]\nname = 'unclosed\n", encoding="utf-8")
        code, out = self._run_gate(
            [
                "--config",
                str(self.config_path),
                "--repo",
                str(self.repo_dir),
                "--receipt",
                str(self.receipt_path),
            ]
        )

        self.assertEqual(code, 1)
        lines = [line.strip() for line in out.strip().splitlines()]
        self.assertTrue(lines[0].startswith("FAIL config:"))
        self.assertEqual(lines[1], "GATE_FAIL config")

    def test_zero_checks_configured_fails_gate(self) -> None:
        """A valid TOML file with zero [[check]] entries fails closed."""
        self.config_path.write_text("# empty config with no checks\n", encoding="utf-8")
        code, out = self._run_gate(
            [
                "--config",
                str(self.config_path),
                "--repo",
                str(self.repo_dir),
                "--receipt",
                str(self.receipt_path),
            ]
        )

        self.assertEqual(code, 1)
        lines = [line.strip() for line in out.strip().splitlines()]
        self.assertIn("zero checks configured", lines[0])
        self.assertEqual(lines[1], "GATE_FAIL config")

    def test_skip_without_deviation_fails_gate(self) -> None:
        """Passing --skip <name> without --deviation <ID> is rejected and fails closed."""
        self.config_path.write_text(
            """
[[check]]
name = "unit-test"
cmd = ["python3", "-c", "import sys; sys.exit(0)"]
timeout_s = 5
required = true
""",
            encoding="utf-8",
        )

        code, out = self._run_gate(
            [
                "--config",
                str(self.config_path),
                "--repo",
                str(self.repo_dir),
                "--receipt",
                str(self.receipt_path),
                "--skip",
                "unit-test",
            ]
        )

        self.assertEqual(code, 1)
        lines = [line.strip() for line in out.strip().splitlines()]
        self.assertTrue(lines[0].startswith("FAIL unit-test:"))
        self.assertEqual(lines[1], "GATE_FAIL unit-test")

    def test_skip_with_deviation_records_skipped_in_receipt(self) -> None:
        """Passing --skip <name> with --deviation <ID> skips execution and records SKIPPED deviation=<ID>."""
        self.config_path.write_text(
            """
[[check]]
name = "broken-check"
cmd = ["python3", "-c", "import sys; sys.exit(1)"]
timeout_s = 5
required = true

[[check]]
name = "good-check"
cmd = ["python3", "-c", "import sys; sys.exit(0)"]
timeout_s = 5
required = true
""",
            encoding="utf-8",
        )

        code, out = self._run_gate(
            [
                "--config",
                str(self.config_path),
                "--repo",
                str(self.repo_dir),
                "--receipt",
                str(self.receipt_path),
                "--skip",
                "broken-check",
                "--deviation",
                "DEV-2026-01",
            ]
        )

        self.assertEqual(code, 0)
        lines = [line.strip() for line in out.strip().splitlines()]
        self.assertEqual(lines[-1], "GATE_OK")

        receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["gate_status"], "GATE_OK")
        self.assertEqual(receipt["checks"][0]["name"], "broken-check")
        self.assertEqual(receipt["checks"][0]["status"], "SKIPPED deviation=DEV-2026-01")
        self.assertEqual(receipt["checks"][1]["name"], "good-check")
        self.assertEqual(receipt["checks"][1]["status"], "PASS")


    def test_non_required_failure_warns_and_gate_ok(self) -> None:
        """A failing required=false check prints WARN, is recorded as WARN, and the gate stays OK."""
        self.config_path.write_text(
            """
[[check]]
name = "good"
cmd = ["python3", "-c", "pass"]
required = true

[[check]]
name = "optional-bad"
cmd = ["python3", "-c", "import sys; sys.exit(3)"]
required = false
""",
            encoding="utf-8",
        )
        code, out = self._run_gate(
            ["--config", str(self.config_path), "--repo", str(self.repo_dir), "--receipt", str(self.receipt_path)]
        )
        lines = out.strip().splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1], "GATE_OK")
        self.assertTrue(any(line.startswith("WARN optional-bad: ") for line in lines))
        receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["gate_status"], "GATE_OK")
        statuses = {c["name"]: c["status"] for c in receipt["checks"]}
        self.assertEqual(statuses, {"good": "PASS", "optional-bad": "WARN"})

    def test_required_failure_still_fails_alongside_warn(self) -> None:
        """A failing required check fails the gate even when a non-required check also fails."""
        self.config_path.write_text(
            """
[[check]]
name = "optional-bad"
cmd = ["python3", "-c", "import sys; sys.exit(1)"]
required = false

[[check]]
name = "req-bad"
cmd = ["python3", "-c", "import sys; sys.exit(1)"]
required = true
""",
            encoding="utf-8",
        )
        code, out = self._run_gate(
            ["--config", str(self.config_path), "--repo", str(self.repo_dir), "--receipt", str(self.receipt_path)]
        )
        self.assertEqual(code, 1)
        self.assertEqual(out.strip().splitlines()[-1], "GATE_FAIL req-bad")

    def test_zero_required_checks_fails_closed(self) -> None:
        """A config whose checks are all required=false fails closed."""
        self.config_path.write_text(
            """
[[check]]
name = "only-optional"
cmd = ["python3", "-c", "pass"]
required = false
""",
            encoding="utf-8",
        )
        code, out = self._run_gate(
            ["--config", str(self.config_path), "--repo", str(self.repo_dir), "--receipt", str(self.receipt_path)]
        )
        self.assertEqual(code, 1)
        self.assertEqual(out.strip().splitlines()[-1], "GATE_FAIL config")


if __name__ == "__main__":
    unittest.main()
