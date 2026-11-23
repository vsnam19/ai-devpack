#!/usr/bin/env python3
# workspace_scope.py - Yocto / multi-component workspace scope resolver and path filter.
#
# Usage:
#   python3 runner/workspace_scope.py list [--repo DIR] [--project-config FILE] [--json]
#   python3 runner/workspace_scope.py resolve [--repo DIR] [--project-config FILE]
#       [--components a,b | --all-components] [--json]
#   python3 runner/workspace_scope.py filter [--repo DIR] [--project-config FILE]
#       [--components a,b | --all-components] [PATH ...]
#
# Resolves the active component set from (in priority):
#   1. CLI --components a,b / --all-components
#   2. Environment variable DEVPACK_COMPONENTS ("all" or comma-separated names)
#   3. [scan] defaults in .devpack/project.toml
#
# Without [workspace] in .devpack/project.toml, the entire repository is treated
# as a single implicit legacy component so existing projects and tests remain
# unaffected.
#
# Exit codes:
#   0 - Command succeeded
#   1 - Unknown component name or workspace validation error
#   2 - Invalid CLI arguments or malformed configuration
"""Component-scoped workspace resolution, discovery, and path filtering for Yocto trees."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any, Iterable, Mapping, Sequence, TypeVar

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

PathLikeT = TypeVar("PathLikeT", str, Path)

DISCOVERY_MARKERS: frozenset[str] = frozenset({"CMakeLists.txt", "Makefile", "meson.build"})
DISCOVERY_SUFFIXES: frozenset[str] = frozenset({".bb", ".bbappend"})
VALID_SCAN_DEFAULTS: frozenset[str] = frozenset({"selected", "all"})


def _strip_toml_comment(line: str) -> str:
    """Strip an unquoted ``#`` comment from a TOML line."""
    in_quote: str | None = None
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_quote == '"':
            escaped = True
            continue
        if ch in ('"', "'"):
            if in_quote is None:
                in_quote = ch
            elif in_quote == ch:
                in_quote = None
            continue
        if ch == "#" and in_quote is None:
            return line[:idx]
    return line


def _parse_toml_scalar_or_array(raw: str) -> Any:
    """Parse a scalar or array TOML value for the Python 3.10 fallback."""
    val = raw.strip()
    if val in ("true", "false"):
        return val == "true"
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        inner = val[1:-1]
        if val[0] == '"':
            return inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        items: list[Any] = []
        buf: list[str] = []
        in_q: str | None = None
        esc = False
        for ch in inner:
            if esc:
                buf.append(ch)
                esc = False
                continue
            if ch == "\\" and in_q == '"':
                buf.append(ch)
                esc = True
                continue
            if ch in ('"', "'"):
                if in_q is None:
                    in_q = ch
                elif in_q == ch:
                    in_q = None
                buf.append(ch)
                continue
            if ch == "," and in_q is None:
                part = "".join(buf).strip()
                if part:
                    items.append(_parse_toml_scalar_or_array(part))
                buf = []
                continue
            buf.append(ch)
        tail = "".join(buf).strip()
        if tail:
            items.append(_parse_toml_scalar_or_array(tail))
        return items
    try:
        return int(val)
    except ValueError:
        return val


