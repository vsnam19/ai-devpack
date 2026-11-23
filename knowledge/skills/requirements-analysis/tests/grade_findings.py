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


TESTS_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class CaseSpec:
    """Expected defects, clean requirements, and report checks for one fixture."""

    clean_ids: tuple[str, ...]
    seeded_patterns: tuple[tuple[str, str], ...]
    seeded_defect_classes: dict[str, tuple[str, ...]]
    min_recall: int
    required_keywords: tuple[str, ...] = ()


CASES: dict[str, CaseSpec] = {
    "protocol-timing": CaseSpec(
        clean_ids=("REQ-COM-001", "REQ-COM-005"),
        seeded_patterns=(
            ("REQ-COM-002", r"\bREQ-COM-002\b"),
            ("REQ-COM-003", r"\bREQ-COM-003\b"),
            ("REQ-COM-004", r"\bREQ-COM-004\b"),
        ),
        seeded_defect_classes={
            "REQ-COM-002": ("q5", "q6", "q8", "q11", "ambiguous", "vague", "unverifiable", "incomplete", "missing timing", "missing tolerance", "missing error"),
            "REQ-COM-003": ("q6", "q8", "q11", "incomplete", "missing unit", "missing timing", "missing tolerance", "missing error", "unverifiable"),
            "REQ-COM-004": ("q6", "q8", "q11", "open placeholder", "tbd", "incomplete", "unverifiable", "missing fault", "missing error"),
        },
        min_recall=2,
    ),
    "dtc-debouncer": CaseSpec(
        clean_ids=("REQ-DTC-010", "REQ-DTC-014"),
        seeded_patterns=(
            ("REQ-DTC-011", r"\bREQ-DTC-011\b"),
            ("REQ-DTC-012", r"\bREQ-DTC-012\b"),
            ("REQ-DTC-013", r"\bREQ-DTC-013\b"),
        ),
        seeded_defect_classes={
            "REQ-DTC-011": ("q15", "q9", "set-level conflict", "conflict", "inconsistent"),
            "REQ-DTC-012": ("q15", "q9", "set-level conflict", "conflict", "inconsistent"),
            "REQ-DTC-013": ("q5", "q6", "q8", "q10", "q11", "ambiguous", "vague", "unverifiable", "incomplete", "non-conforming", "missing error"),
        },
        min_recall=2,
        required_keywords=("conflict",),
    ),
    "can-watchdog": CaseSpec(
        clean_ids=("REQ-CAN-020", "REQ-CAN-023"),
        seeded_patterns=(
            ("REQ-CAN-021", r"\bREQ-CAN-021\b"),
            ("REQ-CAN-022", r"\bREQ-CAN-022\b"),
            ("TMP-1", r"\bTMP-0*1\b"),
        ),
        seeded_defect_classes={
            "REQ-CAN-021": ("q6", "q8", "q11", "incomplete", "missing range", "missing error", "missing fault", "unverifiable"),
            "REQ-CAN-022": ("q2", "q5", "q6", "q8", "q10", "non-atomic", "not singular", "ambiguous", "vague", "hidden timing", "escape clause", "unverifiable", "incomplete", "non-conforming"),
            "TMP-1": ("q1", "q3", "q5", "q6", "q13", "q14", "missing or duplicate id", "missing unique id", "missing initial", "incomplete", "ambiguous", "vague", "missing status", "missing asil", "missing traceability", "traceability", "unlinked"),
        },
        min_recall=2,
        required_keywords=("TMP-",),
    ),
}

ID_TOKEN_PATTERN = re.compile(r"\b(?:REQ|SYS|SWR|TC|TMP)(?:-[A-Z0-9]+)+\b")
HEX_NUMBER_PATTERN = re.compile(r"\b0x[0-9a-fA-F]+\b")
DEC_NUMBER_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\b")
WORD_TOKEN_PATTERN = re.compile(r"\b[a-zA-Z]+\b")
SEPARATOR_CELL_PATTERN = re.compile(r"^:?-{2,}:?$")
TEST_IDEAS_HEADING = "## Derived test ideas"
LIST_MARKER_PATTERN = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
BLOCKED_ENTRY_PATTERN = re.compile(
    r"(?P<id>(?:REQ|SYS|SWR|TC|TMP)(?:-[A-Z0-9]+)+): BLOCKED \(open question\)"
    r"(?: — see findings row (?P=id))?"
)

