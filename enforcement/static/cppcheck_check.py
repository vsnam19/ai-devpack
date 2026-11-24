#!/usr/bin/env python3
# Header: Static analysis wrapper around cppcheck (local stand-in for Coverity).
#
# Usage:
#   python3 enforcement/static/cppcheck_check.py [OPTIONS] [TARGET ...]
#   python3 enforcement/static/cppcheck_check.py --base HEAD --repo-root .
#
# Runs cppcheck on explicit C/C++ targets or files changed since --base, reads
# settings from the [cppcheck] table in preflight.toml (or .devpack/project.toml),
# normalizes findings to repo-relative paths, applies an optional JSON baseline,
# and fails closed if cppcheck is not installed.
#
# Exit codes:
#   0 - Clean (no blocking findings after baseline filtering)
#   1 - Blocking static-analysis findings detected
#   2 - Usage, configuration, baseline, git, or cppcheck execution error
#   3 - TOOL_MISSING (cppcheck executable not found on PATH)
"""Run cppcheck static analysis with TOML configuration, baseline filtering, and fail-closed checks."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Any, Sequence
import xml.etree.ElementTree as ET

_REPO_ROOT_STR = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, _REPO_ROOT_STR)

from enforcement.checks.deviations import verify_deviation  # noqa: E402
from enforcement.checks.scope_check import get_changed_files  # noqa: E402
from runner.safe_git import GitSecurityError  # noqa: E402
from runner.workspace_scope import ComponentSpec, WorkspaceScope, resolve_workspace_scope  # noqa: E402

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

SOURCE_SUFFIXES = (".c", ".cpp", ".cc", ".cxx")
CPP_SUFFIXES = (".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh")
SKIP_DIRS = {
    ".git",
    "build",
    "build-cov",
    "build-rel",
    "build-doxygen",
    "_deps",
    "third_party",
    "generated",
}
DEFAULT_ENABLE = ("warning", "style", "performance", "portability")
DEFAULT_FAIL_ON = ("error", "warning")
DEFAULT_INCLUDE_DIRS = ("include",)
DEFAULT_EXTRA_ARGS: tuple[str, ...] = ()
DEFAULT_MAX_AS_BYTES = 2 * 1024 * 1024 * 1024
BASELINE_SCHEMA_VERSION = 1
KNOWN_FAIL_ON_SEVERITIES: frozenset[str] = frozenset(
    {"error", "warning", "style", "performance", "portability", "information"}
)

_SUPPRESS_ARG_RE = re.compile(r"^--suppress=([A-Za-z0-9_]+)(?::(.+))?$")
_STD_ARG_RE = re.compile(r"^--std=c\+\+\d{2}$")
_DEFINE_ARG_RE = re.compile(r"^-D[A-Za-z_][A-Za-z0-9_]*(?:=[^\r\n\x00]*)?$")
_UNDEF_ARG_RE = re.compile(r"^-U[A-Za-z_][A-Za-z0-9_]*$")
_PLATFORM_ARG_RE = re.compile(r"^--platform=[A-Za-z0-9_-]+$")
_CTU_DEPTH_ARG_RE = re.compile(r"^--max-ctu-depth=[0-9]+$")
_SUPPRESS_PATH_RE = re.compile(r"^[A-Za-z0-9_./*-]+(?::\d+)?$")

_TEMPLATE_5_RE = re.compile(r"^(.+?):(\d+):([^:]+):([^:]+):(.*)$")
_TEMPLATE_6_RE = re.compile(r"^(.+?):(\d+):(\d+):([^:]+):([^:]+):(.*)$")


def validate_fail_on(fail_on: Sequence[str]) -> tuple[str, ...]:
    """Validate that ``fail_on`` is non-empty and contains only known cppcheck severities."""
    normalized = tuple(item.strip().lower() for item in fail_on if str(item).strip())
    if not normalized:
        raise ValueError("[cppcheck].fail_on must be non-empty")
    unknown = [item for item in normalized if item not in KNOWN_FAIL_ON_SEVERITIES]
    if unknown:
        allowed = ", ".join(sorted(KNOWN_FAIL_ON_SEVERITIES))
        raise ValueError(
            f"[cppcheck].fail_on contains unknown severity {unknown[0]!r}; expected one of: {allowed}"
        )
    return normalized


def _is_valid_suppress_arg(arg: str) -> bool:
    """Return True if ``arg`` matches ``--suppress=<id>`` with optional relative ``:path``."""
    match = _SUPPRESS_ARG_RE.match(arg)
    if not match:
        return False
    raw_path = match.group(2)
    if raw_path is None:
        return True
    if not raw_path or raw_path.startswith("/") or "\\" in raw_path or "\x00" in raw_path:
        return False
    if not _SUPPRESS_PATH_RE.match(raw_path):
        return False
    path_only = raw_path.rsplit(":", 1)[0] if re.search(r":\d+$", raw_path) else raw_path
    parts = path_only.split("/")
    return ".." not in parts


def validate_extra_args(extra_args: Sequence[str]) -> tuple[str, ...]:
    """Validate ``cppcheck`` extra arguments against a strict safety allowlist."""
    validated: list[str] = []
    for raw_arg in extra_args:
        arg = str(raw_arg).strip()
        if not arg:
            raise ValueError("[cppcheck].extra_args must not contain empty arguments")
        if (
            arg == "--inline-suppr"
            or arg in ("--check-level=normal", "--check-level=exhaustive")
            or _is_valid_suppress_arg(arg)
            or _STD_ARG_RE.match(arg)
            or _DEFINE_ARG_RE.match(arg)
            or _UNDEF_ARG_RE.match(arg)
            or _PLATFORM_ARG_RE.match(arg)
            or _CTU_DEPTH_ARG_RE.match(arg)
        ):
            validated.append(arg)
            continue
        raise ValueError(
            f"disallowed cppcheck extra_arg {arg!r}; only allowlisted flags "
            "(--inline-suppr, --check-level=normal|exhaustive, --suppress=<id>[:path], "
            "--std=c++NN, -D<NAME>[=value], -U<NAME>, --platform=<name>, "
            "--max-ctu-depth=<n>) are permitted"
        )
    return tuple(validated)


def _strip_toml_comment(line: str) -> str:
    """Strip unquoted ``#`` comments from a TOML line."""
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


def _parse_toml_value(raw: str) -> Any:
    """Parse a scalar or inline array TOML value for the Python 3.10 fallback."""
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
                    items.append(_parse_toml_value(part))
                buf = []
                continue
            buf.append(ch)
        tail = "".join(buf).strip()
        if tail:
            items.append(_parse_toml_value(tail))
        return items
    try:
        return int(val)
    except ValueError:
        return val


def _minimal_toml_loads(text: str) -> dict[str, Any]:
    """Minimal TOML subset parser for Python 3.10 when ``tomllib`` is absent."""
    root: dict[str, Any] = {}
    current: dict[str, Any] = root
    lines = text.splitlines()
    idx = 0
    while idx < len(lines):
        cleaned = _strip_toml_comment(lines[idx]).strip()
        idx += 1
        if not cleaned:
            continue
        if cleaned.startswith("[") and cleaned.endswith("]") and not cleaned.startswith("[["):
            section = cleaned[1:-1].strip()
            target = root
            for part in section.split("."):
                key = part.strip().strip('"').strip("'")
                if key not in target or not isinstance(target[key], dict):
                    target[key] = {}
                target = target[key]
            current = target
            continue
        if "=" not in cleaned:
            raise ValueError(f"Invalid TOML line: {cleaned}")
        key_raw, val_raw = cleaned.split("=", 1)
        key = key_raw.strip().strip('"').strip("'")
        val_str = val_raw.strip()
        while val_str.startswith("[") and not val_str.endswith("]") and idx < len(lines):
            next_line = _strip_toml_comment(lines[idx]).strip()
            idx += 1
            val_str = val_str + " " + next_line
        current[key] = _parse_toml_value(val_str)
    return root


