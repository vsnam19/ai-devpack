#!/usr/bin/env python3
# devpack_memory.py - CLI for inspecting, adding, approving, superseding, expiring, and querying project memory.
#
# Usage:
#   python3 runner/devpack_memory.py [--workdir DIR] <subcommand> [options]
#
# Subcommands:
#   list       List memory entries (filter by --scope, --type, --status)
#   show       Show full contents and metadata of a memory entry by ID
#   add        Add a human-authored memory entry (with secret/PII check)
#   approve    Approve a pending decision/fact entry into active status
#   supersede  Mark an existing entry as superseded
#   expire     Mark an entry (or all past-expires entries with --expired) as expired
#   query      Query active entries matching a task brief (--explain for details)
#
# Exit codes:
#   0 - Operation completed successfully
#   1 - Validation failure, secret/PII rejection, or missing entry
#   2 - Invalid command-line arguments
"""CLI entrypoint for bounded agent memory operations per SPEC_v02 section 5.5."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

RUNNER_DIR = Path(__file__).resolve().parent
REPO_ROOT = RUNNER_DIR.parent
for _p in (str(REPO_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from runner.memory import (  # noqa: E402
    DEFAULT_BUDGET_TOKENS,
    DEFAULT_TOP_K,
    MemoryNotFoundError,
    MemoryValidationError,
    VALID_STATUSES,
    VALID_TYPES,
    add_memory_entry,
    approve_memory_entry,
    expire_memory_entry,
    expire_stale_entries,
    find_memory_entry,
    load_memory_entries,
    memory_root_for,
    retrieve_memory,
    supersede_memory_entry,
)


def _resolve_memory_root(args: argparse.Namespace) -> Path:
    """Resolve the `.devpack/memory` root from ``--memory-dir`` or ``--workdir``."""
    mem_dir = getattr(args, "memory_dir", "")
    if mem_dir:
        return Path(mem_dir).resolve()
    workdir = getattr(args, "workdir", ".")
    return memory_root_for(Path(workdir).resolve())


def _cmd_list(args: argparse.Namespace) -> int:
    """Execute the ``list`` subcommand."""
    root = _resolve_memory_root(args)
    entries = load_memory_entries(root)
    if args.scope:
        entries = [e for e in entries if e.scope == args.scope]
    if args.type:
        entries = [e for e in entries if e.entry_type == args.type]
    if args.status:
        entries = [e for e in entries if e.status == args.status]

    if args.json:
        print(json.dumps([e.to_dict() for e in entries], indent=2))
        return 0

    if not entries:
        print("(no memory entries)")
        return 0

    print(f"{'ID':<28} {'TYPE':<10} {'STATUS':<11} {'SCOPE':<24} {'TAGS'}")
    print(f"{'-'*28} {'-'*10} {'-'*11} {'-'*24} {'-'*20}")
    for entry in entries:
        tags = ",".join(entry.tags)
        print(f"{entry.id:<28} {entry.entry_type:<10} {entry.status:<11} {entry.scope:<24} {tags}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    """Execute the ``show`` subcommand."""
    root = _resolve_memory_root(args)
    entry = find_memory_entry(root, args.id)
    if args.json:
        print(json.dumps(entry.to_dict(), indent=2))
        return 0
    if entry.path is not None:
        print(f"File: {entry.path}")
    print(f"SHA-256: {entry.sha256}")
    print("---")
    if entry.path is not None and entry.path.is_file():
        print(entry.path.read_text(encoding="utf-8").rstrip())
    else:
        print(entry.body)
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    """Execute the ``add`` subcommand."""
    root = _resolve_memory_root(args)
    body = args.body
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    if not body or not body.strip():
        raise MemoryValidationError("Either --body or --body-file with non-empty text is required")

    tags = [t.strip() for item in args.tags for t in item.split(",") if t.strip()]
    supersedes = [s.strip() for item in args.supersedes for s in item.split(",") if s.strip()]

    entry = add_memory_entry(
        root,
        entry_type=args.type,
        scope=args.scope,
        body=body,
        tags=tags,
        source=args.source,
        supersedes=supersedes,
        expires=args.expires,
        status=args.status,
        entry_id=args.id,
        title=args.title,
        patterns_file=args.patterns or None,
    )
    if args.json:
        print(json.dumps(entry.to_dict(), indent=2))
    else:
        print(f"Added memory entry {entry.id} ({entry.status}) at {entry.path}")
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    """Execute the ``approve`` subcommand."""
    root = _resolve_memory_root(args)
    entry = approve_memory_entry(root, args.id, patterns_file=args.patterns or None)
    if args.json:
        print(json.dumps(entry.to_dict(), indent=2))
    else:
        print(f"Approved memory entry {entry.id} -> status={entry.status}")
    return 0


def _cmd_supersede(args: argparse.Namespace) -> int:
    """Execute the ``supersede`` subcommand."""
    root = _resolve_memory_root(args)
    entry = supersede_memory_entry(root, args.id, by_id=args.by)
    if args.json:
        print(json.dumps(entry.to_dict(), indent=2))
    else:
        print(f"Superseded memory entry {entry.id}")
    return 0


def _cmd_expire(args: argparse.Namespace) -> int:
    """Execute the ``expire`` subcommand."""
    root = _resolve_memory_root(args)
    if args.expired:
        expired_list = expire_stale_entries(root)
        if args.json:
            print(json.dumps([e.to_dict() for e in expired_list], indent=2))
        else:
            print(f"Expired {len(expired_list)} stale memory entry(ies)")
            for item in expired_list:
                print(f"  - {item.id} (expires={item.expires})")
        return 0
    if not args.id:
        raise MemoryValidationError("Provide an entry <id> or --expired")
    entry = expire_memory_entry(root, args.id)
    if args.json:
        print(json.dumps(entry.to_dict(), indent=2))
    else:
        print(f"Expired memory entry {entry.id}")
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    """Execute the ``query`` subcommand."""
    root = _resolve_memory_root(args)
    task_text = args.task
    if args.task_file:
        task_text = Path(args.task_file).read_text(encoding="utf-8")
    elif not task_text:
        raise MemoryValidationError("--task or --task-file is required")

    result = retrieve_memory(
        root,
        task_text,
        role=args.role or None,
        engine=args.engine or None,
        top_k=args.top_k,
        budget_tokens=args.budget_tokens,
        reviewer_sees_handoffs=args.reviewer_sees_handoffs,
    )

    if args.json:
        payload = {
            "ids": result.ids,
            "hashes": result.hashes,
            "total_tokens": result.total_tokens,
            "budget_tokens": result.budget_tokens,
            "top_k": result.top_k,
            "matches": [m.to_dict() for m in result.matches],
            "formatted_prompt": result.formatted_prompt,
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(
        f"Selected {len(result.selected)}/{result.top_k} entries "
        f"({result.total_tokens}/{result.budget_tokens} tokens)"
    )
    if result.selected:
        for entry in result.selected:
            print(f"  + {entry.id} [{entry.entry_type}] ({entry.scope}) sha256={entry.sha256[:12]}")

    if args.explain:
        print("\nExplanation:")
        for match in result.matches:
            reasons: list[str] = []
            if match.matched_reqs:
                reasons.append("reqs=" + ",".join(match.matched_reqs))
            if match.matched_modules:
                reasons.append("modules=" + ",".join(match.matched_modules))
            if match.matched_files:
                reasons.append("files=" + ",".join(match.matched_files))
            if match.matched_symbols:
                reasons.append("symbols=" + ",".join(match.matched_symbols))
            detail = "; ".join(reasons) if reasons else "none"
            status_tag = "INCLUDED" if match.included else f"DROPPED:{match.reason}"
            print(
                f"  - {match.entry.id}: {status_tag} "
                f"(score={match.specificity_score}, scope={match.entry.scope}, "
                f"tokens={match.tokens}, created={match.entry.created}, matches=[{detail}])"
            )

    if result.formatted_prompt and not args.explain:
        print("")
        print(result.formatted_prompt.rstrip())
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for ``runner/devpack_memory.py``."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workdir", default=".", help="Repository workdir (default: current directory)")
    common.add_argument("--memory-dir", default="", help="Explicit path to .devpack/memory directory")
    common.add_argument("--json", action="store_true", help="Emit JSON output")
    common.add_argument("--patterns", default="", help="Optional secret/PII patterns file")

    sub_common = argparse.ArgumentParser(add_help=False)
    sub_common.add_argument("--workdir", default=argparse.SUPPRESS, help="Repository workdir (default: current directory)")
    sub_common.add_argument("--memory-dir", default=argparse.SUPPRESS, help="Explicit path to .devpack/memory directory")
    sub_common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit JSON output")
    sub_common.add_argument("--patterns", default=argparse.SUPPRESS, help="Optional secret/PII patterns file")

    parser = argparse.ArgumentParser(
        prog="devpack_memory.py",
        parents=[common],
        description="Manage and query bounded project memory (.devpack/memory).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", parents=[sub_common], help="List memory entries")
    p_list.add_argument("--scope", default="", help="Filter by scope")
    p_list.add_argument("--type", default="", choices=("", *VALID_TYPES), help="Filter by type")
    p_list.add_argument("--status", default="", choices=("", *VALID_STATUSES), help="Filter by status")
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", parents=[sub_common], help="Show a memory entry by ID")
    p_show.add_argument("id", help="Memory entry ID")
    p_show.set_defaults(func=_cmd_show)

    p_add = sub.add_parser("add", parents=[sub_common], help="Add a human-authored memory entry")
    p_add.add_argument("--type", required=True, choices=VALID_TYPES, help="Entry type")
    p_add.add_argument("--scope", default="project", help="Entry scope (default: project)")
    p_add.add_argument("--tags", action="append", default=[], help="Comma-separated or repeatable tags")
    p_add.add_argument("--body", default="", help="Entry body text")
    p_add.add_argument("--body-file", default="", help="Read entry body from file")
    p_add.add_argument("--title", default="", help="Optional short title")
    p_add.add_argument("--id", default="", help="Optional explicit entry ID")
    p_add.add_argument("--source", default="human", help="Source attribution (default: human)")
    p_add.add_argument("--supersedes", action="append", default=[], help="Entry ID(s) superseded by this entry")
    p_add.add_argument("--expires", default="", help="Optional expiry date/timestamp (ISO-8601)")
    p_add.add_argument("--status", default="active", choices=("active", "pending"), help="Initial status (default: active)")
    p_add.set_defaults(func=_cmd_add)

    p_approve = sub.add_parser("approve", parents=[sub_common], help="Approve a pending memory entry")
    p_approve.add_argument("id", help="Pending entry ID to approve")
    p_approve.set_defaults(func=_cmd_approve)

    p_sup = sub.add_parser("supersede", parents=[sub_common], help="Mark an entry as superseded")
    p_sup.add_argument("id", help="Entry ID to mark superseded")
    p_sup.add_argument("--by", default="", help="Optional superseding entry ID")
    p_sup.set_defaults(func=_cmd_supersede)

    p_exp = sub.add_parser("expire", parents=[sub_common], help="Expire an entry or all past-due entries")
    p_exp.add_argument("id", nargs="?", default="", help="Entry ID to expire")
    p_exp.add_argument("--expired", action="store_true", help="Expire all active entries past their expires timestamp")
    p_exp.set_defaults(func=_cmd_expire)

    p_query = sub.add_parser("query", parents=[sub_common], help="Query relevant active entries for a task brief")
    p_query.add_argument("--task", default="", help="Task brief text")
    p_query.add_argument("--task-file", default="", help="Path to task brief file")
    p_query.add_argument("--role", default="", help="Role scope (e.g. implementer, reviewer, fixer)")
    p_query.add_argument("--engine", default="", help="Engine scope (e.g. haiku, sonnet, local)")
    p_query.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help=f"Max entries (default: {DEFAULT_TOP_K})")
    p_query.add_argument(
        "--budget-tokens",
        type=int,
        default=DEFAULT_BUDGET_TOKENS,
        help=f"Token budget (default: {DEFAULT_BUDGET_TOKENS})",
    )
    p_query.add_argument(
        "--reviewer-sees-handoffs",
        action="store_true",
        help="Allow reviewer role to see non-reviewer handoff entries",
    )
    p_query.add_argument("--explain", action="store_true", help="Print match reasons and token counts")
    p_query.set_defaults(func=_cmd_query)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for ``runner/devpack_memory.py``."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (MemoryValidationError, MemoryNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
