"""Unit tests for learning/harvest.py, learning/propose.py, and learning/validate.py."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from learning import harvest, propose, validate  # noqa: E402


def _make_run_dir(
    runs_root: Path,
    run_id: str,
    *,
    started: str,
    engine: str,
    task: str,
    outcome: str = "fail",
    gate_text: str = "",
    proto_errors: int = 0,
    review_text: str = "",
    stop_text: str = "",
) -> Path:
    """Create a synthetic v0.2 run directory for harvest unit tests."""
    rdir = runs_root / run_id
    steps = rdir / "steps"
    steps.mkdir(parents=True, exist_ok=True)
    engines_map = {"implementer": engine}
    if review_text:
        engines_map["reviewer"] = "sonnet"
    (rdir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "kind": "task",
                "started": started,
                "finished": started,
                "engines": engines_map,
                "task": task,
                "outcome": outcome,
                "escalation_reason": "Stopped after 2 rounds" if outcome == "ESCALATE_TO_HUMAN" else "",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (steps / f"01-implementer-{engine}.meta.json").write_text(
        json.dumps(
            {
                "role": "implementer",
                "engine": engine,
                "status": "ok",
                "tool_protocol_errors": proto_errors,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if gate_text:
        (steps / "02-gate.txt").write_text(gate_text, encoding="utf-8")
    if review_text:
        (steps / "03-reviewer-sonnet.meta.json").write_text(
            json.dumps(
                {
                    "role": "reviewer",
                    "engine": "sonnet",
                    "status": "ok",
                    "final_text": review_text,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    if stop_text:
        (rdir / "STOP_REPORT.md").write_text(stop_text, encoding="utf-8")
    return rdir


class LearningLoopTests(unittest.TestCase):
    """Tests covering harvest aggregation/redaction, propose thresholds/40-line cap, and validate A/B logic."""

    def test_harvest_counts_per_engine_rule_since_filter_and_redaction(self) -> None:
        """Verify harvest counts lint, protocol, review, and STOP findings per engine, filters by --since, and redacts secrets/paths."""
        fake_home = "/" + "ho" + "me/alice/secret_proj"
        fake_secret = "SECRET_TOKEN_XYZ_998877"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runs_root = base / "runs"

            # Old run that should be excluded when --since 2026-11-10 is passed
            _make_run_dir(
                runs_root,
                "20261101T100000Z-aaaaaa",
                started="2026-11-01T10:00:00+00:00",
                engine="local",
                task="01-old",
                gate_text="  - [ERROR][MISRA-8.2.2] src/old.cpp:3: C-style cast\n",
            )
            # Newer runs for local and haiku
            _make_run_dir(
                runs_root,
                "20261115T100000Z-bbbbbb",
                started="2026-11-15T10:00:00+00:00",
                engine="local",
                task="02-dtc",
                outcome="ESCALATE_TO_HUMAN",
                gate_text=(
                    "[preflight_lint]\n"
                    f"  - [ERROR][DOXYGEN-PARAM-DIR] {fake_home}/src/dtc.cpp:12: Missing [in] on @param token={fake_secret}\n"
                    "  - [ERROR][DOXYGEN-PARAM-DIR] include/dtc.hpp:15: Missing [in] on @param\n"
                    "  - [ERROR][NO-IMPL-IN-HEADER] include/dtc.hpp:20: inline function in header\n"
                ),
                proto_errors=2,
                review_text=(
                    "| ID | Category | Severity | file:line | Problem (with evidence) | Required fix |\n"
                    "|---|---|---|---|---|---|\n"
                    "| F-1 | COR | major | src/dtc.cpp:30 | Off-by-one threshold | Use >= |\n"
                    "VERDICT: REQUEST_CHANGES\n"
                ),
                stop_text=f"STOP_REPORT: conflict in {fake_home} api_key={fake_secret}\n",
            )
            _make_run_dir(
                runs_root,
                "20261116T100000Z-cccccc",
                started="2026-11-16T10:00:00+00:00",
                engine="local",
                task="03-link",
                gate_text="  - [ERROR][DOXYGEN-PARAM-DIR] src/link.cpp:4: Missing [in]\n",
            )
            _make_run_dir(
                runs_root,
                "20261116T110000Z-dddddd",
                started="2026-11-16T11:00:00+00:00",
                engine="haiku",
                task="04-codec",
                gate_text="  - [ERROR][MISRA-8.2.2] src/codec.cpp:9: C-style cast\n",
            )

            out_dir = base / "reports"
            rc = harvest.main(
                [
                    str(runs_root),
                    "--since",
                    "2026-11-10",
                    "--out-dir",
                    str(out_dir),
                    "--date",
                    "2026-11-23",
                ]
            )
            self.assertEqual(0, rc)
            json_path = out_dir / "2026-11-23.json"
            md_path = out_dir / "2026-11-23.md"
            self.assertTrue(json_path.is_file())
            self.assertTrue(md_path.is_file())

            report = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(3, report["runs_scanned"])
            self.assertEqual(1, report["runs_skipped"])

            local_rules = report["engines"]["local"]
            self.assertEqual(3, local_rules["DOXYGEN-PARAM-DIR"]["count"])
            self.assertEqual(2, local_rules["DOXYGEN-PARAM-DIR"]["runs_affected"])
            self.assertEqual(1, local_rules["NO-IMPL-IN-HEADER"]["count"])
            self.assertEqual(2, local_rules["TOOL-PROTOCOL-ERROR"]["count"])
            self.assertEqual(1, local_rules["REVIEW-COR"]["count"])
            self.assertEqual(1, local_rules["STOP-REPORT"]["count"])
            self.assertEqual(1, local_rules["ESCALATE-TO-HUMAN"]["count"])
            self.assertNotIn("MISRA-8.2.2", local_rules)

            haiku_rules = report["engines"]["haiku"]
            self.assertEqual(1, haiku_rules["MISRA-8.2.2"]["count"])

            for text in (json_path.read_text(encoding="utf-8"), md_path.read_text(encoding="utf-8")):
                self.assertNotIn(fake_home, text)
                self.assertNotIn("/" + "ho" + "me/", text)
                self.assertNotIn(fake_secret, text)

    def test_propose_thresholds_versioning_40_line_cap_and_active_protection(self) -> None:
        """Verify propose enforces min_count threshold, increments version, caps at 40 lines, and never overwrites active profiles."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            findings = [
                {
                    "engine": "local",
                    "rule_id": f"RULE-{idx:03d}",
                    "kind": "lint",
                    "count": 100 - idx,
                    "runs_affected": 5,
                    "examples": [f"example for RULE-{idx:03d}"],
                }
                for idx in range(55)
            ]
            # Add one below threshold (count=2) and one for another engine
            findings.append(
                {
                    "engine": "local",
                    "rule_id": "LOW-COUNT-RULE",
                    "kind": "lint",
                    "count": 2,
                    "runs_affected": 1,
                    "examples": ["low count"],
                }
            )
            findings.append(
                {
                    "engine": "haiku",
                    "rule_id": "HAIKU-ONLY-RULE",
                    "kind": "lint",
                    "count": 50,
                    "runs_affected": 5,
                    "examples": ["haiku only"],
                }
            )
            harvest_path = base / "harvest.json"
            harvest_path.write_text(json.dumps({"findings": findings}), encoding="utf-8")

            active_profile = base / "local_active.md"
            active_profile.write_text("# Engine Profile: local\nVersion: 2\n", encoding="utf-8")
            cand_out = base / "candidate_local.md"

            rc = propose.main(
                [
                    "--harvest",
                    str(harvest_path),
                    "--engine",
                    "local",
                    "--active-profile",
                    str(active_profile),
                    "--out",
                    str(cand_out),
                    "--min-count",
                    "3",
                    "--top-n",
                    "50",
                    "--max-lines",
                    "40",
                ]
            )
            self.assertEqual(0, rc)
            cand_text = cand_out.read_text(encoding="utf-8")
            cand_lines = cand_text.splitlines()
            self.assertLessEqual(len(cand_lines), 40)
            self.assertEqual(40, len(cand_lines))
            self.assertIn("Version: 3", cand_text)
            self.assertIn("RULE-000", cand_text)
            self.assertIn("(100 occurrences)", cand_text)
            self.assertNotIn("LOW-COUNT-RULE", cand_text)
            self.assertNotIn("HAIKU-ONLY-RULE", cand_text)
            # Active profile was not modified
            self.assertEqual("# Engine Profile: local\nVersion: 2\n", active_profile.read_text(encoding="utf-8"))

            # Refusing to overwrite active profile path
            rc_refuse = propose.main(
                [
                    "--harvest",
                    str(harvest_path),
                    "--engine",
                    "local",
                    "--active-profile",
                    str(active_profile),
                    "--out",
                    str(active_profile),
                ]
            )
            self.assertEqual(1, rc_refuse)

    def test_validate_decision_logic_accept_reject_regression_and_no_gain(self) -> None:
        """Verify validate ACCEPTS when lint drops without regression, and REJECTS on regression or no gain."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cand = base / "candidate.md"
            cand.write_text(
                "# Engine Profile: local\nVersion: 1\n- [MISRA-8.2.2] (5 occurrences): Cast -> static_cast\n",
                encoding="utf-8",
            )
            active = base / "active.md"
            active.write_text("# Engine Profile: local\nVersion: 0\n", encoding="utf-8")

            # 1. ACCEPT: lint drops 3.0 -> 0.5, pass rate 0.5 -> 1.0
            active_ok = [
                {"passed": True, "harness_error": False, "lint_findings_first_attempt": 2},
                {"passed": False, "harness_error": False, "lint_findings_first_attempt": 4},
            ]
            cand_accept = [
                {"passed": True, "harness_error": False, "lint_findings_first_attempt": 1},
                {"passed": True, "harness_error": False, "lint_findings_first_attempt": 0},
            ]
            a_json = base / "a.json"
            c_json = base / "c.json"
            a_json.write_text(json.dumps(active_ok), encoding="utf-8")
            c_json.write_text(json.dumps(cand_accept), encoding="utf-8")
            out_dir = base / "reports"
            rc_acc = validate.main(
                [
                    "--engine",
                    "local",
                    "--candidate",
                    str(cand),
                    "--active",
                    str(active),
                    "--results-json-pair",
                    str(a_json),
                    str(c_json),
                    "--out-dir",
                    str(out_dir),
                ]
            )
            self.assertEqual(0, rc_acc)
            report_files = list(out_dir.glob("validate-local-*.json"))
            self.assertEqual(1, len(report_files))
            dec_acc = json.loads(report_files[0].read_text(encoding="utf-8"))
            self.assertEqual("ACCEPT", dec_acc["decision"])
            self.assertAlmostEqual(-2.5, dec_acc["lint_delta"])
            md_report = next(out_dir.glob("validate-local-*.md")).read_text(encoding="utf-8")
            self.assertIn("git commit", md_report)
            # Active profile remains untouched
            self.assertEqual("# Engine Profile: local\nVersion: 0\n", active.read_text(encoding="utf-8"))

            # 2. REJECT on regression: lint drops 3.0 -> 0.5, pass rate drops 1.0 -> 0.5
            active_pass = [
                {"passed": True, "harness_error": False, "lint_findings_first_attempt": 3},
                {"passed": True, "harness_error": False, "lint_findings_first_attempt": 3},
            ]
            cand_regress = [
                {"passed": True, "harness_error": False, "lint_findings_first_attempt": 0},
                {"passed": False, "harness_error": False, "lint_findings_first_attempt": 1},
            ]
            a_json.write_text(json.dumps(active_pass), encoding="utf-8")
            c_json.write_text(json.dumps(cand_regress), encoding="utf-8")
            rc_reg = validate.main(
                [
                    "--engine",
                    "local",
                    "--candidate",
                    str(cand),
                    "--active",
                    str(active),
                    "--results-json-pair",
                    str(a_json),
                    str(c_json),
                    "--out-dir",
                    str(out_dir),
                ]
            )
            self.assertEqual(1, rc_reg)
            dec_reg = json.loads(report_files[0].read_text(encoding="utf-8"))
            self.assertEqual("REJECT", dec_reg["decision"])
            self.assertIn("regressed", dec_reg["reason"].lower())

            # 3. REJECT on no gain: pass rate equal, lint does not drop (2.0 -> 2.0)
            cand_nogain = [
                {"passed": True, "harness_error": False, "lint_findings_first_attempt": 2},
                {"passed": True, "harness_error": False, "lint_findings_first_attempt": 2},
            ]
            active_nogain = [
                {"passed": True, "harness_error": False, "lint_findings_first_attempt": 2},
                {"passed": True, "harness_error": False, "lint_findings_first_attempt": 2},
            ]
            a_json.write_text(json.dumps(active_nogain), encoding="utf-8")
            c_json.write_text(json.dumps(cand_nogain), encoding="utf-8")
            rc_ng = validate.main(
                [
                    "--engine",
                    "local",
                    "--candidate",
                    str(cand),
                    "--active",
                    str(active),
                    "--results-json-pair",
                    str(a_json),
                    str(c_json),
                    "--out-dir",
                    str(out_dir),
                ]
            )
            self.assertEqual(1, rc_ng)
            dec_ng = json.loads(report_files[0].read_text(encoding="utf-8"))
            self.assertEqual("REJECT", dec_ng["decision"])
            self.assertIn("no gain", dec_ng["reason"].lower())

            # 4. Candidate > 40 lines rejected with exit 2
            too_long = base / "too_long.md"
            too_long.write_text("\n".join(f"- line {i}" for i in range(41)) + "\n", encoding="utf-8")
            rc_long = validate.main(
                [
                    "--engine",
                    "local",
                    "--candidate",
                    str(too_long),
                    "--results-json-pair",
                    str(a_json),
                    str(c_json),
                    "--out-dir",
                    str(out_dir),
                ]
            )
            self.assertEqual(2, rc_long)

    def test_validate_end_to_end_ab_with_fake_agent_and_cli_help(self) -> None:
        """Verify validate runs offline A/B with a fake agent reacting to injected profile and CLIs --help exit 0."""
        for script in ("harvest.py", "propose.py", "validate.py"):
            res = subprocess.run(
                ["python3", str(REPO_ROOT / "learning" / script), "--help"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, res.returncode, f"{script} --help failed: {res.stderr}")

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fixture = base / "fixture"
            for sub in ("src", "include", "tests"):
                (fixture / sub).mkdir(parents=True)
            (fixture / "CMakeLists.txt").write_text("# mini\n", encoding="utf-8")
            (fixture / "build_and_test.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            (fixture / "preflight.toml").write_text('[general]\nstd = "c++17"\n', encoding="utf-8")
            (fixture / "src" / "m.cpp").write_text(
                "/**\n * @file m.cpp\n * @brief M.\n */\n"
                "/**\n * @brief Fn.\n * @return Zero.\n */\n"
                "int fn() { return 0; }\n",
                encoding="utf-8",
            )

            tasks_dir = base / "tasks"
            tdir = tasks_dir / "01-smoke-cast"
            (tdir / "hidden").mkdir(parents=True)
            (tdir / "task.md").write_text(
                "# Task 01\n\nCategory: bugfix\nTier: smoke\n\n## Goal\nReturn 42.\n",
                encoding="utf-8",
            )
            (tdir / "setup.patch").write_text(
                "diff --git a/src/m.cpp b/src/m.cpp\n"
                "--- a/src/m.cpp\n+++ b/src/m.cpp\n"
                "@@ -4 +4 @@\n-int fn() { return 0; }\n+int fn() { return -1; }\n",
                encoding="utf-8",
            )
            (tdir / "solution.patch").write_text(
                "diff --git a/src/m.cpp b/src/m.cpp\n"
                "--- a/src/m.cpp\n+++ b/src/m.cpp\n"
                "@@ -4 +4 @@\n-int fn() { return -1; }\n+int fn() { return 42; }\n",
                encoding="utf-8",
            )
            (tdir / "check.sh").write_text(
                "#!/usr/bin/env bash\nset -euo pipefail\ngrep -q '42' \"$2/src/m.cpp\"\n",
                encoding="utf-8",
            )

            fake_agent = base / "fake_claude.py"
            fake_agent.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "prompt = sys.argv[-1]\n"
                "if 'MISRA-8.2.2' in prompt:\n"
                "    code = ('/**\\n * @file m.cpp\\n * @brief M.\\n */\\n'\n"
                "            '/**\\n * @brief Fn.\\n * @return 42.\\n */\\n'\n"
                "            'int fn() { return static_cast<int>(42.0); }\\n')\n"
                "else:\n"
                "    code = ('/**\\n * @file m.cpp\\n * @brief M.\\n */\\n'\n"
                "            '/**\\n * @brief Fn.\\n * @return 42.\\n */\\n'\n"
                "            'int fn() { return (int)42.0; }\\n')\n"
                "pathlib.Path('src/m.cpp').write_text(code, encoding='utf-8')\n"
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'local-coder'}))\n"
                "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1, 'result': 'done'}))\n",
                encoding="utf-8",
            )
            os.chmod(fake_agent, stat.S_IRWXU)

            active_prof = base / "active.md"
            active_prof.write_text("# Engine Profile: local\nVersion: 0\n", encoding="utf-8")
            cand_prof = base / "cand.md"
            cand_prof.write_text(
                "# Engine Profile: local\nVersion: 1\n"
                "- [MISRA-8.2.2] (5 occurrences): C-style cast -> Use static_cast<T>(expr).\n",
                encoding="utf-8",
            )
            out_dir = base / "val_reports"
            old_env = os.environ.copy()
            os.environ["DEVPACK_LOCAL_BASE_URL"] = "http://127.0.0.1:11434"
            os.environ["DEVPACK_LOCAL_AUTH_TOKEN"] = "LOCAL_TEST_TOK"
            try:
                rc = validate.main(
                    [
                        "--engine",
                        "local",
                        "--candidate",
                        str(cand_prof),
                        "--active",
                        str(active_prof),
                        "--k",
                        "1",
                        "--isolation",
                        "local",
                        "--allow-unsandboxed-local",
                        "--agent-bin",
                        str(fake_agent),
                        "--fixture-dir",
                        str(fixture),
                        "--tasks-dir",
                        str(tasks_dir),
                        "--out-dir",
                        str(out_dir),
                    ]
                )
            finally:
                os.environ.clear()
                os.environ.update(old_env)

            self.assertEqual(0, rc)
            rep_json = next(out_dir.glob("validate-local-*.json"))
            dec = json.loads(rep_json.read_text(encoding="utf-8"))
            self.assertEqual("ACCEPT", dec["decision"])
            self.assertGreater(dec["mean_lint_active"], 0.0)
            self.assertEqual(0.0, dec["mean_lint_candidate"])


if __name__ == "__main__":
    unittest.main()