NUMBER_WORDS: dict[str, str] = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
    "thirty": "30",
    "forty": "40",
    "fifty": "50",
    "sixty": "60",
    "seventy": "70",
    "eighty": "80",
    "ninety": "90",
    "hundred": "100",
    "thousand": "1000",
    "dozen": "12",
    "half": "0.5",
}

CHECKLIST_RULE_PATTERN = re.compile(r"\bQ(?:1[0-5]|[1-9])\b", re.IGNORECASE)
CHECKLIST_VOCAB_TERMS: tuple[str, ...] = (
    "missing or duplicate id",
    "missing unique id",
    "duplicate id",
    "non-atomic",
    "not singular",
    "unnecessary",
    "unlinked",
    "wrong abstraction",
    "ambiguous",
    "incomplete",
    "infeasible",
    "unverifiable",
    "inconsistent",
    "non-conforming",
    "missing error",
    "degraded",
    "implementation-biased",
    "missing status",
    "missing asil",
    "missing traceability",
    "traceability",
    "set-level conflict",
    "conflict",
    "escape clause",
    "hidden timing",
    "relative timing",
    "vague",
    "missing unit",
    "missing timing",
    "missing initial",
    "missing fault",
    "missing range",
    "missing tolerance",
    "open placeholder",
    "tbd",
    "passive voice",
    "quantifier",
)


@dataclass(frozen=True)
class TestIdea:
    """One non-empty line of the derived test ideas section, with its list marker removed."""

    text: str
    blocked: bool
    indented: bool = False


def _normalize_ws(text: str) -> str:
    """Collapse internal whitespace runs to single spaces and strip ends."""
    return re.sub(r"\s+", " ", text).strip()


def _strip_outer_quotes(text: str) -> str:
    """Strip one pair of matching outer quotes from a quoted table cell."""
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ('"', "'"):
        return stripped[1:-1].strip()
    return stripped


PLACEHOLDER_CELL_WORDS = {"tbd", "todo", "placeholder", "none", "n/a", "unknown", "lorem", "ipsum"}


def _has_checklist_defect_type(defect_type: str) -> bool:
    """Return True if defect_type names at least one checklist rule ID or vocabulary term."""
    lowered = defect_type.lower()
    if CHECKLIST_RULE_PATTERN.search(defect_type):
        return True
    return any(term in lowered for term in CHECKLIST_VOCAB_TERMS)


def _matches_allowed_defect_class(defect_type: str, allowed: tuple[str, ...]) -> bool:
    """Return True if defect_type matches at least one allowed defect class for a seeded ID."""
    lowered = defect_type.lower()
    for term in allowed:
        if re.fullmatch(r"q(?:1[0-5]|[1-9])", term):
            if re.search(rf"\b{re.escape(term)}\b", lowered):
                return True
        elif term in lowered:
            return True
    return False


def _is_nontrivial_explanation(text: str) -> bool:
    """Return True if a why/question cell has >= 5 words, >= 3 distinct words, and is not a placeholder."""
    words = re.findall(r"[A-Za-z0-9_'-]+", text)
    if len(words) < 5:
        return False
    distinct = {word.lower() for word in words}
    if len(distinct) < 3:
        return False
    stripped = text.strip().lower().rstrip(".?!")
    if stripped in PLACEHOLDER_CELL_WORDS or distinct <= PLACEHOLDER_CELL_WORDS:
        return False
    return True


def _parse_spec_sections(spec_text: str) -> tuple[str, dict[str, str], list[str]]:
    """Return normalized full spec text, per-ID section texts, and unnumbered section texts."""
    sections: dict[str, list[str]] = {}
    unnumbered: list[str] = []
    current_ids: list[str] = []
    in_unnumbered = False

    for line in spec_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            found_ids = ID_TOKEN_PATTERN.findall(stripped)
            if found_ids:
                current_ids = found_ids
                in_unnumbered = False
                for req_id in found_ids:
                    sections.setdefault(req_id, [])
            else:
                current_ids = []
                in_unnumbered = True
            continue
        if stripped.startswith("#") or "shall" not in stripped.lower():
            continue
        if current_ids:
            for req_id in current_ids:
                sections[req_id].append(stripped)
        elif in_unnumbered:
            unnumbered.append(stripped)

    normalized_sections = {
        req_id: _normalize_ws(" ".join(parts)) for req_id, parts in sections.items()
    }
    return _normalize_ws(" ".join(normalized_sections.values()) + " " + " ".join(unnumbered)), normalized_sections, [
        _normalize_ws(part) for part in unnumbered if part.strip()
    ]


