#!/usr/bin/env python3
# safe_git.py - Hardened host-side git wrapper and metadata tamper detector.
#
# Usage:
#   python3 runner/safe_git.py verify <repo_dir>
#   python3 runner/safe_git.py --help
#
# Refuses to invoke git when repository metadata (.git/config, config.worktree,
# hooks/, or .git pointer) is symlinked, contains denylisted executable/include
# keys not present in a pre-agent snapshot, or fails snapshot verification.
#
# Exit codes:
#   0 - Verification succeeded (or --help printed)
#   1 - Unsafe or tampered git metadata detected
#   2 - Invalid CLI arguments
"""Hardened host-side git execution helper and metadata snapshot verifier."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping, Sequence

SAFE_GIT_CONFIG_ARGS: tuple[str, ...] = (
    "-c",
    "core.fsmonitor=",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "diff.external=",
    "-c",
    "core.sshCommand=",
    "-c",
    "protocol.allow=never",
    "-c",
    "core.pager=cat",
)

SAFE_GIT_ENV_OVERRIDES: dict[str, str] = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}

DENYLIST_DESCRIPTION: tuple[str, ...] = (
    "core.fsmonitor",
    "core.hookspath",
    "diff.*.command",
    "diff.external",
    "filter.*",
    "core.sshcommand",
    "alias.*",
    "core.editor",
    "core.pager",
    "include.path",
    "includeif.*",
)

_CORE_DENIED_KEYS: frozenset[str] = frozenset(
    {"fsmonitor", "hookspath", "sshcommand", "editor", "pager"}
)
_DIFF_SHOW_LOG_SUBCOMMANDS: frozenset[str] = frozenset({"diff", "show", "log"})
_TWO_ARG_GLOBAL_FLAGS: frozenset[str] = frozenset(
    {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
        "--exec-path",
        "--config-env",
    }
)
_SECTION_HEADER_RE = re.compile(r'^\[\s*([A-Za-z0-9-]+)(?:\s+"((?:\\.|[^"\\])*)"|\.([A-Za-z0-9-]+))?\s*\]$')


class GitSecurityError(RuntimeError):
    """Raised when repository git metadata is unsafe, symlinked, or tampered."""


@dataclass(frozen=True)
class GitMetadataSnapshot:
    """Immutable pre-agent snapshot of repository git config and hooks metadata."""

    repo_root: Path
    git_dir: Path | None
    common_dir: Path | None
    git_pointer_sha256: str = ""
    config_sha256: str = ""
    worktree_config_sha256: str = ""
    hooks_listing: tuple[tuple[str, str], ...] = ()
    allowed_denied_keys: frozenset[tuple[str, str, str]] = field(default_factory=frozenset)

    def hooks_entries_for_log(self) -> list[str]:
        """Return a JSON-serializable summary of snapshotted hook files and hashes."""
        return [f"{name}:{digest}" for name, digest in self.hooks_listing]


def _sha256_bytes(data: bytes) -> str:
    """Return lowercase hexadecimal SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _strip_git_config_comment(line: str) -> str:
    """Strip unquoted ``#`` or ``;`` comments from a git config line."""
    in_quotes = False
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_quotes:
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if not in_quotes and ch in ("#", ";"):
            return line[:idx]
    return line


def _join_git_config_lines(text: str) -> list[str]:
    """Join backslash-continued lines in git config text."""
    logical: list[str] = []
    buf = ""
    for raw_line in text.splitlines():
        stripped_end = raw_line.rstrip("\r")
        if buf:
            content = stripped_end.lstrip()
        else:
            content = stripped_end
        # Count trailing backslashes outside comments if line continues
        bs_count = 0
        for ch in reversed(content):
            if ch == "\\":
                bs_count += 1
            else:
                break
        if bs_count % 2 == 1:
            buf += content[:-1]
        else:
            logical.append(buf + content)
            buf = ""
    if buf:
        logical.append(buf)
    return logical


