"""Unit test: the eval harness passes its own repository's ``.git`` as ``trusted_git_dir`` (T24).

``run_eval.run_single_trial`` creates the trial work repository with ``git init`` itself, so
the trusted git dir is that repository's ``.git``. ``run_agent`` is replaced by a stub that
records the request; no agent, container or network is used.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

EVAL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = EVAL_DIR.parent
for _p in (str(REPO_ROOT), str(EVAL_DIR), str(REPO_ROOT / "runner")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent_exec  # noqa: E402
import run_eval  # noqa: E402


def _create_mini_fixture_and_task(base_dir: Path) -> tuple[Path, Path]:
    """Create a minimal synthetic fixture and one task; return ``(fixture_dir, tasks_dir)``."""
    fixture_dir = base_dir / "fixture"
    for sub in ("src", "include", "tests"):
        (fixture_dir / sub).mkdir(parents=True, exist_ok=True)
    (fixture_dir / "CMakeLists.txt").write_text("# mini fixture\n", encoding="utf-8")
    (fixture_dir / "build_and_test.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
        encoding="utf-8",
    )
    (fixture_dir / "src" / "sample.cpp").write_text("int value() { return 0; }\n", encoding="utf-8")

    tasks_dir = base_dir / "tasks"
    tdir = tasks_dir / "01-mini-bugfix"
    (tdir / "hidden").mkdir(parents=True, exist_ok=True)
    (tdir / "hidden" / "hidden_test.cpp").write_text("// hidden\n", encoding="utf-8")
    (tdir / "task.md").write_text(
        "# Task: 01-mini-bugfix\n\nCategory: bugfix\n\n## Goal\nFix value() to return 42.\n",
        encoding="utf-8",
    )
    (tdir / "check.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (tdir / "setup.patch").write_text(
        "diff --git a/src/sample.cpp b/src/sample.cpp\n"
        "--- a/src/sample.cpp\n"
        "+++ b/src/sample.cpp\n"
        "@@ -1 +1 @@\n"
        "-int value() { return 0; }\n"
        "+int value() { return -1; }\n",
        encoding="utf-8",
    )
    (tdir / "solution.patch").write_text(
        "diff --git a/src/sample.cpp b/src/sample.cpp\n"
        "--- a/src/sample.cpp\n"
        "+++ b/src/sample.cpp\n"
        "@@ -1 +1 @@\n"
        "-int value() { return -1; }\n"
        "+int value() { return 42; }\n",
        encoding="utf-8",
    )
    return fixture_dir, tasks_dir


class EvalTrustedGitDirTests(unittest.TestCase):
    """``run_single_trial`` gives its agent request the trial repository's own ``.git``."""

    def test_single_trial_request_trusts_its_own_repository_gitdir(self) -> None:
        """The trusted path is ``<trial workdir>/.git``, a directory the harness created with git init."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture_dir, tasks_dir = _create_mini_fixture_and_task(base)
            task = run_eval.load_and_validate_tasks(tasks_dir, "all")[0]
            recorded: list[tuple[agent_exec.AgentRequest, bool]] = []

            def _fake_run_agent(req: agent_exec.AgentRequest) -> agent_exec.AgentResult:
                """Record the request and whether its trusted git dir exists as a directory."""
                recorded.append((req, bool(req.trusted_git_dir) and req.trusted_git_dir.is_dir()))
                return agent_exec.AgentResult(status="ok", exit_code=0, final_text="done")

            with mock.patch.object(run_eval, "run_agent", side_effect=_fake_run_agent):
                run_eval.run_single_trial(
                    task=task,
                    model="haiku",
                    run_index=1,
                    run_root=base / "runs",
                    fixture_dir=fixture_dir,
                    credentials_path=None,
                    isolation="local",
                    agent_bin=str(base / "unused-agent"),
                    timeout_s=30,
                    allow_unsandboxed_local=True,
                )

            self.assertTrue(recorded, "stub run_agent was never called")
            for req, trusted_is_dir in recorded:
                self.assertEqual(req.workdir / ".git", req.trusted_git_dir)
                self.assertTrue(trusted_is_dir)
                self.assertTrue(os.path.isdir(req.workdir))


if __name__ == "__main__":
    unittest.main()