def parse_toml_with_arrays(text: str) -> dict[str, Any]:
    """Parse TOML text supporting ``[table]`` and ``[[array_of_tables]]`` on Python 3.10+."""
    if tomllib is not None:
        data = tomllib.loads(text)
        if not isinstance(data, dict):
            raise ValueError("TOML root must be a table")
        return data

    root: dict[str, Any] = {}  # pragma: no cover - Python 3.10 fallback
    current: dict[str, Any] = root
    lines = text.splitlines()
    idx = 0
    while idx < len(lines):
        cleaned = _strip_toml_comment(lines[idx]).strip()
        idx += 1
        if not cleaned:
            continue
        if cleaned.startswith("[[") and cleaned.endswith("]]"):
            path_parts = [
                p.strip().strip('"').strip("'")
                for p in cleaned[2:-2].strip().split(".")
                if p.strip()
            ]
            if not path_parts:
                raise ValueError(f"Invalid TOML array-of-tables header: {cleaned}")
            target = root
            for part in path_parts[:-1]:
                nxt = target.setdefault(part, {})
                if not isinstance(nxt, dict):
                    raise ValueError(f"Conflicting TOML table path: {cleaned}")
                target = nxt
            leaf = path_parts[-1]
            arr = target.setdefault(leaf, [])
            if not isinstance(arr, list):
                raise ValueError(f"Conflicting TOML array-of-tables path: {cleaned}")
            entry: dict[str, Any] = {}
            arr.append(entry)
            current = entry
            continue
        if cleaned.startswith("[") and cleaned.endswith("]"):
            path_parts = [
                p.strip().strip('"').strip("'")
                for p in cleaned[1:-1].strip().split(".")
                if p.strip()
            ]
            if not path_parts:
                raise ValueError(f"Invalid TOML table header: {cleaned}")
            target = root
            for part in path_parts:
                nxt = target.setdefault(part, {})
                if not isinstance(nxt, dict):
                    raise ValueError(f"Conflicting TOML table path: {cleaned}")
                target = nxt
            current = target
            continue
        if "=" not in cleaned:
            raise ValueError(f"Invalid TOML line: {cleaned}")
        key_raw, val_raw = cleaned.split("=", 1)
        key = key_raw.strip().strip('"').strip("'")
        val_str = val_raw.strip()
        while val_str.startswith("[") and not val_str.endswith("]") and idx < len(lines):
            nxt_line = _strip_toml_comment(lines[idx]).strip()
            idx += 1
            if nxt_line:
                val_str = val_str + " " + nxt_line
        current[key] = _parse_toml_scalar_or_array(val_str)
    return root


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a POSIX glob pattern supporting ``*``, ``?``, and ``**`` to a regex."""
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


def _normalize_rel_posix(raw_path: str, *, field_name: str, allow_dot: bool = False) -> str:
    """Normalize a relative POSIX path and reject absolute paths or ``..`` traversal."""
    cleaned = raw_path.strip().replace("\\", "/")
    if not cleaned:
        raise ValueError(f"{field_name} must be a non-empty relative path")
    if cleaned.startswith("/") or (len(cleaned) >= 2 and cleaned[1] == ":"):
        raise ValueError(f"{field_name} must be relative, got absolute path: {raw_path!r}")
    norm = PurePosixPath(os.path.normpath(cleaned)).as_posix()
    if norm == ".." or norm.startswith("../"):
        raise ValueError(f"{field_name} must not escape root with '..': {raw_path!r}")
    if norm == "." and not allow_dot:
        raise ValueError(f"{field_name} must not be '.'")
    return norm


def _join_rel(base_rel: str, sub_rel: str) -> str:
    """Join two normalized relative POSIX paths."""
    if base_rel in ("", "."):
        return sub_rel
    if sub_rel in ("", "."):
        return base_rel
    return f"{base_rel}/{sub_rel}"


def _path_within_prefix(rel_path: str, prefix_rel: str) -> bool:
    """Return True if normalized repo-relative ``rel_path`` equals or lies beneath ``prefix_rel``."""
    if prefix_rel == ".":
        return True
    return rel_path == prefix_rel or rel_path.startswith(prefix_rel + "/")


def _glob_literal_prefix(repo_rel_glob: str) -> str:
    """Return the non-wildcard directory/file prefix of a normalized repo-relative glob."""
    norm = repo_rel_glob.strip().replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    norm = norm.lstrip("/")
    if not norm:
        return ""
    wildcard_idx = len(norm)
    for ch in "*?[{":
        pos = norm.find(ch)
        if 0 <= pos < wildcard_idx:
            wildcard_idx = pos
    if wildcard_idx == len(norm):
        return norm.rstrip("/")
    raw_prefix = norm[:wildcard_idx]
    if not raw_prefix.endswith("/"):
        return raw_prefix.rsplit("/", 1)[0] if "/" in raw_prefix else ""
    return raw_prefix.rstrip("/")


def _glob_explicitly_names_component(repo_rel_glob: str, comp: "ComponentSpec") -> bool:
    """Return True if ``repo_rel_glob`` explicitly names ``comp``'s path before any wildcard."""
    if comp.repo_rel_path == ".":
        return False
    prefix = _glob_literal_prefix(repo_rel_glob)
    if not prefix or prefix == ".":
        return False
    return _path_within_prefix(prefix, comp.repo_rel_path)