def _unescape_subsection(raw: str) -> str:
    """Unescape ``\\\"`` and ``\\\\`` sequences inside a quoted git config subsection."""
    out: list[str] = []
    escaped = False
    for ch in raw:
        if escaped:
            out.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        else:
            out.append(ch)
    if escaped:
        out.append("\\")
    return "".join(out)


def parse_git_config_entries(config_text: str) -> list[tuple[str, str | None, str, str]]:
    """Parse git config INI text into ``(section, subsection, key, value)`` tuples."""
    entries: list[tuple[str, str | None, str, str]] = []
    section = ""
    subsection: str | None = None

    for raw_line in _join_git_config_lines(config_text):
        cleaned = _strip_git_config_comment(raw_line).strip()
        if not cleaned:
            continue
        if cleaned.startswith("["):
            # Handle inline key after section header if present (e.g. [core] foo = bar)
            close_idx = cleaned.find("]")
            if close_idx == -1:
                continue
            header = cleaned[: close_idx + 1]
            rest = cleaned[close_idx + 1 :].strip()
            match = _SECTION_HEADER_RE.match(header)
            if match:
                section = match.group(1).lower()
                if match.group(2) is not None:
                    subsection = _unescape_subsection(match.group(2))
                elif match.group(3) is not None:
                    subsection = match.group(3).lower()
                else:
                    subsection = None
            if not rest:
                continue
            cleaned = rest

        if not section:
            continue
        if "=" in cleaned:
            raw_key, raw_val = cleaned.split("=", 1)
            key = raw_key.strip().lower()
            val = raw_val.strip()
            if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
                val = _unescape_subsection(val[1:-1])
        else:
            key = cleaned.strip().lower()
            val = "true"
        if key:
            entries.append((section, subsection, key, val))
    return entries


def match_denylisted_git_config_key(
    section: str,
    subsection: str | None,
    key: str,
) -> str | None:
    """Return the matched denylist pattern if ``(section, subsection, key)`` is forbidden, else None."""
    sec = section.lower()
    k = key.lower()
    if sec == "core" and k in _CORE_DENIED_KEYS:
        return f"core.{k}"
    if sec == "diff":
        if subsection is not None and k == "command":
            return "diff.*.command"
        if subsection is None and k == "external":
            return "diff.external"
    if sec == "filter":
        return "filter.*"
    if sec == "alias":
        return "alias.*"
    if sec == "include" and k == "path":
        return "include.path"
    if sec == "includeif":
        return "includeif.*"
    return None


def _format_config_key(section: str, subsection: str | None, key: str) -> str:
    """Format a normalized dotted git config key name for error reporting."""
    if subsection is None:
        return f"{section}.{key}"
    return f"{section}.{subsection}.{key}"


def _extract_denied_keys_from_bytes(config_bytes: bytes) -> list[tuple[str, str, str]]:
    """Return ``(pattern, full_key, value)`` triples for denylisted keys in ``config_bytes``."""
    if not config_bytes:
        return []
    text = config_bytes.decode("utf-8", errors="replace")
    found: list[tuple[str, str, str]] = []
    for section, subsection, key, val in parse_git_config_entries(text):
        pattern = match_denylisted_git_config_key(section, subsection, key)
        if pattern is not None:
            found.append((pattern, _format_config_key(section, subsection, key), val))
    return found