def _load_toml_file(path: Path) -> dict[str, Any]:
    """Load a TOML file into a dictionary using ``tomllib`` or the 3.10 fallback."""
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Failed to read config file {path}: {exc}") from exc
    if tomllib is not None:
        try:
            data = tomllib.loads(raw_text)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Failed to parse TOML in {path}: {exc}") from exc
    else:  # pragma: no cover - Python 3.10 fallback
        try:
            data = _minimal_toml_loads(raw_text)
        except Exception as exc:
            raise ValueError(f"Failed to parse TOML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"TOML root must be a table in {path}")
    return data


@dataclass
class CppcheckConfig:
    """Resolved cppcheck configuration from ``[cppcheck]`` in preflight TOML and CLI."""

    enable: tuple[str, ...] = DEFAULT_ENABLE
    std: str = "c++17"
    include_dirs: tuple[str, ...] = DEFAULT_INCLUDE_DIRS
    suppressions_file: str = ""
    extra_args: tuple[str, ...] = DEFAULT_EXTRA_ARGS
    fail_on: tuple[str, ...] = DEFAULT_FAIL_ON
    max_as_bytes: int = DEFAULT_MAX_AS_BYTES
    timeout_s: int | None = None


@dataclass(frozen=True)
class CppcheckFinding:
    """Normalized static-analysis diagnostic emitted by cppcheck."""

    file: str
    line: int
    id: str
    severity: str
    message: str

    def to_dict(self, *, blocking: bool | None = None) -> dict[str, Any]:
        """Serialize the finding to a JSON-compatible dictionary."""
        payload: dict[str, Any] = {
            "file": self.file,
            "line": self.line,
            "id": self.id,
            "severity": self.severity,
            "message": self.message,
        }
        if blocking is not None:
            payload["blocking"] = blocking
        return payload


@dataclass
class CppcheckRunResult:
    """Structured outcome from running ``cppcheck_check``."""

    exit_code: int
    files_checked: int = 0
    fallback_files_count: int = 0
    findings: list[CppcheckFinding] = field(default_factory=list)
    blocking_findings: list[CppcheckFinding] = field(default_factory=list)
    non_blocking_findings: list[CppcheckFinding] = field(default_factory=list)
    baselined_findings: list[CppcheckFinding] = field(default_factory=list)
    fail_on: tuple[str, ...] = DEFAULT_FAIL_ON
    tool_missing: bool = False
    error_message: str = ""
    raw_xml: str = ""
    raw_stdout: str = ""


def _coerce_str_list(value: Any, field_name: str, *, allow_csv: bool = False) -> tuple[str, ...]:
    """Normalize a TOML string list (or comma-separated string) into a tuple of strings."""
    if isinstance(value, str):
        if allow_csv:
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        cleaned: list[str] = []
        for item in value:
            if allow_csv and "," in item:
                cleaned.extend(part.strip() for part in item.split(",") if part.strip())
            elif item.strip():
                cleaned.append(item.strip())
        return tuple(cleaned)
    raise ValueError(f"[cppcheck].{field_name} must be a list of strings")


def resolve_config_path(config_path: Path | None, repo_root: Path | None) -> Path | None:
    """Locate the preflight or project TOML file to load ``[cppcheck]`` settings from."""
    if config_path is not None:
        resolved = config_path.resolve()
        if not resolved.is_file():
            raise ValueError(f"configuration file does not exist: {config_path}")
        return resolved
    if repo_root is not None:
        for rel in ("preflight.toml", ".devpack/project.toml"):
            candidate = repo_root / rel
            if candidate.is_file():
                return candidate.resolve()
    return None


def load_cppcheck_config(
    config_path: Path | None = None,
    repo_root: Path | None = None,
) -> tuple[CppcheckConfig, Path | None]:
    """Load and validate ``[cppcheck]`` settings from TOML configuration.

    Returns ``(CppcheckConfig, resolved_config_path_or_None)``.
    """
    resolved_cfg = resolve_config_path(config_path, repo_root)
    if resolved_cfg is None:
        return CppcheckConfig(), None

    raw_toml = _load_toml_file(resolved_cfg)
    general_tbl = raw_toml.get("general", {})
    if general_tbl is not None and not isinstance(general_tbl, dict):
        raise ValueError(f"[general] in {resolved_cfg} must be a table")
    cppcheck_tbl = raw_toml.get("cppcheck", {})
    if cppcheck_tbl is not None and not isinstance(cppcheck_tbl, dict):
        raise ValueError(f"[cppcheck] in {resolved_cfg} must be a table")

    cfg = CppcheckConfig()
    if isinstance(general_tbl, dict) and "std" in general_tbl:
        if not isinstance(general_tbl["std"], str) or not general_tbl["std"].strip():
            raise ValueError(f"[general].std in {resolved_cfg} must be a non-empty string")
        cfg.std = general_tbl["std"].strip()

    if isinstance(cppcheck_tbl, dict):
        if "enable" in cppcheck_tbl:
            cfg.enable = _coerce_str_list(cppcheck_tbl["enable"], "enable", allow_csv=True)
        if "std" in cppcheck_tbl:
            if not isinstance(cppcheck_tbl["std"], str) or not cppcheck_tbl["std"].strip():
                raise ValueError(f"[cppcheck].std in {resolved_cfg} must be a non-empty string")
            cfg.std = cppcheck_tbl["std"].strip()
        if "include_dirs" in cppcheck_tbl:
            cfg.include_dirs = _coerce_str_list(cppcheck_tbl["include_dirs"], "include_dirs")
        if "suppressions_file" in cppcheck_tbl:
            suppr = cppcheck_tbl["suppressions_file"]
            if not isinstance(suppr, str):
                raise ValueError(f"[cppcheck].suppressions_file in {resolved_cfg} must be a string")
            cfg.suppressions_file = suppr.strip()
        if "extra_args" in cppcheck_tbl:
            cfg.extra_args = validate_extra_args(
                _coerce_str_list(cppcheck_tbl["extra_args"], "extra_args")
            )
        if "fail_on" in cppcheck_tbl:
            raw_fail = _coerce_str_list(cppcheck_tbl["fail_on"], "fail_on", allow_csv=True)
            cfg.fail_on = validate_fail_on(raw_fail)
        if "max_memory_mb" in cppcheck_tbl:
            mem_mb = cppcheck_tbl["max_memory_mb"]
            if isinstance(mem_mb, bool) or not isinstance(mem_mb, int) or mem_mb < 64:
                raise ValueError(f"[cppcheck].max_memory_mb must be an integer >= 64, got {mem_mb!r}")
            cfg.max_as_bytes = mem_mb * 1024 * 1024
        if "timeout_s" in cppcheck_tbl:
            ts = cppcheck_tbl["timeout_s"]
            if isinstance(ts, bool) or not isinstance(ts, int) or ts < 1:
                raise ValueError(f"[cppcheck].timeout_s must be a positive integer, got {ts!r}")
            cfg.timeout_s = ts

    cfg.extra_args = validate_extra_args(cfg.extra_args)
    cfg.fail_on = validate_fail_on(cfg.fail_on)
    return cfg, resolved_cfg


def normalize_repo_path(raw_path: str, repo_root: Path | None) -> str:
    """Normalize a diagnostic file path to a POSIX path relative to ``repo_root``."""
    cleaned = raw_path.strip().replace("\\", "/")
    if not cleaned:
        return ""
    if repo_root is None:
        p = Path(cleaned)
        return p.as_posix()

    root_resolved = repo_root.resolve()
    candidate = Path(cleaned)
    if not candidate.is_absolute():
        candidate = (root_resolved / candidate).resolve()
    else:
        candidate = candidate.resolve()
    try:
        return candidate.relative_to(root_resolved).as_posix()
    except ValueError:
        return Path(cleaned).as_posix()


def parse_cppcheck_xml(xml_text: str, repo_root: Path | None = None) -> list[CppcheckFinding]:
    """Parse ``cppcheck --xml --xml-version=2`` output into normalized findings."""
    stripped = xml_text.strip()
    if not stripped:
        return []
    start = stripped.find("<?xml")
    if start < 0:
        start = stripped.find("<results")
    if start > 0:
        stripped = stripped[start:]
    end = stripped.rfind("</results>")
    if end >= 0:
        stripped = stripped[: end + len("</results>")]

    try:
        root = ET.fromstring(stripped)
    except ET.ParseError as exc:
        raise ValueError(f"Failed to parse cppcheck XML output: {exc}") from exc

    findings: list[CppcheckFinding] = []
    errors_el = root.find("errors")
    if errors_el is None:
        return findings

    for err in errors_el.findall("error"):
        rule_id = (err.get("id") or "").strip()
        if rule_id == "checkersReport":
            continue
        severity = (err.get("severity") or "information").strip().lower()
        msg = (err.get("msg") or err.get("verbose") or "").strip()
        locations = err.findall("location")
        raw_file = ""
        line_num = 0
        if locations:
            primary = locations[0]
            raw_file = (primary.get("file") or err.get("file0") or "").strip()
            raw_line = (primary.get("line") or "0").strip()
            try:
                line_num = int(raw_line)
            except ValueError:
                line_num = 0
        else:
            raw_file = (err.get("file0") or "").strip()

        norm_file = normalize_repo_path(raw_file, repo_root)
        findings.append(
            CppcheckFinding(
                file=norm_file,
                line=line_num,
                id=rule_id,
                severity=severity,
                message=msg,
            )
        )
    return findings


def parse_cppcheck_template(output_text: str, repo_root: Path | None = None) -> list[CppcheckFinding]:
    """Parse ``--template={file}:{line}:{severity}:{id}:{message}`` output into findings."""
    findings: list[CppcheckFinding] = []
    for raw_line in output_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("Checking "):
            continue
        m6 = _TEMPLATE_6_RE.match(line)
        if m6:
            raw_file, line_str, _col, severity, rule_id, message = m6.groups()
        else:
            m5 = _TEMPLATE_5_RE.match(line)
            if not m5:
                continue
            raw_file, line_str, severity, rule_id, message = m5.groups()
        if rule_id.strip() == "checkersReport":
            continue
        findings.append(
            CppcheckFinding(
                file=normalize_repo_path(raw_file, repo_root),
                line=int(line_str),
                id=rule_id.strip(),
                severity=severity.strip().lower(),
                message=message.strip(),
            )
        )
    return findings


def _finding_key(finding: CppcheckFinding) -> tuple[str, str, str, str]:
    """Return the path-relative baseline key tuple for a finding."""
    return (finding.file, finding.id, finding.severity, finding.message)


def load_baseline(baseline_path: Path) -> Counter[tuple[str, str, str, str]]:
    """Load and validate a cppcheck JSON baseline file into a multiset counter."""
    if not baseline_path.is_file():
        raise ValueError(f"baseline file does not exist: {baseline_path}")
    try:
        raw = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"failed to parse baseline file {baseline_path}: {exc}") from exc

    if isinstance(raw, dict):
        version = raw.get("schema_version", BASELINE_SCHEMA_VERSION)
        if version != BASELINE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported baseline schema_version in {baseline_path}: "
                f"expected {BASELINE_SCHEMA_VERSION}, got {version!r}"
            )
        items = raw.get("entries", raw.get("findings", []))
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError(f"baseline {baseline_path} must be a JSON object or list")

    if not isinstance(items, list):
        raise ValueError(f"baseline entries in {baseline_path} must be a list")

    counts: Counter[tuple[str, str, str, str]] = Counter()
    for entry in items:
        if not isinstance(entry, dict):
            raise ValueError(f"baseline entry must be an object in {baseline_path}")
        raw_file = str(entry.get("file", "")).strip().replace("\\", "/")
        if not raw_file or raw_file.startswith("/") or ".." in Path(raw_file).parts:
            raise ValueError(
                f"baseline file paths must be repository-relative in {baseline_path}: {raw_file!r}"
            )
        rule_id = str(entry.get("id", "")).strip()
        if not rule_id:
            raise ValueError(f"baseline entry missing 'id' in {baseline_path}")
        severity = str(entry.get("severity", "error")).strip().lower()
        message = str(entry.get("message", "")).strip()
        count_raw = entry.get("count", 1)
        if isinstance(count_raw, bool) or not isinstance(count_raw, int) or count_raw < 1:
            raise ValueError(f"baseline entry count must be >= 1 in {baseline_path}: {count_raw!r}")
        counts[(raw_file, rule_id, severity, message)] += count_raw
    return counts


