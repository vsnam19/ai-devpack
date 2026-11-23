#!/usr/bin/env python3
# verify_orchestrator_goals.py - Offline verifier for eval/orchestrator_goals/*/.
#
# Usage:
#   python3 tests/verify_orchestrator_goals.py [--help]
#
# Validates every goal directory under eval/orchestrator_goals/ by loading it
# with eval/run_eval.py, copying eval/fixture/ to an isolated temporary git
# workspace, asserting that check.sh FAILS before solution.patch, and asserting
# that check.sh PASSES after applying solution.patch.
#
# Exit codes:
#   0 - All orchestrator goals verified successfully
#   1 - One or more orchestrator goals failed verification
#   2 - Invalid command-line arguments
"""Offline verification script for every orchestrator goal under eval/orchestrator_goals/."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_ROOT / "eval"
for _p in (str(REPO_ROOT), str(EVAL_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_eval  # noqa: E402


def verify_all_goals() -> int:
    """Validate and grade every goal under ``eval/orchestrator_goals/``."""
    goals_dir = EVAL_DIR / "orchestrator_goals"
    fixture_dir = EVAL_DIR / "fixture"
    goals = run_eval.load_and_validate_orchestrator_goals(goals_dir, "all", "all")
    if not goals:
        print("FAIL: no orchestrator goals found", file=sys.stderr)
        return 1

    env = {**os.environ, "DEVPACK_ROOT": str(REPO_ROOT)}
    failures = 0
    for goal in goals:
        with tempfile.TemporaryDirectory(prefix=f"devpack-goal-{goal.goal_id}-") as tmp:
            work_dir = Path(tmp) / "work"
            shutil.copytree(fixture_dir, work_dir)
            for git_cmd in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "verify@example.invalid"],
                ["git", "config", "user.name", "Goal Verifier"],
                ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "chore: base fixture"],
            ):
                subprocess.run(git_cmd, cwd=work_dir, check=True, capture_output=True)

            base_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=work_dir,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            goal_env = {**env, "DEVPACK_BASELINE_SHA": base_sha}

            before = subprocess.run(
                ["bash", str(goal.check_sh), "--workdir", str(work_dir)],
                cwd=work_dir,
                env=goal_env,
                capture_output=True,
                text=True,
                check=False,
            )
            if before.returncode == 0:
                print(f"FAIL {goal.goal_id}: check.sh passed on unmodified fixture", file=sys.stderr)
                failures += 1
                continue

            solution_patch = goal.goal_dir / "solution.patch"
            if not solution_patch.is_file():
                print(f"FAIL {goal.goal_id}: missing {solution_patch}", file=sys.stderr)
                failures += 1
                continue

            subprocess.run(
                ["git", "apply", str(solution_patch)],
                cwd=work_dir,
                check=True,
                capture_output=True,
            )
            after = subprocess.run(
                ["bash", str(goal.check_sh), "--workdir", str(work_dir)],
                cwd=work_dir,
                env=goal_env,
                capture_output=True,
                text=True,
                check=False,
            )
            if after.returncode != 0:
                print(
                    f"FAIL {goal.goal_id}: check.sh failed after solution.patch:\n"
                    f"{after.stdout}\n{after.stderr}",
                    file=sys.stderr,
                )
                failures += 1
                continue
            print(f"OK {goal.goal_id}")

    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run orchestrator goal verification."""
    parser = argparse.ArgumentParser(
        description="Verify all orchestrator goals under eval/orchestrator_goals/ offline."
    )
    parser.parse_args(argv)
    return verify_all_goals()


if __name__ == "__main__":
    raise SystemExit(main())
