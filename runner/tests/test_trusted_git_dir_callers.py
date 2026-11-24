"""Unit tests: callers pass ``trusted_git_dir`` into every AgentRequest they build (T24).

``devpack_dispatch.dispatch_job`` and ``devpack_run.run_task_workflow`` are exercised with
``run_agent`` replaced by a recording stub, so no agent, container or network is used.
Only the caller that creates its own repository can supply a trusted path; the CLI entry
points receive a user-provided worktree and keep the default ``None``.
"""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import subprocess
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
import devpack_dispatch  # noqa: E402
import devpack_run  # noqa: E402

_GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def _git(args: list[str], cwd: Path) -> None:
    """Run a git command in ``cwd`` with an isolated environment; raise on failure."""
    subprocess.run(["git", *args], cwd=str(cwd), env=_GIT_ENV, check=True, capture_output=True)


def _make_repo(repo: Path) -> None:
    """Create a git repository with one commit at ``repo``."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@example.invalid"], repo)
    _git(["config", "user.name", "Tester"], repo)
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "a.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "init"], repo)


def _ok_result() -> agent_exec.AgentResult:
    """Return a successful canned agent result with no changes."""
    return agent_exec.AgentResult(status="ok", exit_code=0, final_text="done")


class DispatchTrustedGitDirTests(unittest.TestCase):
    """``devpack_dispatch.dispatch_job`` forwards ``trusted_git_dir`` to its AgentRequest."""

    def _dispatch(self, base: Path, **extra) -> list[agent_exec.AgentRequest]:
        """Run one dispatch job with a stubbed agent and return every recorded request."""
        wt = base / "wt"
        _make_repo(wt)
        brief = base / "brief.md"
        brief.write_text("Minimal brief for the trusted git dir test.\n", encoding="utf-8")
        recorded: list[agent_exec.AgentRequest] = []

        def _fake_run_agent(req: agent_exec.AgentRequest) -> agent_exec.AgentResult:
            """Record the request and return a clean result."""
            recorded.append(req)
            return _ok_result()

        with mock.patch.object(devpack_dispatch, "run_agent", side_effect=_fake_run_agent):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                devpack_dispatch.dispatch_job(
                    engine_name="haiku",
                    brief_path=brief,
                    workdir=wt,
                    allow_globs=["src/**"],
                    gate_cmd="true",
                    runs_root=base / "runs",
                    isolation="local",
                    allow_unsandboxed_local=True,
                    mode="autonomous",
                    **extra,
                )
        return recorded

    def test_dispatch_passes_trusted_git_dir_to_agent_request(self) -> None:
        """The trusted path given to dispatch_job reaches the AgentRequest unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            trusted = (base / "trusted-gitdir").resolve()
            recorded = self._dispatch(base, trusted_git_dir=trusted)
        self.assertTrue(recorded, "stub run_agent was never called")
        for req in recorded:
            self.assertEqual(trusted, req.trusted_git_dir)

    def test_dispatch_cli_default_passes_no_trusted_git_dir(self) -> None:
        """Without a trusted path (user-provided worktree), the request carries ``None``."""
        with tempfile.TemporaryDirectory() as tmp:
            recorded = self._dispatch(Path(tmp))
        self.assertTrue(recorded, "stub run_agent was never called")
        for req in recorded:
            self.assertIsNone(req.trusted_git_dir)


class RunTaskWorkflowTrustedGitDirTests(unittest.TestCase):
    """``devpack_run.run_task_workflow`` forwards ``trusted_git_dir`` to every role's AgentRequest."""

    def test_workflow_passes_trusted_git_dir_to_every_role(self) -> None:
        """Implementer, fixer and reviewer requests all carry the trusted path."""
        engine = devpack_run.EngineConfig(name="local", model="local", engine_class="local")
        config = {
            "roles": {"implementer": engine, "reviewer": engine, "fixer": engine},
            "gate_cmd": ["true"],
        }
        trusted = Path("/nonexistent-trusted-gitdir/worktrees/t24").resolve()
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
            return _ok_result()

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            context = devpack_run.load_task_data(None, "t24-task", "T24", "Goal", "result.txt")
            with mock.patch.object(devpack_run, "run_agent", side_effect=_fake_run_agent), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc, summary = devpack_run.run_task_workflow(
                    config,
                    context,
                    base,
                    base / "logs",
                    base,
                    isolation="local",
                    allow_unsandboxed_local=True,
                    trusted_git_dir=trusted,
                    mode="autonomous",
                )
        self.assertEqual(0, rc, summary)
        roles = {req.role for req in recorded}
        self.assertIn("implementer", roles)
        self.assertIn("reviewer", roles)
        for req in recorded:
            self.assertEqual(trusted, req.trusted_git_dir, req.role)


if __name__ == "__main__":
    unittest.main()