def save_baseline(baseline_path: Path, findings: Sequence[CppcheckFinding]) -> None:
    """Write a portable repo-relative JSON baseline manifest for ``findings``."""
    counts: Counter[tuple[str, str, str, str]] = Counter(_finding_key(f) for f in findings)
    entries = [
        {
            "file": file_path,
            "id": rule_id,
            "severity": severity,
            "message": message,
            "count": count,
        }
        for (file_path, rule_id, severity, message), count in sorted(counts.items())
    ]
    payload = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "tool": "cppcheck",
        "entries": entries,
    }
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def filter_findings_with_baseline(
    findings: Sequence[CppcheckFinding],
    baseline_counts: Counter[tuple[str, str, str, str]],
) -> tuple[list[CppcheckFinding], list[CppcheckFinding]]:
    """Separate ``findings`` into ``(active_findings, baselined_findings)``."""
    remaining = Counter(baseline_counts)
    active: list[CppcheckFinding] = []
    baselined: list[CppcheckFinding] = []
    for finding in findings:
        key = _finding_key(finding)
        if remaining.get(key, 0) > 0:
            remaining[key] -= 1
            baselined.append(finding)
        else:
            active.append(finding)
    return active, baselined


def discover_changed_cpp_files(repo_root: Path, base_ref: str) -> list[Path]:
    """Return sorted existing C/C++ files changed since ``base_ref`` in ``repo_root``."""
    changed, err = get_changed_files(repo_root, base_ref)
    if changed is None:
        raise ValueError(f"git base ref error: {err}")

    files: list[Path] = []
    for rel in changed:
        if not rel.lower().endswith(CPP_SUFFIXES):
            continue
        candidate = (repo_root / rel).resolve()
        if candidate.is_file():
            files.append(candidate)
    return files


def collect_cpp_targets(targets: Sequence[str], repo_root: Path) -> list[Path]:
    """Collect existing C/C++ files from positional file or directory targets."""
    collected: list[Path] = []
    seen: set[Path] = set()
    for raw in targets:
        p = Path(raw)
        if not p.is_absolute():
            cwd_cand = (Path.cwd() / p).resolve()
            repo_cand = (repo_root / p).resolve()
            p = cwd_cand if cwd_cand.exists() else repo_cand
        else:
            p = p.resolve()
        if not p.exists():
            raise ValueError(f"target path does not exist: {raw}")
        if p.is_file():
            if p.suffix.lower() in CPP_SUFFIXES and p not in seen:
                seen.add(p)
                collected.append(p)
        elif p.is_dir():
            for child in sorted(p.rglob("*")):
                if any(part in SKIP_DIRS for part in child.relative_to(p).parts):
                    continue
                if child.is_file() and child.suffix.lower() in CPP_SUFFIXES and child not in seen:
                    seen.add(child)
                    collected.append(child)
    return collected


