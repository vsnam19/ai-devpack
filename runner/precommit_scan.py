#!/usr/bin/env python3
# Header: Pre-commit secret scanner using the high-confidence rule set.
# Scans staged files for distinctive credential formats (PEM private keys, AKIA keys,
# sk-ant-/ghp_/gho_/glpat- style tokens, JWTs, Google API keys) and high-entropy quoted
# literals assigned to credential-like names. Bare identifiers such as
# ``token = secretIndex + 1U;`` are not reported. The vendored pack directory
# (``--vendor-dir``, default ``devpack``) is excluded from the scan.
# Log redaction keeps the broader rules in runner/redaction.py get_builtin_secret_rules().
#
# Usage:
#   python3 runner/precommit_scan.py [--repo DIR] [--staged] [--vendor-dir NAME] [--patterns FILE] [PATH ...]
#
# Exit codes:
#   0 - No high-confidence secret found in the scanned files.
#   1 - One or more findings, or invalid configuration.
"""Scan staged files for high-confidence secrets without echoing matched values."""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from enforcement.checks.secret_pii_check import (  # noqa: E402
    get_staged_files,
    load_external_patterns,
    scan_files,
)
from runner.redaction import get_precommit_secret_rules  # noqa: E402

DEFAULT_VENDOR_DIR = "devpack"


def _vendor_dir_default() -> str:
    """Return the vendored pack directory name from ``DEVPACK_VENDOR_DIR`` or the default."""
    return os.environ.get("DEVPACK_VENDOR_DIR", "").strip() or DEFAULT_VENDOR_DIR


def is_vendored(path: pathlib.Path, repo_root: pathlib.Path, vendor_dir: str) -> bool:
    """Return True when ``path`` lies under ``repo_root/vendor_dir``."""
    vendor = vendor_dir.strip().strip("/")
    if not vendor:
        return False
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            return False
    return bool(rel.parts) and rel.parts[: len(pathlib.PurePosixPath(vendor).parts)] == pathlib.PurePosixPath(vendor).parts


def scan_precommit(
    files: list[pathlib.Path],
    repo_root: pathlib.Path,
    *,
    vendor_dir: str,
    staged: bool,
    custom_rules: list | None = None,
) -> list[str]:
    """Return finding lines for ``files`` after excluding the vendored pack directory."""
    kept = [f for f in files if not is_vendored(f, repo_root, vendor_dir)]
    return scan_files(kept, repo_root, get_precommit_secret_rules(), custom_rules or [], staged=staged)


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the pre-commit scanner."""
    parser = argparse.ArgumentParser(prog="precommit_scan.py", description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".", help="Repository root (default: current directory).")
    parser.add_argument("--staged", action="store_true", help="Scan staged index blobs.")
    parser.add_argument(
        "--vendor-dir",
        default=_vendor_dir_default(),
        help="Vendored pack directory excluded from the scan (default: devpack or DEVPACK_VENDOR_DIR).",
    )
    parser.add_argument("--patterns", metavar="FILE", help="Optional extra regex patterns, one per line.")
    parser.add_argument("paths", nargs="*", help="Explicit files to scan when --staged is not set.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the pre-commit scan and return 0 when clean, 1 on findings or error."""
    args = build_arg_parser().parse_args(argv)
    repo_root = pathlib.Path(args.repo).resolve()
    if not repo_root.is_dir():
        print("SECRET_SCAN_ERROR: repository root directory does not exist", file=sys.stderr)
        return 1

    custom_rules: list = []
    if args.patterns:
        loaded, err = load_external_patterns(pathlib.Path(args.patterns).resolve())
        if loaded is None:
            print(f"SECRET_SCAN_ERROR: {err}", file=sys.stderr)
            return 1
        custom_rules = loaded

    if args.staged:
        files, err = get_staged_files(repo_root)
        if files is None:
            print(f"SECRET_SCAN_ERROR: {err}", file=sys.stderr)
            return 1
    else:
        files = [pathlib.Path(p).resolve() for p in args.paths]
        missing = [str(p) for p in files if not p.exists()]
        if missing:
            print(f"SECRET_SCAN_ERROR: path does not exist: {missing[0]}", file=sys.stderr)
            return 1

    findings = scan_precommit(
        files, repo_root, vendor_dir=args.vendor_dir, staged=args.staged, custom_rules=custom_rules
    )
    for line in findings:
        print(line)
    if findings:
        print(f"SECRET_SCAN_FAIL: {len(findings)} high-confidence finding(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
