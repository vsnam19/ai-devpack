"""Tests for the deterministic skill test harness."""

import json
import os
import subprocess
import tempfile
import tomllib
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
            config = tomllib.loads((tests / "skilltest.toml").read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(config["behaviour"]), 2, name)
            for case in config["behaviour"]:
                fixture = case["fixture"].split("/")[-1]
                check = tests / "fixtures" / fixture / "check.sh"
                env = {**os.environ, "DEVPACK_ROOT": str(ROOT)}
                failing = subprocess.run(["bash", str(check), str(tests / "fixtures" / fixture)], check=False, env=env)
                passing = subprocess.run(["bash", str(check), str(tests / "expected" / fixture)], check=False, env=env)
                self.assertNotEqual(failing.returncode, 0, f"{name}/{fixture}")
                self.assertEqual(passing.returncode, 0, f"{name}/{fixture}")

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
