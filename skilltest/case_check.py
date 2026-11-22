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
        if case == "direction-contract":
            return 0 if "@param[in,out] frame" in text and "@param[in] value" in text and "@return" in text else 1
        return 0
    if skill == "review-taxonomy":
        review = directory / "REVIEW.md"
        text = review.read_text(encoding="utf-8") if review.is_file() else ""
        required = "| ID | Category | Severity | file:line | Problem (with evidence) | Required fix |"
        valid_category = any(f"| {code} |" in text for code in ("COR", "REQ", "TST", "GAT", "MIS", "DOC", "IFC", "CPX", "SEC", "PRV"))
        lines = text.splitlines()
        return 0 if required in text and valid_category and any("module.cpp:" in line for line in lines) and lines and lines[-1] in {"VERDICT: APPROVE", "VERDICT: REQUEST_CHANGES"} else 1
    if skill == "skill-authoring":
        draft = directory / "signal-helper"
        return 0 if run(["python3", str(root / "skilltest/skilltest.py"), "lint", str(draft)], directory) else 1
    print(f"unknown case: {case}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
