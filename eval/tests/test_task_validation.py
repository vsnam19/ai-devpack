"""Unit tests for eval task validation: hidden/ is required except for review tasks."""

from __future__ import annotations

from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
