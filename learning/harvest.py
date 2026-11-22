#!/usr/bin/env python3
# harvest.py - Mine run directories for recurring engine failures and lint findings.
#
# Usage:
#   python3 learning/harvest.py <runs-root> [--since YYYY-MM-DD]
#       [--out-dir learning/reports] [--date YYYY-MM-DD] [--max-examples 5] [--help]
#
# Reads run directories (run.json, steps/*.meta.json, steps/*.jsonl, steps/*gate*.txt,
# review taxonomy tables, and STOP_REPORT.md), aggregates recurring failures by
# (engine, rule_id), redacts secrets/paths, and writes <out-dir>/<date>.{md,json}.
#
# Exit codes:
#   0 - Harvest completed and wrote Markdown and JSON reports
#   1 - Runtime error (missing runs-root or unreadable inputs)
#   2 - Invalid command-line arguments
"""Mine ai-devpack run logs for recurring engine failures with redacted snippets."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence

PACK_ROOT = Path(__file__).resolve().parent.parent
if str(PACK_ROOT) not in sys.path:
    sys.path.insert(0, str(PACK_ROOT))

from runner.agent_exec import parse_stream, redact_sensitive_text  # noqa: E402
from runner.devpack_logs import Redactor  # noqa: E402

_LINT_LINE_RE = re.compile(
    r"^\s*(?:-\s*)?\[(ERROR|WARNING|INFO)\]\[([A-Za-z0-9_.-]+)\]\s+(.+?)\s*$"
)
_STEP_STEM_RE = re.compile(r"^(\d+)-([^-]+)-(.+)$")
_REVIEW_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([A-Z]{2,5})\s*\|\s*(blocker|major|minor)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|")
_RUN_ID_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T")
_VALID_REVIEW_CATS = {"COR", "REQ", "TST", "GAT", "MIS", "DOC", "IFC", "CPX", "SEC", "PRV"}


def _parse_iso_date(raw: str) -> datetime | None:
    """Parse an ISO-8601 date or timestamp string into a UTC-aware datetime."""
    cleaned = raw.strip()
    if not cleaned or cleaned == "-":
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        try:
            dt = datetime.strptime(cleaned[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _run_timestamp(run_dir: Path, run_meta: dict[str, Any]) -> datetime | None:
    """Determine the timestamp of a run directory from run.json or its run_id prefix."""
    for key in ("started", "finished"):
        val = run_meta.get(key)
        if isinstance(val, str):
            parsed = _parse_iso_date(val)
            if parsed is not None:
                return parsed
    match = _RUN_ID_DATE_RE.match(run_dir.name)
    if match:
        y, m, d = int(match.group(1)), int(match.group(2)), int(match.group(3))
        return datetime(y, m, d, tzinfo=timezone.utc)
    return None


def discover_run_dirs(runs_root: Path) -> list[Path]:
    """Find all run directories containing ``run.json`` or ``steps/`` beneath ``runs_root``."""
    found: list[Path] = []
    if not runs_root.is_dir():
        return found

    def _walk(directory: Path) -> None:
        """Recursively locate run directories without descending into steps/."""
        if (directory / "run.json").is_file() or (directory / "steps").is_dir():
            found.append(directory)
            return
        for child in sorted(directory.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                _walk(child)

    _walk(runs_root)
    return found


def _default_run_engine(run_meta: dict[str, Any]) -> str:
    """Infer the primary worker/implementer engine name from ``run.json``."""
    engines = run_meta.get("engines")
    if isinstance(engines, dict):
        for role in ("implementer", "fixer", "worker", "eval"):
            val = engines.get(role)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for val in engines.values():
            if isinstance(val, str) and val.strip():
                return val.strip()
    elif isinstance(engines, list) and engines:
        first = engines[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    elif isinstance(engines, str) and engines.strip():
        return engines.strip()
    return "unknown"


def _step_num_and_role_engine(stem: str) -> tuple[int, str, str]:
    """Parse ``NN-role-engine`` into ``(step_num, role, engine)``."""
    match = _STEP_STEM_RE.match(stem)
    if not match:
        return 0, stem, ""
    return int(match.group(1)), match.group(2), match.group(3)


def _record_finding(
    buckets: dict[tuple[str, str], dict[str, Any]],
    *,
    engine: str,
    rule_id: str,
    kind: str,
    snippet: str,
    run_id: str,
    task_id: str,
    max_examples: int,
    redactor: Redactor,
) -> None:
    """Accumulate one redacted failure occurrence into ``buckets``."""
    clean_eng = engine.strip() or "unknown"
    clean_rule = rule_id.strip() or "UNKNOWN"
    key = (clean_eng, clean_rule)
    entry = buckets.get(key)
    if entry is None:
        entry = {
            "engine": clean_eng,
            "rule_id": clean_rule,
            "kind": kind,
            "count": 0,
            "runs": set(),
            "tasks": set(),
            "examples": [],
        }
        buckets[key] = entry
    entry["count"] += 1
    entry["runs"].add(run_id)
    if task_id and task_id != "-":
        entry["tasks"].add(task_id)
    redacted = redact_sensitive_text(redactor.redact(snippet.strip()))
    if redacted and len(entry["examples"]) < max_examples and redacted not in entry["examples"]:
        entry["examples"].append(redacted[:400])


def _extract_review_findings(text: str) -> list[tuple[str, str]]:
    """Extract ``(REVIEW-<CAT>, snippet)`` pairs from review-taxonomy Markdown tables."""
    findings: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        match = _REVIEW_ROW_RE.match(stripped)
        if not match:
            continue
        cat = match.group(2).strip().upper()
        if cat not in _VALID_REVIEW_CATS:
            continue
        severity = match.group(3).strip().lower()
        location = match.group(4).strip()
        problem = match.group(5).strip()
        findings.append((f"REVIEW-{cat}", f"[{severity}] {location}: {problem}"))
    return findings


def harvest_runs(
    runs_root: Path,
    *,
    since: str = "",
    max_examples: int = 5,
) -> dict[str, Any]:
    """Scan run directories under ``runs_root`` and aggregate failures by (engine, rule_id)."""
    if not runs_root.is_dir():
        raise FileNotFoundError(f"Runs root directory not found: {runs_root}")

    since_dt: datetime | None = None
    if since.strip():
        since_dt = _parse_iso_date(since)
        if since_dt is None:
            raise ValueError(f"Invalid --since date '{since}': expected YYYY-MM-DD or ISO-8601")

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    redactor = Redactor()
    def _rec(**kw: Any) -> None:
        """Record a finding using the shared redactor."""
        _record_finding(buckets, redactor=redactor, **kw)
    runs_scanned = 0
    runs_skipped = 0

    for run_dir in discover_run_dirs(runs_root):
        run_json_path = run_dir / "run.json"
        run_meta: dict[str, Any] = {}
        if run_json_path.is_file():
            try:
                loaded = json.loads(run_json_path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(loaded, dict):
                    run_meta = loaded
            except json.JSONDecodeError:
                run_meta = {}

        if since_dt is not None:
            run_dt = _run_timestamp(run_dir, run_meta)
            if run_dt is not None and run_dt < since_dt:
                runs_skipped += 1
                continue

        runs_scanned += 1
        run_id = str(run_meta.get("run_id") or run_dir.name)
        raw_task = run_meta.get("task", "")
        if isinstance(raw_task, dict):
            task_id = str(raw_task.get("id") or raw_task.get("task_id") or raw_task.get("name") or "")
        else:
            task_id = str(raw_task or "")
        default_engine = _default_run_engine(run_meta)

        steps_dir = run_dir / "steps"
        step_engines: list[tuple[int, str, str]] = []

        if steps_dir.is_dir():
            meta_files = sorted(steps_dir.glob("*.meta.json"))
            jsonl_files = sorted(steps_dir.glob("*.jsonl"))
            seen_stems: set[str] = set()

            for meta_path in meta_files:
                stem = meta_path.name[: -len(".meta.json")]
                seen_stems.add(stem)
                step_num, parsed_role, parsed_eng = _step_num_and_role_engine(stem)
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
                except (OSError, json.JSONDecodeError):
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}

                role = str(meta.get("role") or parsed_role or "worker")
                engine = str(meta.get("engine") or parsed_eng or default_engine)
                step_engines.append((step_num, role, engine))

                proto_err = int(meta.get("tool_protocol_errors") or 0)
                if proto_err > 0:
                    for _ in range(proto_err):
                        _rec(
                            engine=engine,
                            rule_id="TOOL-PROTOCOL-ERROR",
                            kind="protocol",
                            snippet="Assistant printed JSON tool call as plain text instead of tool_use block",
                            run_id=run_id,
                            task_id=task_id,
                            max_examples=max_examples,
                        )

                status = str(meta.get("status") or "")
                if status == "timeout":
                    _rec(
                        engine=engine,
                        rule_id="AGENT-TIMEOUT",
                        kind="agent",
                        snippet=str(meta.get("error") or "Agent execution timed out"),
                        run_id=run_id,
                        task_id=task_id,
                        max_examples=max_examples,
                    )
                elif status == "agent_error":
                    _rec(
                        engine=engine,
                        rule_id="AGENT-ERROR",
                        kind="agent",
                        snippet=str(meta.get("error") or meta.get("final_text") or "Agent exited with error"),
                        run_id=run_id,
                        task_id=task_id,
                        max_examples=max_examples,
                    )

                final_text = str(meta.get("final_text") or "")
                if final_text:
                    target_engine = default_engine if role == "reviewer" else engine
                    for rev_rule, rev_snip in _extract_review_findings(final_text):
                        _rec(
                            engine=target_engine,
                            rule_id=rev_rule,
                            kind="review",
                            snippet=rev_snip,
                            run_id=run_id,
                            task_id=task_id,
                            max_examples=max_examples,
                        )
                    if "STOP_REPORT" in final_text:
                        _rec(
                            engine=engine,
                            rule_id="STOP-REPORT",
                            kind="stop",
                            snippet=final_text,
                            run_id=run_id,
                            task_id=task_id,
                            max_examples=max_examples,
                        )

            for jsonl_path in jsonl_files:
                stem = jsonl_path.name[: -len(".jsonl")]
                if stem in seen_stems:
                    continue
                step_num, parsed_role, parsed_eng = _step_num_and_role_engine(stem)
                engine = parsed_eng or default_engine
                step_engines.append((step_num, parsed_role, engine))
                parsed = parse_stream(jsonl_path)
                if parsed.tool_protocol_errors > 0:
                    for _ in range(parsed.tool_protocol_errors):
                        _rec(
                            engine=engine,
                            rule_id="TOOL-PROTOCOL-ERROR",
                            kind="protocol",
                            snippet="Assistant printed JSON tool call as plain text instead of tool_use block",
                            run_id=run_id,
                            task_id=task_id,
                            max_examples=max_examples,
                        )
                if parsed.final_text:
                    target_engine = default_engine if parsed_role == "reviewer" else engine
                    for rev_rule, rev_snip in _extract_review_findings(parsed.final_text):
                        _rec(
                            engine=target_engine,
                            rule_id=rev_rule,
                            kind="review",
                            snippet=rev_snip,
                            run_id=run_id,
                            task_id=task_id,
                            max_examples=max_examples,
                        )

            step_engines.sort(key=lambda item: item[0])

            gate_paths = sorted(
                p for p in steps_dir.iterdir()
                if p.is_file() and p.name.endswith(".txt") and ("gate" in p.name or p.name.endswith("-grader.txt"))
            )
            for g_path in gate_paths:
                g_num_match = re.match(r"^(\d+)", g_path.name)
                g_num = int(g_num_match.group(1)) if g_num_match else 999
                attributed_engine = default_engine
                for s_num, s_role, s_eng in reversed(step_engines):
                    if s_num < g_num and s_role != "reviewer" and s_eng:
                        attributed_engine = s_eng
                        break

                g_text = g_path.read_text(encoding="utf-8", errors="replace")
                saw_specific = False
                for line in g_text.splitlines():
                    l_match = _LINT_LINE_RE.match(line)
                    if l_match:
                        saw_specific = True
                        rule_id = l_match.group(2)
                        msg = l_match.group(3)
                        _rec(
                            engine=attributed_engine,
                            rule_id=rule_id,
                            kind="lint",
                            snippet=msg,
                            run_id=run_id,
                            task_id=task_id,
                            max_examples=max_examples,
                        )
                    elif "SCOPE_VIOLATION" in line:
                        saw_specific = True
                        _rec(
                            engine=attributed_engine,
                            rule_id="SCOPE-VIOLATION",
                            kind="gate",
                            snippet=line.strip(),
                            run_id=run_id,
                            task_id=task_id,
                            max_examples=max_examples,
                        )
                    elif "TEST_LOCK" in line or "test_lock" in line and "FAIL" in line:
                        saw_specific = True
                        _rec(
                            engine=attributed_engine,
                            rule_id="TEST-LOCK-VIOLATION",
                            kind="gate",
                            snippet=line.strip(),
                            run_id=run_id,
                            task_id=task_id,
                            max_examples=max_examples,
                        )
                    elif "STOP_REPORT" in line:
                        saw_specific = True
                        _rec(
                            engine=attributed_engine,
                            rule_id="STOP-REPORT",
                            kind="stop",
                            snippet=line.strip(),
                            run_id=run_id,
                            task_id=task_id,
                            max_examples=max_examples,
                        )

                if not saw_specific and "GATE_FAIL" in g_text:
                    _rec(
                        engine=attributed_engine,
                        rule_id="GATE-FAIL",
                        kind="gate",
                        snippet=g_text.splitlines()[0] if g_text.splitlines() else "GATE_FAIL",
                        run_id=run_id,
                        task_id=task_id,
                        max_examples=max_examples,
                    )

        stop_file = run_dir / "STOP_REPORT.md"
        if stop_file.is_file():
            stop_text = stop_file.read_text(encoding="utf-8", errors="replace").strip()
            _rec(
                engine=default_engine,
                rule_id="STOP-REPORT",
                kind="stop",
                snippet=stop_text or "STOP_REPORT.md present",
                run_id=run_id,
                task_id=task_id,
                max_examples=max_examples,
            )

        if str(run_meta.get("outcome") or "") == "ESCALATE_TO_HUMAN":
            esc_reason = str(run_meta.get("escalation_reason") or "Escalated to human")
            _rec(
                engine=default_engine,
                rule_id="ESCALATE-TO-HUMAN",
                kind="stop",
                snippet=esc_reason,
                run_id=run_id,
                task_id=task_id,
                max_examples=max_examples,
            )

    findings_list: list[dict[str, Any]] = []
    engines_map: dict[str, dict[str, Any]] = {}
    for entry in sorted(buckets.values(), key=lambda x: (x["engine"], -x["count"], x["rule_id"])):
        serialized = {
            "engine": entry["engine"],
            "rule_id": entry["rule_id"],
            "kind": entry["kind"],
            "count": entry["count"],
            "runs_affected": len(entry["runs"]),
            "task_ids": sorted(entry["tasks"]),
            "examples": entry["examples"],
        }
        findings_list.append(serialized)
        engines_map.setdefault(entry["engine"], {})[entry["rule_id"]] = serialized

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runs_root": redact_sensitive_text(str(runs_root)),
        "since": since.strip() or None,
        "runs_scanned": runs_scanned,
        "runs_skipped": runs_skipped,
        "findings": findings_list,
        "engines": engines_map,
    }


def render_harvest_markdown(report: dict[str, Any]) -> str:
    """Format a harvest report dictionary into a human-readable Markdown document."""
    lines = [
        "# Learning Harvest Report",
        "",
        f"- **Generated:** `{report['generated_at']}`",
        f"- **Runs Root:** `{report['runs_root']}`",
        f"- **Since:** `{report['since'] or 'all'}`",
        f"- **Runs Scanned:** `{report['runs_scanned']}` (skipped: `{report['runs_skipped']}`)",
        "",
    ]
    findings = report.get("findings", [])
    if not findings:
        lines.append("No failures or lint findings recorded.")
        lines.append("")
        return "\n".join(lines)

    lines.extend([
        "| Engine | Rule / Category | Kind | Count | Runs | Example |",
        "|---|---|---|---:|---:|---|",
    ])
    for row in findings:
        example = row["examples"][0] if row.get("examples") else "-"
        example = example.replace("|", "\\|").replace("\n", " ")[:120]
        lines.append(
            f"| {row['engine']} | `{row['rule_id']}` | {row['kind']} | "
            f"{row['count']} | {row['runs_affected']} | {example} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for ``learning/harvest.py``."""
    parser = argparse.ArgumentParser(
        prog="harvest.py",
        description="Aggregate recurring failures from run directories by (engine, rule/category).",
    )
    parser.add_argument(
        "runs_root",
        help="Root directory containing run directories (runs/<run-id>/)",
    )
    parser.add_argument(
        "--since",
        default="",
        help="Include only runs on or after DATE (YYYY-MM-DD or ISO-8601)",
    )
    parser.add_argument(
        "--out-dir",
        default=str(PACK_ROOT / "learning" / "reports"),
        help="Directory to write <date>.md and <date>.json (default: learning/reports)",
    )
    parser.add_argument(
        "--date",
        default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        help="Report date stem for <out-dir>/<date>.{md,json} (default: today UTC)",
    )
    parser.add_argument(
        "--max-examples",
        type=int,
        default=5,
        help="Maximum redacted example snippets stored per (engine, rule) (default: 5)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for ``learning/harvest.py``."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.max_examples < 1:
        print("error: --max-examples must be >= 1", file=sys.stderr)
        return 2

    runs_root = Path(args.runs_root).resolve()
    out_dir = Path(args.out_dir).resolve()
    try:
        report = harvest_runs(runs_root, since=args.since, max_examples=args.max_examples)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{args.date}.json"
    md_path = out_dir / f"{args.date}.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_harvest_markdown(report), encoding="utf-8")
    print(md_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
