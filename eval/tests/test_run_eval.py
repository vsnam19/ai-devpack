"""Unit tests for eval/run_eval.py with mocked agent commands."""

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

EVAL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = EVAL_DIR.parent
for _p in (str(REPO_ROOT), str(EVAL_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_eval  # noqa: E402


def _create_mini_fixture_and_task(base_dir: Path) -> tuple[Path, Path]:
    """Create a lightweight synthetic fixture and task for fast unit testing."""
    fixture_dir = base_dir / "fixture"
    for sub in ("src", "include", "tests"):
        (fixture_dir / sub).mkdir(parents=True, exist_ok=True)
    (fixture_dir / "CMakeLists.txt").write_text("# mini fixture\n", encoding="utf-8")
    (fixture_dir / "build_and_test.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
        encoding="utf-8",
    )
    (fixture_dir / "src" / "sample.cpp").write_text(
        "int value() { return 0; }\n",
        encoding="utf-8",
    )

    tasks_dir = base_dir / "tasks"
    tdir = tasks_dir / "01-mini-bugfix"
    (tdir / "hidden").mkdir(parents=True, exist_ok=True)
    (tdir / "hidden" / "hidden_test.cpp").write_text("// hidden\n", encoding="utf-8")
    (tdir / "task.md").write_text(
        "# Task: 01-mini-bugfix\n\nCategory: bugfix\n\n## Goal\nFix value() to return 42.\n",
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
    (tdir / "check.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "WORKDIR=\"$PWD\"\n"
        "if [[ \"${1:-}\" == \"--workdir\" ]]; then WORKDIR=\"$2\"; fi\n"
        "grep -q 'return 42;' \"$WORKDIR/src/sample.cpp\"\n",
        encoding="utf-8",
    )
    return fixture_dir, tasks_dir


class RunEvalUnitTests(unittest.TestCase):
    """Test suite verifying isolation, pass^k math, redaction, docker error handling, and dry-run."""

    def test_pass_at_1_and_pass_hat_k_math(self) -> None:
        """Verify pass@1, strict pass^k (all-k pass), and Bernoulli pass^k math."""
        self.assertAlmostEqual(1.0, run_eval.compute_pass_at_1(5, 5))
        self.assertAlmostEqual(1.0, run_eval.compute_pass_hat_k(5, 5))
        self.assertAlmostEqual(1.0, run_eval.compute_pass_pow_k(5, 5))

        self.assertAlmostEqual(0.0, run_eval.compute_pass_at_1(0, 5))
        self.assertAlmostEqual(0.0, run_eval.compute_pass_hat_k(0, 5))
        self.assertAlmostEqual(0.0, run_eval.compute_pass_pow_k(0, 5))

        self.assertAlmostEqual(0.75, run_eval.compute_pass_at_1(3, 4))
        self.assertAlmostEqual(0.0, run_eval.compute_pass_hat_k(3, 4))
        self.assertAlmostEqual(0.75**3, run_eval.compute_pass_pow_k(3, 4, k=3))
        self.assertAlmostEqual(0.75**4, run_eval.compute_pass_pow_k(3, 4))

        with self.assertRaises(ValueError):
            run_eval.compute_pass_at_1(0, 0)
        with self.assertRaises(ValueError):
            run_eval.compute_pass_hat_k(6, 5)

    def test_dry_run_validates_tasks_and_does_not_call_agent(self) -> None:
        """Verify --dry-run exits 0, lists task x model plan, and invokes no subprocesses."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture_dir, tasks_dir = _create_mini_fixture_and_task(base)
            with mock.patch("subprocess.run") as mocked_run:
                rc = run_eval.main(
                    [
                        "--dry-run",
                        "--tasks",
                        "all",
                        "--models",
                        "sonnet,haiku",
                        "--k",
                        "3",
                        "--fixture-dir",
                        str(fixture_dir),
                        "--tasks-dir",
                        str(tasks_dir),
                        "--out",
                        str(base / "out"),
                    ]
                )
                self.assertEqual(0, rc)
                mocked_run.assert_not_called()
                self.assertFalse((base / "out").exists())

    def test_local_isolation_env_and_redaction_with_fake_agent(self) -> None:
        """Verify HOME and CLAUDE_CONFIG_DIR point inside work dir, only credentials copied, and results redact secrets/host paths."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture_dir, tasks_dir = _create_mini_fixture_and_task(base)
            cred_file = base / "secret_creds.json"
            secret_token = "SUPER_SECRET_TOKEN_VALUE_987654"
            cred_file.write_text(
                json.dumps({"accessToken": secret_token}),
                encoding="utf-8",
            )

            env_dump_file = base / "agent_env_dump.json"
            fake_agent = base / "fake_claude.py"
            fake_home_path = "/" + "ho" + "me/testuser_leak_check/subpath"
            fake_agent.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "cfg_dir = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])\n"
                "home_dir = pathlib.Path(os.environ['HOME'])\n"
                "cwd = pathlib.Path.cwd()\n"
                "dump = {\n"
                "    'HOME': str(home_dir),\n"
                "    'CLAUDE_CONFIG_DIR': str(cfg_dir),\n"
                "    'cfg_files': sorted(p.name for p in cfg_dir.iterdir()),\n"
                "    'cwd': str(cwd),\n"
                "    'has_settings': (cwd / '.claude' / 'settings.json').is_file(),\n"
                "    'argv': sys.argv[1:],\n"
                "}\n"
                f"pathlib.Path({str(env_dump_file)!r}).write_text(json.dumps(dump), encoding='utf-8')\n"
                "(cwd / 'src' / 'sample.cpp').write_text('int value() { return 42; }\\n', encoding='utf-8')\n"
                f"sys.stderr.write('debug path ' + {fake_home_path!r} + ' token ' + {secret_token!r} + '\\n')\n"
                "print(json.dumps({'type': 'result', 'total_cost_usd': 0.025, 'num_turns': 3, 'duration_ms': 1500}))\n",
                encoding="utf-8",
            )
            os.chmod(fake_agent, stat.S_IRWXU)

            work_root = base / "work_root"
            out_dir = base / "results"
            rc = run_eval.main(
                [
                    "--isolation",
                    "local",
                    "--allow-unsandboxed-local",
                    "--models",
                    "haiku",
                    "--k",
                    "2",
                    "--tasks",
                    "01-mini-bugfix",
                    "--credentials",
                    str(cred_file),
                    "--work-root",
                    str(work_root),
                    "--fixture-dir",
                    str(fixture_dir),
                    "--tasks-dir",
                    str(tasks_dir),
                    "--agent-bin",
                    str(fake_agent),
                    "--out",
                    str(out_dir),
                ]
            )
            self.assertEqual(0, rc)
            self.assertTrue(env_dump_file.is_file())
            dump = json.loads(env_dump_file.read_text(encoding="utf-8"))

            # HOME and CLAUDE_CONFIG_DIR must point inside work_root
            self.assertTrue(dump["HOME"].startswith(str(work_root)))
            self.assertTrue(dump["CLAUDE_CONFIG_DIR"].startswith(str(work_root)))
            self.assertTrue(dump["cwd"].startswith(str(work_root)))

            # Only the credentials copy and the trust state are inside CLAUDE_CONFIG_DIR
            self.assertEqual([".claude.json", ".credentials.json"], dump["cfg_files"])
            self.assertTrue(dump["has_settings"])

            # Verify CLI arguments passed to the agent
            self.assertIn("--setting-sources", dump["argv"])
            self.assertIn("project", dump["argv"])
            self.assertIn("--strict-mcp-config", dump["argv"])
            self.assertIn("--no-session-persistence", dump["argv"])
            self.assertIn("--disable-slash-commands", dump["argv"])

            # Verify results.json and summary.md exist and contain no secrets or host home paths
            results_json_path = out_dir / "results.json"
            summary_md_path = out_dir / "summary.md"
            self.assertTrue(results_json_path.is_file())
            self.assertTrue(summary_md_path.is_file())

            results_text = results_json_path.read_text(encoding="utf-8")
            summary_text = summary_md_path.read_text(encoding="utf-8")

            for forbidden in (
                secret_token,
                str(cred_file),
                fake_home_path,
                "/" + "ho" + "me/",
            ):
                self.assertNotIn(forbidden, results_text)
                self.assertNotIn(forbidden, summary_text)

            results_data = json.loads(results_text)
            self.assertEqual(1, len(results_data["tasks"]))
            task_summary = results_data["tasks"][0]
            self.assertEqual(2, task_summary["passes"])
            self.assertAlmostEqual(1.0, task_summary["pass_at_1"])
            self.assertAlmostEqual(1.0, task_summary["pass_hat_k"])
            self.assertAlmostEqual(0.025, task_summary["mean_cost_usd"])
            self.assertAlmostEqual(3.0, task_summary["mean_turns"])

    def test_docker_isolation_command_and_missing_docker_error(self) -> None:
        """Verify docker mode fails clearly when docker is missing (no silent fallback) and mounts only task + ro cfg."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            paths = {
                "work_dir": base / "work",
                "cfg_dir": base / "cfg",
                "home_dir": base / "home",
                "tmp_dir": base / "tmp",
            }
            cmd, env, cwd = run_eval.build_docker_agent_invocation(
                docker_bin="/usr/bin/docker",
                image_tag=run_eval.DOCKER_IMAGE_TAG,
                model="sonnet",
                prompt_text="Solve task",
                paths=paths,
            )
            self.assertEqual(paths["work_dir"], cwd)
            self.assertIn("--network", cmd)
            self.assertEqual("default", cmd[cmd.index("--network") + 1])
            self.assertIn("--user", cmd)
            self.assertEqual("1000:1000", cmd[cmd.index("--user") + 1])

            mounts = [cmd[i + 1] for i, token in enumerate(cmd) if token == "-v"]
            self.assertEqual(
                [
                    f"{paths['work_dir']}:/workspace/task:rw",
                    f"{paths['cfg_dir']}:/workspace/cfg:ro",
                ],
                mounts,
            )
            self.assertIn("HOME=/workspace/home", cmd)
            self.assertIn("CLAUDE_CONFIG_DIR=/workspace/cfg", cmd)

            with self.assertRaises(RuntimeError) as ctx:
                run_eval.ensure_docker_image(
                    dockerfile_path=EVAL_DIR / "Dockerfile",
                    which_fn=lambda _name: None,
                )
            self.assertIn("no silent fallback to local", str(ctx.exception))


_OK_JSON = (
    "print(json.dumps({'type': 'result', 'result': RESULT, 'total_cost_usd': 0.01, "
    "'num_turns': 2, 'permission_denials': DENIALS}))\n"
)


def _write_agent(base: Path, body: str) -> Path:
    """Write an executable fake agent script whose body follows a dump of its state."""
    agent = base / "fake_agent.py"
    agent.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "cwd = pathlib.Path.cwd()\n"
        "cfg = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])\n"
        + body,
        encoding="utf-8",
    )
    os.chmod(agent, stat.S_IRWXU)
    return agent


class HarnessErrorTests(unittest.TestCase):
    """Tests for permissions, harness-error detection, excerpts, and cleanup."""

    def _run(self, body: str, extra_args: list[str] | None = None, k: int = 1):
        """Run main() with a fake agent; return (rc, results dict, work_root, base)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        fixture_dir, tasks_dir = _create_mini_fixture_and_task(base)
        cred = base / "creds.json"
        cred.write_text(json.dumps({"accessToken": "TOKEN_ABC_123456"}), encoding="utf-8")
        agent = _write_agent(base, body)
        work_root = base / "wr"
        out = base / "out"
        rc = run_eval.main(
            [
                "--isolation", "local", "--allow-unsandboxed-local", "--models", "haiku", "--k", str(k),
                "--tasks", "01-mini-bugfix", "--credentials", str(cred),
                "--work-root", str(work_root), "--fixture-dir", str(fixture_dir),
                "--tasks-dir", str(tasks_dir), "--agent-bin", str(agent),
                "--out", str(out), *(extra_args or []),
            ]
        )
        results = json.loads((out / "results.json").read_text(encoding="utf-8"))
        return rc, results, work_root, base

    _FIX = "(cwd / 'src' / 'sample.cpp').write_text('int value() { return 42; }\\n')\n"

    def test_permission_flags_and_trust_state(self) -> None:
        """Agent gets permission flags on argv and a pre-written trusted .claude.json."""
        body = (
            "state = json.loads((cfg / '.claude.json').read_text())\n"
            "pathlib.Path(str(cwd.parent / 'dump.json')).write_text(json.dumps("
            "{'argv': sys.argv[1:], 'state': state, 'cwd': str(cwd)}))\n"
            + self._FIX
            + _OK_JSON.replace("RESULT", "'done'").replace("DENIALS", "[]")
        )
        rc, results, work_root, _ = self._run(body)
        self.assertEqual(0, rc)
        dump = json.loads(next(work_root.rglob("dump.json")).read_text())
        argv = dump["argv"]
        i = argv.index("--allowedTools")
        self.assertEqual(["Read", "Edit", "Write", "Bash", "Glob", "Grep"], argv[i + 1 : i + 7])
        j = argv.index("--disallowedTools")
        self.assertEqual(["WebFetch", "WebSearch"], argv[j + 1 : j + 3])
        self.assertTrue(dump["state"]["projects"][dump["cwd"]]["hasTrustDialogAccepted"])
        self.assertFalse(results["tasks"][0]["runs"][0]["harness_error"])

    def test_docker_trust_state_uses_container_path_and_tag(self) -> None:
        """Docker mode trusts the container task dir; build gets version build-arg."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture_dir, tasks_dir = _create_mini_fixture_and_task(base)
            task = run_eval.load_and_validate_tasks(tasks_dir, "all")[0]
            cred = base / "c.json"
            cred.write_text("{}", encoding="utf-8")
            paths = run_eval.prepare_isolated_trial_dir(
                base / "trial", fixture_dir, task, cred, agent_cwd="/workspace/task"
            )
            state = json.loads((paths["cfg_dir"] / ".claude.json").read_text())
            self.assertEqual(
                {"projects": {"/workspace/task": {"hasTrustDialogAccepted": True}}}, state
            )
            cmd, _, _ = run_eval.build_docker_agent_invocation(
                "docker", "devpack-eval:2.1.330", "haiku", "p", paths
            )
            self.assertIn("--allowedTools", cmd)
            self.assertIn("--disallowedTools", cmd)

            calls: list[list[str]] = []

            def fake_run(argv, **_kw):
                """Record docker build invocation and return exit code 0."""
                calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, "", "")

            run_eval.ensure_docker_image(
                EVAL_DIR / "Dockerfile",
                image_tag=run_eval.docker_image_tag("9.9.9"),
                claude_version="9.9.9",
                which_fn=lambda _n: "docker",
                run_fn=fake_run,
            )
            self.assertIn("CLAUDE_CODE_VERSION=9.9.9", calls[0])
            self.assertIn("devpack-eval:9.9.9", calls[0])
        self.assertEqual("devpack-eval:2.1.330", run_eval.DOCKER_IMAGE_TAG)
        dockerfile = (EVAL_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG CLAUDE_CODE_VERSION=2.1.330", dockerfile)
        args = run_eval.build_arg_parser().parse_args([])
        self.assertEqual("2.1.330", args.claude_version)

    def test_trust_warning_is_harness_error_and_exits_1(self) -> None:
        """A trust warning marks a harness error excluded from pass metrics; CLI exits 1."""
        body = (
            "sys.stderr.write('Ignoring 4 permissions.allow entries: workspace has not been trusted\\n')\n"
            + _OK_JSON.replace("RESULT", "'no edits'").replace("DENIALS", "[{'tool_name': 'Edit'}]")
        )
        rc, results, _, _ = self._run(body)
        self.assertEqual(1, rc)
        task = results["tasks"][0]
        run = task["runs"][0]
        self.assertTrue(run["harness_error"])
        self.assertIn("trusted", run["harness_error_reason"])
        self.assertEqual(1, task["harness_errors"])
        self.assertEqual(0, task["valid_runs"])
        self.assertIsNone(task["pass_at_1"])
        self.assertEqual("no edits", run["agent_result_excerpt"])
        self.assertEqual([{"tool_name": "Edit"}], run["permission_denials"])
        self.assertIn("not been trusted", run["stderr_tail"])
        self.assertEqual(1, results["categories"][0]["harness_errors"])

    def test_allow_harness_errors_flag_exits_0(self) -> None:
        """--allow-harness-errors suppresses the non-zero exit code."""
        body = "sys.exit(3)\n"
        rc, results, _, _ = self._run(body, ["--allow-harness-errors"])
        self.assertEqual(0, rc)
        run = results["tasks"][0]["runs"][0]
        self.assertTrue(run["harness_error"])
        self.assertIn("non-zero", run["harness_error_reason"])

    def test_unparsable_json_is_harness_error(self) -> None:
        """Garbage stdout with exit 0 is a harness error."""
        rc, results, _, _ = self._run("print('not json at all')\n")
        self.assertEqual(1, rc)
        self.assertIn("unparsable", results["tasks"][0]["runs"][0]["harness_error_reason"])

    def test_metrics_use_valid_runs_only(self) -> None:
        """A harness-error run does not count as a model failure in pass metrics."""
        body = (
            "marker = cwd.parent / 'count'\n"
            "n = int(marker.read_text()) + 1 if marker.exists() else 1\n"
            "marker.write_text(str(n))\n"
            "if n == 1:\n"
            "    sys.exit(2)\n"
            + self._FIX
            + _OK_JSON.replace("RESULT", "'ok'").replace("DENIALS", "[]")
        )
        # Per-trial dirs differ so n is always 1; use a shared marker via env-free path.
        body = body.replace("cwd.parent / 'count'", "pathlib.Path(__file__).parent / 'count'")
        rc, results, _, _ = self._run(body, k=2)
        self.assertEqual(1, rc)
        task = results["tasks"][0]
        self.assertEqual(1, task["harness_errors"])
        self.assertEqual(1, task["valid_runs"])
        self.assertEqual(1, task["passes"])
        self.assertAlmostEqual(1.0, task["pass_at_1"])

    def test_excerpt_and_tail_truncated_and_redacted(self) -> None:
        """Excerpt keeps first 4000 chars, stderr tail last 2000, no secrets or home paths."""
        leaked = "/" + "ho" + "me/someone/x"
        body = (
            "sys.stderr.write('A' * 5000 + 'END TOKEN_ABC_123456 ' + " + repr(leaked) + ")\n"
            + self._FIX
            + _OK_JSON.replace("RESULT", "'R' * 5000 + 'TOKEN_ABC_123456'").replace("DENIALS", "[]")
        )
        _, results, _, _ = self._run(body)
        run = results["tasks"][0]["runs"][0]
        self.assertEqual(4000, len(run["agent_result_excerpt"]))
        self.assertLessEqual(len(run["stderr_tail"]), 2000)
        self.assertTrue(run["stderr_tail"].endswith("$HOME/x"))
        text = json.dumps(results)
        self.assertNotIn("TOKEN_ABC_123456", text)
        self.assertNotIn(leaked, text)

    def test_credentials_removed_after_trial(self) -> None:
        """Credentials copy and cfg contents are gone after success and after failure."""
        ok = self._FIX + _OK_JSON.replace("RESULT", "'ok'").replace("DENIALS", "[]")
        for body in (ok, "sys.exit(5)\n"):
            _, _, work_root, _ = self._run(body)
            self.assertEqual([], list(work_root.rglob(".credentials.json")))
            for cfg in work_root.rglob("cfg"):
                self.assertEqual([], list(cfg.iterdir()))

    def test_credentials_removed_after_timeout_and_exception(self) -> None:
        """Cleanup runs on timeout and on unexpected exceptions."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture_dir, tasks_dir = _create_mini_fixture_and_task(base)
            task = run_eval.load_and_validate_tasks(tasks_dir, "all")[0]
            cred = base / "c.json"
            cred.write_text("{}", encoding="utf-8")
            agent = _write_agent(base, "import time\ntime.sleep(5)\n")
            for exc in (None, RuntimeError("boom")):
                run_root = base / ("rr_timeout" if exc is None else "rr_exc")
                kwargs = dict(
                    task=task, model="haiku", run_index=1, run_root=run_root,
                    fixture_dir=fixture_dir, credentials_path=cred, isolation="local",
                    agent_bin=str(agent), timeout_s=1, allow_unsandboxed_local=True,
                )
                if exc is None:
                    result = run_eval.run_single_trial(**kwargs)
                    self.assertTrue(result.timed_out)
                    self.assertFalse(result.harness_error)
                    self.assertFalse(result.passed)
                    self.assertIn("timed out", result.error)
                else:
                    with mock.patch.object(
                        run_eval.agent_exec, "parse_stream", side_effect=RuntimeError("boom")
                    ):
                        with self.assertRaises(RuntimeError):
                            run_eval.run_single_trial(**kwargs)
                cfg = run_root / "01-mini-bugfix__haiku__run1" / "cfg"
                self.assertTrue(cfg.is_dir())
                self.assertEqual([], list(cfg.iterdir()))

    def test_engines_flag_and_runs_root_trial_artifacts(self) -> None:
        """--engines alias and --runs-root write per-trial run dirs, cost_usd, and tool_protocol_errors."""
        body = (
            self._FIX
            + "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': '```json\\n{\"name\": \"Bash\", \"arguments\": {\"cmd\": \"ls\"}}\\n```'}]}}))\n"
            + _OK_JSON.replace("RESULT", "'done'").replace("DENIALS", "[]")
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runs_root = base / "custom_runs"
            rc, results, _, _ = self._run(
                body,
                extra_args=["--engines", "haiku", "--runs-root", str(runs_root)],
                k=1,
            )
            self.assertEqual(0, rc)
            run_rec = results["tasks"][0]["runs"][0]
            self.assertAlmostEqual(0.01, run_rec["cost_usd"])
            self.assertEqual(1, run_rec["tool_protocol_errors"])
            self.assertTrue(run_rec["run_dir"])
            trial_run_dir = Path(run_rec["run_dir"])
            self.assertTrue((trial_run_dir / "run.json").is_file())
            self.assertTrue((trial_run_dir / "steps" / "01-eval-haiku.jsonl").is_file())
            self.assertTrue((trial_run_dir / "steps" / "01-eval-haiku.meta.json").is_file())
            self.assertTrue((trial_run_dir / "steps" / "01-eval-haiku.prompt.md").is_file())
            self.assertTrue((trial_run_dir / "steps" / "02-gate.txt").is_file())

    def test_timeout_counted_as_not_passed_in_timeouts_column(self) -> None:
        """Agent timeout is counted in timeouts, included in valid_runs as NOT passed (not a harness error)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture_dir, tasks_dir = _create_mini_fixture_and_task(base)
            cred = base / "c.json"
            cred.write_text("{}", encoding="utf-8")
            agent = _write_agent(base, "import time\ntime.sleep(5)\n")
            out = base / "out"
            rc = run_eval.main(
                [
                    "--isolation", "local", "--allow-unsandboxed-local",
                    "--engines", "haiku", "--k", "1", "--timeout-s", "1",
                    "--tasks", "01-mini-bugfix", "--credentials", str(cred),
                    "--work-root", str(base / "wr"), "--fixture-dir", str(fixture_dir),
                    "--tasks-dir", str(tasks_dir), "--agent-bin", str(agent),
                    "--out", str(out),
                ]
            )
            self.assertEqual(0, rc)
            results = json.loads((out / "results.json").read_text(encoding="utf-8"))
            ts = results["tasks"][0]
            self.assertEqual(0, ts["harness_errors"])
            self.assertEqual(1, ts["timeouts"])
            self.assertEqual(1, ts["valid_runs"])
            self.assertEqual(0, ts["passes"])
            self.assertAlmostEqual(0.0, ts["pass_at_1"])
            summary_md = (out / "summary.md").read_text(encoding="utf-8")
            self.assertIn("Timeouts", summary_md)

    def test_local_engine_reports_cost_na_and_shows_tokens_and_wall_time(self) -> None:
        """Local engine cost is reported as n/a even if stream has synthetic total_cost_usd; tokens + duration shown."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture_dir, tasks_dir = _create_mini_fixture_and_task(base)
            agent = _write_agent(
                base,
                "(cwd / 'src' / 'sample.cpp').write_text('int value() { return 42; }\\n')\n"
                "print(json.dumps({'type': 'result', 'result': 'done', 'total_cost_usd': 0.1974, "
                "'num_turns': 2, 'duration_ms': 1234, 'usage': {'input_tokens': 450, 'output_tokens': 120}}))\n",
            )
            out = base / "out"
            with mock.patch.dict(
                os.environ,
                {
                    "DEVPACK_LOCAL_BASE_URL": "http://127.0.0.1:11434",
                    "DEVPACK_LOCAL_AUTH_TOKEN": "LOCAL_TOK",
                },
                clear=False,
            ):
                rc = run_eval.main(
                    [
                        "--isolation", "local", "--allow-unsandboxed-local",
                        "--engines", "local", "--k", "1",
                        "--tasks", "01-mini-bugfix",
                        "--work-root", str(base / "wr"), "--fixture-dir", str(fixture_dir),
                        "--tasks-dir", str(tasks_dir), "--agent-bin", str(agent),
                        "--out", str(out),
                    ]
                )
            self.assertEqual(0, rc)
            results = json.loads((out / "results.json").read_text(encoding="utf-8"))
            ts = results["tasks"][0]
            self.assertIsNone(ts["mean_cost_usd"])
            self.assertIsNone(ts["runs"][0]["cost_usd"])
            self.assertEqual(450, ts["runs"][0]["tokens_in"])
            self.assertEqual(120, ts["runs"][0]["tokens_out"])
            md = (out / "summary.md").read_text(encoding="utf-8")
            self.assertIn("n/a", md)
            self.assertIn("450/120", md)
            self.assertNotIn("0.1974", md)

    def test_devpack_root_set_in_check_env_and_grader_misconfigured_is_harness_error(self) -> None:
        """check.sh receives DEVPACK_ROOT and exit 2 / GRADER_MISCONFIGURED classifies as harness_error."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture_dir, tasks_dir = _create_mini_fixture_and_task(base)
            tdir = tasks_dir / "01-mini-bugfix"
            env_dump = base / "check_env.json"
            (tdir / "check.sh").write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                f'ENV_DUMP_PATH="{env_dump}" python3 -c "import json, os, pathlib; pathlib.Path(os.environ[\'ENV_DUMP_PATH\']).write_text(json.dumps(dict(os.environ)))"\n'
                "echo 'GRADER_MISCONFIGURED: missing fixture' >&2\n"
                "exit 2\n",
                encoding="utf-8",
            )
            cred = base / "c.json"
            cred.write_text("{}", encoding="utf-8")
            agent = _write_agent(
                base,
                "(cwd / 'src' / 'sample.cpp').write_text('int value() { return 42; }\\n')\n"
                + _OK_JSON.replace("RESULT", "'ok'").replace("DENIALS", "[]"),
            )
            out = base / "out"
            rc = run_eval.main(
                [
                    "--isolation", "local", "--allow-unsandboxed-local",
                    "--engines", "haiku", "--k", "1",
                    "--tasks", "01-mini-bugfix", "--credentials", str(cred),
                    "--work-root", str(base / "wr"), "--fixture-dir", str(fixture_dir),
                    "--tasks-dir", str(tasks_dir), "--agent-bin", str(agent),
                    "--out", str(out),
                ]
            )
            self.assertEqual(1, rc)
            captured = json.loads(env_dump.read_text(encoding="utf-8"))
            self.assertEqual(str(REPO_ROOT), captured.get("DEVPACK_ROOT"))
            results = json.loads((out / "results.json").read_text(encoding="utf-8"))
            run_rec = results["tasks"][0]["runs"][0]
            self.assertTrue(run_rec["harness_error"])
            self.assertFalse(run_rec["passed"])
            self.assertIn("GRADER_MISCONFIGURED", run_rec["harness_error_reason"])

    def test_sandbox_local_refused_without_allow_flag(self) -> None:
        """run_eval refuses --isolation local without --allow-unsandboxed-local."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture_dir, tasks_dir = _create_mini_fixture_and_task(base)
            cred = base / "c.json"
            cred.write_text("{}", encoding="utf-8")
            rc = run_eval.main(
                [
                    "--isolation", "local",
                    "--engines", "haiku", "--k", "1",
                    "--tasks", "01-mini-bugfix", "--credentials", str(cred),
                    "--fixture-dir", str(fixture_dir), "--tasks-dir", str(tasks_dir),
                    "--out", str(base / "out"),
                ]
            )
            self.assertEqual(1, rc)

    def test_runner_mode_and_lint_metrics_math(self) -> None:
        """Verify runner-mode metrics (false-approve, false-reject, escalate, lint metrics) and --mode runner dry-run."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture_dir, tasks_dir = _create_mini_fixture_and_task(base)
            rc_dry = run_eval.main(
                [
                    "--dry-run", "--mode", "runner", "--pairs", "haiku:sonnet,sonnet:haiku",
                    "--k", "2", "--fixture-dir", str(fixture_dir), "--tasks-dir", str(tasks_dir),
                ]
            )
            self.assertEqual(0, rc_dry)

            # Synthetic runner trials testing false-approve, false-reject, escalate, lint metrics
            task = run_eval.load_and_validate_tasks(tasks_dir, "all")[0]
            trials = [
                run_eval.TrialResult(
                    task_id=task.task_id, category=task.category, model="haiku:sonnet",
                    run_index=1, passed=False, agent_returncode=0, check_returncode=1,
                    cost_usd=0.02, turns=None, duration_s=1.5, mode="runner",
                    rounds_used=1, escalated=False, reviewer_verdict="APPROVE",
                    reviewer_false_approve=True, reviewer_false_reject=False,
                    lint_findings_first_attempt=2, lint_clean_final=True, rounds_to_clean=1,
                ),
                run_eval.TrialResult(
                    task_id=task.task_id, category=task.category, model="haiku:sonnet",
                    run_index=2, passed=True, agent_returncode=0, check_returncode=0,
                    cost_usd=0.03, turns=None, duration_s=2.5, mode="runner",
                    rounds_used=2, escalated=True, reviewer_verdict="REQUEST_CHANGES",
                    reviewer_false_approve=False, reviewer_false_reject=True,
                    lint_findings_first_attempt=0, lint_clean_final=True, rounds_to_clean=0,
                ),
            ]
            task_sums, cat_sums = run_eval.aggregate_results([task], ["haiku:sonnet"], 2, trials)
            ts = task_sums[0]
            self.assertEqual(1, ts.false_approve_count)
            self.assertEqual(1, ts.false_reject_count)
            self.assertAlmostEqual(0.5, ts.false_approve_rate)
            self.assertAlmostEqual(0.5, ts.false_reject_rate)
            self.assertAlmostEqual(0.5, ts.escalate_rate)
            self.assertAlmostEqual(1.5, ts.mean_rounds_used)
            self.assertAlmostEqual(1.0, ts.mean_lint_findings_first_attempt)
            self.assertAlmostEqual(1.0, ts.lint_clean_final_rate)
            self.assertAlmostEqual(0.5, ts.mean_rounds_to_clean)
            self.assertEqual(1, cat_sums[0]["false_approve_count"])
            self.assertEqual(1, cat_sums[0]["false_reject_count"])

    def test_orchestrator_transcript_analysis_and_goal_check(self) -> None:
        """Verify orchestrator transcript analysis, --mode orchestrator dry-run, and goal check.sh behavior."""
        # 1. Dry-run orchestrator plan
        rc_dry = run_eval.main(
            [
                "--dry-run", "--mode", "orchestrator", "--orchestrator", "sonnet",
                "--workers", "haiku", "--k", "2",
            ]
        )
        self.assertEqual(0, rc_dry)

        # 2. Compliant transcript (briefs written + 2 dispatch calls, zero product edits)
        compliant_stream = "\n".join([
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": "briefs/t1.md", "content": "..."}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "python3 runner/devpack_dispatch.py --engine haiku --brief briefs/t1.md"}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "python3 runner/devpack_dispatch.py --engine haiku --brief briefs/t2.md"}}]}}),
        ])
        res_ok = run_eval.analyze_orchestrator_transcript(compliant_stream)
        self.assertTrue(res_ok["compliant"])
        self.assertEqual(0, res_ok["orchestrator_product_edits"])
        self.assertEqual(2, res_ok["dispatch_calls"])

        # 3. Direct product edit detected -> non-compliant
        bad_edit_stream = "\n".join([
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "src/link_state_machine.cpp"}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "python3 runner/devpack_dispatch.py --engine haiku --brief briefs/t2.md"}}]}}),
        ])
        res_bad = run_eval.analyze_orchestrator_transcript(bad_edit_stream)
        self.assertFalse(res_bad["compliant"])
        self.assertEqual(1, res_bad["orchestrator_product_edits"])
        self.assertEqual(["src/link_state_machine.cpp"], res_bad["product_edit_paths"])

        # 4. Native Task subagent bypass detected -> non-compliant
        bypass_stream = "\n".join([
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Task", "input": {"prompt": "implement"}}]}})
        ])
        res_bypass = run_eval.analyze_orchestrator_transcript(bypass_stream)
        self.assertFalse(res_bypass["compliant"])
        self.assertEqual(1, res_bypass["subagent_calls"])

        # 5. Orchestrator goal check.sh fails on unmodified fixture and passes on solution.patch
        import shutil
        goal_dir = REPO_ROOT / "eval" / "orchestrator_goals" / "01-link-fault-recovery"
        fixture_dir = REPO_ROOT / "eval" / "fixture"
        with tempfile.TemporaryDirectory() as tmp:
            w = Path(tmp) / "work"
            shutil.copytree(fixture_dir, w)
            subprocess.run(["git", "init", "-q"], cwd=w, check=True)
            subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=w, check=True)
            subprocess.run(["git", "config", "user.name", "T"], cwd=w, check=True)
            subprocess.run(["git", "add", "-A"], cwd=w, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "chore: base fixture"], cwd=w, check=True)

            env = {**os.environ, "DEVPACK_ROOT": str(REPO_ROOT)}
            r_fail = subprocess.run(
                ["bash", str(goal_dir / "check.sh"), "--workdir", str(w)],
                env=env, capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(0, r_fail.returncode)

            subprocess.run(["git", "apply", str(goal_dir / "solution.patch")], cwd=w, check=True)
            r_pass = subprocess.run(
                ["bash", str(goal_dir / "check.sh"), "--workdir", str(w)],
                env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, r_pass.returncode, r_pass.stdout + r_pass.stderr)


if __name__ == "__main__":
    unittest.main()