def resolve_cppcheck_binary(cppcheck_bin: str) -> str | None:
    """Resolve ``cppcheck_bin`` on PATH or as an executable file path."""
    if os.sep in cppcheck_bin or (os.altsep and os.altsep in cppcheck_bin):
        cand = Path(cppcheck_bin)
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
        return None
    return shutil.which(cppcheck_bin)


def _resolve_within_repo(raw_path: str, repo_root: Path) -> Path:
    """Resolve an include path against the analysed ``repo_root`` and reject paths that escape it.

    Relative paths are joined to ``repo_root``; absolute paths are accepted only when they already lie
    inside it. Raises ``ValueError`` (a usage error) otherwise. The config directory is never consulted,
    so a pack's pristine headers cannot shadow the trial tree's headers.
    """
    p = Path(raw_path)
    candidate = (p if p.is_absolute() else repo_root / p).resolve()
    if not candidate.is_relative_to(repo_root):
        raise ValueError(
            f"include path '{raw_path}' resolves outside the analysed repo root {repo_root}"
        )
    return candidate


def _resolve_suppressions_file(
    raw_path: str,
    config_dir: Path,
    repo_root: Path,
    trusted_dir: Path | None = None,
) -> Path:
    """Resolve ``suppressions_file``: repo root first, then the config directory (pack-provided lists).

    Absolute paths must lie inside ``repo_root``; relative paths must not escape ``repo_root``
    or ``config_dir``. A missing file is returned as the repo-root candidate so the caller
    reports it.

    When ``trusted_dir`` is given (a trusted snapshot prepared by the dispatcher), the path
    must be relative and is resolved ONLY inside that directory; the working tree is never
    consulted. A missing file is returned as the trusted-dir candidate so the caller reports it.
    """
    p = Path(raw_path)
    repo_res = repo_root.resolve()
    if trusted_dir is not None:
        if p.is_absolute():
            raise ValueError(f"suppressions_file '{raw_path}' must be relative to the trusted snapshot")
        trusted_res = trusted_dir.resolve()
        trusted_candidate = (trusted_res / p).resolve()
        if not trusted_candidate.is_relative_to(trusted_res):
            raise ValueError(
                f"suppressions_file '{raw_path}' resolves outside the trusted snapshot {trusted_res}"
            )
        return trusted_candidate
    cfg_res = config_dir.resolve()
    if p.is_absolute():
        return _resolve_within_repo(raw_path, repo_res)
    repo_candidate = (repo_res / p).resolve()
    if not repo_candidate.is_relative_to(repo_res):
        raise ValueError(
            f"suppressions_file '{raw_path}' resolves outside the analysed repo root {repo_res}"
        )
    if repo_candidate.is_file():
        return repo_candidate
    config_candidate = (cfg_res / p).resolve()
    if not config_candidate.is_relative_to(cfg_res):
        raise ValueError(
            f"suppressions_file '{raw_path}' resolves outside configuration directory {cfg_res}"
        )
    if config_candidate.is_file():
        return config_candidate
    return repo_candidate


def _resolve_component_include_dir(
    raw_inc: str,
    comp: ComponentSpec,
    ws_scope: WorkspaceScope,
    repo_root: Path,
) -> Path:
    """Resolve a component include directory against workspace root, component root, or repo root."""
    p = Path(raw_inc)
    if p.is_absolute():
        return _resolve_within_repo(raw_inc, repo_root)
    for base in (ws_scope.workspace_root, comp.abs_path, repo_root):
        cand = (base / p).resolve()
        if not cand.is_relative_to(repo_root):
            raise ValueError(
                f"include path '{raw_inc}' resolves outside the analysed repo root {repo_root}"
            )
        if cand.is_dir():
            return cand
    return _resolve_within_repo(raw_inc, repo_root)