def resolve_git_dirs(repo_root: Path) -> tuple[Path | None, Path | None, Path | None]:
    """Locate ``(.git_entry, git_dir, common_dir)`` for ``repo_root`` without executing git.

    Rejects symlinked ``.git`` entries, malformed ``.git`` pointer files, and symlinked
    ``commondir`` files. Returns ``(None, None, None)`` when ``repo_root`` is not inside a git repo.
    """
    resolved = repo_root.resolve()
    search_roots = [resolved, *resolved.parents]
    for parent in search_roots:
        dot_git = parent / ".git"
        if not dot_git.exists() and not dot_git.is_symlink():
            continue
        if dot_git.is_symlink():
            raise GitSecurityError(f"git_metadata_tampered: symlinked .git entry is forbidden: {dot_git}")
        if dot_git.is_dir():
            return dot_git, dot_git, dot_git
        if dot_git.is_file():
            raw = dot_git.read_text(encoding="utf-8", errors="replace")
            gitdir_raw = ""
            for line in raw.splitlines():
                stripped = line.strip()
                if stripped.lower().startswith("gitdir:"):
                    gitdir_raw = stripped.split(":", 1)[1].strip()
                    break
            if not gitdir_raw:
                raise GitSecurityError(f"git_metadata_tampered: malformed .git pointer file: {dot_git}")
            gd_path = Path(gitdir_raw)
            git_dir = (parent / gd_path).resolve() if not gd_path.is_absolute() else gd_path.resolve()
            if not git_dir.is_dir():
                raise GitSecurityError(f"git_metadata_tampered: missing worktree gitdir: {git_dir}")
            common_file = git_dir / "commondir"
            if common_file.is_symlink():
                raise GitSecurityError(f"git_metadata_tampered: symlinked commondir is forbidden: {common_file}")
            if common_file.is_file():
                cd_raw = common_file.read_text(encoding="utf-8", errors="replace").strip()
                if not cd_raw:
                    raise GitSecurityError(f"git_metadata_tampered: empty commondir file: {common_file}")
                cd_path = Path(cd_raw)
                common_dir = (git_dir / cd_path).resolve() if not cd_path.is_absolute() else cd_path.resolve()
            else:
                common_dir = git_dir
            return dot_git, git_dir, common_dir
        raise GitSecurityError(f"git_metadata_tampered: unsupported .git entry type: {dot_git}")
    return None, None, None


def _read_regular_file_bytes(path: Path, label: str) -> bytes:
    """Read ``path`` as a regular file, raising ``GitSecurityError`` if it is a symlink."""
    if path.is_symlink():
        raise GitSecurityError(f"git_metadata_tampered: symlinked {label} is forbidden: {path}")
    if path.is_file():
        return path.read_bytes()
    if path.exists():
        raise GitSecurityError(f"git_metadata_tampered: non-regular {label} is forbidden: {path}")
    return b""


def _snapshot_hooks_dir(hooks_dir: Path, prefix: str, out: list[tuple[str, str]]) -> None:
    """Append sorted ``(relative_name, digest_or_kind)`` pairs for ``hooks_dir`` into ``out``."""
    if hooks_dir.is_symlink():
        raise GitSecurityError(f"git_metadata_tampered: symlinked hooks directory is forbidden: {hooks_dir}")
    if not hooks_dir.exists():
        return
    if not hooks_dir.is_dir():
        raise GitSecurityError(f"git_metadata_tampered: non-directory hooks path is forbidden: {hooks_dir}")
    for entry in sorted(hooks_dir.rglob("*")):
        rel = f"{prefix}/{entry.relative_to(hooks_dir).as_posix()}"
        if entry.is_symlink():
            out.append((rel, "symlink:" + os.readlink(entry)))
        elif entry.is_file():
            out.append((rel, _sha256_bytes(entry.read_bytes())))
        elif entry.is_dir():
            out.append((rel, "dir"))


def snapshot_git_metadata(repo_root: Path | str) -> GitMetadataSnapshot:
    """Capture a pre-agent snapshot of ``.git/config`` and ``.git/hooks`` for ``repo_root``."""
    root = Path(repo_root).resolve()
    dot_git, git_dir, common_dir = resolve_git_dirs(root)
    if dot_git is None or git_dir is None or common_dir is None:
        return GitMetadataSnapshot(repo_root=root, git_dir=None, common_dir=None)

    pointer_sha = _sha256_bytes(dot_git.read_bytes()) if dot_git.is_file() else "dir"
    cfg_bytes = _read_regular_file_bytes(common_dir / "config", "git config")
    cfg_sha = _sha256_bytes(cfg_bytes) if cfg_bytes or (common_dir / "config").is_file() else ""

    wt_cfg_bytes = b""
    wt_cfg_sha = ""
    if git_dir != common_dir:
        wt_cfg_path = git_dir / "config.worktree"
        wt_cfg_bytes = _read_regular_file_bytes(wt_cfg_path, "worktree git config")
        wt_cfg_sha = _sha256_bytes(wt_cfg_bytes) if wt_cfg_bytes or wt_cfg_path.is_file() else ""

    hooks_entries: list[tuple[str, str]] = []
    _snapshot_hooks_dir(common_dir / "hooks", "hooks", hooks_entries)
    if git_dir != common_dir:
        _snapshot_hooks_dir(git_dir / "hooks", "worktree-hooks", hooks_entries)

    denied = frozenset(
        _extract_denied_keys_from_bytes(cfg_bytes)
        + _extract_denied_keys_from_bytes(wt_cfg_bytes)
    )
    return GitMetadataSnapshot(
        repo_root=root,
        git_dir=git_dir,
        common_dir=common_dir,
        git_pointer_sha256=pointer_sha,
        config_sha256=cfg_sha,
        worktree_config_sha256=wt_cfg_sha,
        hooks_listing=tuple(hooks_entries),
        allowed_denied_keys=denied,
    )


