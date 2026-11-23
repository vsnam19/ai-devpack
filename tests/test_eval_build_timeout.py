# Header: Unit test for build_and_test.sh TimeoutExpired handling in eval/run_eval.py.
"""Unit test ensuring build_and_test.sh timeouts in memory mode become harness errors."""

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
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO_ROOT / "eval"
for _p in (str(REPO_ROOT), str(EVAL_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_eval  # noqa: E402


def _write_exec(path: Path, text: str) -> Path:
    """Write an executable script file at ``path``."""
    path.write_text(text, encoding="utf-8")
    os.chmod(path, stat.S_IRWXU)
    return path


class EvalBuildTimeoutTests(unittest.TestCase):
    """Verify build_and_test.sh TimeoutExpired before proposal acceptance is a harness error."""

    def test_memory_step_build_timeout_becomes_harness_error_and_skips_proposals(self) -> None:
        """TimeoutExpired on build_and_test.sh marks the trial harness_error with build_timeout."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture_dir = base / "fixture"
            for sub in ("src", "include", "tests"):
                (fixture_dir / sub).mkdir(parents=True)
            (fixture_dir / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.16)\n", encoding="utf-8")
            (fixture_dir / "tests" / "test_framework.hpp").write_text("// tf\n", encoding="utf-8")
            (fixture_dir / "src" / "val.txt").write_text("0\n", encoding="utf-8")
            _write_exec(fixture_dir / "build_and_test.sh", "#!/usr/bin/env bash\nexit 0\n")

            mem_tasks = base / "memory_tasks"
            seq_dir = mem_tasks / "01-build-timeout"
            seq_dir.mkdir(parents=True)
            (seq_dir / "sequence.toml").write_text(
                'id = "01-build-timeout"\n'
                'category = "recall"\n'
                'tier = "v2"\n'
                'title = "Build timeout stub"\n'
                "ab_compare = true\n\n"
                "[step_1]\n"
                'role = "implementer"\n'
                'prompt_file = "step1.md"\n'
                'allowed_files = ["src/val.txt"]\n\n'
                "[step_2]\n"
                'role = "implementer"\n'
                'prompt_file = "step2.md"\n'
                'allowed_files = ["src/val.txt"]\n',
                encoding="utf-8",
            )
            (seq_dir / "step1.md").write_text("Step 1 on src/val.txt.\n", encoding="utf-8")
            (seq_dir / "step2.md").write_text("Step 2 on src/val.txt.\n", encoding="utf-8")
            _write_exec(seq_dir / "check.sh", "#!/usr/bin/env bash\nexit 0\n")

            call_log = base / "agent_calls.log"
            fake_agent = _write_exec(
                base / "fake_claude.py",
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                f"with open({str(call_log)!r}, 'a', encoding='utf-8') as fh:\n"
                "    fh.write(sys.argv[-1].splitlines()[0] + '\\n')\n"
                "pathlib.Path('src/val.txt').write_text('1\\n', encoding='utf-8')\n"
                "res = ('Step done.\\n\\n## Memory proposals\\n'\n"
                "       '- type: handoff\\n  id: mem-timeout-handoff\\n  scope: project\\n'\n"
                "       '  tags: REQ-MEM-1\\n  summary: Must not be accepted on build_timeout.\\n')\n"
                "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,\n"
                " 'total_cost_usd': 0.01, 'num_turns': 1, 'duration_ms': 10,\n"
                " 'usage': {'input_tokens': 10, 'output_tokens': 5}, 'result': res}))\n",
            )

            cred = base / "creds.json"
            cred.write_text('{"accessToken":"fake-eval-tok"}', encoding="utf-8")
            real_check = run_eval._run_check_script

            def _run_with_build_timeout(check: Path, *args: object, **kwargs: object) -> object:
                """Simulate a timeout of the isolated memory build grader."""
                if check.name == "check_memory_step.sh":
                    self.assertEqual(kwargs["isolation"], "local")
                    self.assertTrue(kwargs["allow_unsandboxed_local"])
                    return 124, "check.sh timed out", False
                return real_check(check, *args, **kwargs)

            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
                mock.patch.object(run_eval, "_run_check_script", side_effect=_run_with_build_timeout),
            ):
                rc = run_eval.main(
                    [
                        "--mode",
                        "memory",
                        "--engines",
                        "haiku",
                        "--k",
                        "1",
                        "--isolation",
                        "local",
                        "--allow-unsandboxed-local",
                        "--credentials",
                        str(cred),
                        "--fixture-dir",
                        str(fixture_dir),
                        "--memory-tasks-dir",
                        str(mem_tasks),
                        "--work-root",
                        str(base / "work_root"),
                        "--out",
                        str(base / "out"),
                        "--agent-bin",
                        str(fake_agent),
                        "--allow-harness-errors",
                    ]
                )

            self.assertEqual(0, rc)
            records = {}
            for run_json in sorted((base / "out").rglob("run.json")):
                data = json.loads(run_json.read_text(encoding="utf-8"))
                if "memory_arm" in data:
                    records[data["memory_arm"]] = data

            self.assertEqual({"with_memory", "without_memory"}, set(records.keys()))
            self.assertEqual("harness_error", records["with_memory"]["outcome"])
            self.assertEqual("pass", records["without_memory"]["outcome"])

            results_files = sorted((base / "out").rglob("results.json"))
            self.assertGreaterEqual(len(results_files), 1)
            results_text = "".join(p.read_text(encoding="utf-8") for p in results_files)
            self.assertIn("build_timeout", results_text)
            # Step 2 never ran for with_memory (1 call) + 2 steps for without_memory = 3 calls total.
            self.assertEqual(3, len(call_log.read_text(encoding="utf-8").splitlines()))
            # Proposal from timed-out step was never accepted.
            self.assertEqual([], list(base.rglob("mem-timeout-handoff.md")))


if __name__ == "__main__":
    unittest.main()
