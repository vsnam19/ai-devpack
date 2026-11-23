#!/usr/bin/env python3
# Header: Protected test lock and assertion-weakening check.
# Fails if protected files lose existing non-comment, non-blank code lines,
# add/remove/modify preprocessor directive lines, are deleted/renamed, or lose
# assertions on comment-stripped or compiler-preprocessed text, unless authorized
# by a valid external human deviation registry entry (ignored when --no-deviation
# is given for agent commits).
# Exit codes: 0 intact or authorized; 1 violation or git/config error; 2 bad CLI.
"""Enforce test lock on protected test files across changes."""

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import subprocess
import sys

_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from enforcement.checks.deviations import verify_deviation  # noqa: E402
from enforcement.checks.scope_check import glob_to_regex, matches_any_glob  # noqa: E402
from runner.safe_git import GitSecurityError, run_git  # noqa: E402

DEFAULT_ASSERT_REGEX = r"\b(ASSERT|EXPECT|CHECK|REQUIRE)\w*\(|\bassert\b"
_PREPROC_ASSERT_EXTRA_RE = re.compile(r"\bcan_fixture_test::recordCheck\w*\(|\brecordCheck\w*\(")
DEVIATION_PATTERN = re.compile(r"\bDeviation:\s*([A-Za-z0-9_.-]+)")
_PREPROCESSOR_DIRECTIVE_RE = re.compile(
    r"^\s*#\s*(?:if|ifdef|ifndef|elif|else|endif|define|undef|pragma|line|include)\b(.*)$"
)
_INCLUDE_LINE_RE = re.compile(r"^\s*#\s*include\b")


def get_head_deviation(repo_root: pathlib.Path) -> str | None:
    """Return the deviation ID from HEAD's commit message if ``Deviation: <ID>`` is present."""
    try:
        proc = run_git(
            ["log", "-1", "--format=%B", "HEAD"],
            repo_root=repo_root,
            check=False,
        )
    except GitSecurityError:
        return None
    if proc.returncode != 0:
        return None
    match = DEVIATION_PATTERN.search(proc.stdout)
    return match.group(1) if match else None


