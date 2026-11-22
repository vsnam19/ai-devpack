"""Tests for the deterministic skill test harness."""

import json
import os
import subprocess
import tempfile
from skilltest.skilltest import load_config
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "skilltest" / "skilltest.py"


class SkilltestCliTests(unittest.TestCase):
    """Exercise linting, transcript detection, planning, and fake-agent execution."""

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the CLI from a temporary output location."""
        return subprocess.run(
            ["python3", str(CLI), *args], cwd=ROOT, text=True, capture_output=True, check=False
        )

    def test_detect_trigger_variants(self) -> None:
        """Skill and Read tool calls are triggers; unrelated calls are not."""
        from skilltest.skilltest import transcript_triggered

        skill = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "sample"}}]}})
        read = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": ".claude/skills/sample/SKILL.md"}}]}})
        neither = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Glob", "input": {"pattern": "*.md"}}]}})
        self.assertTrue(transcript_triggered(skill, "sample"))
        self.assertTrue(transcript_triggered(read, "sample"))
        self.assertFalse(transcript_triggered(neither, "sample"))

    def test_lint_accepts_existing_skills(self) -> None:
        """All shipped skills meet the deterministic contract."""
        for name in ("misra-cpp-2023", "doxygen-naming", "review-taxonomy", "skill-authoring"):
            result = self.run_cli("lint", f"knowledge/skills/{name}")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_lint_rejects_bad_skills(self) -> None:
        """Lint rejects frontmatter, path, size, and case-count violations."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bad = root / "wrong"
            (bad / "tests").mkdir(parents=True)
            (bad / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")
            (bad / "tests" / "skilltest.toml").write_text("", encoding="utf-8")
            result = self.run_cli("lint", str(bad))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("frontmatter", result.stderr)

    def test_lint_rejects_absolute_paths_multiple_broken_links_and_orphaned_inputs(self) -> None:
        """Lint examines every link and every declared fixture input directory."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sample"
            (root / "tests" / "fixtures" / "used" / "input").mkdir(parents=True)
            (root / "tests" / "fixtures" / "orphan" / "input").mkdir(parents=True)
            (root / "tests" / "graders" / "used").mkdir(parents=True)
            (root / "tests" / "fixtures" / "used" / "input" / "input.txt").write_text("input\n", encoding="utf-8")
            (root / "tests" / "graders" / "used" / "check.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            (root / "SKILL.md").write_text(
                "---\nname: sample\ndescription: sample skill\n---\n"
                "[valid](SKILL.md) and [broken](missing.md) and [root](/arbitrary/file)\n"
                "Examples: /Users/example /opt/tool /usr/bin /var/log /etc/config /tmp/work /mnt/data C:\\\\work\\\\file\n",
                encoding="utf-8",
            )
            (root / "tests" / "skilltest.toml").write_text(
                "[[trigger]]\nexpect = true\n[[trigger]]\nexpect = true\n"
                "[[trigger]]\nexpect = false\n[[trigger]]\nexpect = false\n"
                "[[behaviour]]\nname = 'used'\nfixture = 'fixtures/used/input'\ngrader = 'graders/used'\ncheck = 'bash check.sh'\n",
                encoding="utf-8",
            )
            errors = __import__("skilltest.skilltest", fromlist=["lint_skill"]).lint_skill(root)
            self.assertIn("relative link does not resolve: missing.md", errors)
            self.assertGreaterEqual(errors.count("absolute path found"), 2)
            self.assertIn("unreferenced fixture directory: fixtures/orphan/input", errors)

    def test_aggregation_flags_no_benefit(self) -> None:
        """Equal or worse with-skill pass rate is explicitly flagged."""
        from skilltest.skilltest import aggregate_behaviour

        report = aggregate_behaviour([True, False], [True, True])
        self.assertEqual(report["delta"], -0.5)
        self.assertTrue(report["no_measurable_benefit"])

    def test_behaviour_checks_distinguish_fixture_from_expected(self) -> None:
        """Every declared check rejects its input and accepts its reference output."""
        for name in ("doxygen-naming", "misra-cpp-2023", "review-taxonomy", "skill-authoring"):
            tests = ROOT / "knowledge" / "skills" / name / "tests"
            config = load_config(tests.parent)
            self.assertGreaterEqual(len(config["behaviour"]), 2, name)
            for case in config["behaviour"]:
                fixture = case["fixture"].split("/")[-2]
                grader = tests / case["grader"]
                env = {**os.environ, "DEVPACK_ROOT": str(ROOT)}
                for key in ("category", "severity", "verdict"):
                    if key in case:
                        env[f"SKILLTEST_EXPECTED_{key.upper()}"] = case[key]
                failing = subprocess.run(["bash", "check.sh", str(tests / "fixtures" / fixture / "input")], cwd=grader, check=False, env=env)
                passing = subprocess.run(["bash", "check.sh", str(tests / "expected" / fixture)], cwd=grader, check=False, env=env)
                self.assertNotEqual(failing.returncode, 0, f"{name}/{fixture}")
                self.assertEqual(passing.returncode, 0, f"{name}/{fixture}")

    def test_case_check_rejects_unknown_cases_and_wrong_gate_taxonomy(self) -> None:
        """Case-specific graders fail closed and enforce the declared review taxonomy."""
        case_check = ROOT / "skilltest" / "case_check.py"
        env = {
            **os.environ,
            "DEVPACK_ROOT": str(ROOT),
            "SKILLTEST_EXPECTED_CATEGORY": "GAT",
            "SKILLTEST_EXPECTED_SEVERITY": "blocker",
            "SKILLTEST_EXPECTED_VERDICT": "REQUEST_CHANGES",
        }
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            unknown = subprocess.run(
                ["python3", str(case_check), "doxygen-naming", "misspelled", str(directory)],
                check=False, env=env,
            )
            self.assertEqual(unknown.returncode, 2)
            (directory / "REVIEW.md").write_text(
                "| ID | Category | Severity | file:line | Problem (with evidence) | Required fix |\n"
                "|---|---|---|---|---|---|\n"
                "| R1 | DOC | minor | module.cpp:1 | Missing gate receipt. | Add it. |\n"
                "VERDICT: APPROVE\n",
                encoding="utf-8",
            )
            wrong = subprocess.run(
                ["python3", str(case_check), "review-taxonomy", "gate-review", str(directory)],
                check=False, env=env,
            )
            self.assertNotEqual(wrong.returncode, 0)

    def test_trial_uses_external_grader_and_rejects_grader_tampering(self) -> None:
        """An agent cannot alter a copied grader because no grader is copied into its workspace."""
        from skilltest.skilltest import _execute_trial

        skill = ROOT / "knowledge" / "skills" / "doxygen-naming"
        fixture = skill / "tests" / "fixtures" / "header-contract" / "input"
        grader = skill / "tests" / "graders" / "header-contract"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = root / "tamper_agent"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "from pathlib import Path\n"
                "for base, _, names in os.walk('.'):\n"
                "    for name in names:\n"
                "        if name == 'check.sh': Path(base, name).write_text('exit 0\\n')\n"
                "print('{\\\"type\\\": \\\"result\\\", \\\"result\\\": \\\"done\\\"}')\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            _, passed, _ = _execute_trial(
                skill, "haiku", "do nothing", False, root / "run", fixture, str(fake), "local",
                "bash check.sh", grader,
            )
            self.assertFalse(passed)

    def test_dry_run_prints_plan(self) -> None:
        """Dry-run displays every trial without requiring an agent binary."""
        result = self.run_cli(
            "run", "knowledge/skills/doxygen-naming", "--engines", "haiku,sonnet", "--k", "3", "--dry-run"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TRIAL PLAN", result.stdout)
        self.assertIn("haiku", result.stdout)
        self.assertIn("without_skill", result.stdout)

    def test_run_fake_agent_writes_summary_and_runs(self) -> None:
        """A fake claude executable completes a complete offline run."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = root / "claude"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Skill','input':{'skill':'doxygen-naming'}}]}}))\n"
                "print(json.dumps({'type':'result','result':'done','duration_ms':1}))\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            results = root / "results"
            result = self.run_cli(
                "run", "knowledge/skills/doxygen-naming", "--engines", "haiku", "--k", "1",
                "--agent-bin", str(fake), "--results-root", str(results)
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(list(results.rglob("summary.md")))
            self.assertTrue(list(results.rglob("runs/*/steps/*.jsonl")))


if __name__ == "__main__":
    unittest.main()
