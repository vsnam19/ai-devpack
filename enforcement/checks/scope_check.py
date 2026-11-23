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
import fnmatch
import pathlib
import re
import subprocess
import sys

_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from runner.safe_git import GitSecurityError, run_git  # noqa: E402
from runner.workspace_scope import resolve_workspace_scope  # noqa: E402

GATE_PROTECTED_FILENAMES: frozenset[str] = frozenset(
    {"preflight.toml", ".clang-tidy", "gate.toml", ".gitignore"}
)
_BUILD_OUTPUT_DIR_NAMES: frozenset[str] = frozenset(
    {"build", "build-cov", "build-rel", "build-doxygen", "CMakeFiles", "_deps"}
)
_PACK_OWNED_EXCLUDE_ENTRIES: frozenset[str] = frozenset(
    {".claude", ".claude/", "TASK_PROMPT.md", ".devpack", ".devpack/"}
)


def normalize_rel_posix_path(raw_path: str) -> str:
    """Normalize a repository-relative path for protected/allowed comparisons.

    Backslashes become slashes, surrounding whitespace and leading slashes are removed, and
    a leading ``./`` prefix is removed repeatedly. Only that literal prefix is stripped, so
    dot-prefixed names such as ``.clang-tidy`` or ``.cppcheck-suppressions`` are preserved.
    Returns ``""`` for empty input or the current directory.
    """
    norm = raw_path.strip().replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.lstrip("/")
    if not norm:
        return ""
    text = pathlib.PurePosixPath(norm).as_posix()
    return "" if text == "." else text


def _normalize_rel_path(rel_posix_path: str) -> str:
    """Normalize a repository-relative POSIX path for matching (see ``normalize_rel_posix_path``)."""
    return normalize_rel_posix_path(rel_posix_path)


