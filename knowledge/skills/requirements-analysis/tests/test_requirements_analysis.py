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

    def test_grader_rejects_invented_number_in_test_idea(self) -> None:
        """Grader fails when a derived test idea uses a number that is not in the source."""
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "cand"
            shutil.copytree(TESTS_DIR / "expected" / "protocol-timing", candidate)
            findings = candidate / "review" / "findings.md"
            original = findings.read_text(encoding="utf-8")
            self.assertIn("(`<= 10` ms)", original)

            invented = original.replace("(`<= 10` ms)", "(`<= 25` ms)")
            findings.write_text(invented, encoding="utf-8")
            failures = grade_findings.grade_candidate("protocol-timing", candidate)
            self.assertTrue(
                any("Test idea" in item and "invented numeric value" in item and "25" in item for item in failures),
                failures,
            )

    def test_grader_rejects_test_idea_for_open_question_requirement(self) -> None:
        """Grader fails when a test idea is written for a requirement that has an open question."""
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "cand"
            shutil.copytree(TESTS_DIR / "expected" / "protocol-timing", candidate)
            findings = candidate / "review" / "findings.md"
            original = findings.read_text(encoding="utf-8")
            blocked_line = "- REQ-COM-002: BLOCKED (open question) — see findings row REQ-COM-002"
            self.assertIn(blocked_line, original)

            # Uses only numbers that appear in the source, so only the open-question rule can fire.
            prescribed = original.replace(
                blocked_line,
                "- `REQ-COM-002`: call `startConnect(nowMs)` and check the handshake response within `10` ms.",
            )
            findings.write_text(prescribed, encoding="utf-8")
            failures = grade_findings.grade_candidate("protocol-timing", candidate)
            self.assertTrue(
                any("REQ-COM-002" in item and "open question" in item for item in failures),
                failures,
            )
            self.assertFalse(any("invented numeric value" in item for item in failures), failures)

    def _copy_expected(self, temp: str, case_name: str) -> Path:
        """Copy one expected candidate into a temporary directory and return its path."""
        candidate = Path(temp) / "cand"
        shutil.copytree(TESTS_DIR / "expected" / case_name, candidate)
        return candidate

    def test_star_bullet_with_invented_number_fails(self) -> None:
        """A `*` bullet in the test ideas section is graded and its invented number fails."""
        with tempfile.TemporaryDirectory() as temp:
            candidate = self._copy_expected(temp, "protocol-timing")
            findings = candidate / "review" / "findings.md"
            original = findings.read_text(encoding="utf-8")
            findings.write_text(
                original + "* `REQ-COM-001`: verify the handshake completes within 25 ms.\n",
                encoding="utf-8",
            )
            failures = grade_findings.grade_candidate("protocol-timing", candidate)
            self.assertTrue(
                any("Test idea" in item and item.endswith("not in source: 25") for item in failures),
                failures,
            )

    def test_numbered_list_items_are_parsed_as_ideas(self) -> None:
        """Numbered items (`1.`, `2)`), bullets and plain lines are each parsed, marker removed."""
        markdown = "## Derived test ideas\n\n1. first idea\n2) second idea\n- third idea\nfourth idea\n"
        ideas = grade_findings.parse_test_ideas(markdown)
        self.assertEqual([idea.text for idea in ideas], ["first idea", "second idea", "third idea", "fourth idea"])
        self.assertFalse(any(idea.blocked for idea in ideas))

    def test_numbered_item_with_invented_number_fails(self) -> None:
        """A numbered item is graded: its invented number fails, while its `1.` marker is ignored."""
        with tempfile.TemporaryDirectory() as temp:
            candidate = self._copy_expected(temp, "protocol-timing")
            findings = candidate / "review" / "findings.md"
            original = findings.read_text(encoding="utf-8")
            findings.write_text(
                original + "1. `REQ-COM-001`: verify the handshake completes within 25 ms.\n",
                encoding="utf-8",
            )
            failures = grade_findings.grade_candidate("protocol-timing", candidate)
            invented = [item for item in failures if "invented numeric value" in item]
            self.assertEqual(len(invented), 1, failures)
            self.assertTrue(invented[0].endswith("not in source: 25"), invented)

    def test_blocked_with_extra_text_fails(self) -> None:
        """A blocked line with prose and a number is not the exact blocked form and fails."""
        with tempfile.TemporaryDirectory() as temp:
            candidate = self._copy_expected(temp, "protocol-timing")
            findings = candidate / "review" / "findings.md"
            original = findings.read_text(encoding="utf-8")
            blocked_line = "- REQ-COM-002: BLOCKED (open question) — see findings row REQ-COM-002"
            self.assertIn(blocked_line, original)
            findings.write_text(
                original.replace(
                    blocked_line,
                    "- REQ-COM-002: blocked — waiting on answers; meanwhile verify the handshake within 10 ms.",
                ),
                encoding="utf-8",
            )
            failures = grade_findings.grade_candidate("protocol-timing", candidate)
            self.assertTrue(
                any("REQ-COM-002" in item and "open question" in item for item in failures),
                failures,
            )

    def test_exact_blocked_format_passes(self) -> None:
        """The exact blocked form, with or without the finding-row reference, passes the grader."""
        with tempfile.TemporaryDirectory() as temp:
            candidate = self._copy_expected(temp, "protocol-timing")
            findings = candidate / "review" / "findings.md"
            original = findings.read_text(encoding="utf-8")
            self.assertEqual(grade_findings.grade_candidate("protocol-timing", candidate), [])

            bare = original.replace(
                "- REQ-COM-002: BLOCKED (open question) — see findings row REQ-COM-002",
                "- REQ-COM-002: BLOCKED (open question)",
            )
            self.assertNotEqual(bare, original)
            findings.write_text(bare, encoding="utf-8")
            self.assertEqual(grade_findings.grade_candidate("protocol-timing", candidate), [])

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
