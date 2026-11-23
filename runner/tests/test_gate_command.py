"""Tests for shared gate argv forwarding and pre-agent gate validation in devpack_dispatch.

Covers: the shared gate argv builder in agent_exec, scope and project-config forwarding on the
trusted-config gate path, and a clean refusal (agent never launched) when no gate is configured.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
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

from runner import agent_exec  # noqa: E402
from runner import devpack_dispatch  # noqa: E402

SRC_MOD = (
    "/**\n * @file mod.cpp\n * @brief Sample module.\n */\n"
    "/**\n * @brief Return one.\n * @return Constant 1.\n */\n"
    "int oneValue() { return 1; }\n"
)
GATE_TOML = '[[check]]\nname = "noop"\ncmd = ["true"]\n'


def _make_repo(base: Path, *, with_gate_toml: bool) -> Path:
    """Create a clean git worktree; optionally commit a root gate.toml so the trusted gate path applies."""
    wt = base / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "tests").mkdir()
    (wt / "preflight.toml").write_text('[general]\nstd = "c++17"\n', encoding="utf-8")
    (wt / "src" / "mod.cpp").write_text(SRC_MOD, encoding="utf-8")
    if with_gate_toml:
        (wt / "gate.toml").write_text(GATE_TOML, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=wt, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=wt, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=wt, check=True)
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=wt, check=True)
    return wt


def _write_marker_agent(path: Path, marker: Path) -> Path:
    """Write a fake claude CLI that records that it was launched and emits a stream-json result."""
    body = (
        "#!/usr/bin/env python3\n"
        "import json, pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
        "print(json.dumps({'type': 'result', 'is_error': False, 'result': 'done'}))\n"
    )
    path.write_text(body, encoding="utf-8")
    os.chmod(path, stat.S_IRWXU)
    return path


def _is_gate_argv(cmd: list[str]) -> bool:
    """Return True when ``cmd`` launches the pack's enforcement/gate/gate.py."""
    return len(cmd) > 1 and Path(cmd[1]).parts[-3:] == ("enforcement", "gate", "gate.py")


def _flag_value(argv: list[str], flag: str) -> str:
    """Return the value that follows ``flag`` in ``argv``; fail the test if the flag is absent."""
    index = argv.index(flag)
    return argv[index + 1]


