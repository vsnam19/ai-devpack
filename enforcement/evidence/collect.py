#!/usr/bin/env python3
# Header: Deterministic evidence bundle collector.
# Combines the gate JSON receipt, the last-line VERDICT from a review report,
# git diff statistics relative to --base REF, and the git provenance note from
# "git notes --ref ai-provenance show HEAD" (if present) into
# evidence/<change-id>.json.
#
# Exit codes:
#   0 - Evidence JSON bundle written to evidence/<change-id>.json.
#   1 - Missing/invalid receipt, missing/invalid review VERDICT, invalid git ref, or I/O error.
"""Collect gate receipt, review verdict, diff stat, and provenance note into an evidence bundle."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import pathlib
import re
import subprocess
import sys
from typing import Any

CHANGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
VERDICT_LINE_PATTERN = re.compile(r"^VERDICT:\s*([A-Z_]+)\s*$")


def get_utc_timestamp() -> str:
    """Return the current UTC time formatted as an ISO-8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_review_verdict(review_path: pathlib.Path) -> tuple[str | None, str | None]:
    """Extract the verdict token from the last non-empty line of ``review_path``."""
    if not review_path.is_file():
        return None, f"review file not found: {review_path.name}"

    try:
        lines = review_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"failed to read review file: {exc}"

    non_empty = [line.strip() for line in lines if line.strip()]
    if not non_empty:
        return None, "review file is empty"

    last_line = non_empty[-1]
    match = VERDICT_LINE_PATTERN.match(last_line)
    if not match:
        return None, f"last line of review file does not match 'VERDICT: <STATUS>': '{last_line}'"

    return match.group(1), None


def load_gate_receipt(receipt_path: pathlib.Path) -> tuple[dict[str, Any] | None, str | None]:
    """Load and validate the JSON gate receipt from ``receipt_path``."""
    if not receipt_path.is_file():
        return None, f"receipt file not found: {receipt_path.name}"

    try:
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"invalid receipt JSON: {exc}"

    if not isinstance(data, dict):
        return None, "receipt JSON root must be an object"

    return data, None


def get_git_diff_stat(repo_root: pathlib.Path, base_ref: str) -> tuple[str | None, str | None, str | None]:
    """Return ``(head_sha, diff_stat_text, error)`` for ``repo_root`` against ``base_ref``."""
    verify_base = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", f"{base_ref}^{{commit}}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if verify_base.returncode != 0:
        return None, None, verify_base.stderr.strip() or f"invalid base ref '{base_ref}'"

    head_proc = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if head_proc.returncode != 0:
        return None, None, head_proc.stderr.strip() or "unable to resolve git HEAD"
    head_sha = head_proc.stdout.strip()

    stat_proc = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--stat", base_ref],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if stat_proc.returncode != 0:
        return None, None, stat_proc.stderr.strip() or "git diff --stat failed"

    return head_sha, stat_proc.stdout.strip(), None


def get_provenance_note(repo_root: pathlib.Path) -> str | None:
    """Return the note on ``HEAD`` from ``refs/notes/ai-provenance`` if present, else None."""
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "notes", "--ref", "ai-provenance", "show", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    note = proc.stdout.strip()
    return note if note else None


def build_arg_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser for evidence collection."""
    parser = argparse.ArgumentParser(
        prog="collect.py",
        description=(
            "Bundle gate receipt, review last-line VERDICT, git diff stat, and "
            "ai-provenance git note into evidence/<change-id>.json."
        ),
    )
    parser.add_argument(
        "--change-id",
        required=True,
        metavar="ID",
        help="Unique change identifier used for the output filename evidence/<change-id>.json.",
    )
    parser.add_argument(
        "--receipt",
        required=True,
        metavar="FILE",
        help="Path to the gate receipt JSON file.",
    )
    parser.add_argument(
        "--review",
        required=True,
        metavar="FILE",
        help="Path to the review output file whose last line contains 'VERDICT: ...'.",
    )
    parser.add_argument(
        "--base",
        required=True,
        metavar="REF",
        help="Base git reference for computing diff statistics.",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to the git repository root (default: current directory).",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        metavar="DIR",
        help="Output directory for evidence JSON (default: <repo>/evidence).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Collect evidence bundle and write ``evidence/<change-id>.json``."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    change_id = args.change_id.strip()
    if not CHANGE_ID_PATTERN.match(change_id) or ".." in change_id:
        print(f"EVIDENCE_ERROR: invalid --change-id '{args.change_id}'", file=sys.stderr)
        return 1

    repo_root = pathlib.Path(args.repo).resolve()
    if not repo_root.is_dir():
        print("EVIDENCE_ERROR: repository root directory does not exist", file=sys.stderr)
        return 1

    receipt_data, err = load_gate_receipt(pathlib.Path(args.receipt))
    if receipt_data is None:
        print(f"EVIDENCE_ERROR: {err}", file=sys.stderr)
        return 1

    verdict, err = extract_review_verdict(pathlib.Path(args.review))
    if verdict is None:
        print(f"EVIDENCE_ERROR: {err}", file=sys.stderr)
        return 1

    head_sha, diff_stat, err = get_git_diff_stat(repo_root, args.base)
    if head_sha is None or diff_stat is None:
        print(f"EVIDENCE_ERROR: {err}", file=sys.stderr)
        return 1

    provenance_note = get_provenance_note(repo_root)

    out_dir = pathlib.Path(args.out_dir) if args.out_dir else (repo_root / "evidence")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{change_id}.json"

    bundle: dict[str, Any] = {
        "change_id": change_id,
        "timestamp_utc": get_utc_timestamp(),
        "git_head": head_sha,
        "base_ref": args.base,
        "review_verdict": verdict,
        "verdict": verdict,
        "gate_receipt": receipt_data,
        "receipt": receipt_data,
        "diff_stat": diff_stat,
        "provenance_note": provenance_note,
        "provenance": provenance_note,
    }
    out_file.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    print(f"EVIDENCE_OK: evidence/{change_id}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