def check_git_metadata_unchanged(snapshot: GitMetadataSnapshot) -> tuple[bool, str]:
    """Compare current git config and hooks metadata against ``snapshot``.

    Returns ``(True, "")`` if unchanged, else ``(False, reason)``.
    """
    try:
        current = snapshot_git_metadata(snapshot.repo_root)
    except GitSecurityError as exc:
        return False, str(exc)

    if current.git_pointer_sha256 != snapshot.git_pointer_sha256:
        return False, "git_metadata_tampered: .git pointer or directory changed"
    if current.config_sha256 != snapshot.config_sha256:
        return False, "git_metadata_tampered: .git/config sha256 changed"
    if current.worktree_config_sha256 != snapshot.worktree_config_sha256:
        return False, "git_metadata_tampered: worktree git config sha256 changed"
    if current.hooks_listing != snapshot.hooks_listing:
        return False, "git_metadata_tampered: .git/hooks directory listing or contents changed"
    return True, ""


def verify_git_config_safe(
    repo_root: Path | str,
    *,
    snapshot: GitMetadataSnapshot | None = None,
) -> None:
    """Raise ``GitSecurityError`` if git metadata is unsafe, tampered, or contains denylisted keys."""
    if snapshot is not None:
        unchanged, reason = check_git_metadata_unchanged(snapshot)
        if not unchanged:
            raise GitSecurityError(reason)
        allowed = snapshot.allowed_denied_keys
    else:
        allowed = frozenset()

    dot_git, git_dir, common_dir = resolve_git_dirs(Path(repo_root))
    if dot_git is None or git_dir is None or common_dir is None:
        return

    cfg_bytes = _read_regular_file_bytes(common_dir / "config", "git config")
    for pattern, full_key, val in _extract_denied_keys_from_bytes(cfg_bytes):
        if (pattern, full_key, val) not in allowed:
            raise GitSecurityError(
                f"git_metadata_tampered: forbidden git config key '{full_key}' "
                f"(matches denylist '{pattern}') in {common_dir / 'config'}"
            )

    if git_dir != common_dir:
        wt_cfg_bytes = _read_regular_file_bytes(git_dir / "config.worktree", "worktree git config")
        for pattern, full_key, val in _extract_denied_keys_from_bytes(wt_cfg_bytes):
            if (pattern, full_key, val) not in allowed:
                raise GitSecurityError(
                    f"git_metadata_tampered: forbidden git config key '{full_key}' "
                    f"(matches denylist '{pattern}') in {git_dir / 'config.worktree'}"
                )

    hooks_dir = common_dir / "hooks"
    if hooks_dir.is_symlink():
        raise GitSecurityError(f"git_metadata_tampered: symlinked hooks directory is forbidden: {hooks_dir}")
    if git_dir != common_dir and (git_dir / "hooks").is_symlink():
        raise GitSecurityError(
            f"git_metadata_tampered: symlinked worktree hooks directory is forbidden: {git_dir / 'hooks'}"
        )