class BuildGateArgvTests(unittest.TestCase):
    """The shared builder forwards scope and project config consistently."""

    def test_build_gate_argv_forwards_all_components_and_project_config(self) -> None:
        """All-components scope and a custom project config both appear in the gate argv."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_cfg = root / "custom-project.toml"
            argv = agent_exec.build_gate_argv(
                root / "gate.toml",
                root / "repo",
                all_components=True,
                project_config_path=project_cfg,
            )
            self.assertIn("--all-components", argv)
            self.assertNotIn("--components", argv)
            self.assertEqual(str(project_cfg), _flag_value(argv, "--project-config"))

    def test_build_gate_argv_forwards_named_components_only_when_not_all(self) -> None:
        """A named component selection is forwarded as one comma-separated --components value."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argv = agent_exec.build_gate_argv(
                root / "gate.toml",
                root / "repo",
                all_components=False,
                components=["alpha", "beta"],
            )
            self.assertEqual("alpha,beta", _flag_value(argv, "--components"))
            self.assertNotIn("--all-components", argv)
            self.assertNotIn("--project-config", argv)

    def test_resolve_project_gate_command_keeps_all_components_default(self) -> None:
        """The project gate command still defaults to --all-components for existing callers."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate = root / "gate.toml"
            gate.write_text(GATE_TOML, encoding="utf-8")
            argv = agent_exec.resolve_project_gate_command(root, gate_config=gate)
            self.assertIn("--all-components", argv)
            self.assertEqual(str(gate), _flag_value(argv, "--config"))


class TrustedGateArgvTests(unittest.TestCase):
    """_resolve_gate_command forwards the caller's scope and project config on the trusted path."""

    def test_trusted_gate_forwards_all_components(self) -> None:
        """With all_components=True the trusted gate argv contains --all-components."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            toml = root / "gate.toml"
            toml.write_text(GATE_TOML, encoding="utf-8")
            argv, _timeout = devpack_dispatch._resolve_gate_command(
                "", None, root / "repo", root / "run",
                trusted_gate_toml=toml,
                all_components=True,
            )
            self.assertIsNotNone(argv)
            self.assertIn("--all-components", argv)
            self.assertNotIn("--components", argv)

    def test_trusted_gate_forwards_project_config(self) -> None:
        """With a custom project config the trusted gate argv carries --project-config <path>."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            toml = root / "gate.toml"
            toml.write_text(GATE_TOML, encoding="utf-8")
            project_cfg = root / "custom-project.toml"
            argv, _timeout = devpack_dispatch._resolve_gate_command(
                "", None, root / "repo", root / "run",
                trusted_gate_toml=toml,
                project_config_path=project_cfg,
            )
            self.assertIsNotNone(argv)
            self.assertEqual(str(project_cfg), _flag_value(argv, "--project-config"))

    def test_trusted_gate_forwards_named_components(self) -> None:
        """Named components reach the trusted gate argv as --components."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            toml = root / "gate.toml"
            toml.write_text(GATE_TOML, encoding="utf-8")
            argv, _timeout = devpack_dispatch._resolve_gate_command(
                "", None, root / "repo", root / "run",
                trusted_gate_toml=toml,
                components=["alpha"],
            )
            self.assertIsNotNone(argv)
            self.assertEqual("alpha", _flag_value(argv, "--components"))
            self.assertNotIn("--all-components", argv)


class DispatchGateRunTests(unittest.TestCase):
    """End-to-end dispatch_job checks with a fake agent and a spied gate invocation."""

    def _dispatch(self, base: Path, wt: Path, agent: Path, **overrides: object) -> tuple[int, dict]:
        """Run dispatch_job against ``wt`` with ``agent`` as the agent binary."""
        brief = base / "brief.md"
        brief.write_text("# Brief\nUpdate oneValue() to return 2.\n", encoding="utf-8")
        kwargs: dict = dict(
            engine_name="haiku",
            brief_path=brief,
            workdir=wt,
            allow_globs=("src/**",),
            isolation="local",
            allow_unsandboxed_local=True,
            agent_bin=str(agent),
            runs_root=base / "runs",
        )
        kwargs.update(overrides)
        return devpack_dispatch.dispatch_job(**kwargs)

    def test_dispatch_gate_argv_carries_all_components_and_project_config(self) -> None:
        """dispatch_job hands the gate all-components scope and the project config path."""
        real_run = devpack_dispatch._run_subprocess
        captured: list[list[str]] = []

        def spy(cmd, cwd, timeout_s=300):  # type: ignore[no-untyped-def]
            """Record the gate invocation and report it as passing; delegate everything else."""
            if _is_gate_argv(list(cmd)):
                captured.append(list(cmd))
                return 0, "GATE_OK", ""
            return real_run(cmd, cwd, timeout_s=timeout_s)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = _make_repo(base, with_gate_toml=True)
            project_cfg = base / "custom-project.toml"
            project_cfg.write_text('data_class = "internal"\n', encoding="utf-8")
            agent = _write_marker_agent(base / "fake_claude.py", base / "marker.txt")
            with mock.patch.object(devpack_dispatch, "_run_subprocess", side_effect=spy):
                rc, payload = self._dispatch(
                    base, wt, agent,
                    all_components=True,
                    project_config_path=project_cfg,
                )
            self.assertEqual(1, len(captured), f"gate should run exactly once, got {captured}")
            argv = captured[0]
            self.assertIn("--all-components", argv)
            self.assertEqual(str(project_cfg), _flag_value(argv, "--project-config"))
            self.assertEqual(0, rc, payload)
            self.assertEqual("ok", payload["gate"])

    def test_missing_gate_config_refuses_before_agent_launch(self) -> None:
        """Without any gate configuration dispatch refuses cleanly and never starts the agent."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = _make_repo(base, with_gate_toml=False)
            marker = base / "marker.txt"
            agent = _write_marker_agent(base / "fake_claude.py", marker)
            rc, payload = self._dispatch(base, wt, agent)
            self.assertEqual(1, rc)
            self.assertEqual("refused", payload["status"])
            self.assertEqual("skipped", payload["gate"])
            self.assertIn("gate_config", payload["error"])
            self.assertFalse(marker.exists(), "agent must not be launched without a gate configuration")
            run_meta = json.loads(Path(payload["run_dir"], "run.json").read_text(encoding="utf-8"))
            self.assertEqual("refused", run_meta["outcome"])

    def test_no_gate_runs_agent_without_gate_configuration(self) -> None:
        """--no-gate skips gate resolution entirely, so the agent still runs without gate_config."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = _make_repo(base, with_gate_toml=False)
            marker = base / "marker.txt"
            agent = _write_marker_agent(base / "fake_claude.py", marker)
            _rc, payload = self._dispatch(base, wt, agent, no_gate=True)
            self.assertTrue(marker.exists(), "agent should run when the gate is disabled")
            self.assertEqual("ok", payload["status"])
            self.assertEqual("skipped", payload["gate"])


if __name__ == "__main__":
    unittest.main()
