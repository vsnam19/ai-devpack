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
import shutil
import subprocess
import sys
from typing import Any, Sequence
import xml.etree.ElementTree as ET

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

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
DEFAULT_EXTRA_ARGS = ("--inline-suppr",)
BASELINE_SCHEMA_VERSION = 1

_TEMPLATE_5_RE = re.compile(r"^(.+?):(\d+):([^:]+):([^:]+):(.*)$")
_TEMPLATE_6_RE = re.compile(r"^(.+?):(\d+):(\d+):([^:]+):([^:]+):(.*)$")


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
            cfg.extra_args = _coerce_str_list(cppcheck_tbl["extra_args"], "extra_args")
        if "fail_on" in cppcheck_tbl:
            raw_fail = _coerce_str_list(cppcheck_tbl["fail_on"], "fail_on", allow_csv=True)
            cfg.fail_on = tuple(item.lower() for item in raw_fail)

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
    verify = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", f"{base_ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if verify.returncode != 0:
        err = verify.stderr.strip() or f"invalid base ref '{base_ref}'"
        raise ValueError(f"git base ref error: {err}")

    changed: set[str] = set()
    for cmd in (
        ["git", "-C", str(repo_root), "diff", "--name-only", base_ref],
        ["git", "-C", str(repo_root), "diff", "--name-only", f"{base_ref}..HEAD"],
        ["git", "-C", str(repo_root), "diff", "--cached", "--name-only"],
        ["git", "-C", str(repo_root), "ls-files", "--others", "--exclude-standard"],
    ):
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise ValueError(proc.stderr.strip() or "git diff failed")
        for line in proc.stdout.splitlines():
            cleaned = line.strip()
            if cleaned:
                changed.add(cleaned)

    files: list[Path] = []
    for rel in sorted(changed):
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


def _resolve_relative_to_base(raw_path: str, base_dir: Path, repo_root: Path) -> Path:
    """Resolve a configured relative path against ``base_dir`` or ``repo_root``."""
    p = Path(raw_path)
    if p.is_absolute():
        return p
    cand = (base_dir / p).resolve()
    if cand.exists():
        return cand
    return (repo_root / p).resolve()


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
) -> CppcheckRunResult:
    """Run cppcheck on ``targets`` or git-changed files since ``base_ref``."""
    effective_root = (repo_root or Path.cwd()).resolve()
    config, resolved_cfg_path = load_cppcheck_config(config_path, effective_root)

    if enable_override is not None:
        config.enable = tuple(enable_override)
    if std_override is not None:
        config.std = std_override
    if fail_on_override is not None:
        config.fail_on = tuple(item.lower() for item in fail_on_override)
    if extra_include_dirs:
        config.include_dirs = (*config.include_dirs, *tuple(extra_include_dirs))
    if suppressions_override is not None:
        config.suppressions_file = suppressions_override
    if extra_args_override:
        config.extra_args = (*config.extra_args, *tuple(extra_args_override))

    if not targets and not base_ref.strip():
        raise ValueError("provide one or more target paths or --base <ref>")

    target_files: list[Path]
    if base_ref.strip():
        changed_files = discover_changed_cpp_files(effective_root, base_ref.strip())
        if targets:
            explicit = set(collect_cpp_targets(targets, effective_root))
            target_files = [p for p in changed_files if p in explicit]
        else:
            target_files = changed_files
    else:
        target_files = collect_cpp_targets(targets, effective_root)

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

    if not target_files:
        if baseline_update and baseline_path is not None:
            save_baseline(baseline_path, [])
        return CppcheckRunResult(
            exit_code=0,
            files_checked=0,
            fail_on=config.fail_on,
        )

    cfg_base_dir = resolved_cfg_path.parent if resolved_cfg_path is not None else effective_root
    cmd: list[str] = [resolved_bin]
    if config.enable:
        cmd.append(f"--enable={','.join(config.enable)}")
    if config.std:
        cmd.append(f"--std={config.std}")
    if "--inline-suppr" not in config.extra_args:
        cmd.append("--inline-suppr")
    cmd.extend(["--xml", "--xml-version=2"])

    for raw_inc in config.include_dirs:
        inc_path = _resolve_relative_to_base(raw_inc, cfg_base_dir, effective_root)
        if inc_path.is_dir() or raw_inc in extra_include_dirs:
            try:
                inc_arg = inc_path.relative_to(effective_root).as_posix()
            except ValueError:
                inc_arg = str(inc_path)
            cmd.extend(["-I", inc_arg])

    if config.suppressions_file:
        suppr_path = _resolve_relative_to_base(config.suppressions_file, cfg_base_dir, effective_root)
        if not suppr_path.is_file():
            raise ValueError(f"cppcheck suppressions file does not exist: {config.suppressions_file}")
        cmd.append(f"--suppressions-list={suppr_path}")

    for arg in config.extra_args:
        if arg in ("--xml", "--xml-version=2") or arg.startswith("--template"):
            continue
        if arg not in cmd:
            cmd.append(arg)

    rel_targets: list[str] = []
    for fp in target_files:
        try:
            rel_targets.append(fp.relative_to(effective_root).as_posix())
        except ValueError:
            rel_targets.append(str(fp))
    cmd.extend(rel_targets)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(effective_root),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"cppcheck timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to execute cppcheck: {exc}") from exc

    raw_xml = proc.stderr or ""
    raw_stdout = proc.stdout or ""
    if "<results" not in raw_xml and proc.returncode != 0:
        detail = (raw_xml or raw_stdout).strip() or f"cppcheck exited with {proc.returncode}"
        raise RuntimeError(f"cppcheck execution failed: {detail}")

    parsed = parse_cppcheck_xml(raw_xml, repo_root=effective_root)
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
    if result.tool_missing:
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
        help="Repository root for normalizing paths and loading preflight.toml (default: cwd)",
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
