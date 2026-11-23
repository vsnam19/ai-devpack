#!/usr/bin/env python3
# Header: Task file-scope enforcement check.
# Verifies that all files changed relative to a base git reference match at least
# one allowed glob pattern.
#
# Exit codes:
#   0 - All changed files are within the allowed glob patterns (SCOPE_OK).
#   1 - One or more changed files fall outside the allowed globs, or git/config error.
"""Check that changed files in a git repository stay within allowed glob patterns."""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from runner.workspace_scope import resolve_workspace_scope  # noqa: E402


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a POSIX path glob pattern (supporting ``*``, ``?``, and ``**``) to a regex."""
    norm = pattern.strip().replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    norm = norm.lstrip("/")

    i = 0
    n = len(norm)
    out: list[str] = ["^"]
    while i < n:
        if norm[i : i + 3] == "**/":
            out.append("(?:.+/)?")
            i += 3
        elif norm[i : i + 3] == "/**":
            out.append("(?:/.*)?")
            i += 3
        elif norm[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif norm[i] == "*":
            out.append("[^/]*")
            i += 1
        elif norm[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(norm[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def matches_any_glob(rel_posix_path: str, compiled_globs: list[re.Pattern[str]]) -> bool:
    """Return True if ``rel_posix_path`` matches at least one compiled glob pattern."""
    norm = rel_posix_path.strip().replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    norm = norm.lstrip("/")
    return any(rx.match(norm) is not None for rx in compiled_globs)


def get_changed_files(repo_root: pathlib.Path, base_ref: str) -> tuple[list[str] | None, str | None]:
    """Return sorted list of files changed since ``base_ref`` (committed, staged, working tree, or untracked)."""
    verify = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", f"{base_ref}^{{commit}}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if verify.returncode != 0:
        err = verify.stderr.strip() or f"invalid base ref '{base_ref}'"
        return None, err

    changed: set[str] = set()
    diff_cmds = [
        ["git", "-C", str(repo_root), "diff", "--name-only", base_ref],
        ["git", "-C", str(repo_root), "diff", "--name-only", f"{base_ref}..HEAD"],
        ["git", "-C", str(repo_root), "diff", "--cached", "--name-only"],
        ["git", "-C", str(repo_root), "ls-files", "--others", "--exclude-standard"],
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
            return None, proc.stderr.strip() or "git diff failed"
        for line in proc.stdout.splitlines():
            cleaned = line.strip()
            if cleaned:
                changed.add(cleaned)

    return sorted(changed), None


def build_arg_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser for scope_check."""
    parser = argparse.ArgumentParser(
        prog="scope_check.py",
        description="Fail if files changed since --base are outside the --allow glob patterns.",
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
        "--allow",
        required=True,
        action="extend",
        nargs="+",
        metavar="GLOB",
        help="Allowed POSIX path glob pattern(s) (e.g. 'src/**', 'tests/*.cpp').",
    )
    parser.add_argument(
        "--project-config",
        default="",
        help="Optional path to project.toml for workspace-root boundary checks.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the scope check and return exit code 0 on pass or 1 on violation/error."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    repo_root = pathlib.Path(args.repo).resolve()
    if not repo_root.is_dir():
        print(f"SCOPE_ERROR: repository path does not exist: {args.repo}", file=sys.stderr)
        return 1

    allow_patterns = [p.strip() for p in args.allow if p and p.strip()]
    if not allow_patterns:
        print("SCOPE_ERROR: at least one non-empty --allow glob is required", file=sys.stderr)
        return 1

    proj_cfg = pathlib.Path(args.project_config) if args.project_config else None
    try:
        ws_scope = resolve_workspace_scope(repo_root, project_config_path=proj_cfg)
    except ValueError as exc:
        print(f"SCOPE_ERROR: {exc}", file=sys.stderr)
        return 1

    effective_patterns = list(allow_patterns)
    if ws_scope.configured and ws_scope.workspace_root_rel != ".":
        for pat in allow_patterns:
            norm_pat = pat.strip().replace("\\", "/").lstrip("/")
            if norm_pat.startswith("./"):
                norm_pat = norm_pat[2:]
            if (
                not ws_scope.glob_outside_workspace(norm_pat)
                and norm_pat != ws_scope.workspace_root_rel
                and not norm_pat.startswith(ws_scope.workspace_root_rel + "/")
            ):
                effective_patterns.append(f"{ws_scope.workspace_root_rel}/{norm_pat}")

    compiled_globs = [glob_to_regex(pat) for pat in effective_patterns]
    changed_files, err = get_changed_files(repo_root, args.base)
    if changed_files is None:
        print(f"SCOPE_ERROR: {err}", file=sys.stderr)
        return 1

    if ws_scope.configured:
        outside_ws = [path for path in changed_files if ws_scope.outside_workspace(path)]
        if outside_ws:
            for path in outside_ws:
                print(
                    f"SCOPE_VIOLATION: {path} is outside workspace root "
                    f"({ws_scope.workspace_root_rel})"
                )
            return 1

    violations = [path for path in changed_files if not matches_any_glob(path, compiled_globs)]
    if violations:
        for path in violations:
            print(f"SCOPE_VIOLATION: {path} is outside allowed globs ({', '.join(allow_patterns)})")
        return 1

    print(f"SCOPE_OK: {len(changed_files)} changed file(s) within allowed scope")
    return 0


if __name__ == "__main__":
    sys.exit(main())
