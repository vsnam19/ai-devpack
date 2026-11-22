#!/usr/bin/env python3
# propose.py - Draft a bounded candidate engine profile from a harvest report.
#
# Usage:
#   python3 learning/propose.py --harvest FILE --engine NAME
#       [--active-profile FILE] [--out FILE] [--min-count 3] [--top-n 15]
#       [--max-lines 40] [--help]
#
# Reads a harvest JSON report produced by learning/harvest.py, selects the top-N
# recurring findings for the specified engine meeting --min-count, and drafts a
# candidate profile capped at <= 40 lines. Never modifies active profiles in
# knowledge/engine_profiles/ or runs git commands.
#
# Exit codes:
#   0 - Candidate profile drafted and written successfully
#   1 - Runtime error (missing harvest file or unsafe output path)
#   2 - Invalid command-line arguments or malformed harvest JSON
"""Draft a candidate per-engine profile from harvest findings without editing active profiles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Sequence

PACK_ROOT = Path(__file__).resolve().parent.parent
if str(PACK_ROOT) not in sys.path:
    sys.path.insert(0, str(PACK_ROOT))

from runner.agent_exec import parse_profile_version  # noqa: E402

DEFAULT_MAX_LINES = 40

RULE_GUIDANCE: dict[str, tuple[str, str]] = {
    "DOXYGEN-PARAM-DIR": (
        "Omitted [in]/[out]/[in,out] direction tag on @param",
        "Always document every parameter with @param[in], @param[out], or @param[in,out] matching order.",
    ),
    "DOXYGEN-FILE": (
        "Missing @file or @brief Doxygen comment",
        "Add a /** @file ... @brief ... */ header block and @brief on every class/function.",
    ),
    "NO-IMPL-IN-HEADER": (
        "Declared/defined non-template function implementation inside a header",
        "Keep only the declaration in the .hpp header and place the non-inline definition in the .cpp file.",
    ),
    "MISRA-8.2.2": (
        "Used C-style cast (T)expr",
        "Replace C-style casts with static_cast<T>(expr) or const_cast where appropriate.",
    ),
    "MISRA-21.6.1": (
        "Used dynamic heap allocation (new/delete/malloc/free)",
        "Use fixed-size stack or static storage; avoid heap allocation in runtime paths.",
    ),
    "MISRA-9.3.1": (
        "Omitted {} braces around control-flow body",
        "Always wrap if/else/for/while/do bodies in {} braces.",
    ),
    "MISRA-9.4.2": (
        "Omitted default branch in switch statement",
        "Include an explicit default: branch in every switch statement.",
    ),
    "MISRA-15.1.3": (
        "Single-argument constructor not marked explicit",
        "Mark every single-argument constructor with explicit.",
    ),
    "MISRA-13.3.1": (
        "Virtual override missing override/final specifier",
        "Add override (or final) to every overridden virtual method.",
    ),
    "MISRA-5.7.2": (
        "Left commented-out code in source file",
        "Delete commented-out code instead of leaving dead blocks.",
    ),
    "NAMING-MEANINGFUL": (
        "Used banned placeholder identifier (tmp/val/var/buf/ptr/obj)",
        "Choose descriptive domain-specific identifier names.",
    ),
    "NAMING-CONVENTION": (
        "Identifier violated naming convention",
        "Use CamelCase for types, camelBack for functions/vars, m_ for private members, kCamelCase for constants.",
    ),
    "HIS-PAR": (
        "Function exceeded 5 parameters",
        "Group related parameters into a parameter struct to stay at <= 5 parameters.",
    ),
    "TOOL-PROTOCOL-ERROR": (
        "Printed JSON tool call as plain text instead of invoking native tool_use",
        "Invoke tools through structured tool calls only; never echo JSON tool payloads as text.",
    ),
    "SCOPE-VIOLATION": (
        "Edited files outside allowed scope globs",
        "Restrict all edits strictly to files listed under Files allowed.",
    ),
    "TEST-LOCK-VIOLATION": (
        "Modified protected tests or weakened assertions",
        "Never alter protected tests; fix the implementation or write STOP_REPORT.md on conflict.",
    ),
}


def format_mistake_line(finding: dict[str, Any]) -> str:
    """Format one harvest finding dict into a single profile rule line citing rule ID and count."""
    rule_id = str(finding.get("rule_id") or "UNKNOWN").strip()
    count = int(finding.get("count") or 0)
    if rule_id in RULE_GUIDANCE:
        mistake, fix = RULE_GUIDANCE[rule_id]
    else:
        examples = finding.get("examples") or []
        sample = str(examples[0]).replace("\n", " ").strip() if examples else f"Triggered {rule_id}"
        sample = sample[:90]
        mistake = sample
        fix = f"Fix all {rule_id} violations before finishing."
    return f"- [{rule_id}] ({count} occurrences): {mistake} -> {fix}"


def draft_candidate_profile(
    harvest_data: dict[str, Any],
    *,
    engine_name: str,
    active_profile_text: str = "",
    min_count: int = 3,
    top_n: int = 15,
    max_lines: int = DEFAULT_MAX_LINES,
) -> tuple[str, dict[str, Any]]:
    """Build candidate profile markdown (capped at ``max_lines``) and summary metadata."""
    current_version = parse_profile_version(active_profile_text)
    next_version = (current_version + 1) if current_version is not None else 1

    all_findings = harvest_data.get("findings", [])
    if not isinstance(all_findings, list):
        raise ValueError("Malformed harvest JSON: 'findings' must be a list")

    eligible = [
        item for item in all_findings
        if isinstance(item, dict)
        and str(item.get("engine") or "").strip() == engine_name
        and int(item.get("count") or 0) >= min_count
    ]
    eligible.sort(key=lambda item: (-int(item.get("count") or 0), str(item.get("rule_id") or "")))
    selected = eligible[:top_n]

    header_lines = [
        f"# Engine Profile: {engine_name}",
        f"Version: {next_version}",
        "",
    ]
    rule_lines = [format_mistake_line(item) for item in selected]

    available = max(0, max_lines - len(header_lines))
    kept_rules = rule_lines[:available]
    output_lines = header_lines + kept_rules
    if len(output_lines) > max_lines:
        output_lines = output_lines[:max_lines]

    content = "\n".join(output_lines).rstrip() + "\n"
    meta = {
        "engine": engine_name,
        "version": next_version,
        "eligible_count": len(eligible),
        "selected_count": len(kept_rules),
        "line_count": len(content.splitlines()),
        "rules": [str(item.get("rule_id")) for item in selected[: len(kept_rules)]],
    }
    return content, meta


def _is_protected_profile_path(out_path: Path, active_path: Path, profiles_dir: Path) -> bool:
    """Return True if ``out_path`` targets ``active_path`` or inside ``knowledge/engine_profiles/``."""
    resolved_out = out_path.resolve()
    if resolved_out == active_path.resolve():
        return True
    resolved_dir = profiles_dir.resolve()
    return resolved_out == resolved_dir or resolved_dir in resolved_out.parents


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for ``learning/propose.py``."""
    parser = argparse.ArgumentParser(
        prog="propose.py",
        description="Draft a <=40 line candidate engine profile from a harvest JSON report.",
    )
    parser.add_argument("--harvest", required=True, help="Path to harvest JSON report")
    parser.add_argument("--engine", required=True, help="Target engine name (e.g. local, haiku)")
    parser.add_argument(
        "--active-profile",
        default="",
        help="Optional path to current active profile (default: knowledge/engine_profiles/<engine>.md)",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Output path for candidate profile (default: learning/reports/candidate-<engine>-v<N>.md)",
    )
    parser.add_argument(
        "--min-count",
        type=int,
        default=3,
        help="Minimum failure occurrences required to include a rule (default: 3)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=15,
        help="Maximum number of top recurring rules to include (default: 15)",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=DEFAULT_MAX_LINES,
        help="Maximum total lines allowed in candidate profile (default: 40)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for ``learning/propose.py``."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.min_count < 1 or args.top_n < 1 or args.max_lines < 3:
        print("error: --min-count >= 1, --top-n >= 1, and --max-lines >= 3 required", file=sys.stderr)
        return 2

    harvest_path = Path(args.harvest).resolve()
    if not harvest_path.is_file():
        print(f"error: harvest file not found: {harvest_path}", file=sys.stderr)
        return 1

    try:
        harvest_data = json.loads(harvest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: failed to read harvest JSON: {exc}", file=sys.stderr)
        return 2

    if not isinstance(harvest_data, dict):
        print("error: harvest JSON root must be an object", file=sys.stderr)
        return 2

    profiles_dir = PACK_ROOT / "knowledge" / "engine_profiles"
    active_path = (
        Path(args.active_profile).resolve()
        if args.active_profile
        else (profiles_dir / f"{args.engine}.md").resolve()
    )
    active_text = active_path.read_text(encoding="utf-8") if active_path.is_file() else ""

    try:
        candidate_text, meta = draft_candidate_profile(
            harvest_data,
            engine_name=args.engine,
            active_profile_text=active_text,
            min_count=args.min_count,
            top_n=args.top_n,
            max_lines=args.max_lines,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    out_path = (
        Path(args.out).resolve()
        if args.out
        else (PACK_ROOT / "learning" / "reports" / f"candidate-{args.engine}-v{meta['version']}.md").resolve()
    )
    if _is_protected_profile_path(out_path, active_path, profiles_dir):
        print(
            f"error: refusing to write candidate over active profile directory/file: {out_path}",
            file=sys.stderr,
        )
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(candidate_text, encoding="utf-8")
    print(f"Wrote candidate profile ({meta['selected_count']} rules, {meta['line_count']} lines): {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