def list_base_files(repo_root: pathlib.Path, base_ref: str) -> tuple[list[str] | None, str | None]:
    """List all tracked file paths in ``repo_root`` at git reference ``base_ref``."""
    try:
        verify = run_git(
            ["rev-parse", "--verify", f"{base_ref}^{{commit}}"],
            repo_root=repo_root,
            check=False,
        )
        if verify.returncode != 0:
            return None, verify.stderr.strip() or f"invalid base ref '{base_ref}'"

        proc = run_git(
            ["ls-tree", "-r", "--name-only", base_ref],
            repo_root=repo_root,
            check=False,
        )
        if proc.returncode != 0:
            return None, proc.stderr.strip() or f"failed to list tree at '{base_ref}'"
    except GitSecurityError as exc:
        return None, str(exc)

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
        ["diff", "--name-status", "-M", base_ref],
        ["diff", "--name-status", "-M", f"{base_ref}..HEAD"],
    ]
    for cmd in diff_cmds:
        proc = run_git(cmd, repo_root=repo_root, check=False)
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
    proc = run_git(
        ["show", f"{ref}:{rel_path}"],
        repo_root=repo_root,
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
    """Check that protected test file is byte-identical except trailing whitespace on lines."""
    base_lines = [line.rstrip() for line in base.splitlines()]
    current_lines = [line.rstrip() for line in current.splitlines()]
    return base_lines == current_lines


def count_assertions(text: str, assert_rx: re.Pattern[str]) -> int:
    """Return the number of regex matches of ``assert_rx`` in ``text``."""
    return len(list(assert_rx.finditer(text)))


def _join_line_continuations(text: str) -> list[str]:
    """Return logical source lines with backslash-newline continuations joined."""
    logical: list[str] = []
    buf = ""
    for line in text.splitlines():
        if line.endswith("\\"):
            buf += line[:-1] + " "
        else:
            logical.append(buf + line)
            buf = ""
    if buf:
        logical.append(buf)
    return logical


def extract_preprocessor_directives(comment_stripped_text: str) -> list[str]:
    """Return normalized preprocessor directive lines from comment-stripped source."""
    directives: list[str] = []
    for line in _join_line_continuations(comment_stripped_text):
        stripped = line.strip()
        if _PREPROCESSOR_DIRECTIVE_RE.match(stripped):
            normalized = re.sub(r"^\s*#\s*", "#", stripped)
            normalized = re.sub(r"\s+", " ", normalized)
            directives.append(normalized)
    return directives


def _find_preprocessor_bin() -> str | None:
    """Return path to ``g++`` or ``cpp`` when installed, else None."""
    return shutil.which("g++") or shutil.which("cpp")


def _run_preprocessor(
    compiler_bin: str,
    source_text: str,
    *,
    include_dirs: list[pathlib.Path] | None = None,
    strip_includes: bool = False,
) -> str | None:
    """Run ``g++ -E -P`` or ``cpp -P`` on ``source_text`` and return preprocessed output or None."""
    text_in = source_text
    if strip_includes:
        text_in = "\n".join(
            "" if _INCLUDE_LINE_RE.match(line) else line
            for line in source_text.splitlines()
        )
    cmd = [compiler_bin]
    if pathlib.Path(compiler_bin).name.startswith("cpp"):
        cmd.append("-P")
    else:
        cmd.extend(["-E", "-P", "-x", "c++"])
    for inc in include_dirs or ():
        if inc.is_dir():
            cmd.extend(["-I", str(inc)])
    cmd.append("-")
    try:
        proc = subprocess.run(
            cmd,
            input=text_in,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _count_preprocessed_assertions(
    preprocessed_text: str,
    assert_rx: re.Pattern[str],
    *,
    count_expanded_fixture_checks: bool = False,
) -> int:
    """Count assertions in preprocessed output (macros or expanded fixture checks)."""
    no_strings = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', preprocessed_text)
    count = count_assertions(no_strings, assert_rx)
    if count_expanded_fixture_checks:
        count += len(list(_PREPROC_ASSERT_EXTRA_RE.finditer(no_strings)))
    return count


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
            "Ignore 'Deviation: <ID>' trailers in git commit messages (required for agent-produced "
            "commits)."
        ),
    )
    parser.add_argument(
        "--deviation",
        default="",
        help="Explicit human deviation ID verified against the external DEVPACK_DEVIATIONS registry.",
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
    violated_files: list[str] = []
    preproc_bin = _find_preprocessor_bin()

    for rel_path in protected_files:
        if rel_path in renamed_map:
            violations.append(
                f"TEST_LOCK_VIOLATION: protected test file renamed: {rel_path} -> {renamed_map[rel_path]}"
            )
            violated_files.append(rel_path)
            continue

        worktree_file = repo_root / rel_path
        if rel_path in deleted_set or not worktree_file.is_file():
            violations.append(f"TEST_LOCK_VIOLATION: protected test file deleted: {rel_path}")
            violated_files.append(rel_path)
            continue

        base_raw = read_file_at_ref(repo_root, args.base, rel_path)
        if base_raw is None:
            violations.append(f"TEST_LOCK_VIOLATION: unable to read base version of {rel_path}")
            violated_files.append(rel_path)
            continue

        try:
            current_raw = worktree_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            violations.append(f"TEST_LOCK_VIOLATION: unable to read {rel_path}: {exc}")
            violated_files.append(rel_path)
            continue

        base_content = strip_comments(base_raw)
        current_content = strip_comments(current_raw)
        file_violated = False
        if extract_preprocessor_directives(base_content) != extract_preprocessor_directives(current_content):
            violations.append(
                f"TEST_LOCK_VIOLATION: preprocessor directives modified in {rel_path}"
            )
            file_violated = True
        if not preserves_lines(base_raw, current_raw):
            violations.append(
                f"TEST_LOCK_VIOLATION: protected test file modified (must be byte-identical except trailing whitespace) in {rel_path}"
            )
            file_violated = True
        base_count = count_assertions(base_content, assert_rx)
        current_count = count_assertions(current_content, assert_rx)
        if current_count < base_count:
            violations.append(
                f"TEST_LOCK_VIOLATION: assertion count decreased in {rel_path} "
                f"({base_count} -> {current_count})"
            )
            file_violated = True

        if preproc_bin is not None:
            inc_dirs = [worktree_file.parent, repo_root / "include", repo_root / "tests"]
            base_pp_noinc = _run_preprocessor(preproc_bin, base_content, strip_includes=True)
            curr_pp_noinc = _run_preprocessor(preproc_bin, current_content, strip_includes=True)
            if base_pp_noinc is not None and curr_pp_noinc is not None:
                pp_base = _count_preprocessed_assertions(base_pp_noinc, assert_rx)
                pp_curr = _count_preprocessed_assertions(curr_pp_noinc, assert_rx)
                if pp_curr < pp_base:
                    violations.append(
                        f"TEST_LOCK_VIOLATION: preprocessed assertion count decreased in {rel_path} "
                        f"({pp_base} -> {pp_curr})"
                    )
                    file_violated = True
            base_pp_full = _run_preprocessor(preproc_bin, base_content, include_dirs=inc_dirs)
            curr_pp_full = _run_preprocessor(preproc_bin, current_content, include_dirs=inc_dirs)
            if base_pp_full is not None and curr_pp_full is not None:
                full_base = _count_preprocessed_assertions(
                    base_pp_full, assert_rx, count_expanded_fixture_checks=True
                )
                full_curr = _count_preprocessed_assertions(
                    curr_pp_full, assert_rx, count_expanded_fixture_checks=True
                )
                if full_curr < full_base:
                    violations.append(
                        f"TEST_LOCK_VIOLATION: full-preprocessed assertion count decreased in {rel_path} "
                        f"({full_base} -> {full_curr})"
                    )
                    file_violated = True

        if file_violated:
            violated_files.append(rel_path)

    if violations:
        explicit_dev = (args.deviation or "").strip()
        candidate_id = explicit_dev or (None if args.no_deviation else get_head_deviation(repo_root))
        if candidate_id:
            entry, reason = verify_deviation(
                candidate_id,
                files=violated_files,
                rule="test_lock",
                worktree_roots=(repo_root,),
            )
            if entry is not None:
                print(
                    f"TEST_LOCK_OK: {len(violations)} change(s) authorized by Deviation: "
                    f"{entry.deviation_id} (approved_by={entry.approved_by})"
                )
                return 0
            print(f"TEST_LOCK_DEVIATION_REJECTED: {reason}", file=sys.stderr)
        for msg in violations:
            print(msg)
        return 1

    print(f"TEST_LOCK_OK: {len(protected_files)} protected test file(s) verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
