#!/usr/bin/env python3
# devpack_approve.py - Human/CI-only CLI for listing, inspecting, approving, and rejecting checkpoints.
#
# SECURITY: Human / CI operator command ONLY. Must never be invoked by agents from
# inside a worker or orchestrator sandbox, and the host broker refuses to forward it.
#
# Usage:
#   python3 runner/devpack_approve.py [--workdir DIR] [--state-dir DIR] list [--status pending|decided|all] [--checkpoint ID] [--json]
#   python3 runner/devpack_approve.py [--workdir DIR] [--state-dir DIR] show <id> [--json]
#   python3 runner/devpack_approve.py [--workdir DIR] [--state-dir DIR] approve <id> [--note TEXT] [--json]
#   python3 runner/devpack_approve.py [--workdir DIR] [--state-dir DIR] reject <id> --note TEXT [--json]
#
# Exit codes:
#   0 - Operation succeeded
#   1 - Not found, already decided, validation error, or sandbox refusal
#   2 - Invalid command-line arguments
"""Human/CI-only CLI for managing pipeline checkpoint approvals."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Sequence

RUNNER_DIR = Path(__file__).resolve().parent
PACK_ROOT = RUNNER_DIR.parent
for _p in (str(PACK_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from runner.checkpoints import (  # noqa: E402
        APPROVED,
        CHECKPOINT_SPECS,
        REJECTED,
        decide_approval,
        list_approvals,
        load_pipeline_config,
        resolve_state_dir,
        show_approval,
    )
except ModuleNotFoundError:  # pragma: no cover
    from checkpoints import (  # type: ignore[no-redef] # noqa: E402
        APPROVED,
        CHECKPOINT_SPECS,
        REJECTED,
        decide_approval,
        list_approvals,
        load_pipeline_config,
        resolve_state_dir,
        show_approval,
    )


def _resolve_state_for_cli(args: argparse.Namespace) -> tuple[Path, Path]:
    """Resolve ``(workdir, state_dir)`` from CLI flags and project config."""
    workdir = Path(getattr(args, "workdir", ".")).resolve()
    raw_state = str(getattr(args, "state_dir", "") or "").strip()
    if raw_state:
        return workdir, resolve_state_dir(raw_state, project_root=workdir, agent_worktree=workdir, explicit=True)
    proj_cfg_raw = str(getattr(args, "project_config", "") or "").strip()
    proj_cfg = Path(proj_cfg_raw).resolve() if proj_cfg_raw else None
    cfg = load_pipeline_config(workdir, project_config_path=proj_cfg, agent_worktree=workdir)
    return workdir, Path(cfg.state_dir)


def _cmd_list(args: argparse.Namespace) -> int:
    """List approval records."""
    _workdir, state_dir = _resolve_state_for_cli(args)
    records = list_approvals(
        state_dir,
        status=args.status,
        checkpoint_id=args.checkpoint,
    )
    if args.json:
        clean = [{k: v for k, v in rec.items() if not k.startswith("_")} for rec in records]
        print(json.dumps(clean, indent=2))
        return 0
    if not records:
        print("(no approvals)")
        return 0
    print(f"{'ID':<34} {'CHECKPOINT':<18} {'STATUS':<10} {'SUBJECT'}")
    print(f"{'-'*34} {'-'*18} {'-'*10} {'-'*24}")
    for rec in records:
        print(
            f"{str(rec.get('id', '')):<34} "
            f"{str(rec.get('checkpoint', '')):<18} "
            f"{str(rec.get('status', '')):<10} "
            f"{str(rec.get('subject', ''))}"
        )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    """Show full JSON details of one approval record."""
    _workdir, state_dir = _resolve_state_for_cli(args)
    rec, path = show_approval(state_dir, args.id)
    if args.json:
        print(json.dumps(rec, indent=2))
        return 0
    print(f"File: {path}")
    print(json.dumps(rec, indent=2))
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    """Approve a pending checkpoint."""
    workdir, state_dir = _resolve_state_for_cli(args)
    decision = decide_approval(
        state_dir,
        args.id,
        decision_status=APPROVED,
        note=args.note,
        repo_root=workdir if (workdir / ".git").exists() else None,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "id": decision.approval_id,
                    "checkpoint": decision.checkpoint_id,
                    "status": decision.status,
                    "decided_by": decision.decided_by,
                    "decided_at": decision.decided_at,
                    "note": decision.note,
                    "record_path": str(decision.record_path),
                },
                indent=2,
            )
        )
    else:
        print(
            f"Approved {decision.checkpoint_id} checkpoint {decision.approval_id} "
            f"(by {decision.decided_by})"
        )
    return 0


def _cmd_reject(args: argparse.Namespace) -> int:
    """Reject a pending checkpoint with a required note."""
    if not str(args.note or "").strip():
        raise ValueError("--note is required when rejecting an approval")
    workdir, state_dir = _resolve_state_for_cli(args)
    decision = decide_approval(
        state_dir,
        args.id,
        decision_status=REJECTED,
        note=args.note,
        repo_root=workdir if (workdir / ".git").exists() else None,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "id": decision.approval_id,
                    "checkpoint": decision.checkpoint_id,
                    "status": decision.status,
                    "decided_by": decision.decided_by,
                    "decided_at": decision.decided_at,
                    "note": decision.note,
                    "record_path": str(decision.record_path),
                },
                indent=2,
            )
        )
    else:
        print(
            f"Rejected {decision.checkpoint_id} checkpoint {decision.approval_id} "
            f"(by {decision.decided_by}): {decision.note}"
        )
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for ``runner/devpack_approve.py``."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workdir", default=".", help="Project workdir (default: .)")
    common.add_argument("--state-dir", default="", help="Explicit checkpoint state directory")
    common.add_argument("--project-config", default="", help="Optional project.toml path")
    common.add_argument("--json", action="store_true", help="Emit JSON output")

    sub_common = argparse.ArgumentParser(add_help=False)
    sub_common.add_argument("--workdir", default=argparse.SUPPRESS)
    sub_common.add_argument("--state-dir", default=argparse.SUPPRESS)
    sub_common.add_argument("--project-config", default=argparse.SUPPRESS)
    sub_common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(
        prog="devpack_approve.py",
        parents=[common],
        description=(
            "Human/CI-only CLI to list, inspect, approve, and reject pipeline checkpoints. "
            "Never callable by agents inside a sandbox."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", parents=[sub_common], help="List pending or decided approvals")
    p_list.add_argument(
        "--status",
        choices=("pending", "decided", "all"),
        default="all",
        help="Filter by approval status (default: all)",
    )
    p_list.add_argument(
        "--checkpoint",
        choices=("", *sorted(CHECKPOINT_SPECS)),
        default="",
        help="Filter by checkpoint ID",
    )
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", parents=[sub_common], help="Show one approval record")
    p_show.add_argument("id", help="Approval ID")
    p_show.set_defaults(func=_cmd_show)

    p_approve = sub.add_parser("approve", parents=[sub_common], help="Approve a pending checkpoint")
    p_approve.add_argument("id", help="Pending approval ID")
    p_approve.add_argument("--note", default="", help="Optional approval note")
    p_approve.set_defaults(func=_cmd_approve)

    p_reject = sub.add_parser("reject", parents=[sub_common], help="Reject a pending checkpoint")
    p_reject.add_argument("id", help="Pending approval ID")
    p_reject.add_argument("--note", required=True, help="Required rejection reason note")
    p_reject.set_defaults(func=_cmd_reject)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for ``runner/devpack_approve.py``."""
    if os.environ.get("DEVPACK_AGENT_SANDBOX") == "1":
        print(
            "error: devpack_approve.py is a human/CI-only command and cannot run inside an agent sandbox",
            file=sys.stderr,
        )
        return 1
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except (ValueError, FileNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
