"""Unit tests for eval/run_eval.py --mode memory and eval/memory_tasks/."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
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


def _write_exec(path: Path, content: str) -> Path:
    """Write an executable script to ``path``."""
    path.write_text(content, encoding="utf-8")
    os.chmod(path, stat.S_IRWXU)
    return path


class MemoryEvalTests(unittest.TestCase):
    """Tests for memory sequence loading, --dry-run, A/B execution, and metrics."""

    def test_load_real_memory_sequences_and_dry_run(self) -> None:
        """Verify all >=5 sequences under eval/memory_tasks load cleanly and --dry-run makes no model calls."""
        mem_tasks_dir = EVAL_DIR / "memory_tasks"
        sequences = run_eval.load_and_validate_memory_sequences(mem_tasks_dir)
        self.assertGreaterEqual(len(sequences), 5)
        categories = {s.category for s in sequences}
        self.assertTrue({"recall", "superseded", "distractors", "stale-vs-code", "scope-isolation"} <= categories)

        out_buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("subprocess.run") as mocked_run, contextlib.redirect_stdout(out_buf):
                rc = run_eval.main(
                    [
                        "--dry-run",
                        "--mode",
                        "memory",
                        "--engines",
                        "haiku",
                        "--k",
                        "1",
                        "--out",
                        tmp,
                    ]
                )
            self.assertEqual(0, rc)
            mocked_run.assert_not_called()
        stdout = out_buf.getvalue()
        self.assertIn("DRY-RUN memory plan", stdout)
        self.assertIn("with_memory", stdout)
        self.assertIn("without_memory", stdout)

    def test_memory_mode_ab_recall_and_overrecall_metrics(self) -> None:
        """Verify --mode memory runs fresh agent per step, computes recall, over-recall, tokens, and A/B delta."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture_dir = base / "fixture"
            shutil.copytree(REPO_ROOT / "eval/fixture", fixture_dir)
            (fixture_dir / "src" / "val.txt").write_text("0\n", encoding="utf-8")

            mem_tasks = base / "memory_tasks"
            seq_dir = mem_tasks / "01-recall-seq"
            seq_dir.mkdir(parents=True)
            (seq_dir / "sequence.toml").write_text(
                'id = "01-recall-seq"\n'
                'category = "recall"\n'
                'tier = "v2"\n'
                'title = "Two-step recall"\n'
                "ab_compare = true\n\n"
                "[step_1]\n"
                'role = "implementer"\n'
                'prompt_file = "step1.md"\n'
                'allowed_files = ["src/val.txt"]\n'
                "expect_proposals = true\n\n"
                "[step_2]\n"
                'role = "implementer"\n'
                'prompt_file = "step2.md"\n'
                'allowed_files = ["src/val.txt"]\n'
                "expect_prior_proposals = true\n"
                'forbidden_ids = ["mem-distractor"]\n',
                encoding="utf-8",
            )
            (seq_dir / "step1.md").write_text("Step 1 on src/val.txt REQ-MEM-1.\n", encoding="utf-8")
            (seq_dir / "step2.md").write_text("Step 2 on src/val.txt REQ-MEM-1.\n", encoding="utf-8")
            seed_proj = seq_dir / "seed_memory" / "project"
            seed_proj.mkdir(parents=True)
            (seed_proj / "mem-distractor.md").write_text(
                "---\nid: mem-distractor\ntype: fact\nscope: project\ntags:\n  - REQ-OTHER-9\n"
                'created: 2026-11-20T00:00:00Z\nsource: human\nsupersedes: ""\nexpires: ""\nstatus: active\n---\n\nIrrelevant.\n',
                encoding="utf-8",
            )
            _write_exec(
                seq_dir / "check.sh",
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                'WORKDIR="${2:-$PWD}"\n'
                'grep -qx "MAGIC_99" "$WORKDIR/src/val.txt"\n',
            )

            cred = base / "creds.json"
            cred.write_text('{"accessToken":"fake-eval-tok"}', encoding="utf-8")

            # Fake agent reads the trailing prompt argument (sys.argv[-1]) to check if Step 1 handoff was injected.
            fake_agent = _write_exec(
                base / "fake_claude.py",
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "prompt = sys.argv[-1]\n"
                "if 'Step 1' in prompt:\n"
                "    out = ('Step 1 done.\\n\\n## Memory proposals\\n'\n"
                "           '- type: handoff\\n  id: mem-step1-key\\n  scope: task:01-recall-seq\\n'\n"
                "           '  tags: REQ-MEM-1, val.txt\\n'\n"
                "           '  summary: Write MAGIC_99 in Step 2.\\n')\n"
                "else:\n"
                "    val = 'MAGIC_99' if 'MAGIC_99' in prompt else 'WRONG_0'\n"
                "    pathlib.Path('src/val.txt').write_text(val + '\\n', encoding='utf-8')\n"
                "    out = 'Step 2 done.'\n"
                "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
                "'total_cost_usd':0.01,'num_turns':1,'duration_ms':10,"
                "'usage':{'input_tokens':20,'output_tokens':10},'result':out}))\n",
            )

            out_dir = base / "out"
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
                    str(out_dir),
                    "--agent-bin",
                    str(fake_agent),
                ]
            )
            self.assertEqual(0, rc)
            results = json.loads((out_dir / "results.json").read_text(encoding="utf-8"))
            self.assertEqual("memory", results["mode"])
            self.assertEqual(1, len(results["tasks"]))
            ts = results["tasks"][0]
            self.assertEqual(1.0, ts["pass_with_memory"])
            self.assertEqual(0.0, ts["pass_without_memory"])
            self.assertEqual(1.0, ts["memory_ab_delta"])
            self.assertEqual(1.0, ts["memory_recall_rate"])
            self.assertEqual(0.0, ts["memory_over_recall_rate"])
            self.assertGreater(ts["mean_injected_tokens"], 0)
            summary_md = (out_dir / "summary.md").read_text(encoding="utf-8")
            self.assertIn("## Memory Metrics", summary_md)
            self.assertIn("01-recall-seq", summary_md)

    def _run_enforcement_trial(
        self,
        base: Path,
        edit_snippet: str,
        step1_expected: tuple[str, ...] = (),
        checker_timeout: bool = False,
    ) -> tuple[list[dict], Path, str]:
        """Run a 2-step memory sequence whose step 1 agent runs ``edit_snippet``.

        Returns ``(run.json records, call log, concatenated text of every file under the output dir)``.
        The stub agent records each call in a log outside the workdir, so tests can tell whether step 2 ran.
        ``step1_expected`` sets step 1's ``expected_ids``. ``checker_timeout`` makes the test_lock
        checker raise ``subprocess.TimeoutExpired`` while every other subprocess runs for real.
        """
        fixture_dir = base / "fixture"
        (fixture_dir / "src").mkdir(parents=True)
        (fixture_dir / "include").mkdir(parents=True)
        (fixture_dir / "tests").mkdir(parents=True)
        (fixture_dir / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.16)\n", encoding="utf-8")
        (fixture_dir / "build_and_test.sh").write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
            encoding="utf-8",
        )
        os.chmod(fixture_dir / "build_and_test.sh", stat.S_IRWXU)
        (fixture_dir / "src" / "val.txt").write_text("0\n", encoding="utf-8")
        (fixture_dir / "tests" / "test_framework.hpp").write_text(
            "// Test framework header.\nint check_one() { return 1; }\nint check_two() { return 2; }\n",
            encoding="utf-8",
        )

        mem_tasks = base / "memory_tasks"
        seq_dir = mem_tasks / "01-enforce-seq"
        seq_dir.mkdir(parents=True)
        (seq_dir / "sequence.toml").write_text(
            'id = "01-enforce-seq"\n'
            'category = "recall"\n'
            'tier = "v2"\n'
            'title = "Enforcement stub"\n'
            "ab_compare = true\n\n"
            "[step_1]\n"
            'role = "implementer"\n'
            'prompt_file = "step1.md"\n'
            'allowed_files = ["src/val.txt"]\n'
            f"expected_ids = {json.dumps(list(step1_expected))}\n\n"
            "[step_2]\n"
            'role = "implementer"\n'
            'prompt_file = "step2.md"\n'
            'allowed_files = ["src/val.txt"]\n',
            encoding="utf-8",
        )
        (seq_dir / "step1.md").write_text("Step 1 on src/val.txt.\n", encoding="utf-8")
        (seq_dir / "step2.md").write_text("Step 2 on src/val.txt.\n", encoding="utf-8")
        _write_exec(seq_dir / "check.sh", "#!/usr/bin/env bash\nexit 1\n")

        log = base / "agent_calls.log"
        fake_agent = _write_exec(
            base / "fake_claude.py",
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            f"LOG = {str(log)!r}\n"
            "with open(LOG, 'a', encoding='utf-8') as fh:\n"
            "    fh.write('call\\n')\n"
            "prompt = sys.argv[-1]\n"
            "if 'Step 1' in prompt:\n"
            f"    {edit_snippet}\n"
            "    result = ('Step 1 done.\\n\\n## Memory proposals\\n'\n"
            "              '- type: handoff\\n  id: mem-violation-handoff\\n  scope: project\\n'\n"
            "              '  tags: REQ-MEM-1\\n  summary: Must not be stored after a violation.\\n')\n"
            "else:\n"
            "    pathlib.Path('src/val.txt').write_text('MAGIC_99\\n', encoding='utf-8')\n"
            "    result = 'Step 2 done.'\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,"
            " 'total_cost_usd': 0.01, 'num_turns': 1, 'duration_ms': 10,"
            " 'usage': {'input_tokens': 20, 'output_tokens': 10}, 'result': result}))\n",
        )

        cred = base / "creds.json"
        cred.write_text('{"accessToken":"fake-eval-tok"}', encoding="utf-8")
        work_root = base / "work_root"
        real_run = subprocess.run

        def run_with_checker_timeout(cmd: object, *args: object, **kwargs: object) -> object:
            """Raise TimeoutExpired for the test_lock checker; run every other subprocess for real."""
            if isinstance(cmd, list) and any(str(part).endswith("test_lock.py") for part in cmd):
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=float(kwargs.get("timeout") or 0))
            return real_run(cmd, *args, **kwargs)

        run_patch = (
            mock.patch("subprocess.run", side_effect=run_with_checker_timeout)
            if checker_timeout
            else contextlib.nullcontext()
        )
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), run_patch:
            run_eval.main(
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
                    str(work_root),
                    "--out",
                    str(base / "out"),
                    "--agent-bin",
                    str(fake_agent),
                    "--allow-harness-errors",
                ]
            )
        records = []
        for run_json in sorted((base / "out").rglob("run.json")):
            data = json.loads(run_json.read_text(encoding="utf-8"))
            if "memory_arm" in data:
                records.append(data)
        out_text = "".join(p.read_text(encoding="utf-8") for p in sorted((base / "out").rglob("*.json")))
        return records, log, out_text

    def test_step_editing_test_lock_file_fails_trial_with_test_lock_violation(self) -> None:
        """A step removing a line from tests/test_framework.hpp fails the trial; its proposals and later steps drop."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            edit = (
                "pathlib.Path('tests/test_framework.hpp').write_text("
                "'// Test framework header.\\nint check_one() { return 1; }\\n', encoding='utf-8')"
            )
            records, log, _ = self._run_enforcement_trial(base, edit)
            self.assertEqual(2, len(records), "expected one trial per arm")
            for data in records:
                self.assertEqual("test_lock_violation", data["violation_reason"])
                self.assertEqual("test_lock_violation", data["outcome"])
            # Step 2 never ran: one agent call per trial.
            self.assertEqual(len(records), log.read_text(encoding="utf-8").count("call"))
            # The step's handoff proposal was not accepted.
            self.assertEqual([], list(base.rglob("mem-violation-handoff.md")))

    def test_step_editing_outside_allowed_files_fails_trial_with_scope_violation(self) -> None:
        """A step that writes a file outside allowed_files fails the trial with scope_violation."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            edit = "pathlib.Path('notes.txt').write_text('scratch\\n', encoding='utf-8')"
            records, log, _ = self._run_enforcement_trial(base, edit)
            self.assertEqual(2, len(records), "expected one trial per arm")
            for data in records:
                self.assertEqual("scope_violation", data["violation_reason"])
                self.assertEqual("scope_violation", data["outcome"])
            self.assertEqual(len(records), log.read_text(encoding="utf-8").count("call"))
            self.assertEqual([], list(base.rglob("mem-violation-handoff.md")))

    def test_checker_timeout_is_harness_error_and_evaluation_continues(self) -> None:
        """A test_lock checker timeout marks the trial harness_error with enforcement_timeout; no exception escapes."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            records, log, out_text = self._run_enforcement_trial(base, "pass", checker_timeout=True)
            # Both arms still produce a trial: the timeout does not abort the evaluation.
            self.assertEqual(2, len(records), "expected one trial per arm after a checker timeout")
            for data in records:
                self.assertEqual("harness_error", data["outcome"])
            self.assertIn("enforcement_timeout", out_text)
            # Step 2 never ran after the harness error: one agent call per trial.
            self.assertEqual(len(records), log.read_text(encoding="utf-8").count("call"))

    def test_violating_step_counts_toward_recall(self) -> None:
        """A violating step expecting 2 memories and recalling 0 gives memory_recall_rate 0.0, not 1.0."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            edit = (
                "pathlib.Path('tests/test_framework.hpp').write_text("
                "'// Test framework header.\\nint check_one() { return 1; }\\n', encoding='utf-8')"
            )
            records, _, _ = self._run_enforcement_trial(
                base, edit, step1_expected=("mem-alpha", "mem-beta")
            )
            with_memory = [d for d in records if d.get("memory_arm") == "with_memory"]
            self.assertEqual(1, len(with_memory))
            self.assertEqual("test_lock_violation", with_memory[0]["outcome"])
            self.assertEqual(0.0, with_memory[0]["memory_recall_rate"])

    def test_01_recall_decision_resets_repo_before_step2_so_without_memory_fails(self) -> None:
        """Memory seq 01 resets Step 2 repo to baseline; agent reading code sees no 0xA5U so memory-off fails and memory-on passes."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            seq_src = EVAL_DIR / "memory_tasks" / "01-recall-decision"
            mem_tasks = base / "memory_tasks"
            shutil.copytree(seq_src, mem_tasks / "01-recall-decision")

            # Fake agent inspects src/dtc_debouncer.cpp in Step 2; uses 0xA5U ONLY if in memory prompt or in code.
            fake_agent = _write_exec(
                base / "fake_claude.py",
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "prompt = sys.argv[-1]\n"
                "if 'Step 1:' in prompt:\n"
                "    # Plant 0xA5U in a scratch file and source to verify reset_repo_before strips it before Step 2.\n"
                "    pathlib.Path('src/dtc_debouncer.cpp').write_text("
                "pathlib.Path('src/dtc_debouncer.cpp').read_text(encoding='utf-8') + '\\n// 0xA5U\\n', encoding='utf-8')\n"
                "    out = ('Step 1 complete.\\n\\n## Memory proposals\\n'\n"
                "           '- type: handoff\\n  id: mem-dtc-freeze-sentinel\\n  scope: task:01-recall-decision\\n'\n"
                "           '  tags: REQ-DTC-005, dtc_debouncer, freezeSentinelCode, isFreezeSentinelMatch\\n'\n"
                "           '  summary: Active freeze sentinel code for REQ-DTC-005 in dtc_debouncer is 0xA5U (0x00U when inactive).\\n')\n"
                "else:\n"
                "    code_src = pathlib.Path('src/dtc_debouncer.cpp').read_text(encoding='utf-8')\n"
                "    sentinel = '0xA5U' if ('0xA5U' in prompt or '0xA5U' in code_src) else '0x01U'\n"
                "    hpp = pathlib.Path('include/dtc_debouncer.hpp')\n"
                "    hpp.write_text(hpp.read_text(encoding='utf-8').replace(\n"
                "        'private:',\n"
                "        '    /** @brief Return freeze sentinel. @return Sentinel code. */\\n'\n"
                "        '    std::uint8_t freezeSentinelCode() const noexcept;\\n'\n"
                "        '    /** @brief Match freeze sentinel. @param[in] candidateCode Code. @return True if match. */\\n'\n"
                "        '    bool isFreezeSentinelMatch(std::uint8_t candidateCode) const noexcept;\\n\\nprivate:',\n"
                "    ), encoding='utf-8')\n"
                "    cpp = pathlib.Path('src/dtc_debouncer.cpp')\n"
                "    cpp.write_text(code_src.replace(\n"
                "        '}  // namespace can_fixture',\n"
                "        f'std::uint8_t DtcDebouncer::freezeSentinelCode() const noexcept {{ return m_isLatched ? static_cast<std::uint8_t>({sentinel}) : static_cast<std::uint8_t>(0x00U); }}\\n'\n"
                "        f'bool DtcDebouncer::isFreezeSentinelMatch(std::uint8_t candidateCode) const noexcept {{ return candidateCode == static_cast<std::uint8_t>({sentinel}); }}\\n'\n"
                "        '}  // namespace can_fixture',\n"
                "    ), encoding='utf-8')\n"
                "    out = 'Step 2 complete.'\n"
                "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
                "'total_cost_usd':0.01,'num_turns':1,'duration_ms':10,"
                "'usage':{'input_tokens':20,'output_tokens':10},'result':out}))\n",
            )
            cred = base / "creds.json"
            cred.write_text('{"accessToken":"fake-eval-tok"}', encoding="utf-8")
            out_dir = base / "out"
            rc = run_eval.main(
                [
                    "--mode", "memory", "--engines", "haiku", "--k", "1",
                    "--tasks", "01-recall-decision",
                    "--isolation", "local", "--allow-unsandboxed-local",
                    "--credentials", str(cred),
                    "--fixture-dir", str(EVAL_DIR / "fixture"),
                    "--memory-tasks-dir", str(mem_tasks),
                    "--work-root", str(base / "wr"),
                    "--out", str(out_dir),
                    "--agent-bin", str(fake_agent),
                ]
            )
            self.assertEqual(0, rc)
            results = json.loads((out_dir / "results.json").read_text(encoding="utf-8"))
            ts = results["tasks"][0]
            self.assertEqual(1.0, ts["pass_with_memory"])
            self.assertEqual(0.0, ts["pass_without_memory"])
            self.assertEqual(1.0, ts["memory_ab_delta"])


if __name__ == "__main__":
    unittest.main()
