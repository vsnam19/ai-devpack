"""Unit tests for eval task validation: hidden/ is required except for review tasks."""

from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

EVAL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = EVAL_DIR.parent
for _p in (str(REPO_ROOT), str(EVAL_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_eval  # noqa: E402


def _make_task(
    tasks_dir: Path,
    name: str,
    category: str,
    *,
    with_hidden: bool,
    with_review_patch: bool,
) -> Path:
    """Create a minimal task directory under ``tasks_dir`` and return its path."""
    tdir = tasks_dir / name
    tdir.mkdir(parents=True)
    (tdir / "task.md").write_text(
        f"# Task: {name}\n\nCategory: {category}\n\n## Goal\nSynthetic task.\n",
        encoding="utf-8",
    )
    (tdir / "setup.patch").write_text("", encoding="utf-8")
    (tdir / "solution.patch").write_text("", encoding="utf-8")
    (tdir / "check.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    if with_hidden:
        (tdir / "hidden").mkdir()
        (tdir / "hidden" / "hidden_test.cpp").write_text("// hidden\n", encoding="utf-8")
    if with_review_patch:
        (tdir / "review.patch").write_text("", encoding="utf-8")
    return tdir


class TaskValidationTests(unittest.TestCase):
    """Verify which task files each category must provide."""

    def test_review_task_without_hidden_but_with_review_patch_is_valid(self) -> None:
        """A review task needs review.patch and may omit hidden/."""
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = Path(tmp) / "tasks"
            _make_task(
                tasks_dir,
                "18-review-sample",
                "review",
                with_hidden=False,
                with_review_patch=True,
            )
            specs = run_eval.load_and_validate_tasks(tasks_dir, "all")

        self.assertEqual(["18-review-sample"], [s.task_id for s in specs])
        self.assertEqual("review", specs[0].category)
        self.assertIsNone(specs[0].hidden_dir)

    def test_review_task_with_hidden_dir_keeps_it(self) -> None:
        """A review task that does ship hidden/ still exposes that directory."""
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = Path(tmp) / "tasks"
            tdir = _make_task(
                tasks_dir,
                "18-review-sample",
                "review",
                with_hidden=True,
                with_review_patch=True,
            )
            specs = run_eval.load_and_validate_tasks(tasks_dir, "18-review-sample")

        self.assertEqual(tdir / "hidden", specs[0].hidden_dir)

    def test_review_task_without_review_patch_is_rejected(self) -> None:
        """A review task without review.patch fails validation and names the missing file."""
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = Path(tmp) / "tasks"
            _make_task(
                tasks_dir,
                "18-review-sample",
                "review",
                with_hidden=False,
                with_review_patch=False,
            )
            with self.assertRaises(ValueError) as ctx:
                run_eval.load_and_validate_tasks(tasks_dir, "all")

        self.assertIn("review.patch", str(ctx.exception))
        self.assertIn("18-review-sample", str(ctx.exception))

    def test_bugfix_task_without_hidden_is_rejected(self) -> None:
        """A non-review task without hidden/ still fails validation."""
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = Path(tmp) / "tasks"
            _make_task(
                tasks_dir,
                "01-bugfix-sample",
                "bugfix",
                with_hidden=False,
                with_review_patch=False,
            )
            with self.assertRaises(ValueError) as ctx:
                run_eval.load_and_validate_tasks(tasks_dir, "all")

        self.assertIn("hidden/", str(ctx.exception))
        self.assertIn("01-bugfix-sample", str(ctx.exception))

    def test_bugfix_task_with_hidden_is_valid(self) -> None:
        """A non-review task with hidden/ is valid and keeps its hidden directory."""
        with tempfile.TemporaryDirectory() as tmp:
            tasks_dir = Path(tmp) / "tasks"
            tdir = _make_task(
                tasks_dir,
                "01-bugfix-sample",
                "bugfix",
                with_hidden=True,
                with_review_patch=False,
            )
            specs = run_eval.load_and_validate_tasks(tasks_dir, "all")

        self.assertEqual(tdir / "hidden", specs[0].hidden_dir)
        self.assertEqual("bugfix", specs[0].category)

    def test_shipped_review_task_18_loads(self) -> None:
        """The shipped review task 18 validates without a hidden/ directory (read-only)."""
        specs = run_eval.load_and_validate_tasks(EVAL_DIR / "tasks", "18-review-fault-recovery")

        self.assertEqual(1, len(specs))
        self.assertEqual("review", specs[0].category)
        self.assertIsNone(specs[0].hidden_dir)

    def _setup_task_workdir(self, base: Path, task_id: str) -> tuple[Path, Path, str]:
        """Copy eval/fixture, initialize git, apply setup.patch, and return (task_dir, work_dir, base_sha)."""
        task_dir = EVAL_DIR / "tasks" / task_id
        work = base / "work"
        shutil.copytree(EVAL_DIR / "fixture", work)
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@example.invalid"],
            ["git", "config", "user.name", "T"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", "chore: base"],
            ["git", "apply", "--allow-empty", str(task_dir / "setup.patch")],
            ["git", "add", "-A"],
        ):
            subprocess.run(cmd, cwd=work, check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=work).returncode != 0:
            subprocess.run(["git", "commit", "-q", "-m", "chore: setup"], cwd=work, check=True)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=work, check=True, capture_output=True, text=True
        ).stdout.strip()
        return task_dir, work, base_sha

    def _run_task_check(self, task_dir: Path, work: Path, base_sha: str) -> subprocess.CompletedProcess[str]:
        """Run a task's check.sh against workdir with DEVPACK_ROOT and DEVPACK_BASELINE_SHA."""
        env = {**os.environ, "DEVPACK_ROOT": str(REPO_ROOT), "DEVPACK_BASELINE_SHA": base_sha}
        return subprocess.run(
            ["bash", str(task_dir / "check.sh"), "--workdir", str(work)],
            cwd=work, env=env, capture_output=True, text=True, check=False,
        )

    def test_f2_task_grader_mutants_fail(self) -> None:
        """Reviewer bypass mutants for tasks 04, 08, 09, 10, and 18 all fail check.sh."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # Task 08: trailing /** @brief x */ on signExtend fails even with full block earlier.
            t08, w08, s08 = self._setup_task_workdir(root / "t08", "08-doxygen-naming-codec")
            subprocess.run(["git", "apply", str(t08 / "solution.patch")], cwd=w08, check=True)
            hdr08 = w08 / "include" / "can_codec.hpp"
            hdr08.write_text(
                hdr08.read_text(encoding="utf-8").replace(
                    "/**\n * @brief Sign-extend an unsigned bitLength-bit raw value using two's-complement rules.\n"
                    " * @param[in] rawValue Extracted unsigned bit pattern.\n"
                    " * @param[in] bitLength Number of significant bits in rawValue (1..64).\n"
                    " * @return Signed 64-bit integer value after sign extension.\n */",
                    "/** @brief x */",
                ),
                encoding="utf-8",
            )
            r08 = self._run_task_check(t08, w08, s08)
            self.assertNotEqual(0, r08.returncode, r08.stdout + r08.stderr)

            # Task 09: mismatched @param name fails.
            t09, w09, s09 = self._setup_task_workdir(root / "t09", "09-doxygen-naming-link")
            subprocess.run(["git", "apply", str(t09 / "solution.patch")], cwd=w09, check=True)
            hdr09 = w09 / "include" / "link_state_machine.hpp"
            hdr09.write_text(
                hdr09.read_text(encoding="utf-8").replace("@param[in] nowMs", "@param[in] wrongParam", 1),
                encoding="utf-8",
            )
            r09 = self._run_task_check(t09, w09, s09)
            self.assertNotEqual(0, r09.returncode, r09.stdout + r09.stderr)

            # Task 10: bare STOP fails; missing exact file:line fails; modifying src/ fails.
            t10, w10, s10 = self._setup_task_workdir(root / "t10", "10-trap-contradictory-test")
            (w10 / "STOP_REPORT.md").write_text("STOP\n", encoding="utf-8")
            self.assertNotEqual(0, self._run_task_check(t10, w10, s10).returncode)
            (w10 / "STOP_REPORT.md").write_text(
                "STOP\nContradiction at tests/test_can_codec.cpp:142 only.\n", encoding="utf-8"
            )
            self.assertNotEqual(0, self._run_task_check(t10, w10, s10).returncode)
            (w10 / "STOP_REPORT.md").unlink()
            subprocess.run(["git", "apply", str(t10 / "solution.patch")], cwd=w10, check=True)
            cpp10 = w10 / "src" / "can_codec.cpp"
            cpp10.write_text(cpp10.read_text(encoding="utf-8") + "\n// edited\n", encoding="utf-8")
            self.assertNotEqual(0, self._run_task_check(t10, w10, s10).returncode)

            # Task 04: removing null-pointer tests from solution fails null-guard mutants.
            t04, w04, s04 = self._setup_task_workdir(root / "t04", "04-add-test-codec-bounds")
            subprocess.run(["git", "apply", str(t04 / "solution.patch")], cwd=w04, check=True)
            tcpp04 = w04 / "tests" / "test_can_codec.cpp"
            txt04 = tcpp04.read_text(encoding="utf-8")
            no_null = "\n".join(ln for ln in txt04.splitlines() if "nullptr" not in ln) + "\n"
            tcpp04.write_text(no_null, encoding="utf-8")
            r04 = self._run_task_check(t04, w04, s04)
            self.assertNotEqual(0, r04.returncode, r04.stdout + r04.stderr)

            # Task 18: broad range (1-9999) and >2 extra blockers fail.
            t18, w18, s18 = self._setup_task_workdir(root / "t18", "18-review-fault-recovery")
            subprocess.run(["git", "apply", str(t18 / "negative_broad_range.patch")], cwd=w18, check=True)
            self.assertNotEqual(0, self._run_task_check(t18, w18, s18).returncode)
            subprocess.run(["git", "reset", "-q", "--hard", s18], cwd=w18, check=True)
            subprocess.run(["git", "clean", "-q", "-fd"], cwd=w18, check=True)
            subprocess.run(["git", "apply", str(t18 / "negative_extra_blockers.patch")], cwd=w18, check=True)
            self.assertNotEqual(0, self._run_task_check(t18, w18, s18).returncode)


if __name__ == "__main__":
    unittest.main()
