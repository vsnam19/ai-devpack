#!/usr/bin/env python3
# safe_git.py - Hardened host-side git wrapper and metadata tamper detector.
#
# Usage:
#   python3 runner/safe_git.py verify <repo_dir>
#   python3 runner/safe_git.py --help
#
# Refuses to invoke git when repository metadata is symlinked, when the effective
# repository config (as parsed by git itself via `git config --list --no-includes`)
# contains a denylisted executable/include key not present in a pre-agent snapshot,
# or when any metadata file that can affect host git behaviour changed since the
# snapshot (whole-file SHA-256 of config, config.worktree, info/attributes,
# info/exclude, commondir, gitdir, every hooks/ entry, and the normalised config list).
#
# Filter execution: a filter runs only when `filter.<name>.clean|smudge|process` is
# configured. Repository config may not define `filter.*` (denylisted, and any change
# to config files is a tamper). Attribute files (.gitattributes, info/attributes)
# only select drivers by name, so their contents are not hashed; host git also pins
# `core.attributesFile=/dev/null` and sets `GIT_ATTR_NOSYSTEM=1`.
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
    "-c",
    "core.attributesFile=/dev/null",
)

SAFE_GIT_ENV_OVERRIDES: dict[str, str] = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
}

DENYLIST_DESCRIPTION: tuple[str, ...] = (
    "core.fsmonitor",
    "core.hookspath",
    "core.sshcommand",
    "core.editor",
    "core.pager",
    "diff.*.command",
    "diff.*.textconv",
    "diff.external",
    "filter.*",
    "merge.*.driver",
    "alias.*",
    "include.path",
    "includeif.*",
    "credential.*helper",
    "gpg.program",
    "uploadpack.*",
    "protocol.*",
)

ABSENT_DIGEST = "absent"
_METADATA_FILES: tuple[str, ...] = (
    "config",
    "config.worktree",
    "info/attributes",
    "info/exclude",
    "commondir",
    "gitdir",
)
_CONFIG_LIST_ARGS: tuple[str, ...] = ("config", "--list", "--show-origin", "-z", "--no-includes")
_CONFIG_LIST_TIMEOUT_S = 30
_CORE_DENIED_NAMES: frozenset[str] = frozenset({"fsmonitor", "hookspath", "sshcommand", "editor", "pager"})
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


class GitSecurityError(RuntimeError):
    """Raised when repository git metadata is unsafe, symlinked, or tampered."""


@dataclass(frozen=True)
class GitMetadataSnapshot:
    """Immutable pre-agent snapshot of repository git metadata that can affect host git."""

    repo_root: Path
    git_dir: Path | None
    common_dir: Path | None
    git_pointer_sha256: str = ""
    config_sha256: str = ""
    worktree_config_sha256: str = ""
    metadata_digests: tuple[tuple[str, str], ...] = ()
    hooks_listing: tuple[tuple[str, str], ...] = ()
    allowed_denied_keys: frozenset[tuple[str, str, str]] = field(default_factory=frozenset)

    def hooks_entries_for_log(self) -> list[str]:
        """Return a JSON-serializable summary of snapshotted hook files and hashes."""
        return [f"{name}:{digest}" for name, digest in self.hooks_listing]