def is_gate_protected_path(
    rel_posix_path: str,
    extra_gate_paths: tuple[str, ...] | list[str] | set[str] = (),
) -> bool:
    """Return True when ``rel_posix_path`` is a gate, lint, cppcheck, or control config path."""
    norm = _normalize_rel_path(rel_posix_path)
    if not norm:
        return False
    if norm == ".devpack" or norm.startswith(".devpack/"):
        return True
    if pathlib.PurePosixPath(norm).name in GATE_PROTECTED_FILENAMES:
        return True
    normalized_extras = {_normalize_rel_path(item) for item in extra_gate_paths if str(item).strip()}
    return norm in normalized_extras


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a POSIX path glob pattern (supporting ``*``, ``?``, and ``**``) to a regex."""
    norm = pattern.strip().replace("\\", "/")
    while norm.startswith("./"):
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
    norm = normalize_rel_posix_path(rel_posix_path)
    return any(rx.match(norm) is not None for rx in compiled_globs)


def _gitignore_pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Convert a single gitignore glob pattern (without leading ``!``) to a regex."""
    i = 0
    n = len(pattern)
    out: list[str] = ["^"]
    while i < n:
        if pattern[i : i + 3] == "**/":
            out.append("(?:.+/)?")
            i += 3
        elif pattern[i : i + 3] == "/**":
            out.append("/.*")
            i += 3
        elif pattern[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[":
            end = pattern.find("]", i + 1)
            if end != -1:
                cls = pattern[i + 1 : end]
                if cls.startswith("!"):
                    cls = "^" + cls[1:]
                out.append(f"[{cls}]")
                i = end + 1
            else:
                out.append(re.escape("["))
                i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def _parse_gitignore_rules(
    raw_text: str,
    base_dir_posix: str,
) -> list[tuple[bool, bool, bool, str, re.Pattern[str]]]:
    """Parse gitignore lines into ``(negated, dir_only, anchored, base_dir, regex)`` tuples."""
    rules: list[tuple[bool, bool, bool, str, re.Pattern[str]]] = []
    for raw_line in raw_text.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            continue
        if line.startswith("#"):
            continue
        # Strip unescaped trailing spaces
        while line.endswith(" ") and not line.endswith("\\ "):
            line = line[:-1]
        if not line:
            continue
        negated = False
        if line.startswith("!"):
            negated = True
            line = line[1:]
        elif line.startswith("\\!") or line.startswith("\\#"):
            line = line[1:]
        line = line.replace("\\ ", " ")
        if not line:
            continue
        dir_only = line.endswith("/")
        if dir_only:
            line = line.rstrip("/")
        if not line:
            continue
        anchored = "/" in line
        if line.startswith("/"):
            line = line[1:]
        if not line:
            continue
        rules.append((negated, dir_only, anchored, base_dir_posix, _gitignore_pattern_to_regex(line)))
    return rules


def _load_trusted_base_gitignores(
    repo_root: pathlib.Path,
    base_ref: str,
) -> list[tuple[bool, bool, bool, str, re.Pattern[str]]]:
    """Snapshot all ``.gitignore`` files from ``base_ref`` (never reading working-tree ``.gitignore``)."""
    tree_proc = run_git(
        ["ls-tree", "-r", "-z", "--name-only", base_ref],
        repo_root=repo_root,
        check=False,
    )
    if tree_proc.returncode != 0:
        return []
    gitignore_paths = [
        p
        for p in tree_proc.stdout.split("\0")
        if p and (p == ".gitignore" or p.endswith("/.gitignore"))
    ]
    rules: list[tuple[bool, bool, bool, str, re.Pattern[str]]] = []
    # Apply shallower .gitignore files before deeper ones so deeper rules override
    for gi_path in sorted(gitignore_paths, key=lambda p: (p.count("/"), p)):
        show_proc = run_git(["show", f"{base_ref}:{gi_path}"], repo_root=repo_root, check=False)
        if show_proc.returncode != 0:
            continue
        parent = gi_path.rsplit("/", 1)[0] if "/" in gi_path else ""
        rules.extend(_parse_gitignore_rules(show_proc.stdout, parent))
    return rules


def _load_pack_owned_excludes(repo_root: pathlib.Path) -> set[str]:
    """Return pack-owned harness entries present in ``.git/info/exclude``."""
    try:
        from runner.safe_git import resolve_git_dirs

        _dot, git_dir, common_dir = resolve_git_dirs(repo_root)
    except Exception:
        return set()
    allowed: set[str] = set()
    for base in (d for d in (git_dir, common_dir) if d is not None):
        excl = base / "info" / "exclude"
        if excl.is_file() and not excl.is_symlink():
            for raw in excl.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = raw.strip()
                if stripped in _PACK_OWNED_EXCLUDE_ENTRIES:
                    allowed.add(stripped.rstrip("/"))
    return allowed


def _is_untracked_ignored(
    rel_path: str,
    base_rules: list[tuple[bool, bool, bool, str, re.Pattern[str]]],
    pack_excludes: set[str],
) -> bool:
    """Return True only when ``rel_path`` matches build-output dirs, pack excludes, or base ``.gitignore``."""
    norm = normalize_rel_posix_path(rel_path)
    if not norm:
        return False
    parts = norm.split("/")
    if any(part in _BUILD_OUTPUT_DIR_NAMES for part in parts[:-1]) or parts[0] in _BUILD_OUTPUT_DIR_NAMES:
        return True
    if parts[0] in pack_excludes:
        return True
    if parts[-1] == ".gitignore":
        return False

    ignored = False
    for negated, dir_only, anchored, base_dir, rx in base_rules:
        if base_dir:
            prefix = base_dir + "/"
            if not norm.startswith(prefix):
                continue
            sub = norm[len(prefix) :]
        else:
            sub = norm
        sub_parts = sub.split("/")
        matched = False
        if dir_only:
            if anchored:
                # Pattern must match a directory prefix of sub (so at least one file component follows)
                for idx in range(1, len(sub_parts)):
                    cand = "/".join(sub_parts[:idx])
                    if rx.match(cand) is not None:
                        matched = True
                        break
            else:
                for idx in range(len(sub_parts) - 1):
                    if rx.match(sub_parts[idx]) is not None:
                        matched = True
                        break
        else:
            if anchored:
                if rx.match(sub) is not None:
                    matched = True
                else:
                    # Directory-prefix match: a file inside a matching directory is ignored
                    for idx in range(1, len(sub_parts)):
                        if rx.match("/".join(sub_parts[:idx])) is not None:
                            matched = True
                            break
            else:
                if rx.match(sub_parts[-1]) is not None:
                    matched = True
                else:
                    for idx in range(len(sub_parts) - 1):
                        if rx.match(sub_parts[idx]) is not None:
                            matched = True
                            break
        if matched:
            ignored = not negated
    return ignored


def get_changed_files(repo_root: pathlib.Path, base_ref: str) -> tuple[list[str] | None, str | None]:
    """Return sorted list of files changed since ``base_ref`` (committed, staged, working tree, or untracked).

    Untracked files are enumerated via ``git ls-files --others`` WITHOUT ``--exclude-standard``
    and filtered solely against trusted ``.gitignore`` snapshots from ``base_ref``, pack-owned
    harness entries in ``.git/info/exclude``, and standard build-output directories.
    """
    try:
        verify = run_git(
            ["rev-parse", "--verify", f"{base_ref}^{{commit}}"],
            repo_root=repo_root,
            check=False,
        )
        if verify.returncode != 0:
            err = verify.stderr.strip() or f"invalid base ref '{base_ref}'"
            return None, err

        base_rules = _load_trusted_base_gitignores(repo_root, base_ref)
        pack_excludes = _load_pack_owned_excludes(repo_root)

        changed: set[str] = set()
        diff_cmds = [
            ["diff", "--name-only", "-z", base_ref],
            ["diff", "--name-only", "-z", f"{base_ref}..HEAD"],
            ["diff", "--cached", "--name-only", "-z"],
        ]
        for cmd in diff_cmds:
            proc = run_git(cmd, repo_root=repo_root, check=False)
            if proc.returncode != 0:
                return None, proc.stderr.strip() or "git diff failed"
            for entry in proc.stdout.split("\0"):
                cleaned = entry.strip()
                if cleaned:
                    changed.add(cleaned)

        untracked_proc = run_git(["ls-files", "--others", "-z"], repo_root=repo_root, check=False)
        if untracked_proc.returncode != 0:
            return None, untracked_proc.stderr.strip() or "git ls-files failed"
        for entry in untracked_proc.stdout.split("\0"):
            cleaned = entry.strip()
            if not cleaned:
                continue
            if not _is_untracked_ignored(cleaned, base_rules, pack_excludes):
                changed.add(cleaned)
    except GitSecurityError as exc:
        return None, str(exc)

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
        required=False,
        action="extend",
        nargs="+",
        default=[],
        metavar="GLOB",
        help="Allowed POSIX path glob pattern(s) (e.g. 'src/**', 'tests/*.cpp').",
    )
    parser.add_argument(
        "--list-changed",
        action="store_true",
        help="Print changed/untracked files using trusted base .gitignore rules and exit 0.",
    )
    parser.add_argument("--components", default=None, help="Comma-separated active component names.")
    parser.add_argument("--all-components", action="store_true", help="Check scope against every workspace component.")
    parser.add_argument(
        "--project-config",
        default="",
        help="Optional path to project.toml for workspace-root boundary checks.",
    )
    parser.add_argument(
        "--gate-config-file",
        action="append",
        default=[],
        help="Additional repository-relative gate/lint config path protected from worker edits.",
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

    if args.list_changed:
        changed_files, err = get_changed_files(repo_root, args.base)
        if changed_files is None:
            print(f"SCOPE_ERROR: {err}", file=sys.stderr)
            return 1
        for path in changed_files:
            print(path)
        return 0

    allow_patterns = [p.strip() for p in args.allow if p and p.strip()]
    if not allow_patterns:
        print("SCOPE_ERROR: at least one non-empty --allow glob is required", file=sys.stderr)
        return 1

    proj_cfg = pathlib.Path(args.project_config) if args.project_config else None
    try:
        ws_scope = resolve_workspace_scope(
            repo_root, project_config_path=proj_cfg, components=args.components,
            all_components=args.all_components,
        )
    except ValueError as exc:
        print(f"SCOPE_ERROR: {exc}", file=sys.stderr)
        return 1

    effective_patterns = list(allow_patterns)
    if ws_scope.configured and ws_scope.workspace_root_rel != ".":
        for pat in allow_patterns:
            norm_pat = pat.strip().replace("\\", "/")
            while norm_pat.startswith("./"):
                norm_pat = norm_pat[2:]
            norm_pat = norm_pat.lstrip("/")
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

    violations: list[str] = []
    for path in changed_files:
        if is_gate_protected_path(path, args.gate_config_file):
            violations.append(
                f"SCOPE_VIOLATION: {path} is gate-touched "
                "(gate/lint/cppcheck configuration cannot be modified by workers)"
            )
        if ws_scope.configured:
            if ws_scope.outside_workspace(path):
                violations.append(
                    f"SCOPE_VIOLATION: {path} is outside workspace root "
                    f"({ws_scope.workspace_root_rel})"
                )
            elif ws_scope.is_excluded(path):
                violations.append(f"SCOPE_VIOLATION: {path} is excluded by workspace configuration")
            elif not ws_scope.is_in_active_components(path):
                violations.append(f"SCOPE_VIOLATION: {path} is outside active components")
        if not matches_any_glob(path, compiled_globs):
            violations.append(
                f"SCOPE_VIOLATION: {path} is outside allowed globs ({', '.join(allow_patterns)})"
            )
    if violations:
        for violation in violations:
            print(violation)
        return 1

    print(f"SCOPE_OK: {len(changed_files)} changed file(s) within allowed scope")
    return 0


if __name__ == "__main__":
    sys.exit(main())
