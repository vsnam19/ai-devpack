"""Offline regressions for the concrete round-three grader bypasses."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from test_f5b_grader_regressions import _setup_task, _run_check

ROOT = Path(__file__).resolve().parents[1]


class F6cRegressions(unittest.TestCase):
    def test_naming(self):
        for task, stem, old in (("08-doxygen-naming-codec", "can_codec", "frameBytes"),
                                ("09-doxygen-naming-link", "link_state_machine", "nowMs")):
            with self.subTest(task=task), tempfile.TemporaryDirectory() as tmp:
                td, work, sha = _setup_task(Path(tmp), task)
                subprocess.run(["git", "apply", str(td / "solution.patch")], cwd=work, check=True)
                result = _run_check(td / "check.sh", work, sha)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                originals = {work / f"include/{stem}.hpp": (work / f"include/{stem}.hpp").read_text(),
                             work / f"src/{stem}.cpp": (work / f"src/{stem}.cpp").read_text()}
                for bad in ("qqq", "www", "eee", "signalQqq", "banana"):
                    for path, text in originals.items():
                        path.write_text(re.sub(rf"\b{old}\b", bad, text))
                    result = _run_check(td / "check.sh", work, sha)
                    self.assertEqual(result.returncode, 1, bad + result.stdout + result.stderr)

    def test_fault_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            td, work, sha = _setup_task(Path(tmp), "18-review-fault-recovery")
            subprocess.run(["git", "apply", str(td / "solution.patch")], cwd=work, check=True)
            self.assertEqual(_run_check(td / "check.sh", work, sha).returncode, 0)
            original = (work / "REVIEW.md").read_text()
            bad = ("| Category | Severity | file:line | Problem | Required fix |\n"
                   "| --- | --- | --- | --- | --- |\n"
                   "| COR | major | src/link_state_machine.cpp:131 | In recoverFromFault, m_faultCount should not be a plain std::uint32_t; use std::atomic for thread safety. | Make it atomic. |\n"
                   "| DOC | minor | src/link_state_machine.cpp:10 | Improve style. | Reformat. |\n"
                   "VERDICT: REQUEST_CHANGES\n")
            for text in (bad, original.replace("VERDICT:", "Use std::atomic instead of plain uint32_t.\nVERDICT:")):
                (work / "REVIEW.md").write_text(text)
                result = _run_check(td / "check.sh", work, sha)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_requirements(self):
        tests = ROOT / "knowledge/skills/requirements-analysis/tests"
        for case, ids, titles, clean, defect in (
            ("protocol-timing", ("REQ-COM-002", "REQ-COM-003"), ("Handshake Response Latency", "Heartbeat Timeout"), "REQ-COM-001", "incomplete"),
            ("dtc-debouncer", ("REQ-DTC-011", "REQ-DTC-012"), ("Fault Confirmation Threshold A", "Fault Confirmation Threshold B"), "REQ-DTC-010", "conflict")):
            with tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp)
                shutil.copytree(tests / "expected" / case, work, dirs_exist_ok=True)
                def grade():
                    return subprocess.run(["python3", str(tests / "grade_findings.py"), "--case", case, str(work)], capture_output=True, text=True)
                self.assertEqual(grade().returncode, 0)
                path = work / "review/findings.md"
                original = path.read_text()
                rows = [f'| {rid} | {defect} | "{rid}: {title}" | This item needs more work overall. | Can you please clarify this item? | The system SHALL work. |' for rid, title in zip(ids, titles)]
                path.write_text("| ID | defect type | quote | why it matters | question to stakeholder | proposed rewrite |\n|---|---|---|---|---|---|\n" + "\n".join(rows) + f"\n\n## Derived test ideas\n- {clean}: check it works.\n" + "".join(f"- {rid}: BLOCKED (open question)\n" for rid in ids))
                self.assertEqual(grade().returncode, 1)
                for col, replacement in ((3, "This item needs more work overall."), (4, "Can you please clarify this item?"), (5, "The system SHALL work.")):
                    lines = original.splitlines()
                    for i, line in enumerate(lines):
                        if line.startswith("| REQ-"):
                            cells = line.split("|")
                            cells[col + 1] = replacement
                            lines[i] = "|".join(cells)
                    path.write_text("\n".join(lines))
                    result = grade()
                    self.assertEqual(result.returncode, 1, result.stdout)

    def test_typed_placeholder_requires_open_question(self):
        tests = ROOT / "knowledge/skills/requirements-analysis/tests"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            shutil.copytree(tests / "expected/protocol-timing", work, dirs_exist_ok=True)
            path = work / "review/findings.md"
            original = path.read_text()

            def grade(text):
                path.write_text(text)
                return subprocess.run(
                    ["python3", str(tests / "grade_findings.py"), "--case", "protocol-timing", str(work)],
                    capture_output=True, text=True,
                )

            result = grade(original)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            without_entry = original.replace(
                "- REQ-COM-002: BLOCKED (open question) — see findings row REQ-COM-002", ""
            )
            result = grade(without_entry)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("Missing exact BLOCKED", result.stdout)
            for replacement in ("<TBD: deadline>", "<TBD: ResponseDeadline> ms",
                                "<TBD: response_deadline_ms>", "work", "function", "operate"):
                with self.subTest(replacement=replacement):
                    text = original.replace(
                        "send the handshake response within `<TBD: response_deadline_ms>` ms",
                        replacement,
                    )
                    result = grade(text)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn("rewrite lacks a measurable criterion", result.stdout)

    def test_skill_cases(self):
        env = {**os.environ, "DEVPACK_ROOT": str(ROOT), "SKILLTEST_EXPECTED_CATEGORY": "GAT", "SKILLTEST_EXPECTED_SEVERITY": "blocker", "SKILLTEST_EXPECTED_VERDICT": "REQUEST_CHANGES"}
        def grade(skill, case, work):
            return subprocess.run(["python3", str(ROOT / "skilltest/case_check.py"), skill, case, str(work)], env=env, capture_output=True, text=True)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "candidate"
            shutil.copytree(ROOT / "knowledge/skills/skill-authoring/tests/expected/test-matrix", work)
            self.assertEqual(grade("skill-authoring", "test-matrix", work).returncode, 0)
            (work / "signal-helper/tests/graders/summary/check.sh").write_text('#!/usr/bin/env bash\ncase "$1" in */expected/*) exit 0;; *) exit 1;; esac\n')
            self.assertEqual(grade("skill-authoring", "test-matrix", work).returncode, 1)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            shutil.copytree(ROOT / "knowledge/skills/review-taxonomy/tests/expected/gate-review", work, dirs_exist_ok=True)
            shutil.copy(ROOT / "knowledge/skills/review-taxonomy/tests/fixtures/gate-review/input/module.cpp", work)
            self.assertEqual(grade("review-taxonomy", "gate-review", work).returncode, 0)
            path = work / "REVIEW.md"
            original = path.read_text()
            for problem in ("please check this function", "please verify this function", "test gate evidence"):
                path.write_text(original.replace("The submitted change has no gate receipt for `module.cpp`.", problem))
                self.assertEqual(grade("review-taxonomy", "gate-review", work).returncode, 1)

    def test_cppcheck_guidance(self):
        text = (ROOT / "knowledge/skills/automotive-cpp/SKILL.md").read_text()
        line = next(line for line in text.splitlines() if 'Exit `2`:' in line)
        for term in ("timeout", "internal", "TOOL", "[verify]"):
            self.assertIn(term, line)


if __name__ == "__main__":
    unittest.main()