def _sha256_bytes(data: bytes) -> str:
    """Return lowercase hexadecimal SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


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


def _digest_or_empty(digest: str) -> str:
    """Return ``""`` for an absent file (legacy snapshot field convention), else ``digest``."""
    return "" if digest == ABSENT_DIGEST else digest


def _file_digest(path: Path, label: str) -> str:
    """Return SHA-256 of the full bytes of regular file ``path``, or ``absent`` when missing.

    Symlinks and non-regular entries are refused because git would follow or execute them.
    """
    if path.is_symlink():
        raise GitSecurityError(f"git_metadata_tampered: symlinked {label} is forbidden: {path}")
    if path.is_file():
        return _sha256_bytes(path.read_bytes())
    if path.exists():
        raise GitSecurityError(f"git_metadata_tampered: non-regular {label} is forbidden: {path}")
    return ABSENT_DIGEST


def _snapshot_hooks_dir(hooks_dir: Path, prefix: str, out: dict[str, str]) -> None:
    """Record full-content digests of every entry under ``hooks_dir`` into ``out`` as ``prefix/<rel>``."""
    if hooks_dir.is_symlink():
        raise GitSecurityError(f"git_metadata_tampered: symlinked hooks directory is forbidden: {hooks_dir}")
    if not hooks_dir.exists():
        return
    if not hooks_dir.is_dir():
        raise GitSecurityError(f"git_metadata_tampered: non-directory hooks path is forbidden: {hooks_dir}")
    for entry in sorted(hooks_dir.rglob("*")):
        rel = f"{prefix}/{entry.relative_to(hooks_dir).as_posix()}"
        if entry.is_symlink():
            out[rel] = "symlink:" + os.readlink(entry)
        elif entry.is_file():
            out[rel] = _sha256_bytes(entry.read_bytes())
        elif entry.is_dir():
            out[rel] = "dir"


def _collect_metadata_digests(
    dot_git: Path,
    git_dir: Path,
    common_dir: Path,
) -> dict[str, str]:
    """Return ``label -> digest`` for every metadata file of the git_dir and common_dir (deduplicated)."""
    out: dict[str, str] = {}
    out["dotgit"] = "dir" if dot_git.is_dir() else _file_digest(dot_git, ".git pointer")
    roles: list[tuple[str, Path]] = [("common", common_dir)]
    if git_dir != common_dir:
        roles.append(("gitdir", git_dir))
    for role, base in roles:
        for rel in _METADATA_FILES:
            out[f"{role}:{rel}"] = _file_digest(base / rel, f"{role} {rel}")
        _snapshot_hooks_dir(base / "hooks", f"{role}:hooks", out)
    return out


def _parse_config_list(data: bytes) -> list[tuple[str, str, str]]:
    """Parse ``git config --list --show-origin -z`` output into ``(origin, key, value)`` tuples.

    Records are ``<origin>\\0<key>[\\n<value>]\\0``. Keys are normalised by git itself
    (section and variable names lower-cased), so the denylist sees git's own parse.
    """
    text = data.decode("utf-8", errors="replace")
    parts = text.split("\0")
    if parts and parts[-1] == "":
        parts.pop()
    if len(parts) % 2 != 0:
        raise GitSecurityError("git_config_list_failed: unexpected git config --list -z output")
    entries: list[tuple[str, str, str]] = []
    for idx in range(0, len(parts), 2):
        origin = parts[idx]
        key, _sep, value = parts[idx + 1].partition("\n")
        entries.append((origin, key, value))
    return entries


def _list_config_entries(git_dir: Path) -> tuple[bytes, list[tuple[str, str, str]]]:
    """Run git itself to list the repository config; return ``(raw_output, entries)``.

    Uses the hardened environment and ``--no-includes`` so include directives are never followed.
    """
    cmd = ["git", *SAFE_GIT_CONFIG_ARGS, f"--git-dir={git_dir}", *_CONFIG_LIST_ARGS]
    try:
        proc = subprocess.run(
            cmd,
            env=safe_git_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=_CONFIG_LIST_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitSecurityError(f"git_config_list_failed: {exc}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GitSecurityError(f"git_config_list_failed: git config exited {proc.returncode}: {stderr}")
    return proc.stdout, _parse_config_list(proc.stdout)


def _split_config_key(full_key: str) -> tuple[str, str | None, str] | None:
    """Split a normalised git key ``section[.subsection].name`` into its parts (subsection may contain dots)."""
    if "." not in full_key:
        return None
    section, rest = full_key.split(".", 1)
    if "." in rest:
        subsection, name = rest.rsplit(".", 1)
        return section, subsection, name
    return section, None, rest


def match_denylisted_git_key(full_key: str) -> str | None:
    """Return the denylist pattern matched by normalised git key ``full_key``, else None."""
    parts = _split_config_key(full_key.lower())
    if parts is None:
        return None
    section, subsection, name = parts
    if section == "core" and name in _CORE_DENIED_NAMES:
        return f"core.{name}"
    if section == "diff":
        if subsection is not None and name in ("command", "textconv"):
            return f"diff.*.{name}"
        if subsection is None and name == "external":
            return "diff.external"
        return None
    if section == "filter":
        return "filter.*"
    if section == "merge" and subsection is not None and name == "driver":
        return "merge.*.driver"
    if section == "alias":
        return "alias.*"
    if section == "include" and name == "path":
        return "include.path"
    if section == "includeif":
        return "includeif.*"
    if section == "credential" and name.endswith("helper"):
        return "credential.*helper"
    if section == "gpg" and name == "program":
        return "gpg.program"
    if section == "uploadpack":
        return "uploadpack.*"
    if section == "protocol":
        return "protocol.*"
    return None


def _is_command_origin(origin: str) -> bool:
    """Return True for entries injected on the git command line or by the caller's environment."""
    return origin.startswith("command line:") or origin.startswith("command:")


