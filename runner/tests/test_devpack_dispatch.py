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
from runner.tests.brief_fixtures import valid_brief  # noqa: E402


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


def _write_fake_cppcheck(path: Path) -> Path:
    """Write a fake cppcheck that logs its suppression list contents and reports no findings."""
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "entry = {'argv': sys.argv[1:], 'suppressions_text': None}\n"
        "for arg in sys.argv[1:]:\n"
        "    if arg.startswith('--suppressions-list='):\n"
        "        entry['suppressions_text'] = pathlib.Path(arg.split('=', 1)[1]).read_text(encoding='utf-8')\n"
        "with open(os.environ['FAKE_CPPCHECK_LOG'], 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(entry) + '\\n')\n"
        "sys.stderr.write('<?xml version=\"1.0\"?>\\n<results version=\"2\">\\n"
        "<cppcheck version=\"2.0\"/>\\n<errors>\\n</errors>\\n</results>\\n')\n",
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

    def test_dispatch_shared_comment_aware_lock(self) -> None:
        """Dispatch enforces strict test_lock (rejects assertion and comment edits on protected tests)."""
        for replacement, expected in [("1 == 2", 1), ("1 == 1", 1)]:
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                wt = base / "wt"
                _init_git_repo(wt)
                brief = base / "brief.md"
                brief.write_text("Update protected test.\n", encoding="utf-8")
                documentation = (
                    "/**\n * @file test_mod.cpp\n * @brief Sample tests.\n */\n"
                    "#include <cassert>\n/**\n * @brief Verify one.\n */\n"
                )
                test_path = wt / "tests/test_mod.cpp"
                test_path.write_text(documentation + "void testOne() { assert(1 == 1); }\n")
                subprocess.run(["git", "commit", "-qam", "document tests"], cwd=wt, check=True)
                content = documentation + f"void testOne() {{ assert({replacement}); }} // updated\n"
                agent = _write_fake_agent(
                    base / "agent.py",
                    f"pathlib.Path('tests/test_mod.cpp').write_text({content!r})\n"
                    "print(json.dumps({'type': 'result', 'is_error': False, 'result': 'done'}))\n",
                )
                rc, stdout, stderr = self._run_dispatch([
                    "--engine", "haiku", "--brief", str(brief), "--workdir", str(wt),
                    "--allow", "tests/**", "--gate-cmd", "true",
                    "--runs-root", str(base / "runs"), "--agent-bin", str(agent),
                    "--isolation", "local", "--allow-unsandboxed-local",
                ])
                self.assertEqual(rc, expected, stderr + stdout)
                result = json.loads(stdout)
                self.assertEqual(result["test_lock"], "fail" if expected else "ok")
                self.assertEqual(result["gate"], "fail" if expected else "ok")

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
                'data_class = "confidential"\n',
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
                    "--allow-unsandboxed-local",
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
                    "--allow-unsandboxed-local",
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
            (workdir / "briefs/job.md").write_text(valid_brief("Inspect the project.", ["src/**"]), encoding="utf-8")
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

    def test_qualification_model_change_refusal_and_deviation_override(self) -> None:
        """Verify unqualified/model-changed engines refuse fail-closed and --allow-unqualified requires --deviation."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _init_git_repo(wt)
            brief = base / "brief.md"
            brief.write_text("# Brief\nUpdate oneValue.\n", encoding="utf-8")
            marker = base / "agent_invoked.txt"
            model_file = base / "served_model.txt"
            model_file.write_text("qwen3-v1", encoding="utf-8")
            fake_agent = _write_fake_agent(
                base / "fake_claude.py",
                f"pathlib.Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
                f"model_id = pathlib.Path({str(model_file)!r}).read_text(encoding='utf-8').strip()\n"
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': model_id}))\n"
                "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1, 'result': 'done'}))\n",
            )
            qual_path = wt / ".devpack" / "qualification.json"
            qual_path.parent.mkdir(parents=True, exist_ok=True)
            eng_cfg = devpack_dispatch.resolve_engine("local")
            cfg_hash = devpack_dispatch.compute_engine_config_hash(eng_cfg)
            qual_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "engines": {
                            "local": {
                                "engine": "local",
                                "status": "qualified",
                                "model_id": "qwen3-v1",
                                "model_digest": "",
                                "config_hash": cfg_hash,
                                "date": "2026-11-23T00:00:00+00:00",
                                "eval_summary_path": "eval/results/summary.md",
                            }
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            env_local = {
                "DEVPACK_LOCAL_BASE_URL": "http://127.0.0.1:11434",
                "DEVPACK_LOCAL_AUTH_TOKEN": "LOCAL_TOK",
            }
            runs_root = base / "runs"

            # 1. --allow-unqualified without --deviation -> exit 2
            rc_bad_cli, _, err_bad_cli = self._run_dispatch(
                [
                    "--engine", "local", "--brief", str(brief), "--workdir", str(wt),
                    "--allow-unqualified", "--runs-root", str(runs_root),
                ],
                env_extra=env_local,
            )
            self.assertEqual(2, rc_bad_cli)
            self.assertIn("--deviation", err_bad_cli)

            # 2. Matching init model passes
            rc_ok, out_ok, _ = self._run_dispatch(
                [
                    "--engine", "local", "--brief", str(brief), "--workdir", str(wt),
                    "--gate-cmd", "true", "--runs-root", str(runs_root),
                    "--agent-bin", str(fake_agent), "--isolation", "local", "--allow-unsandboxed-local",
                ],
                env_extra=env_local,
            )
            self.assertEqual(0, rc_ok)
            self.assertEqual("qwen3-v1", json.loads(out_ok.strip())["served_model_id"])

            # 3. Served model changes in init event -> marks unqualified and refuses!
            model_file.write_text("qwen3-v2", encoding="utf-8")
            rc_chg, out_chg, err_chg = self._run_dispatch(
                [
                    "--engine", "local", "--brief", str(brief), "--workdir", str(wt),
                    "--gate-cmd", "true", "--runs-root", str(runs_root),
                    "--agent-bin", str(fake_agent), "--isolation", "local", "--allow-unsandboxed-local",
                ],
                env_extra=env_local,
            )
            self.assertEqual(1, rc_chg)
            self.assertIn("served model changed", err_chg)
            chg_data = json.loads(out_chg.strip())
            self.assertEqual("refused", chg_data["status"])
            self.assertTrue(chg_data["model_changed"])
            qual_after = json.loads(qual_path.read_text(encoding="utf-8"))
            self.assertEqual("unqualified", qual_after["engines"]["local"]["status"])

            # 4. Subsequent dispatch refuses BEFORE launching agent
            marker.unlink(missing_ok=True)
            rc_ref, out_ref, err_ref = self._run_dispatch(
                [
                    "--engine", "local", "--brief", str(brief), "--workdir", str(wt),
                    "--gate-cmd", "true", "--runs-root", str(runs_root),
                    "--agent-bin", str(fake_agent), "--isolation", "local", "--allow-unsandboxed-local",
                ],
                env_extra=env_local,
            )
            self.assertEqual(1, rc_ref)
            self.assertFalse(marker.exists())
            self.assertIn("unqualified", err_ref)
            self.assertEqual("refused", json.loads(out_ref.strip())["status"])

            # 5. --allow-unqualified --deviation DEV-007 overrides and logs deviation
            rc_dev, out_dev, err_dev = self._run_dispatch(
                [
                    "--engine", "local", "--brief", str(brief), "--workdir", str(wt),
                    "--gate-cmd", "true", "--runs-root", str(runs_root),
                    "--agent-bin", str(fake_agent), "--isolation", "local", "--allow-unsandboxed-local",
                    "--allow-unqualified", "--deviation", "DEV-007",
                ],
                env_extra=env_local,
            )
            self.assertEqual(0, rc_dev)
            self.assertTrue(marker.exists())
            self.assertIn("DEV-007", err_dev)
            dev_data = json.loads(out_dev.strip())
            self.assertEqual("DEV-007", dev_data["deviation_id"])
            self.assertTrue(dev_data["allow_unqualified"])
            run_meta = json.loads((Path(dev_data["run_dir"]) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual("DEV-007", run_meta["deviation_id"])

    def test_dispatch_cppcheck_blocking_finding_fails_gate_and_records_static_metrics(self) -> None:
        """Verify blocking cppcheck finding fails dispatch gate and records static metrics in payload and run.json."""
        import shutil

        if shutil.which("cppcheck") is None:
            self.skipTest("cppcheck is not installed on PATH")

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _init_git_repo(wt)
            brief = base / "brief.md"
            brief.write_text("# Brief\nUpdate oneValue().\n", encoding="utf-8")

            bad_cpp = (
                "/**\n * @file mod.cpp\n * @brief Sample module.\n */\n"
                "/**\n * @brief Bad function.\n * @param[in] flag Input flag.\n * @return Value.\n */\n"
                "int oneValue(int flag) {\n"
                "    int *missingPtr = nullptr;\n"
                "    if (flag > 0) {\n"
                "        return *missingPtr;\n"
                "    }\n"
                "    return 0;\n"
                "}\n"
            )
            fake_agent = _write_fake_agent(
                base / "fake_claude.py",
                f"pathlib.Path('src/mod.cpp').write_text({bad_cpp!r}, encoding='utf-8')\n"
                "print(json.dumps({'type': 'result', 'is_error': False, 'total_cost_usd': 0.01, 'num_turns': 1, 'result': 'done'}))\n",
            )
            rc, stdout, _ = self._run_dispatch(
                [
                    "--engine", "haiku",
                    "--brief", str(brief),
                    "--workdir", str(wt),
                    "--allow", "src/**",
                    "--gate-cmd", "true",
                    "--runs-root", str(base / "runs"),
                    "--agent-bin", str(fake_agent),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                ]
            )
            self.assertEqual(1, rc)
            data = json.loads(stdout.strip())
            self.assertEqual("fail", data["gate"])
            self.assertGreater(data["static_findings"], 0)
            self.assertFalse(data["static_clean"])
            rdir = Path(data["run_dir"])
            gate_txt = (rdir / "steps" / "02-gate.txt").read_text(encoding="utf-8")
            self.assertIn("[cppcheck]", gate_txt)
            self.assertIn("nullPointer", gate_txt)
            run_meta = json.loads((rdir / "run.json").read_text(encoding="utf-8"))
            self.assertGreater(run_meta["static_findings_first_attempt"], 0)
            self.assertFalse(run_meta["static_clean_final"])

    def test_worker_edit_to_preflight_toml_is_ignored_and_flagged_gate_touched(self) -> None:
        """Worker editing preflight.toml to weaken fail_on still fails gate on known finding, flags gate-touched, and records config source+hash."""
        import hashlib
        import shutil

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _init_git_repo(wt)
            orig_toml = (
                '[general]\nstd = "c++17"\n\n'
                '[cppcheck]\nenable = ["warning", "style", "performance", "portability"]\n'
                'fail_on = ["error", "warning"]\n'
            )
            (wt / "preflight.toml").write_text(orig_toml, encoding="utf-8")
            subprocess.run(["git", "add", "preflight.toml"], cwd=wt, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "set cppcheck fail_on"], cwd=wt, check=True)
            expected_sha = hashlib.sha256(orig_toml.encode("utf-8")).hexdigest()

            brief = base / "brief.md"
            brief.write_text("# Brief\nWeaken config and introduce defect.\n", encoding="utf-8")

            bad_cpp = (
                "/**\n * @file mod.cpp\n * @brief Sample module.\n */\n"
                "/**\n * @brief Bad function.\n * @param[in] flag Input flag.\n * @return Value.\n */\n"
                "int oneValue(int flag) {\n"
                "    int *missingPtr = nullptr;\n"
                "    if (flag > 0) {\n"
                "        return *missingPtr;\n"
                "    }\n"
                "    return (int)flag;\n"
                "}\n"
            )
            evil_toml = (
                '[general]\nstd = "c++17"\ndisabled_rules = ["MISRA-8.2.2"]\n\n'
                '[cppcheck]\nfail_on = []\nextra_args = ["--addon=x.py"]\n'
            )
            # Even with --allow "**", modifying preflight.toml must be a gate-touched scope violation
            fake_agent = _write_fake_agent(
                base / "fake_claude.py",
                f"pathlib.Path('preflight.toml').write_text({evil_toml!r}, encoding='utf-8')\n"
                f"pathlib.Path('src/mod.cpp').write_text({bad_cpp!r}, encoding='utf-8')\n"
                "print(json.dumps({'type': 'result', 'is_error': False, 'total_cost_usd': 0.01, 'num_turns': 1, 'result': 'done'}))\n",
            )
            rc, stdout, _ = self._run_dispatch(
                [
                    "--engine", "haiku",
                    "--brief", str(brief),
                    "--workdir", str(wt),
                    "--allow", "**",
                    "--gate-cmd", "true",
                    "--runs-root", str(base / "runs"),
                    "--agent-bin", str(fake_agent),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                ]
            )
            self.assertEqual(1, rc)
            data = json.loads(stdout.strip())
            self.assertEqual("violation", data["scope"])
            self.assertEqual("fail", data["gate"])
            self.assertIn("preflight.toml", data["gate_touched"])
            self.assertIn("gate-touched", data["scope_detail"])
            self.assertGreater(data["lint_findings"], 0)
            self.assertEqual("base:preflight.toml", data["gate_config_source"])
            self.assertEqual(expected_sha, data["gate_config_sha256"])

            rdir = Path(data["run_dir"])
            gate_txt = (rdir / "steps" / "02-gate.txt").read_text(encoding="utf-8")
            self.assertIn("MISRA-8.2.2", gate_txt)
            self.assertIn("gate-touched", gate_txt)
            if shutil.which("cppcheck") is not None:
                self.assertGreater(data["static_findings"], 0)
                self.assertIn("nullPointer", gate_txt)

            run_meta = json.loads((rdir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual("base:preflight.toml", run_meta["gate_config_source"])
            self.assertEqual(expected_sha, run_meta["gate_config_sha256"])
            self.assertIn("preflight.toml", run_meta["gate_touched"])

    def test_repo_without_preflight_uses_trusted_project_or_pack_config_and_blocks_worker_created_toml(self) -> None:
        """When repo has no preflight.toml, gate uses project/pack config and ignores worker-created preflight.toml."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            (wt / "src").mkdir(parents=True)
            (wt / "tests").mkdir(parents=True)
            (wt / "src" / "mod.cpp").write_text(
                "/**\n * @file mod.cpp\n * @brief Sample module.\n */\n"
                "/**\n * @brief Return one.\n * @return Constant 1.\n */\n"
                "int oneValue() { return 1; }\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=wt, check=True)
            subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=wt, check=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=wt, check=True)
            subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init without preflight"], cwd=wt, check=True)

            ext_proj = base / "trusted_project.toml"
            ext_proj.write_text(
                'data_class = "internal"\nallow_unsandboxed_local = true\n'
                '[general]\nstd = "c++17"\n'
                '[cppcheck]\nfail_on = ["error", "warning"]\n',
                encoding="utf-8",
            )
            brief = base / "brief.md"
            brief.write_text("# Brief\nTry creating preflight.toml.\n", encoding="utf-8")

            fake_agent = _write_fake_agent(
                base / "fake_claude.py",
                "pathlib.Path('preflight.toml').write_text('[general]\\ndisabled_rules = [\"MISRA-8.2.2\"]\\n', encoding='utf-8')\n"
                "pathlib.Path('src/mod.cpp').write_text('int oneValue() { return (int)2.0; }\\n', encoding='utf-8')\n"
                "print(json.dumps({'type': 'result', 'is_error': False, 'result': 'done'}))\n",
            )
            rc, stdout, _ = self._run_dispatch(
                [
                    "--engine", "haiku",
                    "--brief", str(brief),
                    "--workdir", str(wt),
                    "--project-config", str(ext_proj),
                    "--allow", "**",
                    "--gate-cmd", "true",
                    "--runs-root", str(base / "runs"),
                    "--agent-bin", str(fake_agent),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                ]
            )
            self.assertEqual(1, rc)
            data = json.loads(stdout.strip())
            self.assertEqual("project_config", data["gate_config_source"])
            self.assertEqual("violation", data["scope"])
            self.assertIn("preflight.toml", data["gate_touched"])
            self.assertGreater(data["lint_findings"], 0)
            run_meta = json.loads((Path(data["run_dir"]) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual("project_config", run_meta["gate_config_source"])
            self.assertEqual(64, len(run_meta["gate_config_sha256"]))


    def test_dot_prefixed_protected_paths_are_not_mangled_by_normalisation(self) -> None:
        """Only a literal leading './' is removed, so '.cppcheck-suppressions' stays protected by its real name."""
        from enforcement.checks.scope_check import is_gate_protected_path, normalize_rel_posix_path

        self.assertEqual(".cppcheck-suppressions", normalize_rel_posix_path("./.cppcheck-suppressions"))
        self.assertEqual(".clang-tidy", normalize_rel_posix_path("././.clang-tidy"))
        self.assertEqual("a/b", normalize_rel_posix_path(".//a/./b"))
        self.assertEqual("", normalize_rel_posix_path("./"))
        refs = devpack_dispatch._extract_referenced_config_paths(
            b'[cppcheck]\nsuppressions_file = "./.cppcheck-suppressions"\n'
        )
        self.assertEqual((".cppcheck-suppressions",), refs)
        self.assertTrue(is_gate_protected_path(".cppcheck-suppressions", refs))

    def test_worker_edits_to_dot_named_gate_files_are_gate_touched(self) -> None:
        """With allow '**', a worker editing .cppcheck-suppressions or .clang-tidy is flagged gate-touched."""
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _init_git_repo(wt)
            base_toml = '[cppcheck]\nsuppressions_file = "./.cppcheck-suppressions"\n'
            (wt / "preflight.toml").write_text(base_toml, encoding="utf-8")
            (wt / ".cppcheck-suppressions").write_text("# trusted list\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "trusted suppressions"], cwd=wt, check=True)
            expected_sha = hashlib.sha256(b"# trusted list\n").hexdigest()

            brief = base / "brief.md"
            brief.write_text("# Brief\nHide a defect by editing suppressions.\n", encoding="utf-8")
            fake_agent = _write_fake_agent(
                base / "fake_claude.py",
                "pathlib.Path('.cppcheck-suppressions').write_text('*:src/mod.cpp\\n', encoding='utf-8')\n"
                "pathlib.Path('.clang-tidy').write_text('Checks: \"*\"\\n', encoding='utf-8')\n"
                "print(json.dumps({'type': 'result', 'is_error': False, 'result': 'done'}))\n",
            )
            rc, stdout, _ = self._run_dispatch(
                [
                    "--engine", "haiku",
                    "--brief", str(brief),
                    "--workdir", str(wt),
                    "--allow", "**",
                    "--gate-cmd", "true",
                    "--runs-root", str(base / "runs"),
                    "--agent-bin", str(fake_agent),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                ]
            )
            self.assertEqual(1, rc)
            data = json.loads(stdout.strip())
            self.assertEqual("violation", data["scope"])
            self.assertIn(".cppcheck-suppressions", data["gate_touched"])
            self.assertIn(".clang-tidy", data["gate_touched"])
            run_meta = json.loads((Path(data["run_dir"]) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(".cppcheck-suppressions", run_meta["gate_suppressions_path"])
            self.assertEqual(expected_sha, run_meta["gate_suppressions_sha256"])

    def test_trusted_config_snapshots_sibling_suppressions_from_base_commit(self) -> None:
        """The suppression file named by the trusted config is copied beside the snapshot from the base commit."""
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _init_git_repo(wt)
            (wt / "cfg").mkdir()
            (wt / "preflight.toml").write_text('[cppcheck]\nsuppressions_file = "cfg/suppr.txt"\n', encoding="utf-8")
            (wt / "cfg" / "suppr.txt").write_text("unusedFunction:src/legacy.cpp\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "suppressions"], cwd=wt, check=True)
            (wt / "cfg" / "suppr.txt").write_text("nullPointer:src/mod.cpp\n", encoding="utf-8")

            with tempfile.TemporaryDirectory() as snap:
                trusted = devpack_dispatch.resolve_trusted_gate_config(wt, Path(snap), base_ref="HEAD")
                snapshot_copy = Path(snap) / "cfg" / "suppr.txt"
                self.assertEqual(b"unusedFunction:src/legacy.cpp\n", snapshot_copy.read_bytes())
                self.assertEqual("cfg/suppr.txt", trusted.suppressions_rel_path)
                self.assertEqual(
                    hashlib.sha256(b"unusedFunction:src/legacy.cpp\n").hexdigest(),
                    trusted.suppressions_sha256,
                )
                self.assertIn("cfg/suppr.txt", trusted.protected_rel_paths)

    def _make_devpack_repo(self, wt: Path, committed: dict[str, str]) -> None:
        """Create a clean repo whose only config is ``.devpack/project.toml``, plus the committed ``committed`` files."""
        _init_git_repo(wt)
        (wt / "preflight.toml").unlink()
        (wt / ".devpack").mkdir()
        (wt / ".devpack" / "project.toml").write_text(
            '[cppcheck]\nsuppressions_file = "cfg/suppr.txt"\n', encoding="utf-8"
        )
        for rel, text in committed.items():
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(text, encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "devpack config"], cwd=wt, check=True)

    def test_root_level_suppressions_found_for_devpack_config(self) -> None:
        """Config in .devpack/ naming cfg/suppr.txt snapshots the committed root-level file, not the working tree."""
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "wt"
            self._make_devpack_repo(wt, {"cfg/suppr.txt": "unusedFunction:src/legacy.cpp\n"})
            (wt / "cfg" / "suppr.txt").write_text("nullPointer:src/mod.cpp\n", encoding="utf-8")

            with tempfile.TemporaryDirectory() as snap:
                trusted = devpack_dispatch.resolve_trusted_gate_config(wt, Path(snap), base_ref="HEAD")
                self.assertEqual(b"unusedFunction:src/legacy.cpp\n", (Path(snap) / "cfg" / "suppr.txt").read_bytes())
                self.assertEqual("cfg/suppr.txt", trusted.suppressions_rel_path)
                self.assertEqual(
                    hashlib.sha256(b"unusedFunction:src/legacy.cpp\n").hexdigest(),
                    trusted.suppressions_sha256,
                )

    def test_config_relative_sibling_suppressions_still_resolve(self) -> None:
        """A suppression file beside the config (no root-level copy) is snapshotted from the config directory."""
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "wt"
            self._make_devpack_repo(wt, {".devpack/suppr.txt": "unusedFunction:src/legacy.cpp\n"})
            (wt / ".devpack" / "project.toml").write_text(
                '[cppcheck]\nsuppressions_file = "suppr.txt"\n', encoding="utf-8"
            )
            subprocess.run(["git", "commit", "-q", "-am", "point at sibling"], cwd=wt, check=True)

            with tempfile.TemporaryDirectory() as snap:
                trusted = devpack_dispatch.resolve_trusted_gate_config(wt, Path(snap), base_ref="HEAD")
                self.assertEqual(b"unusedFunction:src/legacy.cpp\n", (Path(snap) / "suppr.txt").read_bytes())
                self.assertEqual("suppr.txt", trusted.suppressions_rel_path)
                self.assertIsNotNone(trusted.suppressions_sha256)

    def test_repo_root_copy_wins_over_config_sibling(self) -> None:
        """When both the repo root and the config directory hold the file, the repo-root copy is snapshotted."""
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "wt"
            self._make_devpack_repo(
                wt,
                {"cfg/suppr.txt": "root-copy\n", ".devpack/cfg/suppr.txt": "config-copy\n"},
            )

            with tempfile.TemporaryDirectory() as snap:
                trusted = devpack_dispatch.resolve_trusted_gate_config(wt, Path(snap), base_ref="HEAD")
                self.assertEqual(b"root-copy\n", (Path(snap) / "cfg" / "suppr.txt").read_bytes())
                self.assertEqual("cfg/suppr.txt", trusted.suppressions_rel_path)

    def test_untracked_working_tree_suppressions_are_never_snapshotted(self) -> None:
        """A suppression file that exists only in the working tree is not trusted: no copy, no hash."""
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "wt"
            self._make_devpack_repo(wt, {})
            (wt / "cfg").mkdir()
            (wt / "cfg" / "suppr.txt").write_text("nullPointer:src/mod.cpp\n", encoding="utf-8")

            with tempfile.TemporaryDirectory() as snap:
                trusted = devpack_dispatch.resolve_trusted_gate_config(wt, Path(snap), base_ref="HEAD")
                self.assertFalse((Path(snap) / "cfg" / "suppr.txt").exists())
                self.assertIsNone(trusted.suppressions_sha256)
                self.assertEqual("cfg/suppr.txt", trusted.suppressions_rel_path)

    def _run_trusted_suppressions_job(
        self,
        base: Path,
        *,
        tamper: bool,
        cfg_rel: str = "preflight.toml",
        suppr_rel: str = "suppressions.txt",
    ) -> tuple[dict, list[dict], str]:
        """Dispatch a job whose trusted config names a suppression file; return (payload, cppcheck calls, trusted text)."""
        wt = base / "wt"
        _init_git_repo(wt)
        if cfg_rel != "preflight.toml":
            (wt / "preflight.toml").unlink()
        trusted_list = "unusedFunction:src/legacy.cpp\n"
        (wt / cfg_rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / cfg_rel).write_text(f'[cppcheck]\nsuppressions_file = "{suppr_rel}"\n', encoding="utf-8")
        (wt / suppr_rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / suppr_rel).write_text(trusted_list, encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "trusted suppressions"], cwd=wt, check=True)

        fake_bin = base / "fakebin"
        fake_bin.mkdir()
        cpp_log = base / "cppcheck_calls.jsonl"
        _write_fake_cppcheck(fake_bin / "cppcheck")
        brief = base / "brief.md"
        brief.write_text("# Brief\nTouch the module.\n", encoding="utf-8")
        good_cpp = (
            "/**\n * @file mod.cpp\n * @brief Sample module.\n */\n"
            "/**\n * @brief Return one.\n * @return Constant 1.\n */\n"
            "int oneValue() { return 2 - 1; }\n"
        )
        tamper_line = (
            f"pathlib.Path({suppr_rel!r}).write_text('nullPointer:src/mod.cpp\\n', encoding='utf-8')\n"
            if tamper
            else ""
        )
        fake_agent = _write_fake_agent(
            base / "fake_claude.py",
            f"pathlib.Path('src/mod.cpp').write_text({good_cpp!r}, encoding='utf-8')\n"
            + tamper_line
            + "print(json.dumps({'type': 'result', 'is_error': False, 'result': 'done'}))\n",
        )
        env = {
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_CPPCHECK_LOG": str(cpp_log),
        }
        rc, stdout, _ = self._run_dispatch(
            [
                "--engine", "haiku",
                "--brief", str(brief),
                "--workdir", str(wt),
                "--allow", "**",
                "--gate-cmd", "true",
                "--runs-root", str(base / "runs"),
                "--agent-bin", str(fake_agent),
                "--isolation", "local",
                "--allow-unsandboxed-local",
            ],
            env_extra=env,
        )
        data = json.loads(stdout.strip())
        calls = [json.loads(line) for line in cpp_log.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(calls, "fake cppcheck was not invoked")
        data["_rc"] = rc
        return data, calls, trusted_list

    def test_trusted_sibling_suppressions_pass_when_worker_leaves_them_alone(self) -> None:
        """A trusted config with sibling suppressions.txt lets a clean job pass; cppcheck gets the base-commit list."""
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            data, calls, trusted_list = self._run_trusted_suppressions_job(Path(tmp), tamper=False)
            self.assertEqual(0, data["_rc"])
            self.assertEqual("ok", data["scope"])
            self.assertEqual("ok", data["gate"])
            self.assertEqual(trusted_list, calls[-1]["suppressions_text"])
            run_meta = json.loads((Path(data["run_dir"]) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha256(trusted_list.encode("utf-8")).hexdigest(), run_meta["gate_suppressions_sha256"])

    def test_worker_modified_working_tree_suppressions_are_ignored(self) -> None:
        """Worker-modified suppression file is ignored: cppcheck receives the base-commit copy and the edit is gate-touched."""
        with tempfile.TemporaryDirectory() as tmp:
            data, calls, trusted_list = self._run_trusted_suppressions_job(Path(tmp), tamper=True)
            self.assertEqual(1, data["_rc"])
            self.assertEqual("violation", data["scope"])
            self.assertIn("suppressions.txt", data["gate_touched"])
            self.assertEqual(trusted_list, calls[-1]["suppressions_text"])
            run_meta = json.loads((Path(data["run_dir"]) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual("suppressions.txt", run_meta["gate_suppressions_path"])

    def test_devpack_config_with_root_level_suppressions_passes_gate(self) -> None:
        """.devpack/project.toml naming a committed root-level cfg/suppr.txt reaches cppcheck via the snapshot."""
        with tempfile.TemporaryDirectory() as tmp:
            data, calls, trusted_list = self._run_trusted_suppressions_job(
                Path(tmp),
                tamper=False,
                cfg_rel=".devpack/project.toml",
                suppr_rel="cfg/suppr.txt",
            )
            self.assertEqual(0, data["_rc"])
            self.assertEqual("ok", data["gate"])
            self.assertEqual(trusted_list, calls[-1]["suppressions_text"])
            run_meta = json.loads((Path(data["run_dir"]) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual("cfg/suppr.txt", run_meta["gate_suppressions_path"])

    def test_dispatch_fails_with_git_metadata_tampered_and_skips_marker(self) -> None:
        """If an agent writes core.fsmonitor to .git/config, dispatch fails with git_metadata_tampered and never runs host git."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _init_git_repo(wt)
            marker = base / "fsmonitor_marker.txt"
            brief = base / "brief.md"
            brief.write_text("# Brief\nUpdate mod.\n", encoding="utf-8")
            fake_agent = _write_fake_agent(
                base / "fake_claude.py",
                "cfg = pathlib.Path('.git/config')\n"
                f"cfg.write_text(cfg.read_text(encoding='utf-8') + '\\n[core]\\n\\tfsmonitor = touch {marker}\\n', encoding='utf-8')\n"
                "print(json.dumps({'type': 'result', 'is_error': False, 'result': 'done'}))\n",
            )
            rc, stdout, _ = self._run_dispatch(
                [
                    "--engine", "haiku",
                    "--brief", str(brief),
                    "--workdir", str(wt),
                    "--allow", "src/**",
                    "--gate-cmd", "true",
                    "--runs-root", str(base / "runs"),
                    "--agent-bin", str(fake_agent),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                ]
            )
            self.assertEqual(1, rc)
            self.assertFalse(marker.exists())
            data = json.loads(stdout.strip())
            self.assertEqual("violation", data["scope"])
            self.assertEqual("git_metadata_tampered", data["scope_violation"])
            self.assertTrue(data["git_metadata_tampered"])
            run_meta = json.loads((Path(data["run_dir"]) / "run.json").read_text(encoding="utf-8"))
            self.assertIn("git_config_sha256", run_meta)
            self.assertEqual(64, len(run_meta["git_config_sha256"]))
            self.assertIsInstance(run_meta["git_hooks_listing"], list)
            self.assertEqual("git_metadata_tampered", run_meta["scope_violation"])

    def test_dispatch_ignores_workdir_project_toml_opt_out_and_honours_operator_config(self) -> None:
        """Workdir .devpack/project.toml allow_unsandboxed_local is ignored with warning; DEVPACK_OPERATOR_CONFIG is honoured."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _init_git_repo(wt)
            (wt / ".devpack").mkdir()
            (wt / ".devpack" / "project.toml").write_text(
                'data_class = "internal"\nallow_unsandboxed_local = true\n',
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "commit project.toml"], cwd=wt, check=True)
            brief = base / "brief.md"
            brief.write_text("# Brief\nSandbox test.\n", encoding="utf-8")
            fake_agent = _write_fake_agent(
                base / "fake_claude.py",
                "print(json.dumps({'type': 'result', 'is_error': False, 'result': 'done'}))\n",
            )

            rc_refused, out_refused, err_refused = self._run_dispatch(
                [
                    "--engine", "haiku",
                    "--brief", str(brief),
                    "--workdir", str(wt),
                    "--allow", "src/**",
                    "--gate-cmd", "true",
                    "--runs-root", str(base / "runs"),
                    "--agent-bin", str(fake_agent),
                    "--isolation", "local",
                ]
            )
            self.assertEqual(1, rc_refused)
            self.assertIn("ignoring allow_unsandboxed_local", err_refused)
            refused_data = json.loads(out_refused.strip())
            self.assertEqual("refused", refused_data["status"])
            refused_meta = json.loads((Path(refused_data["run_dir"]) / "run.json").read_text(encoding="utf-8"))
            self.assertTrue(refused_meta["ignored_workdir_sandbox_opt_out"])
            self.assertIn("ignoring allow_unsandboxed_local", refused_meta["sandbox_warning"])

            # Operator config outside workdir authorises local execution
            op_cfg = base / "operator.toml"
            op_cfg.write_text("allow_unsandboxed_local = true\n", encoding="utf-8")
            rc_op, out_op, _ = self._run_dispatch(
                [
                    "--engine", "haiku",
                    "--brief", str(brief),
                    "--workdir", str(wt),
                    "--allow", "src/**",
                    "--gate-cmd", "true",
                    "--runs-root", str(base / "runs"),
                    "--agent-bin", str(fake_agent),
                    "--isolation", "local",
                ],
                env_extra={"DEVPACK_OPERATOR_CONFIG": str(op_cfg)},
            )
            self.assertEqual(0, rc_op)
            op_data = json.loads(out_op.strip())
            self.assertEqual("ok", op_data["status"])


if __name__ == "__main__":
    unittest.main()
