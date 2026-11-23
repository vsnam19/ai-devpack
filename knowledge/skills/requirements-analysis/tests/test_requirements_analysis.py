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

    def test_cases_have_at_least_two_clean_ids_and_overflagging_fails(self) -> None:
        """Every case defines >= 2 clean IDs and flagging any clean ID fails grading."""
        for case_name, spec in grade_findings.CASES.items():
            self.assertGreaterEqual(len(spec.clean_ids), 2, case_name)
            self.assertGreaterEqual(spec.min_recall, len(spec.seeded_patterns) - 1, case_name)
            for clean_id in spec.clean_ids:
                with tempfile.TemporaryDirectory() as temp:
                    candidate = self._copy_expected(temp, case_name)
                    findings = candidate / "review" / "findings.md"
                    original = findings.read_text(encoding="utf-8")
                    # Insert a finding row for clean_id right before ## Set-level gaps and conflicts.
                    spec_text = (candidate / "spec.md").read_text(encoding="utf-8")
                    quote_line = ""
                    capture = False
                    for ln in spec_text.splitlines():
                        if ln.startswith(f"## {clean_id}:"):
                            capture = True
                            continue
                        if capture and ln.strip():
                            quote_line = ln.strip()
                            break
                    flagged = original.replace(
                        "\n\n## Set-level gaps and conflicts",
                        f'\n| {clean_id} | Ambiguous wording [low, candidate] | "{quote_line}" | Fake finding. | None? | '
                        f"WHEN triggered, the module SHALL respond. |\n\n## Set-level gaps and conflicts",
                    )
                    findings.write_text(flagged, encoding="utf-8")
                    failures = grade_findings.grade_candidate(case_name, candidate)
                    self.assertTrue(any(clean_id in item for item in failures), (case_name, clean_id, failures))

    def test_mutants_for_verbatim_quote_defect_type_number_words_and_unlinked_ideas(self) -> None:
        """Mutants covering every reviewer bypass fail the grader."""
        with tempfile.TemporaryDirectory() as temp:
            # 1. Reviewer bypass style: defect_type='x', quote='x', number words, unlinked ideas.
            cand1 = Path(temp) / "rev_bypass"
            shutil.copytree(TESTS_DIR / "fixtures" / "dtc-debouncer" / "input", cand1)
            (cand1 / "review").mkdir()
            (cand1 / "review" / "findings.md").write_text(
                "| ID | defect type | quote | why it matters | question to stakeholder | proposed rewrite |\n"
                "|---|---|---|---|---|---|\n"
                "| REQ-DTC-011 | x | x | x | x | The debouncer SHALL confirm after ten samples. |\n"
                "| REQ-DTC-012 | x | x | x | x | The debouncer SHALL confirm after fifty samples. |\n"
                "| REQ-DTC-013 | x | x | x | x | The debouncer SHALL do things. |\n\n"
                "## Open questions\nconflict\n\n"
                "## Derived test ideas\n"
                "- Check that the hundredth sample confirms the fault for threshold A.\n"
                "- Check that the fiftieth sample confirms the fault for threshold B.\n",
                encoding="utf-8",
            )
            f1 = grade_findings.grade_candidate("dtc-debouncer", cand1)
            self.assertTrue(any("checklist vocabulary" in item for item in f1), f1)
            self.assertTrue(any("quote is too short" in item for item in f1), f1)
            self.assertTrue(any("invented numeric value" in item and "10" in item for item in f1), f1)
            self.assertTrue(any("BLOCKED (open question)" in item for item in f1), f1)
            self.assertTrue(any("must reference a clean requirement ID" in item for item in f1), f1)

            # 2. Non-verbatim quote fails.
            cand2 = Path(temp) / "non_verbatim"
            shutil.copytree(TESTS_DIR / "expected" / "protocol-timing", cand2)
            f2_path = cand2 / "review" / "findings.md"
            f2_text = f2_path.read_text(encoding="utf-8")
            f2_path.write_text(
                f2_text.replace(
                    '"The `LinkManager` shall respond quickly after receiving a handshake request."',
                    '"The LinkManager responds fast to a handshake."',
                ),
                encoding="utf-8",
            )
            f2 = grade_findings.grade_candidate("protocol-timing", cand2)
            self.assertTrue(any("verbatim" in item for item in f2), f2)

            # 3. Non-vocabulary defect type fails.
            cand3 = Path(temp) / "bad_defect_type"
            shutil.copytree(TESTS_DIR / "expected" / "can-watchdog", cand3)
            f3_path = cand3 / "review" / "findings.md"
            f3_path.write_text(
                f3_path.read_text(encoding="utf-8").replace(
                    "Incomplete specification + missing error behaviour [high, candidate]",
                    "Bad quality issue [high, candidate]",
                ),
                encoding="utf-8",
            )
            f3 = grade_findings.grade_candidate("can-watchdog", cand3)
            self.assertTrue(any("checklist vocabulary" in item for item in f3), f3)

            # 4. Number words (zero..twenty, hundred, thousand, dozen, half) fail when not in source.
            for word, expected_num in (("ten", "10"), ("twenty", "20"), ("hundred", "100"), ("thousand", "1000"), ("dozen", "12"), ("half", "0.5")):
                cand4 = Path(temp) / f"nw_{word}"
                case_use = "can-watchdog" if word == "ten" else "protocol-timing"
                shutil.copytree(TESTS_DIR / "expected" / case_use, cand4)
                f4_path = cand4 / "review" / "findings.md"
                txt4 = f4_path.read_text(encoding="utf-8")
                if case_use == "can-watchdog":
                    txt4 = txt4.replace(
                        "`<TBD: notify_deadline_ms>` ms.",
                        f"{word} ms.",
                    )
                else:
                    txt4 = txt4.replace(
                        "`<TBD: response_deadline_ms>` ms.",
                        f"{word} ms.",
                    )
                f4_path.write_text(txt4, encoding="utf-8")
                f4 = grade_findings.grade_candidate(case_use, cand4)
                self.assertTrue(
                    any("invented numeric value" in item and expected_num in item for item in f4),
                    (word, expected_num, f4),
                )

            # 5. Unlinked test idea (omitting ID) fails even without invented numbers.
            cand5 = Path(temp) / "unlinked_idea"
            shutil.copytree(TESTS_DIR / "expected" / "protocol-timing", cand5)
            f5_path = cand5 / "review" / "findings.md"
            f5_path.write_text(
                f5_path.read_text(encoding="utf-8") + "- Verify handshake timeout triggers Fault.\n",
                encoding="utf-8",
            )
            f5 = grade_findings.grade_candidate("protocol-timing", cand5)
            self.assertTrue(any("must reference a clean requirement ID" in item for item in f5), f5)


if __name__ == "__main__":
    unittest.main()