def _coerce_str_list(value: Any, field_name: str) -> tuple[str, ...]:
    """Validate and normalize a TOML list of strings."""
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return tuple(item.strip() for item in value if item.strip())


@dataclass(frozen=True)
class ComponentSpec:
    """Single component definition inside a workspace root."""

    name: str
    path: str
    repo_rel_path: str
    abs_path: Path
    compile_commands: str = ""
    include_dirs: tuple[str, ...] = ()
    build_cmd: str = ""
    test_cmd: str = ""
    discovered: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize component metadata to a JSON-compatible dictionary."""
        return {
            "name": self.name,
            "path": self.path,
            "repo_rel_path": self.repo_rel_path,
            "compile_commands": self.compile_commands,
            "include_dirs": list(self.include_dirs),
            "build_cmd": self.build_cmd,
            "test_cmd": self.test_cmd,
            "discovered": self.discovered,
        }


@dataclass(frozen=True)
class WorkspaceConfig:
    """Parsed workspace, component, and scan settings from project configuration."""

    repo_root: Path
    configured: bool
    workspace_root_rel: str
    workspace_root: Path
    exclude: tuple[str, ...]
    components: tuple[ComponentSpec, ...]
    scan_default: str
    scan_selected: tuple[str, ...]
    config_path: Path | None = None


@dataclass
class WorkspaceScope:
    """Resolved active component scope with path predicates and filtering helpers."""

    repo_root: Path
    configured: bool
    workspace_root_rel: str
    workspace_root: Path
    exclude: tuple[str, ...]
    all_components: tuple[ComponentSpec, ...]
    active_components: tuple[ComponentSpec, ...]
    scan_mode: str
    scan_default: str = "all"
    default_selected: tuple[str, ...] = ()
    _exclude_regexes: tuple[re.Pattern[str], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Pre-compile exclude glob patterns."""
        self._exclude_regexes = tuple(_glob_to_regex(pat) for pat in self.exclude)

    def to_repo_relative(self, path: str | Path) -> str | None:
        """Convert ``path`` (relative to repo_root, or absolute) into a repo-relative POSIX string.

        Returns ``None`` if ``path`` resolves outside ``self.repo_root``.
        """
        raw = str(path).strip().replace("\\", "/")
        if not raw:
            return None
        candidate = Path(raw)
        if candidate.is_absolute():
            repo_resolved = self.repo_root.resolve()
            cand_resolved = candidate.resolve()
            try:
                rel = cand_resolved.relative_to(repo_resolved).as_posix()
                return "." if rel == "." else rel
            except ValueError:
                try:
                    rel_raw = candidate.relative_to(self.repo_root).as_posix()
                    return "." if rel_raw == "." else rel_raw
                except ValueError:
                    return None
        norm = PurePosixPath(os.path.normpath(raw)).as_posix()
        if norm == ".." or norm.startswith("../"):
            return None
        return "." if norm == "." else norm

    def outside_workspace(self, path: str | Path) -> bool:
        """Return True when ``path`` lies outside the configured workspace root (or repo root)."""
        rel = self.to_repo_relative(path)
        if rel is None:
            return True
        if not self.configured:
            return False
        if rel == ".":
            return self.workspace_root_rel != "."
        return not _path_within_prefix(rel, self.workspace_root_rel)

    def glob_outside_workspace(self, glob_pattern: str) -> bool:
        """Return True if a ``Files allowed`` glob can match files outside ``workspace.root``."""
        raw = glob_pattern.strip().replace("\\", "/")
        if not raw:
            return True
        if raw.startswith(("/", "~")) or ".." in PurePosixPath(raw).parts:
            return True
        if raw.startswith("./"):
            raw = raw[2:]
        if not self.configured:
            return False
        if self.workspace_root_rel == ".":
            return False

        # Exact workspace root or explicitly rooted under workspace_root_rel
        if raw == self.workspace_root_rel or raw.startswith(self.workspace_root_rel + "/"):
            return False

        first_seg = raw.split("/", 1)[0]
        if any(ch in first_seg for ch in "*?[{"):
            # Unscoped root wildcards like "**" or "*.cpp" match outside workspace.root
            return True

        # Check workspace-relative paths/globs (e.g. "middleware/ipc_bus/**")
        ws_candidate = self.workspace_root / first_seg
        comp_first_segs = {
            c.path.split("/", 1)[0] for c in self.all_components if c.path and c.path != "."
        }
        if ws_candidate.exists() or first_seg in comp_first_segs:
            return False

        return True

    def is_excluded(self, path: str | Path) -> bool:
        """Return True if ``path`` matches any ``[workspace].exclude`` glob."""
        if not self._exclude_regexes:
            return False
        rel = self.to_repo_relative(path)
        if rel is None:
            return False
        return any(rx.match(rel) is not None for rx in self._exclude_regexes)

    def component_for_path(self, path: str | Path) -> ComponentSpec | None:
        """Return the active ``ComponentSpec`` owning ``path``, or ``None`` if outside."""
        rel = self.to_repo_relative(path)
        if rel is None or self.outside_workspace(rel):
            return None
        # Prefer longest matching component path in case of nested paths
        ordered = sorted(
            self.active_components,
            key=lambda c: (0 if c.repo_rel_path == "." else len(c.repo_rel_path)),
            reverse=True,
        )
        for comp in ordered:
            if _path_within_prefix(rel, comp.repo_rel_path):
                return comp
        return None

    def is_in_active_components(self, path: str | Path) -> bool:
        """Return True if ``path`` belongs to at least one active component."""
        return self.component_for_path(path) is not None

    def filter(self, paths: Iterable[PathLikeT]) -> list[PathLikeT]:
        """Keep only paths inside active components and outside ``[workspace].exclude``."""
        kept: list[PathLikeT] = []
        for item in paths:
            rel = self.to_repo_relative(item)
            if rel is None:
                continue
            if self.is_excluded(rel):
                continue
            if self.configured and not self.is_in_active_components(rel):
                continue
            kept.append(item)
        return kept

    def path_globs(self) -> list[str]:
        """Return repo-relative POSIX glob patterns covering all active components."""
        if not self.configured:
            return ["**"]
        globs: list[str] = []
        for comp in self.active_components:
            if comp.repo_rel_path == ".":
                globs.append("**")
            else:
                globs.append(f"{comp.repo_rel_path}/**")
        return globs

    def target_dirs(self) -> list[Path]:
        """Return existing directory paths for active components."""
        if not self.configured:
            return [self.repo_root]
        return [comp.abs_path for comp in self.active_components if comp.abs_path.is_dir()]

    def components_touched_by_globs(self, allow_globs: Sequence[str]) -> tuple[ComponentSpec, ...]:
        """Return configured components whose paths are explicitly named by ``allow_globs``."""
        if not self.configured or not allow_globs:
            return ()
        repo_globs: list[str] = []
        for raw_pat in allow_globs:
            norm = raw_pat.strip().replace("\\", "/").lstrip("/")
            if norm.startswith("./"):
                norm = norm[2:]
            if not norm:
                continue
            repo_globs.append(norm)
            if (
                self.workspace_root_rel != "."
                and not self.glob_outside_workspace(norm)
                and norm != self.workspace_root_rel
                and not norm.startswith(self.workspace_root_rel + "/")
            ):
                repo_globs.append(f"{self.workspace_root_rel}/{norm}")
        touched = [
            comp
            for comp in self.all_components
            if any(_glob_explicitly_names_component(pat, comp) for pat in repo_globs)
        ]
        return tuple(touched)

    def with_allow_globs(self, allow_globs: Sequence[str]) -> "WorkspaceScope":
        """Return a scope whose active set is the union of active components and glob-named ones."""
        if not self.configured or not allow_globs:
            return self
        touched = self.components_touched_by_globs(allow_globs)
        if not touched:
            return self
        active_names = {comp.name for comp in self.active_components}
        added = [comp for comp in touched if comp.name not in active_names]
        if not added:
            return self
        merged_names = active_names | {comp.name for comp in added}
        merged = tuple(comp for comp in self.all_components if comp.name in merged_names)
        return WorkspaceScope(
            repo_root=self.repo_root,
            configured=self.configured,
            workspace_root_rel=self.workspace_root_rel,
            workspace_root=self.workspace_root,
            exclude=self.exclude,
            all_components=self.all_components,
            active_components=merged,
            scan_mode=self.scan_mode,
            scan_default=self.scan_default,
            default_selected=self.default_selected,
        )

    def resolve_path_in_workspace(self, rel_path: str) -> Path | None:
        """Resolve a relative brief/config path against ``repo_root`` or ``workspace_root``."""
        cleaned = rel_path.strip().replace("\\", "/")
        if not cleaned or cleaned.startswith("/"):
            return None
        repo_cand = (self.repo_root / cleaned).resolve()
        if repo_cand.is_relative_to(self.repo_root.resolve()) and repo_cand.exists():
            return repo_cand
        if self.configured and self.workspace_root != self.repo_root:
            ws_cand = (self.workspace_root / cleaned).resolve()
            if ws_cand.is_relative_to(self.workspace_root.resolve()) and ws_cand.exists():
                return ws_cand
        return None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable summary of the resolved workspace scope."""
        return {
            "configured": self.configured,
            "workspace_root": self.workspace_root_rel,
            "scan_mode": self.scan_mode,
            "components": [comp.name for comp in self.active_components],
            "all_components": [comp.name for comp in self.all_components],
            "globs": self.path_globs(),
            "exclude": list(self.exclude),
        }


def discover_components(
    repo_root: Path,
    workspace_root: Path,
    workspace_root_rel: str,
) -> tuple[ComponentSpec, ...]:
    """Discover direct subdirectories of ``workspace_root`` that look like buildable components."""
    if not workspace_root.is_dir():
        return ()
    found: list[ComponentSpec] = []
    for child in sorted(workspace_root.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.name.startswith("."):
            continue
        has_marker = False
        try:
            for item in child.iterdir():
                if not item.is_file():
                    continue
                if item.name in DISCOVERY_MARKERS or item.suffix in DISCOVERY_SUFFIXES:
                    has_marker = True
                    break
        except OSError:
            continue
        if has_marker:
            comp_rel = child.name
            repo_rel = _join_rel(workspace_root_rel, comp_rel)
            found.append(
                ComponentSpec(
                    name=child.name,
                    path=comp_rel,
                    repo_rel_path=repo_rel,
                    abs_path=child.resolve(),
                    discovered=True,
                )
            )
    return tuple(found)


def _select_project_config_path(
    repo_root: Path,
    project_config_path: Path | str | None,
) -> Path | None:
    """Locate the project TOML file to inspect for ``[workspace]`` settings."""
    if project_config_path is not None and str(project_config_path):
        cand = Path(project_config_path)
        if cand.is_file():
            return cand
        return None
    default_cfg = repo_root / ".devpack" / "project.toml"
    return default_cfg if default_cfg.is_file() else None


def load_workspace_config(
    repo_root: Path | str = ".",
    project_config_path: Path | str | None = None,
) -> WorkspaceConfig:
    """Load ``[workspace]``, ``[[component]]``, and ``[scan]`` from project config."""
    root = Path(repo_root).resolve()
    cfg_path = _select_project_config_path(root, project_config_path)

    raw_toml: dict[str, Any] = {}
    if cfg_path is not None:
        try:
            raw_toml = parse_toml_with_arrays(cfg_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"Failed to read project config {cfg_path}: {exc}") from exc
        except Exception as exc:
            raise ValueError(f"Failed to parse TOML in {cfg_path}: {exc}") from exc

    if "workspace" not in raw_toml:
        implicit = ComponentSpec(
            name="default",
            path=".",
            repo_rel_path=".",
            abs_path=root,
            discovered=False,
        )
        return WorkspaceConfig(
            repo_root=root,
            configured=False,
            workspace_root_rel=".",
            workspace_root=root,
            exclude=(),
            components=(implicit,),
            scan_default="all",
            scan_selected=("default",),
            config_path=cfg_path,
        )

    ws_tbl = raw_toml.get("workspace")
    if not isinstance(ws_tbl, Mapping):
        raise ValueError("[workspace] in project config must be a table")
    raw_root = ws_tbl.get("root")
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise ValueError("[workspace].root must be a non-empty relative path string")
    ws_rel = _normalize_rel_posix(raw_root, field_name="[workspace].root", allow_dot=True)
    ws_abs = (root / ws_rel).resolve() if ws_rel != "." else root
    if not ws_abs.is_relative_to(root):
        raise ValueError(f"[workspace].root escapes repository root: {raw_root!r}")

    exclude = _coerce_str_list(ws_tbl.get("exclude"), "[workspace].exclude")

    raw_components = raw_toml.get("component")
    components: list[ComponentSpec] = []
    if raw_components is not None:
        if not isinstance(raw_components, list):
            raise ValueError("[[component]] entries must form an array of tables")
        seen_names: set[str] = set()
        for idx, entry in enumerate(raw_components, start=1):
            if not isinstance(entry, Mapping):
                raise ValueError(f"[[component]] #{idx} must be a TOML table")
            name_raw = entry.get("name")
            if not isinstance(name_raw, str) or not name_raw.strip():
                raise ValueError(f"[[component]] #{idx} is missing a non-empty 'name'")
            name = name_raw.strip()
            if name.lower() == "all":
                raise ValueError("[[component]].name must not be 'all'")
            if name in seen_names:
                raise ValueError(f"Duplicate [[component]].name '{name}'")
            seen_names.add(name)

            path_raw = entry.get("path")
            if not isinstance(path_raw, str) or not path_raw.strip():
                raise ValueError(f"[[component]] '{name}' is missing a non-empty 'path'")
            comp_rel = _normalize_rel_posix(
                path_raw,
                field_name=f"[[component]] '{name}'.path",
                allow_dot=True,
            )
            comp_abs = (ws_abs / comp_rel).resolve() if comp_rel != "." else ws_abs
            if not comp_abs.is_relative_to(ws_abs):
                raise ValueError(
                    f"[[component]] '{name}'.path escapes workspace.root: {path_raw!r}"
                )
            repo_rel = _join_rel(ws_rel, comp_rel)

            cc_raw = entry.get("compile_commands", "")
            if cc_raw is None:
                cc_raw = ""
            if not isinstance(cc_raw, str):
                raise ValueError(f"[[component]] '{name}'.compile_commands must be a string")
            compile_commands = (
                _normalize_rel_posix(
                    cc_raw,
                    field_name=f"[[component]] '{name}'.compile_commands",
                    allow_dot=False,
                )
                if cc_raw.strip()
                else ""
            )

            include_dirs = _coerce_str_list(
                entry.get("include_dirs"),
                f"[[component]] '{name}'.include_dirs",
            )
            build_cmd = str(entry.get("build_cmd", "") or "").strip()
            test_cmd = str(entry.get("test_cmd", "") or "").strip()
            components.append(
                ComponentSpec(
                    name=name,
                    path=comp_rel,
                    repo_rel_path=repo_rel,
                    abs_path=comp_abs,
                    compile_commands=compile_commands,
                    include_dirs=include_dirs,
                    build_cmd=build_cmd,
                    test_cmd=test_cmd,
                    discovered=False,
                )
            )
    else:
        components = list(discover_components(root, ws_abs, ws_rel))

    scan_tbl = raw_toml.get("scan", {})
    if scan_tbl is None:
        scan_tbl = {}
    if not isinstance(scan_tbl, Mapping):
        raise ValueError("[scan] in project config must be a table")
    scan_selected = _coerce_str_list(scan_tbl.get("selected"), "[scan].selected")
    if "default" in scan_tbl:
        scan_default = str(scan_tbl["default"]).strip().lower()
    elif scan_selected:
        scan_default = "selected"
    else:
        scan_default = "all"
    if scan_default not in VALID_SCAN_DEFAULTS:
        raise ValueError(
            f"[scan].default must be one of {', '.join(sorted(VALID_SCAN_DEFAULTS))}, got {scan_default!r}"
        )

    return WorkspaceConfig(
        repo_root=root,
        configured=True,
        workspace_root_rel=ws_rel,
        workspace_root=ws_abs,
        exclude=exclude,
        components=tuple(components),
        scan_default=scan_default,
        scan_selected=scan_selected,
        config_path=cfg_path,
    )


def _parse_component_names(raw: str | Sequence[str]) -> list[str]:
    """Normalize a comma-separated string or sequence of component names."""
    items = [raw] if isinstance(raw, str) else list(raw)
    names: list[str] = []
    seen: set[str] = set()
    for item in items:
        for part in str(item).split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                names.append(cleaned)
    return names


def _resolve_named_components(
    requested: Sequence[str],
    all_components: Sequence[ComponentSpec],
) -> tuple[ComponentSpec, ...]:
    """Validate requested component names against ``all_components`` or raise ValueError."""
    by_name = {comp.name: comp for comp in all_components}
    valid_list = ", ".join(sorted(by_name)) if by_name else "(none)"
    unknown = [name for name in requested if name not in by_name]
    if unknown:
        raise ValueError(
            f"Unknown component(s): {', '.join(unknown)}. Valid components: {valid_list}"
        )
    return tuple(by_name[name] for name in requested)


def resolve_workspace_scope(
    repo_root: Path | str = ".",
    *,
    project_config_path: Path | str | None = None,
    components: str | Sequence[str] | None = None,
    all_components: bool = False,
    env: Mapping[str, str] | None = None,
) -> WorkspaceScope:
    """Resolve active component scope with priority: CLI > DEVPACK_COMPONENTS > [scan] defaults."""
    if all_components and components is not None and str(components).strip():
        raise ValueError("Cannot specify both --components and --all-components")

    cfg = load_workspace_config(repo_root, project_config_path=project_config_path)
    env_map = os.environ if env is None else env

    if not cfg.configured:
        cli_names = _parse_component_names(components) if components is not None else []
        if cli_names:
            _resolve_named_components(cli_names, cfg.components)
        env_raw = str(env_map.get("DEVPACK_COMPONENTS", "")).strip()
        if env_raw and env_raw.lower() != "all":
            env_names = _parse_component_names(env_raw)
            if env_names:
                _resolve_named_components(env_names, cfg.components)
        return WorkspaceScope(
            repo_root=cfg.repo_root,
            configured=False,
            workspace_root_rel=".",
            workspace_root=cfg.repo_root,
            exclude=(),
            all_components=cfg.components,
            active_components=cfg.components,
            scan_mode="legacy",
            scan_default="all",
            default_selected=("default",),
        )

    cli_names = _parse_component_names(components) if components is not None else []
    if components is not None and not cli_names:
        raise ValueError("--components must specify at least one component name")

    env_raw = str(env_map.get("DEVPACK_COMPONENTS", "")).strip()

    if all_components:
        active = cfg.components
        scan_mode = "all"
    elif cli_names:
        active = _resolve_named_components(cli_names, cfg.components)
        scan_mode = "selected"
    elif env_raw:
        if env_raw.lower() == "all":
            active = cfg.components
            scan_mode = "all"
        else:
            env_names = _parse_component_names(env_raw)
            if not env_names:
                raise ValueError("DEVPACK_COMPONENTS is set but names no components")
            active = _resolve_named_components(env_names, cfg.components)
            scan_mode = "selected"
    elif cfg.scan_default == "all":
        active = cfg.components
        scan_mode = "all"
    else:
        if not cfg.scan_selected:
            raise ValueError(
                "[scan].default is 'selected' but [scan].selected is empty; "
                "set [scan].selected or pass --components / --all-components"
            )
        active = _resolve_named_components(cfg.scan_selected, cfg.components)
        scan_mode = "selected"

    return WorkspaceScope(
        repo_root=cfg.repo_root,
        configured=True,
        workspace_root_rel=cfg.workspace_root_rel,
        workspace_root=cfg.workspace_root,
        exclude=cfg.exclude,
        all_components=cfg.components,
        active_components=active,
        scan_mode=scan_mode,
        scan_default=cfg.scan_default,
        default_selected=cfg.scan_selected,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for ``runner/workspace_scope.py``."""
    parser = argparse.ArgumentParser(
        prog="workspace_scope.py",
        description="Inspect, resolve, and filter Yocto workspace component scopes.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("list", "List all configured or discovered components in the workspace"),
        ("resolve", "Resolve active components and print path globs or JSON"),
        ("filter", "Filter input paths to those inside active components and outside excludes"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument(
            "--repo",
            default=".",
            help="Path to repository root (default: .)",
        )
        p.add_argument(
            "--project-config",
            default="",
            help="Optional path to project.toml (default: <repo>/.devpack/project.toml)",
        )
        if name in ("resolve", "filter"):
            p.add_argument(
                "--components",
                default=None,
                help="Comma-separated component names to activate",
            )
            p.add_argument(
                "--all-components",
                action="store_true",
                help="Activate all configured/discovered components",
            )
        if name in ("list", "resolve"):
            p.add_argument(
                "--json",
                action="store_true",
                help="Emit JSON output",
            )
        if name == "filter":
            p.add_argument(
                "paths",
                nargs="*",
                help="Paths to filter (reads newline-delimited paths from stdin when omitted)",
            )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for ``runner/workspace_scope.py``."""
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    repo_root = Path(args.repo)
    if not repo_root.is_dir():
        print(f"error: repository directory not found: {repo_root}", file=sys.stderr)
        return 2
    proj_cfg = Path(args.project_config) if args.project_config else None
    if proj_cfg is not None and not proj_cfg.is_file():
        print(f"error: project config not found: {proj_cfg}", file=sys.stderr)
        return 2

    try:
        if args.command == "list":
            cfg = load_workspace_config(repo_root, project_config_path=proj_cfg)
            if args.json:
                payload = {
                    "configured": cfg.configured,
                    "workspace_root": cfg.workspace_root_rel,
                    "exclude": list(cfg.exclude),
                    "scan_default": cfg.scan_default,
                    "scan_selected": list(cfg.scan_selected),
                    "components": [c.to_dict() for c in cfg.components],
                }
                print(json.dumps(payload, indent=2))
            else:
                for comp in cfg.components:
                    tag = " (discovered)" if comp.discovered else ""
                    print(f"{comp.name}\t{comp.repo_rel_path}{tag}")
            return 0

        scope = resolve_workspace_scope(
            repo_root,
            project_config_path=proj_cfg,
            components=args.components,
            all_components=bool(args.all_components),
        )
        if args.command == "resolve":
            if args.json:
                print(json.dumps(scope.to_dict(), indent=2))
            else:
                for glob in scope.path_globs():
                    print(glob)
            return 0

        raw_paths = list(args.paths)
        if not raw_paths and not sys.stdin.isatty():
            raw_paths = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
        for kept in scope.filter(raw_paths):
            print(kept)
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
