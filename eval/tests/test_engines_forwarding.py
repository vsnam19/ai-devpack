"""Unit tests: orchestrator trials forward the engines config to the broker; worker names are stripped.

No real agents, brokers or networks are used. The broker process is replaced by a fake Popen
that records its argv and reports an immediate exit, so the trial stops before any agent runs.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
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
from runner import devpack_broker  # noqa: E402
from runner.agent_exec import EngineConfig  # noqa: E402

_REAL_POPEN = subprocess.Popen


class _ExitedProcess:
    """Stand-in for a broker process that has already exited with status 1."""

    pid = 0

    def poll(self) -> int:
        """Report that the process has exited."""
        return 1

    def wait(self, timeout: float | None = None) -> int:
        """Return the exit status immediately."""
        return 1


class OrchestratorEnginesForwardingTests(unittest.TestCase):
    """run_orchestrator_trial must pass the engines config path to the broker command."""

    def setUp(self) -> None:
        """Create temporary goal, fixture and run directories."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.fixture = self.base / "fixture"
        self.fixture.mkdir()
        (self.fixture / "README.md").write_text("fixture\n", encoding="utf-8")
        self.goal_md = self.base / "goal.md"
        self.goal_md.write_text("# Goal\n\nDo the thing.\n", encoding="utf-8")
        self.check_sh = self.base / "check.sh"
        self.check_sh.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        self.goal = run_eval.OrchestratorGoalSpec(
            goal_id="99-fake-goal",
            category="test",
            tier="test",
            goal_dir=self.base,
            goal_md=self.goal_md,
            check_sh=self.check_sh,
        )
        self.engine = EngineConfig(name="sonnet", model="sonnet", engine_class="cloud")
        self.run_root = self.base / "runs-root-trials"
        self.run_root.mkdir()

    def _capture_broker_argv(self, engines_config: Path | None) -> list[str]:
        """Run one trial with a fake broker Popen and return the broker argv it was started with."""
        captured: list[list[str]] = []

        def fake_popen(argv, *args, **kwargs):
            """Record broker launches; delegate every other process to the real Popen."""
            if any("devpack_broker.py" in str(part) for part in argv):
                captured.append([str(part) for part in argv])
                return _ExitedProcess()
            return _REAL_POPEN(argv, *args, **kwargs)

        kwargs = {"engines_config": engines_config} if engines_config is not None else {}
        with mock.patch.object(run_eval.subprocess, "Popen", side_effect=fake_popen):
            with self.assertRaises(RuntimeError):
                run_eval.run_orchestrator_trial(
                    goal=self.goal,
                    orch_label="sonnet",
                    orch_engine=self.engine,
                    worker_engines_str="custom-worker",
                    run_index=1,
                    run_root=self.run_root,
                    fixture_dir=self.fixture,
                    credentials_path=None,
                    isolation="local",
                    agent_bin="/nonexistent/fake-agent",
                    timeout_s=30,
                    runs_root=self.base / "runs",
                    allow_unsandboxed_local=True,
                    **kwargs,
                )
        self.assertEqual(1, len(captured), "exactly one broker command must be launched")
        return captured[0]

    def test_engines_config_is_forwarded_as_absolute_path(self) -> None:
        """A custom engines file reaches the broker as --engines <absolute path>."""
        engines = self.base / "custom.toml"
        engines.write_text("version = 2\n", encoding="utf-8")
        argv = self._capture_broker_argv(engines)
        self.assertIn("--engines", argv)
        value = argv[argv.index("--engines") + 1]
        self.assertEqual(str(engines.resolve()), value)
        self.assertTrue(Path(value).is_absolute())
        self.assertEqual("custom-worker", argv[argv.index("--workers") + 1])

    def test_no_engines_config_means_no_engines_flag(self) -> None:
        """Without a custom engines file the broker keeps its default and no --engines flag is sent."""
        argv = self._capture_broker_argv(None)
        self.assertNotIn("--engines", argv)


class WorkerNameParsingTests(unittest.TestCase):
    """The broker strips worker names and drops empty entries."""

    def test_whitespace_is_stripped_around_names(self) -> None:
        """"haiku, local" parses to ["haiku", "local"]."""
        self.assertEqual(["haiku", "local"], devpack_broker.parse_worker_names("haiku, local"))

    def test_empty_entries_are_dropped(self) -> None:
        """Empty and whitespace-only entries are ignored."""
        self.assertEqual(["haiku"], devpack_broker.parse_worker_names(" , haiku,, "))

    def test_no_names_is_refused(self) -> None:
        """A list that names no engine is rejected instead of allowing every engine."""
        with self.assertRaises(ValueError):
            devpack_broker.parse_worker_names(" , ")


if __name__ == "__main__":
    unittest.main()
