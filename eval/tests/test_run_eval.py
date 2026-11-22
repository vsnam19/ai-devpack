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

            # Only the credentials copy is inside CLAUDE_CONFIG_DIR
            self.assertEqual([".credentials.json"], dump["cfg_files"])
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


if __name__ == "__main__":
    unittest.main()
