"""Unit tests for eval/run_eval.py --mode memory and eval/memory_tasks/."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
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
                "           '- type: handoff\\n  id: mem-step1-key\\n  scope: project\\n'\n"
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


if __name__ == "__main__":
    unittest.main()
