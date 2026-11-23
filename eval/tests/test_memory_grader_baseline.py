"""Regression tests: memory graders must lint against the external baseline, never against HEAD.

Each test builds a throwaway git repository in a temporary directory from the eval fixture,
applies the sequence's reference solution, and commits it as the worker. An unapproved inline
suppression committed on top of that must make the grader fail, and a missing baseline must
make every grader exit with GRADER_MISCONFIGURED (exit 2). No network, no real agents.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = EVAL_DIR.parent
MEMORY_TASKS = EVAL_DIR / "memory_tasks"
FIXTURE = EVAL_DIR / "fixture"

# Sequence ID -> source file that receives the injected suppression comment.
SEQUENCE_SOURCES = {
    "01-recall-decision": "src/dtc_debouncer.cpp",
    "02-superseded-decision": "src/link_state_machine.cpp",
    "03-distractors-overrecall": "src/ring_buffer.cpp",
    "04-stale-vs-code": "src/can_codec.cpp",
    "05-scope-isolation": "src/link_state_machine.cpp",
}

# Unapproved suppression forms: cppcheck-suppress (caught by cppcheck), NOLINT and pragma
# (caught only by preflight's base-relative SUPPRESSION-WITHOUT-DEVIATION rule).
SUPPRESSION_LINES = (
    "// cppcheck-suppress nullPointer\n",
    "// NOLINT(readability-identifier-naming)\n",
    '#pragma GCC diagnostic ignored "-Wunused-function"\n',
)


def _git(cwd: Path, *args: str) -> str:
    """Run git with a fixed test identity and return its stripped stdout."""
    result = subprocess.run(
        ["git", "-c", "user.email=grader@example.invalid", "-c", "user.name=Grader Test", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _build_repo(root: Path, seq_id: str, *, apply_solution: bool) -> tuple[Path, str]:
    """Copy the fixture, commit it as the baseline, and optionally commit the solution as the worker.

    Returns the work directory and the baseline commit SHA.
    """
    work = root / "work"
    shutil.copytree(FIXTURE, work)
    _git(work, "init", "-q")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "chore: base fixture")
    base_sha = _git(work, "rev-parse", "HEAD")
    if apply_solution:
        subprocess.run(
            ["git", "apply", str(MEMORY_TASKS / seq_id / "solution.patch")],
            cwd=work,
            check=True,
            capture_output=True,
        )
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "feat: worker solution")
    return work, base_sha


def _run_grader(seq_id: str, work: Path, *, baseline_sha: str | None) -> subprocess.CompletedProcess[str]:
    """Run the sequence's check.sh against WORK with an optional external baseline SHA."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("DEVPACK_BASELINE_SHA", "TRIAL_DIR")
    }
    env["DEVPACK_ROOT"] = str(REPO_ROOT)
    if baseline_sha is not None:
        env["DEVPACK_BASELINE_SHA"] = baseline_sha
    return subprocess.run(
        ["bash", str(MEMORY_TASKS / seq_id / "check.sh"), "--workdir", str(work)],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


class MemoryGraderBaselineTests(unittest.TestCase):
    """Graders must diff lint against the external baseline so worker commits cannot hide suppressions."""

    def test_unapproved_suppression_committed_by_worker_fails_grader(self) -> None:
        """A suppression committed on top of the solution fails lint before any test runs."""
        for seq_id, source in SEQUENCE_SOURCES.items():
            for suppression in SUPPRESSION_LINES:
                with self.subTest(sequence=seq_id, suppression=suppression.strip()):
                    with tempfile.TemporaryDirectory() as tmp:
                        work, base_sha = _build_repo(Path(tmp), seq_id, apply_solution=True)
                        with (work / source).open("a", encoding="utf-8") as handle:
                            handle.write(suppression)
                        _git(work, "add", "-A")
                        _git(work, "commit", "-q", "-m", "fix: worker suppression")

                        result = _run_grader(seq_id, work, baseline_sha=base_sha)
                        output = result.stdout + result.stderr

                        self.assertEqual(result.returncode, 1, output)
                        self.assertNotIn("PASS:", output)
                        self.assertNotIn("TEST SUMMARY", output, "tests must not run after a lint failure")

    def test_missing_baseline_is_grader_misconfigured(self) -> None:
        """Without DEVPACK_BASELINE_SHA every memory grader exits 2 with GRADER_MISCONFIGURED."""
        for seq_id in SEQUENCE_SOURCES:
            with self.subTest(sequence=seq_id):
                with tempfile.TemporaryDirectory() as tmp:
                    work, _ = _build_repo(Path(tmp), seq_id, apply_solution=False)
                    result = _run_grader(seq_id, work, baseline_sha=None)

                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("GRADER_MISCONFIGURED", result.stderr)


if __name__ == "__main__":
    unittest.main()
