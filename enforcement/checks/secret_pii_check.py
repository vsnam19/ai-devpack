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
import sys

_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from runner.safe_git import GitSecurityError, run_git  # noqa: E402


def get_builtin_rules() -> list[tuple[str, re.Pattern[str]]]:
    """Return the shared redaction rules for staged and explicit-file scanning."""
    from runner.redaction import get_builtin_secret_rules
    return get_builtin_secret_rules()


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
    """Return paths of staged files (ACMR) in ``repo_root``.

    Paths are returned even if the working-tree copy was deleted, because the
    staged index blob is what gets scanned.
    """
    try:
        proc = run_git(
            ["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"],
            repo_root=repo_root,
            text=False,
            check=False,
        )
    except GitSecurityError as exc:
        return None, str(exc)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        return None, err or "git diff --cached failed"

    files: list[pathlib.Path] = []
    for raw in proc.stdout.split(b"\0"):
        rel = raw.decode("utf-8", errors="surrogateescape")
        if rel:
            files.append(repo_root / rel)
    return files, None


def read_staged_blob(repo_root: pathlib.Path, rel_path: str) -> bytes | None:
    """Return the staged index content of ``rel_path`` via ``git show :<path>``, or None on failure."""
    try:
        proc = run_git(
            ["show", f":{rel_path}"],
            repo_root=repo_root,
            text=False,
            check=False,
        )
    except GitSecurityError:
        return None
    return proc.stdout if proc.returncode == 0 else None


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
    staged: bool = False,
) -> list[str]:
    """Scan ``files`` against ``builtin_rules`` and ``custom_rules``.

    Returns a list of formatted violation lines ``<file>:<line>: [<rule_id>]``
    that never include the matched secret value. When ``staged`` is true the
    content is read from the git index instead of the working tree.
    """
    all_rules = builtin_rules + custom_rules
    findings: list[str] = []

    for fp in files:
        if staged:
            display_path = fp.relative_to(repo_root).as_posix()
        else:
            try:
                display_path = fp.resolve().relative_to(repo_root).as_posix()
            except ValueError:
                display_path = fp.name

        for rule_id, rx in custom_rules:
            if rx.search(display_path):
                findings.append(f"{display_path}:0: forbidden pattern matched in path ({rule_id})")

        if staged:
            blob = read_staged_blob(repo_root, display_path)
            if blob is None:
                findings.append(f"{display_path}:0: staged content unreadable")
                continue
            raw_bytes = blob
        else:
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
        help="Scan staged git files, reading the staged index blob (git show :<path>).",
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
        files_to_scan = [f for f in files_to_scan if f.resolve() not in exclude_files]
    else:
        files_to_scan, err = collect_target_files(repo_root, args.paths, exclude_files)
        if files_to_scan is None:
            print(f"SECRET_PII_ERROR: {err}", file=sys.stderr)
            return 1

    findings = scan_files(files_to_scan, repo_root, builtin_rules, custom_rules, staged=args.staged)
    if findings:
        for item in findings:
            print(item)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
