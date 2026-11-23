"""Offline unit tests for the requirements-analysis skill and deterministic grader."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

TESTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = TESTS_DIR.parent
REPO_ROOT = SKILL_DIR.parents[2]
for path in (str(REPO_ROOT), str(TESTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import grade_findings  # noqa: E402
from skilltest import skilltest  # noqa: E402


class RequirementsAnalysisSkillTests(unittest.TestCase):
    """Verify lint conformance, fixture/expected behaviour, and anti-invention checks."""

    def test_skilltest_lint_passes(self) -> None:
        """Skill frontmatter, body length, relative links, and test suite pass lint."""
        self.assertEqual(skilltest.lint_skill(SKILL_DIR), [])

    def test_fixtures_fail_and_expected_pass(self) -> None:
        """Every input fixture fails its grader and every expected output passes."""
        config = skilltest.load_config(SKILL_DIR)
        self.assertGreaterEqual(len(config["behaviour"]), 3)
        positives = [c for c in config["trigger"] if c["expect"] is True]
        negatives = [c for c in config["trigger"] if c["expect"] is False]
        self.assertGreaterEqual(len(positives), 3)
        self.assertGreaterEqual(len(negatives), 3)

        env = {**os.environ, "DEVPACK_ROOT": str(REPO_ROOT)}
        for case in config["behaviour"]:
            name = case["name"]
            grader = TESTS_DIR / case["grader"]
            input_dir = TESTS_DIR / case["fixture"]
            expected_dir = TESTS_DIR / "expected" / name

            failing = subprocess.run(
                ["bash", "check.sh", str(input_dir)],
                cwd=grader,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(failing.returncode, 0, f"{name} input unexpectedly passed")

            passing = subprocess.run(
                ["bash", "check.sh", str(expected_dir)],
                cwd=grader,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(passing.returncode, 0, f"{name} expected failed: {passing.stdout}\n{passing.stderr}")

    def test_grader_rejects_invented_numbers_and_clean_findings(self) -> None:
        """Grader fails when a rewrite invents a number or flags the clean requirement."""
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "cand"
            shutil.copytree(TESTS_DIR / "expected" / "protocol-timing", candidate)
            findings = candidate / "review" / "findings.md"
            original = findings.read_text(encoding="utf-8")

            invented = original.replace("<TBD: response_deadline_ms>", "25")
            findings.write_text(invented, encoding="utf-8")
            failures = grade_findings.grade_candidate("protocol-timing", candidate)
            self.assertTrue(any("invented numeric value" in item and "25" in item for item in failures), failures)

            flagged_clean = original.replace(
                "\n\n## Set-level gaps and conflicts",
                "\n| REQ-COM-001 | Ambiguous wording [low, candidate] | "
                '"WHEN `startConnect(nowMs)` is called..." | Fake finding. | None? | '
                "WHEN `startConnect(nowMs)` is called, the `LinkManager` SHALL transition. |\n\n"
                "## Set-level gaps and conflicts",
            )
            findings.write_text(flagged_clean, encoding="utf-8")
            failures_clean = grade_findings.grade_candidate("protocol-timing", candidate)
            self.assertTrue(any("REQ-COM-001" in item for item in failures_clean), failures_clean)

    def test_grader_rejects_low_recall(self) -> None:
        """Grader fails when too few seeded defect IDs appear in the findings table."""
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "cand"
            shutil.copytree(TESTS_DIR / "fixtures" / "dtc-debouncer" / "input", candidate)
            (candidate / "review").mkdir()
            (candidate / "review" / "findings.md").write_text(
                "# Findings\n\n"
                "| ID | defect type | quote | why it matters | question to stakeholder | proposed rewrite |\n"
                "|---|---|---|---|---|---|\n"
                '| REQ-DTC-013 | Ambiguous wording [high, candidate] | "process incoming fault samples appropriately" | Untestable. | What step sizes apply? | WHEN `reportSample()` is called, the `DtcDebouncer` SHALL update `counter()` by `<TBD: step>`. |\n\n'
                "Conflict noted in prose.\n",
                encoding="utf-8",
            )
            failures = grade_findings.grade_candidate("dtc-debouncer", candidate)
            self.assertTrue(any("Recall too low" in item for item in failures), failures)


if __name__ == "__main__":
    unittest.main()
