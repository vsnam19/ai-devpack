#!/usr/bin/env python3
# grade_findings.py - Deterministically grade requirements-analysis skill outputs.
#
# Usage: python3 grade_findings.py --case CASE CANDIDATE_DIR
# Exit codes: 0 candidate passed; 1 candidate failed checks; 2 invalid arguments or missing source.
"""Deterministic grader for requirements-analysis behaviour fixtures."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys


@dataclass(frozen=True)
class CaseSpec:
    """Expected defects, clean requirements, and report checks for one fixture."""

    clean_ids: tuple[str, ...]
    seeded_patterns: tuple[tuple[str, str], ...]
    min_recall: int
    required_keywords: tuple[str, ...] = ()


CASES: dict[str, CaseSpec] = {
    "protocol-timing": CaseSpec(
        clean_ids=("REQ-COM-001",),
        seeded_patterns=(
            ("REQ-COM-002", r"\bREQ-COM-002\b"),
            ("REQ-COM-003", r"\bREQ-COM-003\b"),
            ("REQ-COM-004", r"\bREQ-COM-004\b"),
        ),
        min_recall=2,
    ),
    "dtc-debouncer": CaseSpec(
        clean_ids=("REQ-DTC-010",),
        seeded_patterns=(
            ("REQ-DTC-011", r"\bREQ-DTC-011\b"),
            ("REQ-DTC-012", r"\bREQ-DTC-012\b"),
            ("REQ-DTC-013", r"\bREQ-DTC-013\b"),
        ),
        min_recall=2,
        required_keywords=("conflict",),
    ),
    "can-watchdog": CaseSpec(
        clean_ids=("REQ-CAN-020",),
        seeded_patterns=(
            ("REQ-CAN-021", r"\bREQ-CAN-021\b"),
            ("REQ-CAN-022", r"\bREQ-CAN-022\b"),
            ("TMP-1", r"\bTMP-0*1\b"),
        ),
        min_recall=2,
        required_keywords=("TMP-",),
    ),
}

ID_TOKEN_PATTERN = re.compile(r"\b(?:REQ|SYS|SWR|TC|TMP)(?:-[A-Z0-9]+)+\b")
HEX_NUMBER_PATTERN = re.compile(r"\b0x[0-9a-fA-F]+\b")
DEC_NUMBER_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\b")
SEPARATOR_CELL_PATTERN = re.compile(r"^:?-{2,}:?$")
TEST_IDEAS_HEADING = "## Derived test ideas"
LIST_MARKER_PATTERN = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
BLOCKED_ENTRY_PATTERN = re.compile(
    r"(?P<id>(?:REQ|SYS|SWR|TC|TMP)(?:-[A-Z0-9]+)+): BLOCKED \(open question\)"
    r"(?: — see findings row (?P=id))?"
)


@dataclass(frozen=True)
class TestIdea:
    """One non-empty line of the derived test ideas section, with its list marker removed."""

    text: str
    blocked: bool


def extract_numbers(text: str) -> set[str]:
    """Return numeric literals from text after stripping requirement and trace IDs."""
    without_ids = ID_TOKEN_PATTERN.sub(" ", text)
    hex_tokens = {match.group(0).lower() for match in HEX_NUMBER_PATTERN.finditer(without_ids)}
    without_hex = HEX_NUMBER_PATTERN.sub(" ", without_ids)
    dec_tokens = {match.group(0) for match in DEC_NUMBER_PATTERN.finditer(without_hex)}
    return hex_tokens | dec_tokens


def _split_markdown_row(line: str) -> list[str]:
    """Split a Markdown pipe-delimited row into trimmed cell strings."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    """Return True if every cell in a Markdown row is a header separator."""
    return bool(cells) and all(SEPARATOR_CELL_PATTERN.fullmatch(cell) for cell in cells)


def _is_findings_header(cells: list[str]) -> bool:
    """Return True if a row header matches the six-column findings table contract."""
    if len(cells) < 6:
        return False
    joined = [cell.lower() for cell in cells]
    return (
        "id" in joined[0]
        and "defect" in joined[1]
        and "quote" in joined[2]
        and "why" in joined[3]
        and "question" in joined[4]
        and "rewrite" in joined[5]
    )


def parse_findings_table(markdown_text: str) -> list[dict[str, str]]:
    """Extract findings rows from the six-column findings table in Markdown text."""
    lines = markdown_text.splitlines()
    for index, line in enumerate(lines[:-1]):
        if "|" not in line:
            continue
        header_cells = _split_markdown_row(line)
        if not _is_findings_header(header_cells):
            continue
        sep_cells = _split_markdown_row(lines[index + 1])
        if not _is_separator_row(sep_cells):
            continue
        rows: list[dict[str, str]] = []
        for row_line in lines[index + 2 :]:
            if "|" not in row_line or not row_line.strip():
                break
            cells = _split_markdown_row(row_line)
            if len(cells) < 6:
                raise ValueError(f"Findings row has fewer than 6 columns: {row_line}")
            rows.append(
                {
                    "id": cells[0],
                    "defect_type": cells[1],
                    "quote": cells[2],
                    "why": cells[3],
                    "question": cells[4],
                    "rewrite": cells[5],
                }
            )
        return rows
    raise ValueError("Missing findings table with columns: ID | defect type | quote | why it matters | question to stakeholder | proposed rewrite")


def parse_test_ideas(markdown_text: str) -> list[TestIdea]:
    """Return one TestIdea per non-empty, non-heading line of the `## Derived test ideas` section.

    The list marker (`-`, `*`, `+`, `1.`, `1)`) is removed before the content is checked, so
    numbers in the marker never count as values. A line is blocked only when its content matches
    exactly `<ID>: BLOCKED (open question)`, optionally followed by ` — see findings row <ID>`.
    Every other line is a test idea that must obey the number and open-question rules.
    """
    lines = markdown_text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == TEST_IDEAS_HEADING:
            start = index + 1
            break
    if start is None:
        raise ValueError(f"Missing '{TEST_IDEAS_HEADING}' section")
    ideas: list[TestIdea] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        content = LIST_MARKER_PATTERN.sub("", line, count=1).strip()
        if not content:
            continue
        ideas.append(
            TestIdea(
                text=content,
                blocked=BLOCKED_ENTRY_PATTERN.fullmatch(content) is not None,
            )
        )
    return ideas


def grade_candidate(case_name: str, candidate_dir: Path) -> list[str]:
    """Return failure reasons for a candidate directory; empty list means pass."""
    if case_name not in CASES:
        raise KeyError(f"Unknown case: {case_name}")
    spec = CASES[case_name]
    source_path = candidate_dir / "spec.md"
    findings_path = candidate_dir / "review" / "findings.md"
    if not source_path.is_file():
        raise FileNotFoundError(f"Source specification missing: {source_path}")
    if not findings_path.is_file():
        return ["Missing review/findings.md"]
    findings_text = findings_path.read_text(encoding="utf-8")
    if not findings_text.strip():
        return ["review/findings.md is empty"]
    try:
        rows = parse_findings_table(findings_text)
    except ValueError as error:
        return [str(error)]
    if not rows:
        return ["Findings table has no rows"]

    failures: list[str] = []
    source_numbers = extract_numbers(source_path.read_text(encoding="utf-8"))

    for clean_id in spec.clean_ids:
        clean_re = re.compile(rf"\b{re.escape(clean_id)}\b", re.IGNORECASE)
        for row in rows:
            if clean_re.search(row["id"]):
                failures.append(f"Clean requirement {clean_id} must not have a finding in table")
                break

    matched_seeded: set[str] = set()
    for label, pattern in spec.seeded_patterns:
        compiled = re.compile(pattern, re.IGNORECASE)
        if any(compiled.search(row["id"]) for row in rows):
            matched_seeded.add(label)
    if len(matched_seeded) < spec.min_recall:
        failures.append(
            f"Recall too low: matched {len(matched_seeded)}/{len(spec.seeded_patterns)} seeded defects ({sorted(matched_seeded)}), need >= {spec.min_recall}"
        )

    for row_index, row in enumerate(rows, 1):
        for field in ("id", "defect_type", "quote", "why", "question", "rewrite"):
            if not row[field] or row[field].lower() in {"none", "n/a", "-"}:
                failures.append(f"Row {row_index} ({row['id'] or 'unknown'}) has empty {field}")
        invented = sorted(extract_numbers(row["rewrite"]) - source_numbers)
        if invented:
            failures.append(
                f"Row {row_index} ({row['id']}) invented numeric value(s) in proposed rewrite not in source: {', '.join(invented)}"
            )
        if "SHALL" not in row["rewrite"] and "shall" not in row["rewrite"]:
            failures.append(f"Row {row_index} ({row['id']}) proposed rewrite does not use SHALL")

    open_ids = {
        ref
        for row in rows
        if row["question"].strip()
        for ref in ID_TOKEN_PATTERN.findall(row["id"])
    }
    try:
        test_ideas = parse_test_ideas(findings_text)
    except ValueError as error:
        failures.append(str(error))
        test_ideas = []
    for idea_index, idea in enumerate(test_ideas, 1):
        idea_refs = ", ".join(sorted(set(ID_TOKEN_PATTERN.findall(idea.text)))) or "unknown"
        invented_in_idea = sorted(extract_numbers(idea.text) - source_numbers)
        if invented_in_idea:
            failures.append(
                f"Test idea {idea_index} ({idea_refs}) invented numeric value(s) not in source: {', '.join(invented_in_idea)}"
            )
        if idea.blocked:
            continue
        for ref in sorted(set(ID_TOKEN_PATTERN.findall(idea.text)) & open_ids):
            failures.append(
                f"Test idea {idea_index} references {ref}, which has an open question; "
                f"use the exact blocked entry '{ref}: BLOCKED (open question)' instead"
            )

    lowered = findings_text.lower()
    for keyword in spec.required_keywords:
        if keyword.lower() not in lowered:
            failures.append(f"Report is missing required keyword: {keyword}")

    return failures


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and run deterministic grading for one candidate directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, choices=sorted(CASES))
    parser.add_argument("candidate_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        failures = grade_candidate(args.case, args.candidate_dir.resolve())
    except (FileNotFoundError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print(f"PASS: {args.case}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
