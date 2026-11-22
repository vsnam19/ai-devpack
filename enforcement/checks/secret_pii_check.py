#!/usr/bin/env python3
# Header: Secret and PII pre-commit / repository scanner.
# Scans staged git files (--staged) or explicit file/directory paths using built-in
# secret regular expressions (private key headers, AWS-style keys, generic password
# assignments) and optional external regex patterns (--patterns FILE outside the repository).
# Prints "file:line" (with rule name) WITHOUT echoing the matched secret.
# Does not block AI attribution.
#
# Exit codes:
#   0 - No secrets or forbidden PII patterns detected.
#   1 - One or more secret/PII hits found, or invalid configuration/git state.
"""Scan staged or specified files for secrets and PII without echoing matched secrets."""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
import sys


def get_builtin_rules() -> list[tuple[str, re.Pattern[str]]]:
    """Return built-in secret detection rules as ``(rule_id, compiled_regex)`` pairs."""
    begin_word = "-----BEGIN"
    priv_key_word = "PRIVATE" + " KEY-----"
    pwd_word = "pass" + "word"
    passwd_word = "pass" + "wd"
    aws_sec_word = "aws_secret" + "_access_key"

    return [
        (
            "private-key-header",
            re.compile(rf"{begin_word}\s+(?:[A-Z0-9 ]+\s+)?{priv_key_word}"),
        ),
        (
            "aws-access-key-id",
            re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
        ),
        (
            "aws-secret-key",
            re.compile(rf"(?i)\b{aws_sec_word}\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{{20,}}"),
        ),
        (
            "generic-password",
            re.compile(
                rf"(?i)\b(?:{pwd_word}|{passwd_word})\s*=\s*(?!['\"]?\s*(?:$|#))['\"]?[^\s'\";#]+"
            ),
        ),
    ]


def load_external_patterns(patterns_file: pathlib.Path) -> tuple[list[tuple[str, re.Pattern[str]]] | None, str | None]:
    """Load custom regular expressions (one per line) from ``patterns_file``."""
    if not patterns_file.is_file():
        return None, f"patterns file not found: {patterns_file.name}"

    try:
        lines = patterns_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"unable to read patterns file: {exc}"

    rules: list[tuple[str, re.Pattern[str]]] = []
    for idx, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            compiled = re.compile(stripped, re.IGNORECASE)
        except re.error as exc:
            return None, f"invalid regex on line {idx} of patterns file: {exc}"
        rules.append((f"custom-pattern-{idx}", compiled))

    return rules, None


def get_staged_files(repo_root: pathlib.Path) -> tuple[list[pathlib.Path] | None, str | None]:
    """Return list of staged files (ACMR) in ``repo_root``."""
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None, proc.stderr.strip() or "git diff --cached failed"

    files: list[pathlib.Path] = []
    for line in proc.stdout.splitlines():
        rel = line.strip()
        if not rel:
            continue
        candidate = (repo_root / rel).resolve()
        if candidate.is_file():
            files.append(candidate)
    return files, None


def collect_target_files(
    repo_root: pathlib.Path,
    raw_paths: list[str],
    exclude_files: set[pathlib.Path],
) -> tuple[list[pathlib.Path] | None, str | None]:
    """Resolve target files from explicit ``raw_paths`` or by walking ``repo_root``."""
    targets = [pathlib.Path(p) for p in raw_paths] if raw_paths else [repo_root]
    collected: list[pathlib.Path] = []
    seen: set[pathlib.Path] = set()

    for target in targets:
        if not target.exists() and not target.is_absolute():
            candidate = repo_root / target
            if candidate.exists():
                target = candidate

        if not target.exists():
            return None, f"target path does not exist: {target}"

        if target.is_file():
            resolved = target.resolve()
            if resolved not in exclude_files and resolved not in seen:
                seen.add(resolved)
                collected.append(resolved)
        elif target.is_dir():
            for root, dirs, files in os.walk(target):
                dirs[:] = sorted(d for d in dirs if d not in {".git", ".venv", "venv", "__pycache__"})
                for fname in sorted(files):
                    if fname == ".git":
                        continue
                    fp = (pathlib.Path(root) / fname).resolve()
                    if fp in exclude_files or fp in seen:
                        continue
                    if fp.is_file():
                        seen.add(fp)
                        collected.append(fp)

    return collected, None


def scan_files(
    files: list[pathlib.Path],
    repo_root: pathlib.Path,
    builtin_rules: list[tuple[str, re.Pattern[str]]],
    custom_rules: list[tuple[str, re.Pattern[str]]],
) -> list[str]:
    """Scan ``files`` against ``builtin_rules`` and ``custom_rules``.

    Returns a list of formatted violation lines ``<file>:<line>: [<rule_id>]``
    that never include the matched secret value.
    """
    all_rules = builtin_rules + custom_rules
    findings: list[str] = []

    for fp in files:
        try:
            display_path = fp.resolve().relative_to(repo_root).as_posix()
        except ValueError:
            display_path = fp.name

        for rule_id, rx in custom_rules:
            if rx.search(display_path):
                findings.append(f"{display_path}:0: forbidden pattern matched in path ({rule_id})")

        try:
            raw_bytes = fp.read_bytes()
        except OSError:
            continue

        if b"\x00" in raw_bytes[:8192]:
            continue

        text = raw_bytes.decode("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for rule_id, rx in all_rules:
                if rx.search(line):
                    findings.append(f"{display_path}:{lineno}: forbidden secret/PII pattern ({rule_id})")
                    break

    return findings


def build_arg_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser for secret_pii_check."""
    parser = argparse.ArgumentParser(
        prog="secret_pii_check.py",
        description=(
            "Scan staged files (--staged) or explicit paths for secrets and custom PII "
            "patterns. Reports file:line without echoing the secret."
        ),
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to the repository root (default: current directory).",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Scan staged git files (git diff --cached --name-only --diff-filter=ACMR).",
    )
    parser.add_argument(
        "--patterns",
        default=os.environ.get("DEVPACK_SECRET_PATTERNS"),
        metavar="FILE",
        help="Optional path to an external file of forbidden regex patterns (one per line).",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files or directories to scan when --staged is not set.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the secret and PII scanner and return 0 if clean or 1 on findings/error."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    repo_root = pathlib.Path(args.repo).resolve()
    if not repo_root.is_dir():
        print("SECRET_PII_ERROR: repository root directory does not exist", file=sys.stderr)
        return 1

    builtin_rules = get_builtin_rules()
    custom_rules: list[tuple[str, re.Pattern[str]]] = []
    exclude_files: set[pathlib.Path] = set()

    if args.patterns:
        patterns_path = pathlib.Path(args.patterns).resolve()
        exclude_files.add(patterns_path)
        loaded, err = load_external_patterns(patterns_path)
        if loaded is None:
            print(f"SECRET_PII_ERROR: {err}", file=sys.stderr)
            return 1
        custom_rules = loaded

    if args.staged:
        files_to_scan, err = get_staged_files(repo_root)
        if files_to_scan is None:
            print(f"SECRET_PII_ERROR: {err}", file=sys.stderr)
            return 1
        files_to_scan = [f for f in files_to_scan if f not in exclude_files]
    else:
        files_to_scan, err = collect_target_files(repo_root, args.paths, exclude_files)
        if files_to_scan is None:
            print(f"SECRET_PII_ERROR: {err}", file=sys.stderr)
            return 1

    findings = scan_files(files_to_scan, repo_root, builtin_rules, custom_rules)
    if findings:
        for item in findings:
            print(item)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
