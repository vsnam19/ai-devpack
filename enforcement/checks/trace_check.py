#!/usr/bin/env python3
# Header: Bidirectional requirement-to-test traceability check.
# Parses requirements from a text file (lines of the form "REQ-ID: description")
# and scans test files matching --tests globs for "@req REQ-ID" tags.
# Reports untested requirements and unknown test tags; exits 1 on either.
#
# Exit codes:
#   0 - Every requirement has >= 1 test tag and every test tag refers to a known requirement.
#   1 - One or more untested requirements, unknown @req tags, or input/parse errors.
"""Requirement-to-test bidirectional traceability verification.

ALM Adapter Interface
---------------------
External Application Lifecycle Management (ALM) systems (such as Polarion,
DOORS Next, or Jama Connect) can integrate with ``trace_check.py`` in two ways:
1. CLI export adapter: export approved requirements to a flat UTF-8 text file
   where each line conforms to ``REQ-ID: <summary text>`` (lines starting with
   ``#`` are treated as comments) and invoke ``trace_check.py --requirements <export_file>``.
2. Python adapter protocol: implement a loader conforming to :class:`ALMAdapter`
   with ``load_requirements() -> dict[str, str]`` returning a mapping of
   ``{req_id: description}`` and pass it to :func:`verify_traceability`.
"""

from __future__ import annotations

import argparse
import glob
import os
import pathlib
import re
import sys
from typing import Protocol

_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from enforcement.checks.scope_check import glob_to_regex, matches_any_glob  # noqa: E402

REQ_LINE_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+)\s*:\s*(.+)$")
TEST_TAG_PATTERN = re.compile(r"@req\s+([A-Za-z0-9_.-]+)")


class ALMAdapter(Protocol):
    """Protocol for ALM requirement loader adapters."""

    def load_requirements(self) -> dict[str, str]:
        """Return a mapping from requirement ID (``REQ-ID``) to requirement text."""


def parse_requirements_file(req_path: pathlib.Path) -> tuple[dict[str, str] | None, str | None]:
    """Parse a ``REQ-ID: text`` requirements file into an ordered dictionary."""
    if not req_path.is_file():
        return None, f"requirements file not found: {req_path}"

    try:
        lines = req_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"failed to read requirements file: {exc}"

    requirements: dict[str, str] = {}
    for lineno, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = REQ_LINE_PATTERN.match(stripped)
        if not match:
            return None, f"malformed requirement at {req_path}:{lineno}: '{stripped}'"
        req_id, req_text = match.group(1), match.group(2).strip()
        if not req_text:
            return None, f"empty requirement description at {req_path}:{lineno}"
        requirements[req_id] = req_text

    if not requirements:
        return None, f"no requirements found in {req_path}"

    return requirements, None


def resolve_test_files(repo_root: pathlib.Path, patterns: list[str]) -> list[pathlib.Path]:
    """Resolve file paths and glob patterns against ``repo_root`` and cwd."""
    found: dict[pathlib.Path, pathlib.Path] = {}
    relative_globs: list[str] = []

    for pat in patterns:
        cleaned = pat.strip()
        if not cleaned:
            continue

        direct_path = pathlib.Path(cleaned)
        if direct_path.is_file():
            resolved = direct_path.resolve()
            found[resolved] = direct_path
            continue

        repo_direct = repo_root / cleaned
        if repo_direct.is_file():
            resolved = repo_direct.resolve()
            found[resolved] = repo_direct
            continue

        for match_str in glob.glob(cleaned, recursive=True):
            p = pathlib.Path(match_str)
            if p.is_file():
                found[p.resolve()] = p

        for match_str in glob.glob(str(repo_root / cleaned), recursive=True):
            p = pathlib.Path(match_str)
            if p.is_file():
                found[p.resolve()] = p

        if not direct_path.is_absolute():
            relative_globs.append(cleaned)

    if relative_globs:
        compiled = [glob_to_regex(g) for g in relative_globs]
        for root, dirs, files in os.walk(repo_root):
            dirs[:] = sorted(d for d in dirs if d not in {".git", ".venv", "venv", "__pycache__"})
            for fname in sorted(files):
                candidate = pathlib.Path(root) / fname
                try:
                    rel_posix = candidate.resolve().relative_to(repo_root).as_posix()
                except ValueError:
                    continue
                if matches_any_glob(rel_posix, compiled):
                    found[candidate.resolve()] = candidate

    return [found[k] for k in sorted(found.keys())]