def snapshot_git_metadata(repo_root: Path | str) -> GitMetadataSnapshot:
    """Capture a pre-agent snapshot of all host-git-relevant metadata for ``repo_root``.

    Denylisted keys present at snapshot time are recorded as allowed; any later change to
    the metadata files is reported by ``check_git_metadata_unchanged``.
    """
    root = Path(repo_root).resolve()
    dot_git, git_dir, common_dir = resolve_git_dirs(root)
    if dot_git is None or git_dir is None or common_dir is None:
        return GitMetadataSnapshot(repo_root=root, git_dir=None, common_dir=None)

    digests = _collect_metadata_digests(dot_git, git_dir, common_dir)
    raw_list, entries = _list_config_entries(git_dir)
    digests["config-list"] = _sha256_bytes(raw_list)

    denied: set[tuple[str, str, str]] = set()
    for origin, key, value in entries:
        if _is_command_origin(origin):
            continue
        pattern = match_denylisted_git_key(key)
        if pattern is not None:
            denied.add((pattern, key, value))

    hooks = tuple(sorted((label, dig) for label, dig in digests.items() if ":hooks/" in label))
    return GitMetadataSnapshot(
        repo_root=root,
        git_dir=git_dir,
        common_dir=common_dir,
        git_pointer_sha256=_digest_or_empty(digests["dotgit"]),
        config_sha256=_digest_or_empty(digests["common:config"]),
        worktree_config_sha256=_digest_or_empty(
            digests.get("gitdir:config.worktree", digests["common:config.worktree"])
        ),
        metadata_digests=tuple(sorted(digests.items())),
        hooks_listing=hooks,
        allowed_denied_keys=frozenset(denied),
    )


def check_git_metadata_unchanged(snapshot: GitMetadataSnapshot) -> tuple[bool, str]:
    """Compare current git metadata (every snapshotted file and the config list) against ``snapshot``.

    Returns ``(True, "")`` if unchanged, else ``(False, reason)`` starting with ``git_metadata_tampered``.
    """
    try:
        current = snapshot_git_metadata(snapshot.repo_root)
    except GitSecurityError as exc:
        return False, str(exc)

    if (current.git_dir, current.common_dir) != (snapshot.git_dir, snapshot.common_dir):
        return False, "git_metadata_tampered: git directory layout changed since snapshot"
    before = dict(snapshot.metadata_digests)
    after = dict(current.metadata_digests)
    for label in sorted(set(before) | set(after)):
        if before.get(label, ABSENT_DIGEST) != after.get(label, ABSENT_DIGEST):
            return False, f"git_metadata_tampered: {label} changed since snapshot"
    return True, ""


def verify_git_config_safe(
    repo_root: Path | str,
    *,
    snapshot: GitMetadataSnapshot | None = None,
) -> None:
    """Raise ``GitSecurityError`` if git metadata is unsafe, tampered, or has new denylisted keys."""
    allowed: frozenset[tuple[str, str, str]] = frozenset()
    if snapshot is not None:
        unchanged, reason = check_git_metadata_unchanged(snapshot)
        if not unchanged:
            raise GitSecurityError(reason)
        allowed = snapshot.allowed_denied_keys

    current = snapshot_git_metadata(Path(repo_root))
    if current.git_dir is None:
        return
    for pattern, key, value in sorted(current.allowed_denied_keys):
        if (pattern, key, value) not in allowed:
            raise GitSecurityError(
                f"git_metadata_tampered: forbidden git config key '{key}' (matches denylist '{pattern}')"
            )


def safe_git_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return environment dictionary with system/global git config, attributes and prompts neutralised."""
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
        description="Verify repository git metadata and config against the safe_git denylist.",
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
