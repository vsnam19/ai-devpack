#!/usr/bin/env python3
"""Check skill behaviour fixtures deterministically.

Exit codes: 0 for a passing candidate, 1 for a failed candidate, and 2 for invalid usage.
The DEVPACK_ROOT environment variable must name the pack root.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def run(command: list[str], directory: Path) -> bool:
    """Run one required command without accepting failures."""
    return subprocess.run(command, cwd=directory, check=False).returncode == 0


def main(argv: list[str]) -> int:
    """Validate one case directory selected by its skill and case names."""
    if len(argv) != 4:
        print("usage: case_check.py SKILL CASE DIRECTORY", file=sys.stderr)
        return 2
    root_value = os.environ.get("DEVPACK_ROOT")
    if not root_value:
        print("DEVPACK_ROOT is required", file=sys.stderr)
        return 2
    root = Path(root_value)
    skill, case, directory_text = argv[1:]
    directory = Path(directory_text).resolve()
    known_cases = {
        "doxygen-naming": {"direction-contract", "header-contract"},
        "misra-cpp-2023": {"runtime-counter", "constructor-contract"},
        "review-taxonomy": {"bounds-review", "gate-review"},
        "skill-authoring": {"frontmatter-draft", "test-matrix"},
    }
    if case not in known_cases.get(skill, set()):
        print(f"unknown case: {case}", file=sys.stderr)
        return 2
    if skill in {"doxygen-naming", "misra-cpp-2023"}:
        source = directory / ("signal.hpp" if skill == "doxygen-naming" else "counter.cpp")
        if not source.is_file():
            return 1
        compile_target = source
        if source.suffix == ".hpp":
            compile_target = directory / "compile.cpp"
        if not run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-fsyntax-only", str(compile_target)], directory):
            return 1
        if not run(["python3", str(root / "enforcement/lint/preflight_lint.py"), str(source)], directory):
            return 1
        text = source.read_text(encoding="utf-8")
        if skill == "doxygen-naming" and case == "direction-contract":
            return 0 if "@param[in,out] frame" in text and "@param[in] value" in text and "@return" in text else 1
        if skill == "doxygen-naming" and case == "header-contract":
            return 0 if "@file signal.hpp" in text and "scaleValue" in text and "@param[in] rawValue" in text and "@param[out] scaledValue" in text and "@return" in text else 1
        if skill == "misra-cpp-2023" and case == "runtime-counter":
            return 0 if "readCounter" in text and "std::int32_t" in text else 1
        if skill == "misra-cpp-2023" and case == "constructor-contract":
            return 0 if "explicit Counter" in text and "m_value" in text and "std::int32_t" in text else 1
        print(f"unknown case: {case}", file=sys.stderr)
        return 2
    if skill == "review-taxonomy":
        review = directory / "REVIEW.md"
        text = review.read_text(encoding="utf-8") if review.is_file() else ""
        required = "| ID | Category | Severity | file:line | Problem (with evidence) | Required fix |"
        expected_category = os.environ.get("SKILLTEST_EXPECTED_CATEGORY")
        expected_severity = os.environ.get("SKILLTEST_EXPECTED_SEVERITY")
        expected_verdict = os.environ.get("SKILLTEST_EXPECTED_VERDICT")
        if not all((expected_category, expected_severity, expected_verdict)):
            print("review expectations are required", file=sys.stderr)
            return 2
        lines = text.splitlines()
        expected_row = f"| {expected_category} | {expected_severity} |"
        return 0 if required in text and expected_row in text and any("module.cpp:" in line for line in lines) and lines and lines[-1] == f"VERDICT: {expected_verdict}" else 1
    if skill == "skill-authoring":
        draft = directory / "signal-helper"
        if case == "frontmatter-draft":
            skill_file = draft / "SKILL.md"
            text = skill_file.read_text(encoding="utf-8") if skill_file.is_file() else ""
            return 0 if text.startswith("---\nname: signal-helper\n") and "description:" in text else 1
        if case == "test-matrix":
            return 0 if run(["python3", str(root / "skilltest/skilltest.py"), "lint", str(draft)], directory) else 1
        print(f"unknown case: {case}", file=sys.stderr)
        return 2
    print(f"unknown case: {case}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