def scan_test_tags(
    test_files: list[pathlib.Path],
    repo_root: pathlib.Path,
) -> tuple[dict[str, list[str]], list[tuple[str, str, int]]]:
    """Scan ``test_files`` for ``@req REQ-ID`` tags.

    Returns ``(tag_to_locations, all_occurrences)`` where ``all_occurrences`` is
    a list of ``(req_id, display_path, lineno)`` tuples.
    """
    tag_to_locations: dict[str, list[str]] = {}
    occurrences: list[tuple[str, str, int]] = []

    for file_path in test_files:
        try:
            rel_display = file_path.resolve().relative_to(repo_root).as_posix()
        except ValueError:
            rel_display = file_path.name

        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in TEST_TAG_PATTERN.finditer(line):
                req_id = match.group(1)
                loc = f"{rel_display}:{lineno}"
                tag_to_locations.setdefault(req_id, []).append(loc)
                occurrences.append((req_id, rel_display, lineno))

    return tag_to_locations, occurrences


def verify_traceability(
    requirements: dict[str, str],
    occurrences: list[tuple[str, str, int]],
) -> tuple[list[str], list[tuple[str, str, int]]]:
    """Return ``(untested_req_ids, unknown_tag_occurrences)``."""
    covered_reqs = {req_id for req_id, _, _ in occurrences}
    untested = [req_id for req_id in requirements if req_id not in covered_reqs]
    unknown = [(req_id, path, lineno) for req_id, path, lineno in occurrences if req_id not in requirements]
    return untested, unknown


def build_arg_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser for trace_check."""
    parser = argparse.ArgumentParser(
        prog="trace_check.py",
        description=(
            "Check requirement-to-test traceability: report untested requirements and "
            "unknown @req tags; exit 1 on either."
        ),
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Base repository directory for resolving test globs (default: current directory).",
    )
    parser.add_argument(
        "--requirements",
        required=True,
        metavar="FILE",
        help="Path to the requirements file with 'REQ-ID: text' lines.",
    )
    parser.add_argument(
        "--tests",
        required=True,
        action="extend",
        nargs="+",
        metavar="GLOB",
        help="Test file paths or glob patterns to scan for '@req REQ-ID' tags.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run traceability check and return 0 if fully traced or 1 on any gap/unknown tag."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    repo_root = pathlib.Path(args.repo).resolve()
    req_path = pathlib.Path(args.requirements)
    if not req_path.is_absolute() and not req_path.is_file():
        candidate = repo_root / req_path
        if candidate.is_file():
            req_path = candidate

    requirements, err = parse_requirements_file(req_path)
    if requirements is None:
        print(f"TRACE_ERROR: {err}", file=sys.stderr)
        return 1

    test_files = resolve_test_files(repo_root, args.tests)
    _, occurrences = scan_test_tags(test_files, repo_root)
    untested, unknown = verify_traceability(requirements, occurrences)

    has_failure = False
    for req_id in untested:
        print(f"UNTESTED_REQ: {req_id}")
        has_failure = True

    for req_id, rel_path, lineno in unknown:
        print(f"UNKNOWN_TAG: {req_id} at {rel_path}:{lineno}")
        has_failure = True

    if has_failure:
        return 1

    print(f"TRACE_OK: {len(requirements)} requirement(s) traced across {len(test_files)} test file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
