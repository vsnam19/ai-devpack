#!/usr/bin/env python3
"""Check skill behaviour fixtures deterministically.

Exit codes: 0 for a passing candidate, 1 for a failed candidate, and 2 for invalid usage.
The DEVPACK_ROOT environment variable must name the pack root.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from skilltest.frontmatter import load_config, parse_frontmatter  # noqa: E402


REVIEW_DEFECT_TERMS: dict[str, tuple[str, ...]] = {
    "bounds-review": (
        "bounds",
        "index",
        "range",
        "out-of-bounds",
        "out of bounds",
        "values[index]",
        "unchecked",
    ),
    "gate-review": (
        "gate",
        "receipt",
        "check",
        "verify",
        "deterministic",
        "evidence",
        "skipped",
    ),
}


def _strip_outer_quotes(value: str) -> str:
    """Strip matching single or double outer quotes from a frontmatter scalar."""
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ('"', "'"):
        return stripped[1:-1].strip()
    return stripped


def _check_review_taxonomy(
    directory: Path,
    case: str,
    expected_category: str,
    expected_severity: str,
    expected_verdict: str,
) -> int:
    """Validate REVIEW.md table format, defect range, problem text, and trailing verdict."""
    review = directory / "REVIEW.md"
    if not review.is_file():
        return 1
    text = review.read_text(encoding="utf-8")
    non_empty_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not non_empty_lines or non_empty_lines[-1] != f"VERDICT: {expected_verdict}":
        return 1

    table_rows = [line for line in non_empty_lines if line.startswith("|") and line.endswith("|")]
    if len(table_rows) < 3:
        return 1
    header_cells = [c.strip().lower() for c in table_rows[0].strip("|").split("|")]
    if not re.match(r"^\|(\s*:?-{2,}:?\s*\|)+$", table_rows[1]):
        return 1

    def _col(predicate) -> int | None:
        return next((idx for idx, h in enumerate(header_cells) if predicate(h)), None)

    cat_idx = _col(lambda h: "category" in h)
    sev_idx = _col(lambda h: "severity" in h)
    loc_idx = _col(lambda h: "file" in h or "line" in h)
    prob_idx = _col(lambda h: "problem" in h)
    fix_idx = _col(lambda h: "fix" in h)
    if None in (cat_idx, sev_idx, loc_idx, prob_idx, fix_idx):
        return 1

    module_path = directory / "module.cpp"
    max_line = max(1, len(module_path.read_text(encoding="utf-8").splitlines())) if module_path.is_file() else 1
    loc_re = re.compile(r"(?:^|[\s`/])module\.cpp:(\d+)(?:-(\d+))?")
    defect_terms = REVIEW_DEFECT_TERMS.get(case, ())

    for row in table_rows[2:]:
        cells = [c.strip() for c in row.strip("|").split("|")]
        if len(cells) < len(header_cells):
            continue
        category = cells[cat_idx].strip("` ").upper()
        severity = cells[sev_idx].strip("` ").lower()
        loc = cells[loc_idx]
        problem = cells[prob_idx].lower()
        if category != expected_category.upper() or severity != expected_severity.lower():
            continue
        m = loc_re.search(loc)
        if not m:
            continue
        start_ln = int(m.group(1))
        end_ln = int(m.group(2)) if m.group(2) else start_ln
        if start_ln < 1 or end_ln < start_ln or end_ln > max_line:
            continue
        if defect_terms and not any(term in problem for term in defect_terms):
            continue
        return 0
    return 1


def _check_skill_authoring(draft: Path, case: str, root: Path) -> int:
    """Validate skill-authoring frontmatter-draft and test-matrix candidate drafts."""
    if case == "frontmatter-draft":
        skill_file = draft / "SKILL.md"
        if not skill_file.is_file():
            return 1
        meta, _, fm_errors = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        if fm_errors:
            return 1
        name = _strip_outer_quotes(meta.get("name", ""))
        description = _strip_outer_quotes(meta.get("description", ""))
        if name != "signal-helper":
            return 1
        words = re.findall(r"[A-Za-z0-9_'-]+", description)
        return 0 if len(words) >= 10 else 1

    if case == "test-matrix":
        if not run(["python3", str(root / "skilltest/skilltest.py"), "lint", str(draft)], draft.parent):
            return 1
        config = load_config(draft)
        cases = config.get("behaviour", [])
        if not cases:
            return 1
        for beh in cases:
            fixture_dir = draft / "tests" / beh["fixture"]
            grader_dir = draft / "tests" / beh["grader"]
            cmd = shlex.split(beh["check"])
            if not fixture_dir.is_dir() or not grader_dir.is_dir() or not cmd:
                return 1
            res = subprocess.run(
                [*cmd, str(fixture_dir)],
                cwd=grader_dir,
                env={**os.environ, "DEVPACK_ROOT": str(root)},
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0:
                return 1
        return 0

    return 2


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
        expected_category = os.environ.get("SKILLTEST_EXPECTED_CATEGORY")
        expected_severity = os.environ.get("SKILLTEST_EXPECTED_SEVERITY")
        expected_verdict = os.environ.get("SKILLTEST_EXPECTED_VERDICT")
        if not all((expected_category, expected_severity, expected_verdict)):
            print("review expectations are required", file=sys.stderr)
            return 2
        return _check_review_taxonomy(
            directory, case, expected_category, expected_severity, expected_verdict
        )
    if skill == "skill-authoring":
        draft = directory / "signal-helper"
        result = _check_skill_authoring(draft, case, root)
        if result == 2:
            print(f"unknown case: {case}", file=sys.stderr)
        return result
    print(f"unknown case: {case}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
