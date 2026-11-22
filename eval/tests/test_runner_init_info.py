"""Unit tests: runner-mode trials report the implementer step's served model and digest.

The workflow is replaced by a stub that writes step logs and a recorded-style stream
transcript into the trial run directory. No real agent, network or model call is made.
"""

from __future__ import annotations

import json
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


def _write_fixture_and_task(base: Path) -> tuple[Path, Path]:
    """Create a minimal fixture directory and one task directory under ``base``."""
    fixture_dir = base / "fixture"
    (fixture_dir / "src").mkdir(parents=True)
    (fixture_dir / "build_and_test.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (fixture_dir / "src" / "sample.cpp").write_text("int value() { return 0; }\n", encoding="utf-8")
    tasks_dir = base / "tasks"
    tdir = tasks_dir / "01-mini-bugfix"
    (tdir / "hidden").mkdir(parents=True)
    (tdir / "hidden" / "hidden_test.cpp").write_text("// hidden\n", encoding="utf-8")
    (tdir / "task.md").write_text(
        "# Task: 01-mini-bugfix\n\nCategory: bugfix\n\n## Goal\nFix value().\n",
        encoding="utf-8",
    )
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
    (tdir / "check.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    return fixture_dir, tasks_dir


def _write_stream(path: Path, model: str, digest: str) -> None:
    """Write a minimal stream-json transcript whose init event reports ``model`` and ``digest``."""
    init_event = {"type": "system", "subtype": "init", "model": model, "model_digest": digest}
    result_event = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
    path.write_text(json.dumps(init_event) + "\n" + json.dumps(result_event) + "\n", encoding="utf-8")


def _make_stub_workflow(implementer_model: str, implementer_digest: str):
    """Return a stand-in for ``devpack_run.run_task_workflow`` whose implementer step reports init info."""

    def _stub(**kwargs):
        log_dir = Path(kwargs["log_dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript = log_dir / "impl_stream.jsonl"
        if implementer_model:
            _write_stream(transcript, implementer_model, implementer_digest)
        else:
            transcript.write_text(
                json.dumps({"type": "result", "subtype": "success", "result": "done"}) + "\n",
                encoding="utf-8",
            )
        step_name = "step_01_implement.json"
        (log_dir / step_name).write_text(
            json.dumps(
                {
                    "role": "implementer",
                    "engine": "haiku",
                    "transcript_path": str(transcript),
                }
            ),
            encoding="utf-8",
        )
        summary = {
            "status": "APPROVED",
            "verdict": "APPROVE",
            "cost_usd": 0.0,
            "duration_s": 1.0,
            "step_logs": [step_name],
        }
        return 0, summary

    return _stub


class RunnerTrialInitInfoTests(unittest.TestCase):
    """Verify ``run_runner_trial`` fills init_model/init_digest from the implementer step."""

    def _run_trial(self, base: Path, implementer_model: str, implementer_digest: str) -> run_eval.TrialResult:
        """Execute one runner trial against the stub workflow and return its TrialResult."""
        fixture_dir, tasks_dir = _write_fixture_and_task(base)
        task = run_eval.load_and_validate_tasks(tasks_dir, "all")[0]
        engine = run_eval.EngineConfig(name="haiku", model="haiku", engine_class="cloud")
        with mock.patch.object(
            devpack_run,
            "run_task_workflow",
            side_effect=_make_stub_workflow(implementer_model, implementer_digest),
        ), mock.patch.object(run_eval, "_run_check_script", return_value=(0, "")):
            return run_eval.run_runner_trial(
                task=task,
                pair_label="haiku:haiku",
                impl_engine=engine,
                rev_engine=engine,
                fixer_engine=engine,
                run_index=1,
                run_root=base / "runs_root",
                fixture_dir=fixture_dir,
                credentials_path=None,
                isolation="local",
                agent_bin="unused",
                timeout_s=60,
                max_fix_rounds=0,
                runs_root=base / "runs",
                allow_unsandboxed_local=True,
            )

    def test_runner_trial_reports_implementer_served_model_and_digest(self) -> None:
        """A stub implementer step reporting m-served/d1 yields TrialResult init_model and init_digest."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run_trial(Path(tmp), "m-served", "d1")
            self.assertEqual("m-served", result.init_model)
            self.assertEqual("d1", result.init_digest)
            self.assertEqual("runner", result.mode)

    def test_runner_trial_leaves_init_empty_when_no_init_info(self) -> None:
        """When the implementer transcript has no init event, init fields stay empty (nothing invented)."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run_trial(Path(tmp), "", "")
            self.assertEqual("", result.init_model)
            self.assertEqual("", result.init_digest)


if __name__ == "__main__":
    unittest.main()
