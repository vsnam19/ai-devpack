"""Unit tests: ``skills_enabled`` is threaded from the workflow and CLI into every AgentRequest.

The agent is replaced by a recording stub (``devpack_run.run_agent``) that returns
canned results. No real agent, network or model call is made.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

RUNNER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = RUNNER_DIR.parent
for _p in (str(REPO_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent_exec  # noqa: E402
import devpack_run  # noqa: E402


def _config() -> dict:
    """Return an engines config dict with one local engine serving all three roles."""
    engine = devpack_run.EngineConfig(name="local", model="local", engine_class="local")
    return {
        "roles": {"implementer": engine, "reviewer": engine, "fixer": engine},
        "gate_cmd": ["true"],
    }


def _run_recorded_workflow(base: Path, **extra) -> list[agent_exec.AgentRequest]:
    """Run one workflow with reviewer REQUEST_CHANGES then APPROVE; return every recorded request.

    The sequence exercises the implementer, the fixer and the reviewer twice.
    """
    recorded: list[agent_exec.AgentRequest] = []
    reviews = iter(
        [
            agent_exec.AgentResult(status="ok", exit_code=0, final_text="VERDICT: REQUEST_CHANGES"),
            agent_exec.AgentResult(status="ok", exit_code=0, final_text="VERDICT: APPROVE"),
        ]
    )

    def _fake_run_agent(req: agent_exec.AgentRequest) -> agent_exec.AgentResult:
        """Record the request and return a canned result for its role."""
        recorded.append(req)
        if req.role == "reviewer":
            return next(reviews)
        return agent_exec.AgentResult(status="ok", exit_code=0, final_text="done")

    context = devpack_run.load_task_data(None, "skills-task", "Skills", "Goal", "result.txt")
    with mock.patch.object(devpack_run, "run_agent", side_effect=_fake_run_agent):
        rc, summary = devpack_run.run_task_workflow(
            _config(),
            context,
            base,
            base / "logs",
            base,
            isolation="local",
            allow_unsandboxed_local=True,
            mode="autonomous",
            **extra,
        )
    if rc != 0:
        raise AssertionError(f"workflow did not approve: {summary}")
    return recorded


class SkillsThreadingTests(unittest.TestCase):
    """Verify the skills flag reaches every role's AgentRequest and defaults to off."""

    def test_skills_on_sets_flag_on_every_role_request(self) -> None:
        """With skills_enabled=True, implementer, fixer and reviewer requests all carry True."""
        with tempfile.TemporaryDirectory() as tmp:
            requests = _run_recorded_workflow(Path(tmp), skills_enabled=True)
        roles = [req.role for req in requests]
        self.assertIn("implementer", roles)
        self.assertIn("fixer", roles)
        self.assertIn("reviewer", roles)
        self.assertGreaterEqual(len(requests), 4)
        for req in requests:
            self.assertIs(True, req.skills_enabled, f"role {req.role} lost skills_enabled")

    def test_default_leaves_flag_off_on_every_role_request(self) -> None:
        """Without the argument, every AgentRequest keeps skills_enabled=False."""
        with tempfile.TemporaryDirectory() as tmp:
            requests = _run_recorded_workflow(Path(tmp))
        self.assertGreaterEqual(len(requests), 4)
        for req in requests:
            self.assertIs(False, req.skills_enabled, f"role {req.role} default is not False")

    def test_cli_skills_flag_defaults_off_and_enables_when_given(self) -> None:
        """The devpack_run CLI parses --skills to True and omits it to False."""
        parser = devpack_run.build_arg_parser()
        self.assertFalse(parser.parse_args([]).skills)
        self.assertTrue(parser.parse_args(["--skills"]).skills)

    def test_cli_forwards_skills_flag_to_workflow(self) -> None:
        """main() passes args.skills into run_task_workflow as skills_enabled."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with mock.patch.object(
                devpack_run, "run_task_workflow", return_value=(0, {})
            ) as workflow, mock.patch.object(devpack_run, "load_engine_config", return_value=_config()):
                devpack_run.main(
                    ["--worktree", str(base), "--log-dir", str(base / "logs"), "--skills"]
                )
                self.assertIs(True, workflow.call_args.kwargs["skills_enabled"])
                devpack_run.main(["--worktree", str(base), "--log-dir", str(base / "logs")])
                self.assertIs(False, workflow.call_args.kwargs["skills_enabled"])


if __name__ == "__main__":
    unittest.main()
