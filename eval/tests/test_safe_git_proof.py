"""Regression tests for run_verified_test_binary in eval/lib/safe_git.sh.

A test binary must succeed only when its exit status is 0 AND its final line is a
DEVPACK_TESTS_COMPLETED proof with failed=0. A binary that prints a passing proof
and then exits non-zero must be rejected.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

EVAL_DIR = Path(__file__).resolve().parent.parent
SAFE_GIT = EVAL_DIR / "lib" / "safe_git.sh"


def _write_fake_binary(directory: Path, body: str) -> Path:
    """Write an executable bash script with ``body`` into ``directory``."""
    path = directory / "fake_test_binary"
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _run_verified(binary: Path, expected_min: int = 1) -> subprocess.CompletedProcess[str]:
    """Source safe_git.sh in a fresh bash and run ``binary`` through run_verified_test_binary."""
    script = (
        'source "$1"\n'
        'run_verified_test_binary "fake binary" "$2" "$3"\n'
    )
    env = dict(os.environ)
    env.pop("DEVPACK_BASELINE_SHA", None)
    return subprocess.run(
        ["bash", "-c", script, "_", str(SAFE_GIT), str(expected_min), str(binary)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )


class RunVerifiedTestBinaryTests(unittest.TestCase):
    """Exit status and completion proof must both be required."""

    def test_passing_proof_with_exit_zero_is_accepted(self) -> None:
        """A binary that exits 0 and prints checks=3 failed=0 as its last line passes."""
        with tempfile.TemporaryDirectory() as tmp:
            binary = _write_fake_binary(
                Path(tmp),
                'echo "DEVPACK_TESTS_COMPLETED checks=3 failed=0"\nexit 0\n',
            )
            result = _run_verified(binary, expected_min=3)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_proof_printed_then_nonzero_return_is_rejected(self) -> None:
        """A binary that prints a passing proof and then returns 1 must fail the grader."""
        with tempfile.TemporaryDirectory() as tmp:
            binary = _write_fake_binary(
                Path(tmp),
                'main() {\n'
                '  echo "DEVPACK_TESTS_COMPLETED checks=3 failed=0"\n'
                '  return 1\n'
                '}\n'
                'main "$@"\n',
            )
            result = _run_verified(binary, expected_min=3)
        self.assertEqual(1, result.returncode)
        self.assertIn("exited with status 1", result.stderr)

    def test_proof_printed_then_crash_status_is_rejected(self) -> None:
        """A binary that prints a passing proof and then exits with a crash-like status fails."""
        with tempfile.TemporaryDirectory() as tmp:
            binary = _write_fake_binary(
                Path(tmp),
                'echo "DEVPACK_TESTS_COMPLETED checks=3 failed=0"\nexit 139\n',
            )
            result = _run_verified(binary, expected_min=3)
        self.assertEqual(139, result.returncode)
        self.assertIn("exited with status 139", result.stderr)

    def test_exit_zero_with_failed_nonzero_proof_is_rejected(self) -> None:
        """A proof reporting failed=1 is rejected even when the binary exits 0."""
        with tempfile.TemporaryDirectory() as tmp:
            binary = _write_fake_binary(
                Path(tmp),
                'echo "DEVPACK_TESTS_COMPLETED checks=3 failed=1"\nexit 0\n',
            )
            result = _run_verified(binary, expected_min=3)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("failed=1", result.stderr)

    def test_missing_proof_line_is_rejected(self) -> None:
        """A binary that exits 0 without printing the completion proof is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            binary = _write_fake_binary(Path(tmp), 'echo "all good"\nexit 0\n')
            result = _run_verified(binary, expected_min=1)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("did not emit final DEVPACK_TESTS_COMPLETED", result.stderr)


if __name__ == "__main__":
    unittest.main()
