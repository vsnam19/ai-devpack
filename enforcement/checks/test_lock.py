#!/usr/bin/env python3
# Header: Protected test lock and assertion-weakening check.
# Fails if protected files lose existing non-comment, non-blank code lines,
# are deleted/renamed, or lose assertions, unless HEAD has "Deviation: <ID>"
# (ignored when --no-deviation is given).
# Insertions and C/C++ comment edits are allowed.
# Exit codes: 0 intact or authorized; 1 violation or git/config error; 2 bad CLI.
"""Enforce test lock on protected test files across changes."""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from enforcement.checks.scope_check import glob_to_regex, matches_any_glob  # noqa: E402

DEFAULT_ASSERT_REGEX = r"\b(ASSERT|EXPECT|CHECK|REQUIRE)\w*\(|\bassert\b"
DEVIATION_PATTERN = re.compile(r"\bDeviation:\s*([A-Za-z0-9_.-]+)")


def get_head_deviation(repo_root: pathlib.Path) -> str | None:
    """Return the deviation ID from HEAD's commit message if ``Deviation: <ID>`` is present."""
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "log", "-1", "--format=%B", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    match = DEVIATION_PATTERN.search(proc.stdout)
    return match.group(1) if match else None


def list_base_files(repo_root: pathlib.Path, base_ref: str) -> tuple[list[str] | None, str | None]:
    """List all tracked file paths in ``repo_root`` at git reference ``base_ref``."""
    verify = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", f"{base_ref}^{{commit}}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if verify.returncode != 0:
        return None, verify.stderr.strip() or f"invalid base ref '{base_ref}'"

    proc = subprocess.run(
        ["git", "-C", str(repo_root), "ls-tree", "-r", "--name-only", base_ref],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None, proc.stderr.strip() or f"failed to list tree at '{base_ref}'"

    files = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return sorted(files), None


def detect_renames_and_deletions(
    repo_root: pathlib.Path,
    base_ref: str,
) -> tuple[dict[str, str], set[str]]:
    """Return ``(renamed_map, deleted_set)`` comparing ``base_ref`` to working tree and HEAD."""
    renamed: dict[str, str] = {}
    deleted: set[str] = set()
    diff_cmds = [
        ["git", "-C", str(repo_root), "diff", "--name-status", "-M", base_ref],
        ["git", "-C", str(repo_root), "diff", "--name-status", "-M", f"{base_ref}..HEAD"],
    ]
    for cmd in diff_cmds:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            continue
        for raw_line in proc.stdout.splitlines():
            parts = raw_line.strip().split("\t")
            if not parts or not parts[0]:
                continue
            status = parts[0]
            if status.startswith("R") and len(parts) >= 3:
                old_path, new_path = parts[1], parts[2]
                if old_path != new_path:
                    renamed[old_path] = new_path
            elif status.startswith("D") and len(parts) >= 2:
                deleted.add(parts[1])
    return renamed, deleted


def read_file_at_ref(repo_root: pathlib.Path, ref: str, rel_path: str) -> str | None:
    """Return file content at ``ref:rel_path``, or None on error."""
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{ref}:{rel_path}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def strip_comments(text: str) -> str:
    """Remove C/C++ comments, preserving newlines and quoted/raw string contents."""
    token_rx = re.compile(
        r'R"(?P<delimiter>[^ ()\\\t\r\n]{0,16})\(.*?\)(?P=delimiter)"'
        r'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
        r'|//(?:\\\n|[^\n])*|/\*.*?(?:\*/|\Z)',
        re.DOTALL,
    )

    def replace(match: re.Match[str]) -> str:
        """Replace comment tokens with whitespace without joining adjacent code."""
        token = match.group()
        if token.startswith(("//", "/*")):
            return " " + "\n" * token.count("\n")
        return token

    return token_rx.sub(replace, text)


def code_lines(text: str) -> list[str]:
    """Return significant lines, ignoring whitespace at comment boundaries."""
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def preserves_lines(base: str, current: str) -> bool:
    """Check that existing code lines remain in order, allowing insertions anywhere."""
    remaining = iter(code_lines(current))
    return all(any(candidate == line for candidate in remaining) for line in code_lines(base))


def count_assertions(text: str, assert_rx: re.Pattern[str]) -> int:
    """Return the number of regex matches of ``assert_rx`` in ``text``."""
    return len(list(assert_rx.finditer(text)))


def build_arg_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser for test_lock."""
    parser = argparse.ArgumentParser(
        prog="test_lock.py",
        description=(
            "Fail if protected test code lines are changed/removed, files deleted/renamed, or assertion count "
            "decreases compared to --base, unless HEAD commit message contains 'Deviation: <ID>'."
        ),
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to the git repository root (default: current directory).",
    )
    parser.add_argument(
        "--base",
        required=True,
        metavar="REF",
        help="Base git reference to compare against (e.g. main, HEAD~1).",
    )
    parser.add_argument(
        "--protected",
        required=True,
        action="extend",
        nargs="+",
        metavar="GLOB",
        help="Glob pattern(s) identifying protected test files.",
    )
    parser.add_argument(
        "--assert-regex",
        "--assertion-regex",
        "--regex",
        dest="assert_regex",
        default=DEFAULT_ASSERT_REGEX,
        help=f"Regular expression for assertion tokens (default: {DEFAULT_ASSERT_REGEX!r}).",
    )
    parser.add_argument(
        "--no-deviation",
        action="store_true",
        default=False,
        help=(
            "Ignore 'Deviation: <ID>' trailers on HEAD. Use when the checked code may commit its own "
            "exemption (eval runs); default off keeps the product gate behaviour."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the test lock check and return 0 on pass or 1 on violation/error."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    repo_root = pathlib.Path(args.repo).resolve()
    if not repo_root.is_dir():
        print(f"TEST_LOCK_ERROR: repository path does not exist: {args.repo}", file=sys.stderr)
        return 1

    protected_globs = [p.strip() for p in args.protected if p and p.strip()]
    if not protected_globs:
        print("TEST_LOCK_ERROR: at least one non-empty --protected glob is required", file=sys.stderr)
        return 1

    try:
        assert_rx = re.compile(args.assert_regex)
    except re.error as exc:
        print(f"TEST_LOCK_ERROR: invalid assertion regex: {exc}", file=sys.stderr)
        return 1

    base_files, err = list_base_files(repo_root, args.base)
    if base_files is None:
        print(f"TEST_LOCK_ERROR: {err}", file=sys.stderr)
        return 1

    compiled_globs = [glob_to_regex(pat) for pat in protected_globs]
    protected_files = [f for f in base_files if matches_any_glob(f, compiled_globs)]

    renamed_map, deleted_set = detect_renames_and_deletions(repo_root, args.base)
    violations: list[str] = []

    for rel_path in protected_files:
        if rel_path in renamed_map:
            violations.append(
                f"TEST_LOCK_VIOLATION: protected test file renamed: {rel_path} -> {renamed_map[rel_path]}"
            )
            continue

        worktree_file = repo_root / rel_path
        if rel_path in deleted_set or not worktree_file.is_file():
            violations.append(f"TEST_LOCK_VIOLATION: protected test file deleted: {rel_path}")
            continue

        base_content = read_file_at_ref(repo_root, args.base, rel_path)
        if base_content is None:
            violations.append(f"TEST_LOCK_VIOLATION: unable to read base version of {rel_path}")
            continue

        try:
            current_content = worktree_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            violations.append(f"TEST_LOCK_VIOLATION: unable to read {rel_path}: {exc}")
            continue

        base_content = strip_comments(base_content)
        current_content = strip_comments(current_content)
        if not preserves_lines(base_content, current_content):
            violations.append(
                f"TEST_LOCK_VIOLATION: existing non-comment test lines modified or deleted in {rel_path}"
            )
        base_count = count_assertions(base_content, assert_rx)
        current_count = count_assertions(current_content, assert_rx)
        if current_count < base_count:
            violations.append(
                f"TEST_LOCK_VIOLATION: assertion count decreased in {rel_path} "
                f"({base_count} -> {current_count})"
            )

    if violations:
        deviation_id = None if args.no_deviation else get_head_deviation(repo_root)
        if deviation_id:
            print(f"TEST_LOCK_OK: {len(violations)} change(s) authorized by Deviation: {deviation_id}")
            return 0
        for msg in violations:
            print(msg)
        return 1

    print(f"TEST_LOCK_OK: {len(protected_files)} protected test file(s) verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