def _resolve_compile_db_entry_path(
    entry: dict[str, Any],
    cc_path: Path,
    repo_root: Path,
    ws_scope: WorkspaceScope | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    """Resolve an entry's source file path and enforce repository/workspace containment."""
    raw_file = str(entry.get("file", "") or "").strip()
    if not raw_file:
        return None, entry
    repo_res = repo_root.resolve()
    ws_res = ws_scope.workspace_root.resolve() if (ws_scope is not None and ws_scope.configured) else None
    fp = Path(raw_file)
    copied = dict(entry)
    raw_dir = str(entry.get("directory", "") or "").strip()

    if fp.is_absolute():
        resolved_fp = fp.resolve()
        if not resolved_fp.is_relative_to(repo_res):
            raise ValueError(
                f"compile_commands entry '{raw_file}' resolves outside repository root {repo_res}"
            )
        if ws_res is not None:
            # Reject symlinks whose lexical path sits inside workspace_root but target escapes it
            try:
                fp.relative_to(ws_res)
                in_ws_lexically = True
            except ValueError:
                in_ws_lexically = False
            if in_ws_lexically and not resolved_fp.is_relative_to(ws_res):
                raise ValueError(
                    f"compile_commands entry '{raw_file}' escapes workspace root {ws_res} via symlink"
                )
        return resolved_fp, copied

    if raw_dir:
        dir_path = Path(raw_dir)
        if dir_path.is_absolute():
            dir_res = dir_path.resolve()
            if not dir_res.is_relative_to(repo_res):
                raise ValueError(
                    f"compile_commands directory '{raw_dir}' resolves outside repository root {repo_res}"
                )
            unresolved_fp = dir_path / fp
            resolved_fp = unresolved_fp.resolve()
        else:
            repo_dir_res = (repo_res / dir_path).resolve()
            cc_dir_res = (cc_path.parent / dir_path).resolve()
            repo_cand = (repo_res / dir_path / fp).resolve()
            cc_cand = (cc_path.parent / dir_path / fp).resolve()
            if (repo_res / dir_path / fp).is_symlink() or repo_cand.exists():
                unresolved_fp = repo_res / dir_path / fp
                resolved_fp = repo_cand
                if not repo_dir_res.is_relative_to(repo_res):
                    raise ValueError(
                        f"compile_commands directory '{raw_dir}' escapes repository root {repo_res}"
                    )
            elif (cc_path.parent / dir_path / fp).is_symlink() or cc_cand.exists():
                unresolved_fp = cc_path.parent / dir_path / fp
                resolved_fp = cc_cand
                if not cc_dir_res.is_relative_to(repo_res):
                    raise ValueError(
                        f"compile_commands directory '{raw_dir}' escapes repository root {repo_res}"
                    )
                copied["directory"] = str(cc_dir_res)
            else:
                unresolved_fp = repo_res / dir_path / fp
                resolved_fp = repo_cand
    else:
        repo_cand = (repo_res / fp).resolve()
        cc_cand = (cc_path.parent / fp).resolve()
        if (repo_res / fp).is_symlink() or repo_cand.exists():
            unresolved_fp = repo_res / fp
            resolved_fp = repo_cand
            copied["directory"] = str(repo_res)
        elif (cc_path.parent / fp).is_symlink() or cc_cand.exists():
            unresolved_fp = cc_path.parent / fp
            resolved_fp = cc_cand
            copied["directory"] = str(cc_path.parent.resolve())
        else:
            unresolved_fp = repo_res / fp
            resolved_fp = repo_cand

    if not resolved_fp.is_relative_to(repo_res):
        raise ValueError(
            f"compile_commands entry '{raw_file}' resolves outside repository root {repo_res}"
        )
    if ws_res is not None:
        norm_unresolved = Path(os.path.normpath(str(unresolved_fp)))
        try:
            norm_unresolved.relative_to(ws_res)
            in_ws_lexically = True
        except ValueError:
            in_ws_lexically = False
        if in_ws_lexically and not resolved_fp.is_relative_to(ws_res):
            raise ValueError(
                f"compile_commands entry '{raw_file}' escapes workspace root {ws_res} via symlink"
            )
    return resolved_fp, copied


def _load_compile_commands_tus(cc_path: Path, repo_root: Path) -> set[Path]:
    """Return resolved source file paths listed in a ``compile_commands.json`` database."""
    try:
        entries = json.loads(cc_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(entries, list):
        return set()
    tus: set[Path] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        resolved_fp, _ = _resolve_compile_db_entry_path(entry, cc_path, repo_root)
        if resolved_fp is not None:
            tus.add(resolved_fp)
    return tus


def _build_filtered_compile_db(
    cc_path: Path,
    repo_root: Path,
    ws_scope: WorkspaceScope,
    comp: ComponentSpec,
    *,
    allowed_files: set[Path] | None = None,
) -> tuple[list[dict[str, Any]], set[Path]]:
    """Filter ``cc_path`` entries to files inside ``comp`` (active, non-excluded)."""
    try:
        entries = json.loads(cc_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], set()
    if not isinstance(entries, list):
        return [], set()

    filtered: list[dict[str, Any]] = []
    matched_tus: set[Path] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        resolved_fp, normalized_entry = _resolve_compile_db_entry_path(
            entry, cc_path, repo_root, ws_scope=ws_scope
        )
        if resolved_fp is None:
            continue
        if ws_scope.outside_workspace(resolved_fp) or ws_scope.is_excluded(resolved_fp):
            continue
        if ws_scope.component_for_path(resolved_fp) != comp:
            continue
        if allowed_files is not None and resolved_fp not in allowed_files:
            continue
        filtered.append(normalized_entry)
        matched_tus.add(resolved_fp)
    return filtered, matched_tus


def _append_component_include_args(
    cmd: list[str],
    comp: ComponentSpec,
    config: CppcheckConfig,
    ws_scope: WorkspaceScope,
    effective_root: Path,
    extra_include_dirs: Sequence[str],
) -> None:
    """Append resolved ``-I`` flags for component fallback or non-compile-DB scans."""
    inc_sources = list(comp.include_dirs) if comp.include_dirs else list(config.include_dirs)
    for raw_inc in extra_include_dirs:
        if raw_inc not in inc_sources:
            inc_sources.append(raw_inc)
    for raw_inc in inc_sources:
        inc_path = _resolve_component_include_dir(raw_inc, comp, ws_scope, effective_root)
        if inc_path.is_dir() or raw_inc in extra_include_dirs:
            cmd.extend(["-I", inc_path.relative_to(effective_root).as_posix()])


def _expand_workspace_cpp_targets(
    targets: Sequence[str],
    repo_root: Path,
    ws_scope: WorkspaceScope,
) -> tuple[list[Path], bool]:
    """Expand targets under ``ws_scope`` and return ``(files, has_explicit_file_target)``."""
    ws_root_res = ws_scope.workspace_root.resolve()
    has_explicit_file = False
    if not targets:
        expanded = [str(d) for d in ws_scope.target_dirs()]
        files = collect_cpp_targets(expanded, repo_root) if expanded else []
        return ws_scope.filter(files), False

    expanded_dirs_or_files: list[str] = []
    seen: set[str] = set()
    for raw in targets:
        p = Path(raw)
        if not p.is_absolute():
            cwd_cand = (Path.cwd() / p).resolve()
            repo_cand = (repo_root / p).resolve()
            p = cwd_cand if cwd_cand.exists() else repo_cand
        else:
            p = p.resolve()
        if not p.exists():
            raise ValueError(f"target path does not exist: {raw}")
        if p.is_dir():
            if ws_root_res.is_relative_to(p):
                for comp_dir in ws_scope.target_dirs():
                    s = str(comp_dir)
                    if s not in seen:
                        seen.add(s)
                        expanded_dirs_or_files.append(s)
            elif p.is_relative_to(ws_root_res):
                for comp_dir in ws_scope.target_dirs():
                    comp_res = comp_dir.resolve()
                    if comp_res.is_relative_to(p):
                        s = str(comp_res)
                        if s not in seen:
                            seen.add(s)
                            expanded_dirs_or_files.append(s)
                    elif p.is_relative_to(comp_res):
                        s = str(p)
                        if s not in seen:
                            seen.add(s)
                            expanded_dirs_or_files.append(s)
        else:
            has_explicit_file = True
            s = str(p)
            if s not in seen:
                seen.add(s)
                expanded_dirs_or_files.append(s)

    files = collect_cpp_targets(expanded_dirs_or_files, repo_root) if expanded_dirs_or_files else []
    return ws_scope.filter(files), has_explicit_file


_SUPPRESS_COMMENT_RE = re.compile(r"\bcppcheck-suppress\b")
_DEVIATION_INLINE_RE = re.compile(r"\bDeviation:\s*(DEV-[A-Za-z0-9_.-]+)")


def _find_unapproved_inline_suppressions(
    target_files: Sequence[Path],
    repo_root: Path,
) -> list[CppcheckFinding]:
    """Return blocking findings for any ``cppcheck-suppress`` comment lacking a registry Deviation."""
    findings: list[CppcheckFinding] = []
    for fp in target_files:
        try:
            rel_file = fp.resolve().relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            rel_file = fp.name
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for idx, line in enumerate(lines, start=1):
            if not _SUPPRESS_COMMENT_RE.search(line):
                continue
            m = _DEVIATION_INLINE_RE.search(line)
            if m:
                entry, _ = verify_deviation(
                    m.group(1),
                    files=(rel_file,),
                    rule="SUPPRESSION-WITHOUT-DEVIATION",
                    worktree_roots=(repo_root,),
                )
                if entry is not None:
                    continue
            findings.append(
                CppcheckFinding(
                    file=rel_file,
                    line=idx,
                    id="SUPPRESSION-WITHOUT-DEVIATION",
                    severity="error",
                    message=(
                        "Inline cppcheck-suppress requires 'Deviation: DEV-<n>' on the same line "
                        "matching DEVPACK_DEVIATIONS"
                    ),
                )
            )
    return findings


_ABS_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*(?:"([^"]+)"|<([^>]+)>)')
_CPPCHECK_OOM_RE = re.compile(
    r"virtual memory exhausted|cannot allocate memory|out of memory|"
    r"terminate called after throwing an instance of 'std::bad_alloc'|allocation failed",
    re.IGNORECASE,
)


def _make_rlimit_preexec(max_as_bytes: int | None):
    """Return a preexec_fn clamping ``RLIMIT_AS`` to ``max_as_bytes``."""
    if max_as_bytes is None or max_as_bytes <= 0:
        return None

    def _apply() -> None:
        try:
            _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            if hard == resource.RLIM_INFINITY or hard > max_as_bytes:
                resource.setrlimit(resource.RLIMIT_AS, (max_as_bytes, max_as_bytes))
            else:
                target = min(max_as_bytes, hard)
                resource.setrlimit(resource.RLIMIT_AS, (target, hard))
        except (ValueError, OSError):
            pass

    return _apply


def _find_absolute_includes_in_files(
    target_files: Sequence[Path],
    repo_root: Path,
) -> list[CppcheckFinding]:
    """Reject absolute or ``/dev/*`` ``#include`` directives before launching cppcheck."""
    findings: list[CppcheckFinding] = []
    comment_rx = re.compile(r"//[^\n]*|/\*[\s\S]*?\*/")
    for fp in target_files:
        try:
            raw = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        stripped = comment_rx.sub(lambda m: "\n" * m.group(0).count("\n"), raw)
        rel_file = normalize_repo_path(str(fp), repo_root)
        for idx, line in enumerate(stripped.splitlines(), start=1):
            m = _ABS_INCLUDE_RE.match(line)
            if not m:
                continue
            inc = (m.group(1) or m.group(2) or "").strip()
            if not inc:
                continue
            is_abs = (
                inc.startswith(("/", "\\"))
                or bool(re.match(r"^[A-Za-z]:[/\\]", inc))
                or inc.startswith("/dev/")
                or "/dev/" in inc
            )
            if not is_abs:
                try:
                    resolved = (fp.parent / inc).resolve()
                    if str(resolved) == "/dev" or str(resolved).startswith("/dev/"):
                        is_abs = True
                except OSError:
                    pass
            if is_abs:
                findings.append(
                    CppcheckFinding(
                        file=rel_file,
                        line=idx,
                        id="ABSOLUTE-INCLUDE",
                        severity="error",
                        message=f"Absolute or device path #include '{inc}' is forbidden",
                    )
                )
    return findings


def _invoke_cppcheck_cmd(
    cmd: list[str],
    effective_root: Path,
    timeout_s: int,
    *,
    max_as_bytes: int = DEFAULT_MAX_AS_BYTES,
    target_hint: str = ".",
) -> tuple[list[CppcheckFinding], str, str]:
    """Execute a cppcheck command with timeout and ``RLIMIT_AS`` and return ``(findings, raw_xml, raw_stdout)``."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(effective_root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            preexec_fn=_make_rlimit_preexec(max_as_bytes),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            [
                CppcheckFinding(
                    file=target_hint,
                    line=1,
                    id="LINT-RESOURCE-LIMIT",
                    severity="error",
                    message=f"LINT-RESOURCE-LIMIT: cppcheck timed out after {timeout_s}s",
                )
            ],
            "",
            "",
        )
    except OSError as exc:
        raise RuntimeError(f"failed to execute cppcheck: {exc}") from exc

    raw_xml = proc.stderr or ""
    raw_stdout = proc.stdout or ""
    limit_sigs = {signal.SIGKILL, signal.SIGSEGV, signal.SIGABRT}
    if hasattr(signal, "SIGXCPU"):
        limit_sigs.add(signal.SIGXCPU)
    if (proc.returncode < 0 and (-proc.returncode) in limit_sigs) or _CPPCHECK_OOM_RE.search(
        raw_xml + "\n" + raw_stdout
    ):
        return (
            [
                CppcheckFinding(
                    file=target_hint,
                    line=1,
                    id="LINT-RESOURCE-LIMIT",
                    severity="error",
                    message=f"LINT-RESOURCE-LIMIT: cppcheck exceeded memory/resource limit (rc={proc.returncode})",
                )
            ],
            raw_xml,
            raw_stdout,
        )
    if "<results" not in raw_xml and proc.returncode != 0:
        detail = (raw_xml or raw_stdout).strip() or f"cppcheck exited with {proc.returncode}"
        raise RuntimeError(f"cppcheck execution failed: {detail}")

    return parse_cppcheck_xml(raw_xml, repo_root=effective_root), raw_xml, raw_stdout


def run_cppcheck_check(
    *,
    targets: Sequence[str] = (),
    repo_root: Path | None = None,
    config_path: Path | None = None,
    base_ref: str = "",
    baseline_path: Path | None = None,
    baseline_update: bool = False,
    cppcheck_bin: str = "cppcheck",
    fail_on_override: Sequence[str] | None = None,
    std_override: str | None = None,
    enable_override: Sequence[str] | None = None,
    extra_include_dirs: Sequence[str] = (),
    suppressions_override: str | None = None,
    extra_args_override: Sequence[str] = (),
    timeout_s: int = 300,
    components: str | Sequence[str] | None = None,
    all_components: bool = False,
    project_config_path: Path | None = None,
    trusted_suppressions_dir: Path | None = None,
    allow_empty: bool = False,
    max_as_bytes: int | None = None,
) -> CppcheckRunResult:
    """Run cppcheck on ``targets`` or git-changed files since ``base_ref``.

    ``trusted_suppressions_dir`` pins relative ``suppressions_file`` resolution to a trusted
    snapshot directory (see ``_resolve_suppressions_file``).
    """
    effective_root = (repo_root or Path.cwd()).resolve()
    config, resolved_cfg_path = load_cppcheck_config(config_path, effective_root)
    ws_scope = resolve_workspace_scope(
        effective_root,
        project_config_path=project_config_path,
        components=components,
        all_components=all_components,
    )

    if enable_override is not None:
        config.enable = tuple(enable_override)
    if std_override is not None:
        config.std = std_override
    if fail_on_override is not None:
        config.fail_on = validate_fail_on(fail_on_override)
    if extra_include_dirs:
        config.include_dirs = (*config.include_dirs, *tuple(extra_include_dirs))
    if suppressions_override is not None:
        config.suppressions_file = suppressions_override
    if extra_args_override:
        config.extra_args = validate_extra_args((*config.extra_args, *tuple(extra_args_override)))
    else:
        config.extra_args = validate_extra_args(config.extra_args)
    config.fail_on = validate_fail_on(config.fail_on)

    if not targets and not base_ref.strip() and not ws_scope.configured and not allow_empty:
        raise ValueError("provide one or more target paths or --base <ref>")

    warning = ws_scope.selection_warning()
    if warning:
        print(warning, file=sys.stderr)
    invalid = []
    for raw in targets:
        target = Path(raw)
        if not target.is_absolute():
            cwd_target = (Path.cwd() / target).resolve()
            target = cwd_target if cwd_target.exists() else effective_root / target
        if target.is_file() and (ws_scope.is_excluded(target) or
                                (ws_scope.configured and not ws_scope.is_in_active_components(target))):
            invalid.append(raw)
    if invalid:
        return CppcheckRunResult(exit_code=1, error_message="OUT_OF_SCOPE: " + ", ".join(invalid))

    target_files: list[Path]
    has_explicit_file_target = False
    if base_ref.strip():
        changed_files = discover_changed_cpp_files(effective_root, base_ref.strip())
        if targets:
            if ws_scope.configured:
                explicit_list, has_explicit_file_target = _expand_workspace_cpp_targets(
                    targets, effective_root, ws_scope
                )
                explicit = set(explicit_list)
            else:
                explicit = set(collect_cpp_targets(targets, effective_root))
            target_files = [p for p in changed_files if p in explicit]
        else:
            target_files = changed_files
        if ws_scope.configured:
            target_files = ws_scope.filter(target_files)
    elif ws_scope.configured:
        target_files, has_explicit_file_target = _expand_workspace_cpp_targets(
            targets, effective_root, ws_scope
        )
    else:
        target_files = collect_cpp_targets(targets, effective_root)

    if not target_files:
        no_changes = bool(base_ref.strip()) and not changed_files
        if allow_empty or no_changes:
            return CppcheckRunResult(exit_code=0, error_message="SKIP: no C/C++ changes", fail_on=config.fail_on)
        return CppcheckRunResult(exit_code=1, error_message="NO_FILES_CHECKED: no matching in-scope C/C++ files found", fail_on=config.fail_on)

    abs_inc_findings = _find_absolute_includes_in_files(target_files, effective_root)
    if abs_inc_findings:
        return CppcheckRunResult(
            exit_code=1,
            files_checked=len(target_files),
            findings=abs_inc_findings,
            blocking_findings=abs_inc_findings,
            fail_on=config.fail_on,
        )

    effective_timeout_s = config.timeout_s if config.timeout_s is not None else timeout_s
    env_timeout = os.environ.get("DEVPACK_CPPCHECK_TIMEOUT_S", "").strip()
    if env_timeout.isdigit() and int(env_timeout) >= 1 and timeout_s == 300:
        effective_timeout_s = int(env_timeout)
    effective_max_as = config.max_as_bytes
    env_as = os.environ.get("DEVPACK_LINT_MAX_AS_BYTES", "").strip()
    if env_as.isdigit() and int(env_as) >= 1024 * 1024:
        effective_max_as = int(env_as)
    if max_as_bytes is not None:
        effective_max_as = max_as_bytes

    resolved_bin = resolve_cppcheck_binary(cppcheck_bin)
    if resolved_bin is None:
        return CppcheckRunResult(
            exit_code=3,
            fail_on=config.fail_on,
            tool_missing=True,
            error_message=(
                f"TOOL_MISSING: cppcheck executable '{cppcheck_bin}' not found on PATH; "
                "install cppcheck (fail-closed static analysis)."
            ),
        )

    baseline_counts: Counter[tuple[str, str, str, str]] = Counter()
    if baseline_path is not None and not baseline_update:
        baseline_counts = load_baseline(baseline_path)

    cfg_base_dir = resolved_cfg_path.parent if resolved_cfg_path is not None else effective_root

    def _build_base_cmd() -> list[str]:
        cmd: list[str] = [resolved_bin]
        if config.enable:
            cmd.append(f"--enable={','.join(config.enable)}")
        if config.std:
            cmd.append(f"--std={config.std}")
        cmd.extend(["--xml", "--xml-version=2"])
        if config.suppressions_file:
            suppr_path = _resolve_suppressions_file(
                config.suppressions_file,
                cfg_base_dir,
                effective_root,
                trusted_dir=trusted_suppressions_dir,
            )
            if not suppr_path.is_file():
                raise ValueError(
                    f"cppcheck suppressions file does not exist: {config.suppressions_file}"
                )
            cmd.append(f"--suppressions-list={suppr_path}")
        if ws_scope.configured and "--suppress=missingIncludeSystem" not in config.extra_args:
            cmd.append("--suppress=missingIncludeSystem")
        for arg in config.extra_args:
            if arg not in cmd:
                cmd.append(arg)
        return cmd

    def _rel_str(fp: Path) -> str:
        try:
            return fp.relative_to(effective_root).as_posix()
        except ValueError:
            return str(fp)

    parsed: list[CppcheckFinding] = []
    raw_xml_parts: list[str] = []
    raw_stdout_parts: list[str] = []
    fallback_files_count = 0

    if ws_scope.configured:
        grouped: dict[str, tuple[ComponentSpec, list[Path]]] = {}
        for fp in target_files:
            comp = ws_scope.component_for_path(fp)
            if comp is None:
                continue
            if comp.name not in grouped:
                grouped[comp.name] = (comp, [])
            grouped[comp.name][1].append(fp)

        target_rel_set = {_rel_str(fp) for fp in target_files}
        subset_scan = bool(base_ref.strip()) or has_explicit_file_target
        restrict_compile_db = bool(base_ref.strip()) or bool(targets)

        for comp, comp_files in grouped.values():
            cc_path: Path | None = None
            if comp.compile_commands:
                cand_cc = _resolve_within_repo(comp.compile_commands, effective_root)
                if cand_cc.is_file():
                    cc_path = cand_cc

            if cc_path is not None:
                allowed_set = {fp.resolve() for fp in comp_files} if restrict_compile_db else None
                filtered_entries, cc_tus = _build_filtered_compile_db(
                    cc_path,
                    effective_root,
                    ws_scope,
                    comp,
                    allowed_files=allowed_set,
                )
                if filtered_entries:
                    with tempfile.TemporaryDirectory(prefix="devpack-cppcheck-") as cc_tmp_dir:
                        temp_cc = Path(cc_tmp_dir) / "compile_commands.json"
                        temp_cc.write_text(json.dumps(filtered_entries, indent=2), encoding="utf-8")
                        cmd = _build_base_cmd()
                        cmd.append(f"--project={temp_cc}")
                        comp_findings, r_xml, r_out = _invoke_cppcheck_cmd(
                            cmd,
                            effective_root,
                            effective_timeout_s,
                            max_as_bytes=effective_max_as,
                            target_hint=_rel_str(comp_files[0]),
                        )
                    raw_xml_parts.append(r_xml)
                    raw_stdout_parts.append(r_out)
                    for f in comp_findings:
                        if (
                            not ws_scope.outside_workspace(f.file)
                            and not ws_scope.is_excluded(f.file)
                            and ws_scope.component_for_path(f.file) == comp
                            and (not restrict_compile_db or f.file in target_rel_set)
                        ):
                            parsed.append(f)

                if subset_scan:
                    unmatched_files = [fp for fp in comp_files if fp.resolve() not in cc_tus]
                elif filtered_entries:
                    unmatched_files = [
                        fp
                        for fp in comp_files
                        if fp.suffix.lower() in SOURCE_SUFFIXES and fp.resolve() not in cc_tus
                    ]
                else:
                    unmatched_files = list(comp_files)

                if unmatched_files:
                    fallback_files_count += len(unmatched_files)
                    cmd = _build_base_cmd()
                    _append_component_include_args(
                        cmd, comp, config, ws_scope, effective_root, extra_include_dirs
                    )
                    cmd.extend(_rel_str(fp) for fp in unmatched_files)
                    comp_findings, r_xml, r_out = _invoke_cppcheck_cmd(
                        cmd,
                        effective_root,
                        effective_timeout_s,
                        max_as_bytes=effective_max_as,
                        target_hint=_rel_str(unmatched_files[0]),
                    )
                    raw_xml_parts.append(r_xml)
                    raw_stdout_parts.append(r_out)
                    for f in comp_findings:
                        if f.id == "LINT-RESOURCE-LIMIT" or (
                            not ws_scope.outside_workspace(f.file)
                            and not ws_scope.is_excluded(f.file)
                            and ws_scope.component_for_path(f.file) == comp
                        ):
                            parsed.append(f)
            else:
                cmd = _build_base_cmd()
                _append_component_include_args(
                    cmd, comp, config, ws_scope, effective_root, extra_include_dirs
                )
                cmd.extend(_rel_str(fp) for fp in comp_files)
                comp_findings, r_xml, r_out = _invoke_cppcheck_cmd(
                    cmd,
                    effective_root,
                    effective_timeout_s,
                    max_as_bytes=effective_max_as,
                    target_hint=_rel_str(comp_files[0]),
                )
                raw_xml_parts.append(r_xml)
                raw_stdout_parts.append(r_out)
                parsed.extend(comp_findings)

        raw_xml = "\n".join(part for part in raw_xml_parts if part)
        raw_stdout = "\n".join(part for part in raw_stdout_parts if part)
    else:
        cmd = _build_base_cmd()
        for raw_inc in config.include_dirs:
            inc_path = _resolve_within_repo(raw_inc, effective_root)
            if inc_path.is_dir() or raw_inc in extra_include_dirs:
                cmd.extend(["-I", inc_path.relative_to(effective_root).as_posix()])
        cmd.extend(_rel_str(fp) for fp in target_files)
        parsed, raw_xml, raw_stdout = _invoke_cppcheck_cmd(
            cmd,
            effective_root,
            effective_timeout_s,
            max_as_bytes=effective_max_as,
            target_hint=_rel_str(target_files[0]),
        )

    if "--inline-suppr" in config.extra_args:
        parsed.extend(_find_unapproved_inline_suppressions(target_files, effective_root))

    if baseline_update and baseline_path is not None:
        save_baseline(baseline_path, parsed)
        active_findings = list(parsed)
        baselined_findings: list[CppcheckFinding] = []
    elif baseline_path is not None:
        active_findings, baselined_findings = filter_findings_with_baseline(parsed, baseline_counts)
    else:
        active_findings = list(parsed)
        baselined_findings = []

    fail_set = {s.lower() for s in config.fail_on}
    blocking = [f for f in active_findings if f.severity.lower() in fail_set]
    non_blocking = [f for f in active_findings if f.severity.lower() not in fail_set]
    exit_code = 0 if (baseline_update or not blocking) else 1

    return CppcheckRunResult(
        exit_code=exit_code,
        files_checked=len(target_files),
        fallback_files_count=fallback_files_count,
        findings=active_findings,
        blocking_findings=blocking,
        non_blocking_findings=non_blocking,
        baselined_findings=baselined_findings,
        fail_on=config.fail_on,
        raw_xml=raw_xml,
        raw_stdout=raw_stdout,
    )


def format_result_json(result: CppcheckRunResult) -> str:
    """Format a ``CppcheckRunResult`` as a JSON string."""
    fail_set = {s.lower() for s in result.fail_on}
    payload: dict[str, Any] = {
        "tool": "cppcheck",
        "files_checked": result.files_checked,
        "fallback_files": result.fallback_files_count,
        "total_findings": len(result.findings),
        "blocking_findings": len(result.blocking_findings),
        "non_blocking_findings": len(result.non_blocking_findings),
        "suppressed_by_baseline": len(result.baselined_findings),
        "fail_on": list(result.fail_on),
        "tool_missing": result.tool_missing,
        "findings": [
            f.to_dict(blocking=(f.severity.lower() in fail_set)) for f in result.findings
        ],
    }
    if result.error_message:
        payload["error"] = result.error_message
    return json.dumps(payload, indent=2)


def format_result_text(result: CppcheckRunResult) -> str:
    """Format a ``CppcheckRunResult`` into human-readable summary lines."""
    if result.error_message:
        return result.error_message
    fail_set = {s.lower() for s in result.fail_on}
    lines: list[str] = []
    if result.findings:
        status = "FAIL" if result.blocking_findings else "PASS"
        lines.append(
            f"[{status}] cppcheck ({len(result.blocking_findings)} blocking, "
            f"{len(result.non_blocking_findings)} non-blocking)"
        )
        for finding in result.findings:
            loc = f"{finding.file}:{finding.line}" if finding.line else finding.file
            tag = "" if finding.severity.lower() in fail_set else "[non-blocking]"
            lines.append(
                f"  - [{finding.severity.upper()}][{finding.id}]{tag} {loc}: {finding.message}"
            )
    else:
        lines.append("[PASS] cppcheck")
    if result.fallback_files_count > 0:
        lines.append(
            f"Note: {result.fallback_files_count} file(s) missing from compile_commands.json "
            "analysed via include_dirs fallback."
        )
    lines.append(
        f"\nSummary: {result.files_checked} file(s) checked, {len(result.findings)} finding(s) "
        f"({len(result.blocking_findings)} blocking, {len(result.non_blocking_findings)} non-blocking, "
        f"{len(result.baselined_findings)} baselined)."
    )
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for ``enforcement/static/cppcheck_check.py``."""
    parser = argparse.ArgumentParser(
        prog="cppcheck_check.py",
        description="Run cppcheck static analysis on C/C++ targets or changed files vs --base.",
    )
    parser.add_argument("--allow-empty", action="store_true", help="Allow an intentionally empty scan")
    parser.add_argument(
        "targets",
        nargs="*",
        help="Target C/C++ file(s) or directory(ies) to analyze",
    )
    parser.add_argument(
        "--repo-root",
        "--repo",
        dest="repo_root",
        type=Path,
        default=None,
        help=(
            "Analysed repository root: relative include paths resolve here, paths are normalized "
            "against it, and preflight.toml is loaded from it when --config is absent (default: cwd)"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to preflight.toml containing [cppcheck] table",
    )
    parser.add_argument(
        "--base",
        default="",
        help="Git base ref to discover changed C/C++ files against",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Optional JSON baseline file suppressing pre-existing findings",
    )
    parser.add_argument(
        "--baseline-update",
        action="store_true",
        help="Write or update --baseline with the current findings",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit normalized JSON output",
    )
    parser.add_argument(
        "--cppcheck-bin",
        default=os.environ.get("DEVPACK_CPPCHECK_BIN", "cppcheck"),
        help="cppcheck executable name or path (default: cppcheck)",
    )
    parser.add_argument(
        "--fail-on",
        default=None,
        help="Comma-separated severities that fail the check (default: error,warning)",
    )
    parser.add_argument(
        "--std",
        default=None,
        help="Language standard passed to cppcheck (e.g. c++17)",
    )
    parser.add_argument(
        "--enable",
        default=None,
        help="Comma-separated cppcheck --enable checks",
    )
    parser.add_argument(
        "-I",
        "--include-dir",
        action="append",
        dest="include_dirs",
        default=[],
        help="Additional include directory passed to cppcheck (repeatable)",
    )
    parser.add_argument(
        "--suppressions-file",
        default=None,
        help="Optional cppcheck suppressions file",
    )
    parser.add_argument(
        "--trusted-suppressions-dir",
        type=Path,
        default=None,
        help=(
            "Trusted snapshot directory: relative suppressions_file is resolved only inside it "
            "(never from the working tree)"
        ),
    )
    parser.add_argument(
        "--extra-arg",
        action="append",
        dest="extra_args",
        default=[],
        help="Extra argument forwarded to cppcheck (repeatable)",
    )
    parser.add_argument(
        "--timeout-s",
        type=int,
        default=300,
        help="Timeout in seconds for cppcheck subprocess (default: 300)",
    )
    parser.add_argument(
        "--max-memory-mb",
        type=int,
        default=None,
        help="Address-space limit (RLIMIT_AS) in MiB for cppcheck subprocess (default: 2048)",
    )
    parser.add_argument(
        "--components",
        default=None,
        help="Comma-separated workspace component names to analyze (overrides [scan] defaults)",
    )
    parser.add_argument(
        "--all-components",
        action="store_true",
        help="Analyze all configured/discovered workspace components",
    )
    parser.add_argument(
        "--project-config",
        type=Path,
        default=None,
        help="Optional path to project.toml (default: <repo-root>/.devpack/project.toml)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for ``enforcement/static/cppcheck_check.py``."""
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.baseline_update and args.baseline is None:
        print("error: --baseline-update requires --baseline", file=sys.stderr)
        return 2
    if args.timeout_s < 1:
        print("error: --timeout-s must be >= 1", file=sys.stderr)
        return 2
    if args.max_memory_mb is not None and args.max_memory_mb < 64:
        print("error: --max-memory-mb must be >= 64", file=sys.stderr)
        return 2
    if args.project_config is not None and str(args.project_config) != "none" and not args.project_config.is_file():
        print(f"error: project config not found: {args.project_config}", file=sys.stderr)
        return 2

    fail_on_override = (
        [s.strip() for s in args.fail_on.split(",") if s.strip()]
        if args.fail_on is not None
        else None
    )
    enable_override = (
        [s.strip() for s in args.enable.split(",") if s.strip()]
        if args.enable is not None
        else None
    )

    try:
        result = run_cppcheck_check(
            targets=args.targets,
            repo_root=args.repo_root,
            config_path=args.config,
            base_ref=args.base,
            baseline_path=args.baseline,
            baseline_update=args.baseline_update,
            cppcheck_bin=args.cppcheck_bin,
            fail_on_override=fail_on_override,
            std_override=args.std,
            enable_override=enable_override,
            extra_include_dirs=args.include_dirs,
            suppressions_override=args.suppressions_file,
            extra_args_override=args.extra_args,
            timeout_s=args.timeout_s,
            components=args.components,
            all_components=bool(args.all_components),
            project_config_path=args.project_config,
            trusted_suppressions_dir=args.trusted_suppressions_dir,
            allow_empty=args.allow_empty,
            max_as_bytes=(args.max_memory_mb * 1024 * 1024) if args.max_memory_mb is not None else None,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if result.tool_missing:
        print(result.error_message, file=sys.stderr)
        if args.json:
            print(format_result_json(result))
        return 3

    if args.json:
        print(format_result_json(result))
    else:
        print(format_result_text(result))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