def extract_numbers(text: str) -> set[str]:
    """Return numeric literals and number words from text after stripping requirement IDs."""
    without_ids = ID_TOKEN_PATTERN.sub(" ", text)
    hex_tokens = {match.group(0).lower() for match in HEX_NUMBER_PATTERN.finditer(without_ids)}
    without_hex = HEX_NUMBER_PATTERN.sub(" ", without_ids)
    dec_tokens = {match.group(0) for match in DEC_NUMBER_PATTERN.finditer(without_hex)}
    word_tokens = {
        NUMBER_WORDS[match.group(0).lower()]
        for match in WORD_TOKEN_PATTERN.finditer(without_hex)
        if match.group(0).lower() in NUMBER_WORDS
    }
    return hex_tokens | dec_tokens | word_tokens


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
        indented = line[:1].isspace()
        content = LIST_MARKER_PATTERN.sub("", line, count=1).strip()
        if not content:
            continue
        ideas.append(
            TestIdea(
                text=content,
                blocked=BLOCKED_ENTRY_PATTERN.fullmatch(content) is not None,
                indented=indented,
            )
        )
    return ideas


def grade_candidate(case_name: str, candidate_dir: Path) -> list[str]:
    """Return failure reasons for a candidate directory; empty list means pass."""
    if case_name not in CASES:
        raise KeyError(f"Unknown case: {case_name}")
    spec = CASES[case_name]
    source_path = TESTS_DIR / "fixtures" / case_name / "input" / "spec.md"
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
    spec_text = source_path.read_text(encoding="utf-8")
    source_numbers = extract_numbers(spec_text)
    norm_spec, sections_by_id, unnumbered_sections = _parse_spec_sections(spec_text)

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

        for cell_name in ("why", "question"):
            if row[cell_name] and not _is_nontrivial_explanation(row[cell_name]):
                failures.append(
                    f"Row {row_index} ({row['id']}) {cell_name} cell is trivial or placeholder (requires >= 5 non-placeholder words)"
                )

        if row["defect_type"] and not _has_checklist_defect_type(row["defect_type"]):
            failures.append(
                f"Row {row_index} ({row['id']}) defect type '{row['defect_type']}' is not from the checklist vocabulary"
            )
        elif row["defect_type"]:
            for label, pattern in spec.seeded_patterns:
                if re.search(pattern, row["id"], re.IGNORECASE):
                    allowed = spec.seeded_defect_classes.get(label, ())
                    if allowed and not _matches_allowed_defect_class(row["defect_type"], allowed):
                        failures.append(
                            f"Row {row_index} ({row['id']}) defect type '{row['defect_type']}' does not match allowed defect classes for {label}: {', '.join(allowed)}"
                        )

        clean_quote = _normalize_ws(_strip_outer_quotes(row["quote"]))
        if len(clean_quote) < 10 or len(clean_quote.split()) < 2:
            failures.append(
                f"Row {row_index} ({row['id']}) quote is too short to be a verbatim requirement quote"
            )
        else:
            row_ids = ID_TOKEN_PATTERN.findall(row["id"])
            matched_section = next((sections_by_id[rid] for rid in row_ids if rid in sections_by_id), None)
            if matched_section is not None:
                if clean_quote not in matched_section:
                    failures.append(
                        f"Row {row_index} ({row['id']}) quote does not appear verbatim in {row_ids[0]}"
                    )
            elif any(rid.startswith("TMP-") for rid in row_ids) and unnumbered_sections:
                if not any(clean_quote in sec for sec in unnumbered_sections):
                    failures.append(
                        f"Row {row_index} ({row['id']}) quote does not appear verbatim in an unnumbered requirement"
                    )
            elif clean_quote not in norm_spec:
                failures.append(
                    f"Row {row_index} ({row['id']}) quote does not appear verbatim in source specification"
                )

        # Match source concepts, allowing the domain synonyms used in the references.
        stopwords = {"the", "shall", "when", "while", "then", "into", "after", "before", "within",
                     "with", "from", "that", "this", "and", "true", "false", "linkmanager",
                     "can", "cancodec", "dtcdebouncer", "received", "called", "system"}
        terms = {word.lower() for word in re.findall(r"[A-Za-z]+|\d+", clean_quote)
                 if len(word) > 2 or word.isdigit()} - stopwords
        aliases = {
            "quickly": {"handshake", "response", "latency", "deadline"},
            "corrupted": {"corruption", "corrupted", "crc", "dlc"},
            "samples": {"sample", "samples", "counter", "step", "clamping"},
            "counter": {"threshold", "confirmation", "confirmed", "req-dtc-012"},
            "default": {"default", "defaults", "per-signal"},
        }
        for word in tuple(terms):
            terms.update(aliases.get(word, ()))
        for field in ("why", "question"):
            if not any(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", row[field], re.I)
                       for term in terms):
                failures.append(f"Row {row_index} ({row['id']}) {field} must reference a specific quoted term/value")

        rewrite = row["rewrite"]
        # A typed unresolved quantity needs a unit; open questions must also have
        # matching BLOCKED entries below, even if another criterion is present.
        criterion = re.search(
            r"(?:\b\d+(?:\.\d+)?|<TBD: [a-z]+(?:_[a-z0-9]+)*>)`?\s*"
            r"(?:ms|s|us|bytes?|bits?|cycles?|samples?|degC|%)(?![A-Za-z])"
            r"|\b[A-Za-z_]\w*::[A-Za-z_]\w*", rewrite
        )
        # Non-numeric rules may also remain typed and unresolved (for example
        # state_rule); they must describe the SHALL action, not just its guard.
        action = re.split(r"\bSHALL\b", rewrite, maxsplit=1, flags=re.I)[-1]
        unresolved_rule = re.search(
            r"<TBD: [a-z]+(?:_[a-z0-9]+)*_(?:rule|state|flag|mode|class|values)>", action
        )
        if not (criterion or unresolved_rule) or re.search(r"\bSHALL\s+(?:work|function|operate)\b", rewrite, re.I):
            failures.append(f"Row {row_index} ({row['id']}) rewrite lacks a measurable criterion")
        subjects = re.findall(r"`(LinkManager|CanCodec|DtcDebouncer)`", clean_quote)
        if subjects and not all(subject in rewrite for subject in subjects):
            failures.append(f"Row {row_index} ({row['id']}) rewrite must retain the requirement subject")

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
    clean_id_set = set(spec.clean_ids)
    try:
        test_ideas = parse_test_ideas(findings_text)
    except ValueError as error:
        failures.append(str(error))
        test_ideas = []

    blocked_ids: set[str] = set()
    active_parent_clean = False
    for idea_index, idea in enumerate(test_ideas, 1):
        idea_tokens = set(ID_TOKEN_PATTERN.findall(idea.text))
        idea_refs = ", ".join(sorted(idea_tokens)) or "unknown"
        invented_in_idea = sorted(extract_numbers(idea.text) - source_numbers)
        if invented_in_idea:
            failures.append(
                f"Test idea {idea_index} ({idea_refs}) invented numeric value(s) not in source: {', '.join(invented_in_idea)}"
            )
        if idea.blocked:
            blocked_match = BLOCKED_ENTRY_PATTERN.fullmatch(idea.text)
            if blocked_match:
                blocked_ids.add(blocked_match.group("id"))
            active_parent_clean = False
            continue
        for ref in sorted(idea_tokens & open_ids):
            failures.append(
                f"Test idea {idea_index} references {ref}, which has an open question; "
                f"use the exact blocked entry '{ref}: BLOCKED (open question)' instead"
            )
        has_clean_ref = bool(idea_tokens & clean_id_set)
        if not idea.indented:
            active_parent_clean = has_clean_ref and not bool(idea_tokens & open_ids)
            if not has_clean_ref:
                failures.append(
                    f"Test idea {idea_index} must reference a clean requirement ID or use '{TEST_IDEAS_HEADING}' BLOCKED format"
                )
        elif not has_clean_ref and not active_parent_clean:
            failures.append(
                f"Test idea {idea_index} is not linked to a clean requirement ID"
            )

    missing_blocked = sorted(open_ids - blocked_ids)
    if missing_blocked:
        failures.append(
            f"Missing exact BLOCKED (open question) entry in '{TEST_IDEAS_HEADING}' for: {', '.join(missing_blocked)}"
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
