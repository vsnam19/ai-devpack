"""Unit tests for eval/run_eval.py context/brief arms, ablation attribution, and failure-causes reporting."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
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


def _make_fixture_and_tasks(base_dir: Path) -> tuple[Path, Path]:
    """Create a synthetic C++ fixture and two graded tasks (one with oracle.md, one without)."""
    fixture_dir = base_dir / "fixture"
    for sub in ("include", "src", "tests"):
        (fixture_dir / sub).mkdir(parents=True, exist_ok=True)
    (fixture_dir / "CMakeLists.txt").write_text("# fixture\n", encoding="utf-8")
    (fixture_dir / "build_and_test.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
        encoding="utf-8",
    )
    (fixture_dir / "include" / "widget.hpp").write_text(
        "/** @file widget.hpp\n * @brief Widget header.\n */\n"
        "/** @brief Return widget status. */\n"
        "int widgetStatus() noexcept;\n",
        encoding="utf-8",
    )
    (fixture_dir / "src" / "widget.cpp").write_text(
        "#include \"widget.hpp\"\nint widgetStatus() noexcept { return 1; }\n",
        encoding="utf-8",
    )

    setup_patch = (
        "diff --git a/src/widget.cpp b/src/widget.cpp\n"
        "--- a/src/widget.cpp\n"
        "+++ b/src/widget.cpp\n"
        "@@ -1,2 +1,2 @@\n"
        " #include \"widget.hpp\"\n"
        "-int widgetStatus() noexcept { return 1; }\n"
        "+int widgetStatus() noexcept { return 0; }\n"
    )
    tasks_dir = base_dir / "tasks"

    # Task 1: has oracle.md
    t1 = tasks_dir / "11-mini-oracle"
    (t1 / "hidden").mkdir(parents=True, exist_ok=True)
    (t1 / "hidden" / "h.cpp").write_text("// hidden\n", encoding="utf-8")
    (t1 / "task.md").write_text(
        "# Task: 11-mini-oracle\n\n"
        "Category: vague-bugfix\n"
        "Tier: v2\n\n"
        "## Goal\n"
        "Fix widget status.\n\n"
        "Extra details in second paragraph.\n\n"
        "## Files allowed\n"
        "- `src/widget.cpp`\n",
        encoding="utf-8",
    )
    (t1 / "oracle.md").write_text(
        "## Oracle Design\n"
        "- Update `widgetStatus()` in `src/widget.cpp` to return 42.\n",
        encoding="utf-8",
    )
    (t1 / "setup.patch").write_text(setup_patch, encoding="utf-8")
    (t1 / "solution.patch").write_text(setup_patch, encoding="utf-8")
    (t1 / "check.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "WORKDIR=\"$PWD\"\n"
        "if [[ \"${1:-}\" == \"--workdir\" ]]; then WORKDIR=\"$2\"; fi\n"
        "grep -q 'return 42;' \"$WORKDIR/src/widget.cpp\"\n",
        encoding="utf-8",
    )

    # Task 2: no oracle.md
    t2 = tasks_dir / "12-mini-nooracle"
    (t2 / "hidden").mkdir(parents=True, exist_ok=True)
    (t2 / "hidden" / "h.cpp").write_text("// hidden\n", encoding="utf-8")
    (t2 / "task.md").write_text(
        "# Task: 12-mini-nooracle\n\n"
        "Category: vague-bugfix\n"
        "Tier: v2\n\n"
        "## Goal\n"
        "Fix widget status to 99.\n",
        encoding="utf-8",
    )
    (t2 / "setup.patch").write_text(setup_patch, encoding="utf-8")
    (t2 / "solution.patch").write_text(setup_patch, encoding="utf-8")
    (t2 / "check.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "WORKDIR=\"$PWD\"\n"
        "if [[ \"${1:-}\" == \"--workdir\" ]]; then WORKDIR=\"$2\"; fi\n"
        "grep -q 'return 99;' \"$WORKDIR/src/widget.cpp\"\n",
        encoding="utf-8",
    )
    return fixture_dir, tasks_dir


def _write_fake_agent(base_dir: Path, body: str) -> Path:
    """Create an executable Python fake agent CLI script."""
    script = base_dir / "fake_agent.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "cwd = pathlib.Path.cwd()\n"
        "prompt = sys.argv[-1]\n"
        + body,
        encoding="utf-8",
    )
    os.chmod(script, stat.S_IRWXU)
    return script


class EvalArmsAndAblationTests(unittest.TestCase):
    """Verify prompt arm construction, ablation attribution rules, dry-run, and summary reporting."""

    def test_arm_selection_builds_expected_prompts_and_skips_missing_oracle(self) -> None:
        """Verify standard, minimal, oracle, no_oracle skip, and repomap Code map prompt assembly."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture_dir, tasks_dir = _make_fixture_and_tasks(base)
            tasks = {t.task_id: t for t in run_eval.load_and_validate_tasks(tasks_dir, "all")}
            t1 = tasks["11-mini-oracle"]
            t2 = tasks["12-mini-nooracle"]

            # Standard + none is byte-for-byte identical to task.md
            std_txt, std_skip = run_eval.build_task_prompt_text(
                t1, brief_variant="standard", context_mode="none", work_dir=fixture_dir
            )
            self.assertIsNone(std_skip)
            self.assertEqual(t1.task_md.read_text(encoding="utf-8"), std_txt)

            # Minimal keeps title + first paragraph of Goal, drops extra paragraphs and Files allowed
            min_txt, min_skip = run_eval.build_task_prompt_text(
                t1, brief_variant="minimal", context_mode="none", work_dir=fixture_dir
            )
            self.assertIsNone(min_skip)
            self.assertIn("Fix widget status.", min_txt)
            self.assertNotIn("Extra details", min_txt)
            self.assertNotIn("Files allowed", min_txt)

            # Oracle appends oracle.md when present, and skips with no_oracle when absent
            ora_txt, ora_skip = run_eval.build_task_prompt_text(
                t1, brief_variant="oracle", context_mode="none", work_dir=fixture_dir
            )
            self.assertIsNone(ora_skip)
            self.assertIn("## Oracle Design", ora_txt)
            self.assertIn("return 42", ora_txt)

            no_ora_txt, no_ora_skip = run_eval.build_task_prompt_text(
                t2, brief_variant="oracle", context_mode="none", work_dir=fixture_dir
            )
            self.assertEqual("", no_ora_txt)
            self.assertEqual("no_oracle", no_ora_skip)

            # Repomap injects ## Code map with symbols from fixture
            rm_txt, rm_skip = run_eval.build_task_prompt_text(
                t1, brief_variant="standard", context_mode="repomap", work_dir=fixture_dir, repomap_budget=800
            )
            self.assertIsNone(rm_skip)
            self.assertIn("## Code map", rm_txt)
            self.assertIn("widgetStatus", rm_txt)

            # Repomap off has no ## Code map
            self.assertNotIn("## Code map", std_txt)

    def test_compute_ablation_cause_table(self) -> None:
        """Table-driven verification of SPEC 5.6 ablation attribution rules."""
        cases = [
            # (label, base_passed, oracle, repomap, skill, analyze_cause, signals, expected)
            ("base_passed", True, False, False, False, "MODEL", {}, None),
            ("only_oracle", False, True, False, False, "MODEL", {}, "BRIEF"),
            ("only_repomap", False, False, True, False, "MODEL", {}, "CONTEXT"),
            ("only_skill", False, False, False, True, "MODEL", {}, "SKILL"),
            ("oracle_skipped_repomap_ok", False, None, True, False, "MODEL", {}, "CONTEXT"),
            ("multi_repomap_and_oracle", False, True, True, False, "MODEL", {}, "CONTEXT"),
            ("none_pass_analyze_tool", False, False, False, False, "TOOL", {}, "TOOL"),
            ("none_pass_signal_denied", False, False, False, False, "MODEL", {"denied_calls": [{"tool": "Bash"}]}, "TOOL"),
            ("none_pass_signal_over_ctx", False, False, False, False, None, {"prompt_over_context": True}, "TOOL"),
            ("none_pass_signal_proto_err", False, False, False, False, None, {"tool_protocol_errors": 2}, "TOOL"),
            ("none_pass_analyze_env", False, False, False, False, "ENV", {}, "ENV"),
            ("none_pass_analyze_model", False, False, False, False, "MODEL", {}, "MODEL"),
            ("none_pass_ambiguous", False, False, False, False, None, {}, "MODEL_OR_TOOL"),
        ]
        for label, base_ok, o_ok, r_ok, s_ok, a_cause, sigs, expected in cases:
            with self.subTest(label=label):
                got = run_eval.compute_ablation_cause(
                    base_ok,
                    oracle_passed=o_ok,
                    repomap_passed=r_ok,
                    skill_passed=s_ok,
                    analyze_cause=a_cause,
                    analyze_signals=sigs,
                )
                self.assertEqual(expected, got)

    def test_dry_run_ablate_lists_ablation_arms(self) -> None:
        """Verify --dry-run --ablate lists oracle-brief, repomap, and skill-on/off arms."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture_dir, tasks_dir = _make_fixture_and_tasks(base)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = run_eval.main(
                    [
                        "--dry-run",
                        "--mode", "single",
                        "--engines", "haiku",
                        "--k", "1",
                        "--ablate",
                        "--fixture-dir", str(fixture_dir),
                        "--tasks-dir", str(tasks_dir),
                    ]
                )
            self.assertEqual(0, rc)
            out = buf.getvalue()
            self.assertIn("ablate: enabled", out)
            self.assertIn("oracle-brief", out)
            self.assertIn("repomap", out)
            self.assertIn("skill-on", out)
            self.assertIn("oracle=yes", out)
            self.assertIn("oracle=no", out)

    def test_ablate_end_to_end_records_run_json_attribution_and_summary_table(self) -> None:
        """End-to-end --ablate with fake agent attributes BRIEF/CONTEXT/MODEL and renders Failure causes table."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture_dir, tasks_dir = _make_fixture_and_tasks(base)
            # Add task 13 that only passes with ## Code map (repomap -> CONTEXT)
            t3 = tasks_dir / "13-mini-context"
            (t3 / "hidden").mkdir(parents=True, exist_ok=True)
            (t3 / "hidden" / "h.cpp").write_text("// hidden\n", encoding="utf-8")
            (t3 / "task.md").write_text(
                "# Task: 13-mini-context\n\nCategory: vague-bugfix\nTier: v2\n\n"
                "## Goal\nSet widgetStatus to 77.\n",
                encoding="utf-8",
            )
            setup_patch_t3 = (
                "diff --git a/src/widget.cpp b/src/widget.cpp\n"
                "--- a/src/widget.cpp\n"
                "+++ b/src/widget.cpp\n"
                "@@ -1,2 +1,2 @@\n"
                " #include \"widget.hpp\"\n"
                "-int widgetStatus() noexcept { return 1; }\n"
                "+int widgetStatus() noexcept { return 0; }\n"
            )
            (t3 / "setup.patch").write_text(setup_patch_t3, encoding="utf-8")
            (t3 / "solution.patch").write_text(setup_patch_t3, encoding="utf-8")
            (t3 / "check.sh").write_text(
                "#!/usr/bin/env bash\nset -euo pipefail\nWORKDIR=\"$PWD\"\n"
                "if [[ \"${1:-}\" == \"--workdir\" ]]; then WORKDIR=\"$2\"; fi\n"
                "grep -q 'return 77;' \"$WORKDIR/src/widget.cpp\"\n",
                encoding="utf-8",
            )

            # Fake agent:
            # - For task 11-mini-oracle: succeeds only when ## Oracle Design is in prompt (BRIEF)
            # - For task 13-mini-context: succeeds only when ## Code map is in prompt (CONTEXT)
            # - For task 12-mini-nooracle: always fails (MODEL)
            agent_bin = _write_fake_agent(
                base,
                "target = cwd / 'src' / 'widget.cpp'\n"
                "if '11-mini-oracle' in prompt and '## Oracle Design' in prompt:\n"
                "    target.write_text('#include \"widget.hpp\"\\nint widgetStatus() noexcept { return 42; }\\n', encoding='utf-8')\n"
                "elif '13-mini-context' in prompt and '## Code map' in prompt:\n"
                "    target.write_text('#include \"widget.hpp\"\\nint widgetStatus() noexcept { return 77; }\\n', encoding='utf-8')\n"
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'local-coder'}))\n"
                "print(json.dumps({'type': 'result', 'is_error': False, 'result': 'done', 'num_turns': 1, 'usage': {'input_tokens': 20, 'output_tokens': 5}}))\n",
            )
            out_dir = base / "out"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with mock.patch.dict(
                    os.environ,
                    {"DEVPACK_LOCAL_BASE_URL": "http://127.0.0.1:11434", "DEVPACK_LOCAL_AUTH_TOKEN": "TOK"},
                    clear=False,
                ):
                    rc = run_eval.main(
                        [
                            "--mode", "single",
                            "--engines", "local",
                            "--k", "1",
                            "--ablate",
                            "--isolation", "local",
                            "--allow-unsandboxed-local",
                            "--agent-bin", str(agent_bin),
                            "--fixture-dir", str(fixture_dir),
                            "--tasks-dir", str(tasks_dir),
                            "--work-root", str(base / "work"),
                            "--out", str(out_dir),
                        ]
                    )
            self.assertEqual(0, rc)

            results = json.loads((out_dir / "results.json").read_text(encoding="utf-8"))
            summary_md = (out_dir / "summary.md").read_text(encoding="utf-8")

            tasks_by_id = {t["task_id"]: t for t in results["tasks"]}
            self.assertEqual("BRIEF", tasks_by_id["11-mini-oracle"]["ablation_cause"])
            self.assertEqual("CONTEXT", tasks_by_id["13-mini-context"]["ablation_cause"])
            self.assertEqual("MODEL", tasks_by_id["12-mini-nooracle"]["ablation_cause"])

            # Each failed base trial ran devpack_logs analyze and stored suspected_cause
            self.assertIsNotNone(tasks_by_id["11-mini-oracle"]["runs"][0]["suspected_cause"])
            self.assertEqual(
                "no_oracle",
                tasks_by_id["12-mini-nooracle"]["ablation_arms"]["oracle-brief"]["skip_reason"],
            )

            # Check run.json records context mode for repomap runs
            run_jsons = [
                json.loads(p.read_text(encoding="utf-8"))
                for p in (out_dir).rglob("run.json")
            ]
            self.assertTrue(any(rj.get("context") == "repomap" for rj in run_jsons), run_jsons)
            self.assertTrue(any(rj.get("context") == "none" for rj in run_jsons), run_jsons)

            # Summary contains Failure causes table with counts and shares
            self.assertIn("## Failure causes", summary_md)
            self.assertIn("| Engine | Cause | Count | Share | Example task |", summary_md)
            self.assertIn("| local | BRIEF | 1 | 33.3% | 11-mini-oracle |", summary_md)
            self.assertIn("| local | CONTEXT | 1 | 33.3% | 13-mini-context |", summary_md)
            self.assertIn("| local | MODEL | 1 | 33.3% | 12-mini-nooracle |", summary_md)
            self.assertIn("## Ablation Arms", summary_md)


if __name__ == "__main__":
    unittest.main()