def safe_git_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return environment dictionary with system/global git config and terminal prompts disabled."""
    env = dict(base_env) if base_env is not None else os.environ.copy()
    env.update(SAFE_GIT_ENV_OVERRIDES)
    return env


def _parse_leading_git_args(args: Sequence[str]) -> tuple[Path | None, int | None, str | None]:
    """Scan leading git options to find ``-C <dir>`` target and subcommand index/name."""
    target_dir: Path | None = None
    idx = 0
    n = len(args)
    while idx < n:
        tok = args[idx]
        if tok == "--":
            break
        if tok in _TWO_ARG_GLOBAL_FLAGS:
            if tok == "-C" and idx + 1 < n:
                target_dir = Path(args[idx + 1])
            idx += 2
            continue
        if tok.startswith("-C") and len(tok) > 2:
            target_dir = Path(tok[2:])
            idx += 1
            continue
        if tok.startswith("-c") and len(tok) > 2:
            idx += 1
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        return target_dir, idx, tok
    return target_dir, None, None


def build_safe_git_cmd(
    args: Sequence[str],
    *,
    repo_root: Path | str | None = None,
) -> list[str]:
    """Build hardened ``git`` argv with ``-c`` overrides and ``--no-ext-diff``/``--no-textconv``."""
    arg_list = [str(a) for a in args]
    detected_c_dir, subcmd_idx, subcmd = _parse_leading_git_args(arg_list)

    if subcmd_idx is not None and subcmd in _DIFF_SHOW_LOG_SUBCOMMANDS:
        # Locate '--' separator after subcommand so flags precede pathspecs.
        tail = arg_list[subcmd_idx + 1 :]
        dashdash_pos = tail.index("--") if "--" in tail else len(tail)
        opts_slice = tail[:dashdash_pos]
        inject: list[str] = []
        if "--no-ext-diff" not in opts_slice:
            inject.append("--no-ext-diff")
        if "--no-textconv" not in opts_slice:
            inject.append("--no-textconv")
        if inject:
            arg_list = (
                arg_list[: subcmd_idx + 1]
                + inject
                + arg_list[subcmd_idx + 1 :]
            )

    prefix = ["git", *SAFE_GIT_CONFIG_ARGS]
    if repo_root is not None and detected_c_dir is None:
        prefix.extend(["-C", str(repo_root)])
    return prefix + arg_list


def run_git(
    args: Sequence[str],
    *,
    repo_root: Path | str | None = None,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    snapshot: GitMetadataSnapshot | None = None,
    timeout: int | float | None = None,
    check: bool = False,
    text: bool = True,
    capture_output: bool = True,
    stdin: int | None = subprocess.DEVNULL,
) -> subprocess.CompletedProcess[Any]:
    """Verify git metadata safety and execute ``git`` with hardened config flags and environment."""
    detected_c_dir, _, _ = _parse_leading_git_args([str(a) for a in args])
    verify_root: Path | None = None
    if repo_root is not None:
        verify_root = Path(repo_root)
    elif detected_c_dir is not None:
        verify_root = detected_c_dir if detected_c_dir.is_absolute() or cwd is None else (Path(cwd) / detected_c_dir)
    elif cwd is not None:
        verify_root = Path(cwd)

    if verify_root is not None:
        verify_git_config_safe(verify_root, snapshot=snapshot)
    elif snapshot is not None:
        verify_git_config_safe(snapshot.repo_root, snapshot=snapshot)

    cmd = build_safe_git_cmd(args, repo_root=repo_root)
    merged_env = safe_git_env(env)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        env=merged_env,
        stdin=stdin,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        check=check,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for ``runner/safe_git.py``."""
    parser = argparse.ArgumentParser(
        prog="safe_git.py",
        description="Verify repository .git/config and hooks metadata against the safe_git denylist.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    verify_p = sub.add_parser("verify", help="Verify git metadata safety for a repository directory.")
    verify_p.add_argument("repo", help="Path to git repository or worktree.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for verifying repository git metadata safety."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.command == "verify":
        try:
            verify_git_config_safe(Path(args.repo))
        except GitSecurityError as exc:
            sys.stderr.write(f"FAIL: {exc}\n")
            return 1
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
