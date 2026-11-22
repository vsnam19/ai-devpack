"""Unit tests for runner/devpack_dispatch.py, orchestrator config, and sandbox policies."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

RUNNER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = RUNNER_DIR.parent
for _p in (str(REPO_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import devpack_dispatch  # noqa: E402
from skilltest import skilltest  # noqa: E402
from eval.run_eval import ORCHESTRATOR_PACK_PATH  # noqa: E402
from runner.devpack_broker import Broker, CLIENT_RELATIVE, CLIENT_SOURCE  # noqa: E402


def _init_git_repo(workdir: Path) -> None:
    """Create a minimal clean C++ git worktree for dispatch tests."""
    (workdir / "src").mkdir(parents=True, exist_ok=True)
    (workdir / "tests").mkdir(parents=True, exist_ok=True)
    (workdir / "preflight.toml").write_text('[general]\nstd = "c++17"\n', encoding="utf-8")
    (workdir / "src" / "mod.cpp").write_text(
        "/**\n * @file mod.cpp\n * @brief Sample module.\n */\n"
        "/**\n * @brief Return one.\n * @return Constant 1.\n */\n"
        "int oneValue() { return 1; }\n",
        encoding="utf-8",
    )
    (workdir / "tests" / "test_mod.cpp").write_text(
        "#include <cassert>\nvoid testOne() { assert(1 == 1); }\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=workdir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=workdir, check=True)


def _write_fake_agent(path: Path, script_body: str) -> Path:
    """Write an executable fake claude CLI returning stream-json output."""
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        + script_body,
        encoding="utf-8",
    )
    os.chmod(path, stat.S_IRWXU)
    return path


class DevpackDispatchTests(unittest.TestCase):
    """Test suite for dispatch ok/fail/scope/data-class/sandbox/JSON line and orchestrator configs."""

    def _run_dispatch(self, argv: list[str], env_extra: dict[str, str] | None = None) -> tuple[int, str, str]:
        """Run devpack_dispatch.main capturing stdout and stderr."""
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        old_env = os.environ.copy()
        try:
            if env_extra:
                os.environ.update(env_extra)
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                rc = devpack_dispatch.main(argv)
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def test_dispatch_ok_and_json_line_shape(self) -> None:
        """Verify dispatch runs agent, gate, scope, test_lock, injects checklist, writes run dir, and prints one JSON line."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _init_git_repo(wt)
            brief = base / "brief.md"
            brief.write_text("# Brief\nUpdate oneValue() to return 2.\n", encoding="utf-8")

            long_text = "X" * 1200
            fake_agent = _write_fake_agent(
                base / "fake_claude.py",
                "pathlib.Path('src/mod.cpp').write_text("
                "'/**\\n * @file mod.cpp\\n * @brief Sample module.\\n */\\n"
                "/**\\n * @brief Return two.\\n * @return Constant 2.\\n */\\n"
                "int oneValue() { return 2; }\\n', encoding='utf-8')\n"
                f"print(json.dumps({{'type': 'result', 'subtype': 'success', 'is_error': False, "
                f"'total_cost_usd': 0.0123, 'num_turns': 2, 'duration_ms': 200, "
                f"'usage': {{'input_tokens': 30, 'output_tokens': 15}}, 'result': {long_text!r}}}))\n",
            )
            fake_gate = _write_fake_agent(base / "gate.py", "print('GATE_OK')\n")
            runs_root = base / "runs"

            rc, stdout, _stderr = self._run_dispatch(
                [
                    "--engine", "haiku",
                    "--brief", str(brief),
                    "--workdir", str(wt),
                    "--allow", "src/**",
                    "--gate-cmd", f"python3 {fake_gate}",
                    "--runs-root", str(runs_root),
                    "--agent-bin", str(fake_agent),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                ]
            )
            self.assertEqual(0, rc)
            lines = [ln for ln in stdout.strip().splitlines() if ln.strip()]
            self.assertEqual(1, len(lines), f"Expected exactly 1 JSON line, got: {stdout}")
            data = json.loads(lines[0])
            for key in ("run_dir", "status", "gate", "scope", "changed_files", "final_text_tail", "cost_usd"):
                self.assertIn(key, data)
            self.assertEqual("ok", data["status"])
            self.assertEqual("ok", data["gate"])
            self.assertEqual("ok", data["scope"])
            self.assertEqual(["src/mod.cpp"], data["changed_files"])
            self.assertEqual(800, len(data["final_text_tail"]))
            self.assertAlmostEqual(0.0123, data["cost_usd"], places=6)

            rdir = Path(data["run_dir"])
            self.assertTrue((rdir / "run.json").is_file())
            self.assertTrue((rdir / "transcript.md").is_file())
            self.assertTrue((rdir / "index.html").is_file())
            self.assertTrue((rdir / "steps" / "02-gate.txt").is_file())
            prompt_saved = (rdir / "steps" / "01-worker-haiku.prompt.md").read_text(encoding="utf-8")
            self.assertIn("Code Quality Checklist", prompt_saved)

    def test_dispatch_gate_fail_and_preflight_lint_fail(self) -> None:
        """Verify failing gate_cmd or preflight_lint sets gate='fail' and exits 1."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _init_git_repo(wt)
            brief = base / "brief.md"
            brief.write_text("# Brief\nIntroduce lint issue.\n", encoding="utf-8")

            # Agent introduces C-style cast + missing Doxygen
            fake_agent = _write_fake_agent(
                base / "fake_claude.py",
                "pathlib.Path('src/mod.cpp').write_text('int oneValue() { return (int)2.0; }\\n', encoding='utf-8')\n"
                "print(json.dumps({'type': 'result', 'is_error': False, 'total_cost_usd': 0.01, 'num_turns': 1, 'result': 'done'}))\n",
            )
            fake_gate = _write_fake_agent(base / "gate.py", "print('GATE_OK')\n")
            rc, stdout, _ = self._run_dispatch(
                [
                    "--engine", "haiku",
                    "--brief", str(brief),
                    "--workdir", str(wt),
                    "--allow", "src/**",
                    "--gate-cmd", f"python3 {fake_gate}",
                    "--runs-root", str(base / "runs"),
                    "--agent-bin", str(fake_agent),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                ]
            )
            self.assertEqual(1, rc)
            data = json.loads(stdout.strip())
            self.assertEqual("ok", data["status"])
            self.assertEqual("fail", data["gate"])
            self.assertGreater(data["lint_findings"], 0)
            gate_txt = (Path(data["run_dir"]) / "steps" / "02-gate.txt").read_text(encoding="utf-8")
            self.assertIn("MISRA-8.2.2", gate_txt)

    def test_dispatch_scope_violation(self) -> None:
        """Verify editing outside --allow sets scope='violation' and exits 1."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _init_git_repo(wt)
            brief = base / "brief.md"
            brief.write_text("# Brief\nEdit out of scope.\n", encoding="utf-8")

            fake_agent = _write_fake_agent(
                base / "fake_claude.py",
                "pathlib.Path('README_EXTRA.md').write_text('oops\\n', encoding='utf-8')\n"
                "print(json.dumps({'type': 'result', 'is_error': False, 'total_cost_usd': 0.01, 'num_turns': 1, 'result': 'done'}))\n",
            )
            fake_gate = _write_fake_agent(base / "gate.py", "print('GATE_OK')\n")
            rc, stdout, _ = self._run_dispatch(
                [
                    "--engine", "haiku",
                    "--brief", str(brief),
                    "--workdir", str(wt),
                    "--allow", "src/**",
                    "--gate-cmd", f"python3 {fake_gate}",
                    "--runs-root", str(base / "runs"),
                    "--agent-bin", str(fake_agent),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                ]
            )
            self.assertEqual(1, rc)
            data = json.loads(stdout.strip())
            self.assertEqual("violation", data["scope"])
            self.assertIn("README_EXTRA.md", data["changed_files"])

    def test_dispatch_data_class_refusal_and_local_cost_null(self) -> None:
        """Verify confidential project refuses cloud engine, allows local engine, and reports local cost_usd=null."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _init_git_repo(wt)
            (wt / ".devpack").mkdir()
            (wt / ".devpack" / "project.toml").write_text(
                'data_class = "confidential"\nallow_unsandboxed_local = true\n',
                encoding="utf-8",
            )
            brief = base / "brief.md"
            brief.write_text("# Brief\nTest data class.\n", encoding="utf-8")
            marker = base / "invoked.txt"
            fake_agent = _write_fake_agent(
                base / "fake_claude.py",
                f"pathlib.Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
                "print(json.dumps({'type': 'result', 'is_error': False, 'total_cost_usd': 0.1974, 'num_turns': 1, 'usage': {'input_tokens': 100, 'output_tokens': 40}, 'result': 'done'}))\n",
            )
            fake_gate = _write_fake_agent(base / "gate.py", "print('GATE_OK')\n")

            # Cloud engine 'haiku' (max_data_class=internal) must be refused on confidential repo
            rc_cloud, out_cloud, err_cloud = self._run_dispatch(
                [
                    "--engine", "haiku",
                    "--brief", str(brief),
                    "--workdir", str(wt),
                    "--gate-cmd", f"python3 {fake_gate}",
                    "--runs-root", str(base / "runs"),
                    "--agent-bin", str(fake_agent),
                    "--isolation", "local",
                ]
            )
            self.assertEqual(1, rc_cloud)
            self.assertFalse(marker.exists())
            self.assertIn("confidential", err_cloud)
            cloud_data = json.loads(out_cloud.strip())
            self.assertEqual("refused", cloud_data["status"])

            # Local engine (max_data_class=confidential) succeeds and cost_usd is null
            rc_local, out_local, _ = self._run_dispatch(
                [
                    "--engine", "local",
                    "--brief", str(brief),
                    "--workdir", str(wt),
                    "--gate-cmd", f"python3 {fake_gate}",
                    "--runs-root", str(base / "runs"),
                    "--agent-bin", str(fake_agent),
                    "--isolation", "local",
                ],
                env_extra={
                    "DEVPACK_LOCAL_BASE_URL": "http://127.0.0.1:11434",
                    "DEVPACK_LOCAL_AUTH_TOKEN": "LOCAL_TEST_TOKEN",
                },
            )
            self.assertEqual(0, rc_local)
            self.assertTrue(marker.exists())
            local_data = json.loads(out_local.strip())
            self.assertEqual("ok", local_data["status"])
            self.assertIsNone(local_data["cost_usd"])
            self.assertEqual(100, local_data["tokens_in"])
            self.assertEqual(40, local_data["tokens_out"])

    def test_sandbox_defaults_and_refusals_across_dispatch_and_skilltest(self) -> None:
        """Verify dispatch and skilltest default to docker and refuse --isolation local without allow flag."""
        disp_args = devpack_dispatch.build_arg_parser().parse_args(
            ["--engine", "haiku", "--brief", "b.md", "--workdir", "."]
        )
        self.assertEqual("docker", disp_args.isolation)
        self.assertFalse(disp_args.allow_unsandboxed_local)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _init_git_repo(wt)
            brief = base / "brief.md"
            brief.write_text("# Brief\nCheck sandbox.\n", encoding="utf-8")
            rc, out, err = self._run_dispatch(
                [
                    "--engine", "haiku",
                    "--brief", str(brief),
                    "--workdir", str(wt),
                    "--runs-root", str(base / "runs"),
                    "--isolation", "local",
                ]
            )
            self.assertEqual(1, rc)
            self.assertIn("--allow-unsandboxed-local", err)
            self.assertEqual("refused", json.loads(out.strip())["status"])

            # skilltest.py defaults to docker and refuses --isolation local without flag
            st_rc = skilltest.main(
                [
                    "run",
                    str(REPO_ROOT / "knowledge" / "skills" / "doxygen-naming"),
                    "--engines", "haiku",
                    "--k", "1",
                    "--isolation", "local",
                    "--results-root", str(base / "st_res"),
                ]
            )
            self.assertEqual(1, st_rc)

        # Confirm skilltest parser defaults to docker
        proc = subprocess.run(
            ["python3", str(REPO_ROOT / "skilltest" / "skilltest.py"), "run", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, proc.returncode)
        self.assertIn("--allow-unsandboxed-local", proc.stdout)

    def test_broker_response_contains_rendered_transcript(self) -> None:
        """The allowed path submits to the broker and returns readable worker logs."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workdir = base / "work"
            _init_git_repo(workdir)
            (workdir / "briefs").mkdir()
            (workdir / "briefs/job.md").write_text("Inspect the project.\n", encoding="utf-8")
            worker = _write_fake_agent(
                base / "worker.py",
                "print(json.dumps({'type': 'result', 'is_error': False, 'result': 'broker worker done'}))\n",
            )
            broker = Broker(
                workdir, base / "runs", isolation="local", allow_unsandboxed_local=True,
                agent_bin=str(worker), gate_cmd="true",
            )
            try:
                self.assertEqual(CLIENT_SOURCE.read_bytes(), broker.client_path.read_bytes())
                self.assertEqual([broker.client_path], [
                    path for path in (workdir / ORCHESTRATOR_PACK_PATH).rglob("*") if path.is_file()
                ])
                request_id = "a" * 32
                request = {"engine": "haiku", "brief": "briefs/job.md", "allow": ["src/**"]}
                (broker.queue / f"{request_id}.request.json").write_text(json.dumps(request), encoding="utf-8")
                broker.poll()
                response = json.loads((broker.queue / f"{request_id}.response.json").read_text(encoding="utf-8"))
                self.assertEqual("ok", response["status"], response)
                self.assertIn("run_dir", response)
                transcript = Path(response["run_dir"]) / "transcript.md"
                self.assertTrue(transcript.is_file())
                self.assertIn("broker worker done", transcript.read_text(encoding="utf-8"))
            finally:
                broker.close()

    def test_orchestrator_settings_and_checklist_constraints(self) -> None:
        """Verify orchestrator settings.json is valid JSON denying product edits, CLAUDE.md <= 80 lines, checklist <= 25 lines."""
        settings_path = REPO_ROOT / "orchestrator" / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        perms = settings["permissions"]
        allow = perms["allow"]
        deny = perms["deny"]

        self.assertIn("Read", allow)
        self.assertIn("Glob", allow)
        self.assertIn("Grep", allow)
        self.assertIn("Edit(briefs/**)", allow)
        self.assertIn("Write(briefs/**)", allow)
        self.assertNotIn("Edit(runs/**)", allow)
        self.assertNotIn("Write(runs/**)", allow)
        client_path = ORCHESTRATOR_PACK_PATH / "runner/devpack_dispatch.py"
        client_command = f"python3 {client_path}"
        self.assertEqual(client_path, CLIENT_RELATIVE)
        self.assertIn(f"Bash({client_command} --submit:*)", allow)
        for permission in allow:
            self.assertNotIn("devpack_logs.py", permission)
            if permission.startswith("Bash(") and "devpack_dispatch.py" in permission:
                self.assertEqual(f"Bash({client_command} --submit:*)", permission)
        for host_script in ("devpack_dispatch.py", "devpack_logs.py"):
            self.assertNotIn(f"Bash(python3 runner/{host_script} *)", allow)

        for disallowed in ("Edit", "Write"):
            self.assertNotIn(disallowed, allow)
        for protected in ("src/**", "include/**", "tests/**", "runner/**", "enforcement/**"):
            self.assertIn(f"Edit({protected})", deny)
            self.assertIn(f"Write({protected})", deny)

        claude_lines = (REPO_ROOT / "orchestrator" / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(claude_lines), 80)
        self.assertIn(client_command, "\n".join(claude_lines))

        checklist_lines = (REPO_ROOT / "runner" / "quality_checklist.md").read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(checklist_lines), 25)


if __name__ == "__main__":
    unittest.main()
