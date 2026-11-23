"""Unit tests: ``run_runner_trial`` passes the trial's skill arm into the runner workflow.

The workflow is replaced by a mock, so no real agent, network or model call is made.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

EVAL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = EVAL_DIR.parent
for _p in (str(REPO_ROOT), str(EVAL_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_eval  # noqa: E402
from runner import devpack_run  # noqa: E402
from eval.tests.test_runner_init_info import _write_fixture_and_task  # noqa: E402


def _workflow_stub(**kwargs):
    """Return a minimal approved workflow result without running any agent."""
    return 0, {"status": "APPROVED", "verdict": "APPROVE", "cost_usd": 0.0, "duration_s": 1.0, "step_logs": []}


class RunnerTrialSkillsArmTests(unittest.TestCase):
    """Verify ``run_runner_trial`` forwards ``skills_enabled`` to ``run_task_workflow``."""

    def _run_trial(self, base: Path, skills_enabled: bool | None) -> tuple[mock.MagicMock, Path]:
        """Run one runner trial with the given arm and return the workflow mock and trial root."""
        fixture_dir, tasks_dir = _write_fixture_and_task(base)
        task = run_eval.load_and_validate_tasks(tasks_dir, "all")[0]
        engine = run_eval.EngineConfig(name="haiku", model="haiku", engine_class="cloud")
        kwargs = {}
        if skills_enabled is not None:
            kwargs["skills_enabled"] = skills_enabled
        run_root = base / "runs_root"
        with mock.patch.object(
            devpack_run, "run_task_workflow", side_effect=_workflow_stub
        ) as workflow, mock.patch.object(run_eval, "_run_check_script", return_value=(0, "")):
            run_eval.run_runner_trial(
                task=task,
                pair_label="haiku:haiku",
                impl_engine=engine,
                rev_engine=engine,
                fixer_engine=engine,
                run_index=1,
                run_root=run_root,
                fixture_dir=fixture_dir,
                credentials_path=None,
                isolation="local",
                agent_bin="unused",
                timeout_s=60,
                max_fix_rounds=0,
                runs_root=base / "runs",
                allow_unsandboxed_local=True,
                **kwargs,
            )
        return workflow, run_root

    def test_skill_arm_on_passes_true_and_installs_skills(self) -> None:
        """A skills-on trial calls the workflow with skills_enabled=True and installs skills in its workspace."""
        with tempfile.TemporaryDirectory() as tmp:
            workflow, run_root = self._run_trial(Path(tmp), skills_enabled=True)
            self.assertIs(True, workflow.call_args.kwargs["skills_enabled"])
            installed = list(run_root.rglob("SKILL.md"))
            self.assertTrue(installed, "skills-on trial installed no SKILL.md under its workspace")

    def test_skill_arm_off_passes_false_and_installs_nothing(self) -> None:
        """A skills-off trial calls the workflow with skills_enabled=False and installs no skills."""
        with tempfile.TemporaryDirectory() as tmp:
            workflow, run_root = self._run_trial(Path(tmp), skills_enabled=False)
            self.assertIs(False, workflow.call_args.kwargs["skills_enabled"])
            self.assertEqual([], list(run_root.rglob("SKILL.md")))

    def test_default_trial_passes_false(self) -> None:
        """A trial built without the argument keeps the workflow flag False."""
        with tempfile.TemporaryDirectory() as tmp:
            workflow, _ = self._run_trial(Path(tmp), skills_enabled=None)
            self.assertIs(False, workflow.call_args.kwargs["skills_enabled"])


if __name__ == "__main__":
    unittest.main()
