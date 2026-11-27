#!/usr/bin/env python3
# Header: Fail-closed deterministic quality gate runner.
# Reads a TOML configuration of ordered [[check]] entries, executes each check
# with the repository root as working directory, writes a JSON receipt, and
# prints GATE_OK or GATE_FAIL <first failing check>.
#
# Exit codes:
#   0 - All required checks passed (or were skipped with an explicit deviation ID): GATE_OK.
#       Failing checks with required = false are reported as WARN and do not fail the gate.
#   1 - Configuration invalid/missing, zero checks, zero required checks, or a required check failed: GATE_FAIL.
"""Fail-closed deterministic gate runner.

Reads an ordered list of checks from a TOML file, executes each command inside
the target repository root, prints one status line per check followed by a final
``GATE_OK`` or ``GATE_FAIL <first failing check>`` line, and writes a JSON
receipt containing check names, statuses, exit codes, durations, git HEAD (when
available), and UTC timestamp.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import pathlib
import subprocess
import sys
import time
import tomllib
from typing import Any

_REPO_ROOT_STR = str(pathlib.Path(__file__).resolve().parents[2])
if _REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, _REPO_ROOT_STR)

from runner.redaction import redact_text  # noqa: E402
from runner.safe_git import GitSecurityError, run_git  # noqa: E402
from runner.workspace_scope import resolve_workspace_scope  # noqa: E402


def get_utc_timestamp() -> str:
    """Return the current UTC time formatted as an ISO-8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_git_head(repo_root: pathlib.Path) -> str | None:
    """Return the 40-character git HEAD SHA for ``repo_root``, or None if not a git repo."""
    try:
        proc = run_git(
            ["rev-parse", "HEAD"],
            repo_root=repo_root,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, GitSecurityError):
        return None
    if proc.returncode == 0:
        sha = proc.stdout.strip()
        return sha if sha else None
    return None


def write_receipt(
    receipt_path: pathlib.Path,
    gate_status: str,
    first_failed: str | None,
    repo_root: pathlib.Path,
    checks_results: list[dict[str, Any]],
    error: str | None = None,
    components: list[str] | None = None,
    scan_mode: str | None = None,
    unchecked_components: list[str] | None = None,
) -> None:
    """Write the gate execution receipt as formatted JSON to ``receipt_path``."""
    payload: dict[str, Any] = {
        "gate_status": gate_status,
        "status": gate_status,
        "first_failed_check": first_failed,
        "git_head": get_git_head(repo_root) if repo_root.is_dir() else None,
        "timestamp_utc": get_utc_timestamp(),
        "checks": checks_results,
    }
    if components is not None:
        payload["components"] = list(components)
    if scan_mode is not None:
        payload["scan_mode"] = scan_mode
    if unchecked_components is not None:
        payload["unchecked_components"] = list(unchecked_components)
    if error is not None:
        payload["error"] = error
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o666)
    with os.fdopen(fd, "w", encoding="utf-8") as receipt:
        receipt.write(json.dumps(payload, indent=2) + "\n")


def parse_checks_config(config_path: pathlib.Path) -> tuple[list[dict[str, Any]] | None, str | None, str | None]:
    """Load and validate ``[[check]]`` entries from a TOML configuration file.

    Returns a tuple ``(checks, fail_target, error_reason)``. On success,
    ``checks`` is a non-empty list of validated check dictionaries and the other
    two items are ``None``.
    """
    if not config_path.is_file():
        return None, "config", f"config file not found: {config_path.name}"

    try:
        raw_text = config_path.read_text(encoding="utf-8")
        data = tomllib.loads(raw_text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return None, "config", f"invalid TOML configuration: {exc}"

    raw_checks = data.get("check")
    if not isinstance(raw_checks, list) or len(raw_checks) == 0:
        return None, "config", "zero checks configured"

    validated: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for idx, entry in enumerate(raw_checks):
        if not isinstance(entry, dict):
            return None, "config", f"check entry #{idx + 1} is not a table"

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            return None, "config", f"check entry #{idx + 1} missing non-empty 'name'"
        name = name.strip()
        if name in seen_names:
            return None, name, f"duplicate check name '{name}'"
        seen_names.add(name)

        cmd = entry.get("cmd")
        if (
            not isinstance(cmd, list)
            or len(cmd) == 0
            or not all(isinstance(part, str) and len(part) > 0 for part in cmd)
        ):
            return None, name, "invalid 'cmd' (must be a non-empty list of non-empty strings)"

        timeout_s = entry.get("timeout_s", 300)
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
            return None, name, "invalid 'timeout_s' (must be a positive number)"

        required = entry.get("required", True)
        if not isinstance(required, bool):
            return None, name, "invalid 'required' (must be a boolean)"

        validated.append(
            {
                "name": name,
                "cmd": list(cmd),
                "timeout_s": float(timeout_s),
                "required": required,
            }
        )

    if not any(item["required"] for item in validated):
        return None, "config", "zero required checks configured"

    return validated, None, None


def redacted_output_tail(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    """Return the final sixty redacted lines of normal or timeout subprocess output."""
    pieces = [part.decode("utf-8", errors="replace") if isinstance(part, bytes) else part or "" for part in (stdout, stderr)]
    return "\n".join(redact_text("\n".join(pieces)).splitlines()[-60:])


def run_single_check(
    check: dict[str, Any],
    repo_root: pathlib.Path,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute one check command in ``repo_root`` and return its result record."""
    name: str = check["name"]
    cmd: list[str] = [part.replace("{pack_root}", _REPO_ROOT_STR) for part in check["cmd"]]
    timeout_s: float = check["timeout_s"]
    required: bool = check["required"]

    start_time = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        duration_s = round(time.monotonic() - start_time, 6)
    except FileNotFoundError:
        duration_s = round(time.monotonic() - start_time, 6)
        return {
            "name": name,
            "status": "FAIL",
            "exit_code": 127,
            "duration_s": duration_s,
            "required": required,
            "reason": f"command not found ({cmd[0]})",
        }
    except PermissionError:
        duration_s = round(time.monotonic() - start_time, 6)
        return {
            "name": name,
            "status": "FAIL",
            "exit_code": 126,
            "duration_s": duration_s,
            "required": required,
            "reason": f"command not executable ({cmd[0]})",
        }
    except subprocess.TimeoutExpired as exc:
        duration_s = round(time.monotonic() - start_time, 6)
        return {
            "name": name,
            "status": "FAIL",
            "exit_code": 124,
            "duration_s": duration_s,
            "required": required,
            "reason": f"timeout after {timeout_s:g}s",
            "output_tail": redacted_output_tail(exc.stdout, exc.stderr),
        }
    except OSError as exc:
        duration_s = round(time.monotonic() - start_time, 6)
        return {
            "name": name,
            "status": "FAIL",
            "exit_code": 127,
            "duration_s": duration_s,
            "required": required,
            "reason": f"os error ({exc})",
        }

    if proc.returncode != 0:
        return {
            "name": name,
            "status": "FAIL",
            "exit_code": proc.returncode,
            "duration_s": duration_s,
            "required": required,
            "reason": f"non-zero exit ({proc.returncode})",
            "output_tail": redacted_output_tail(proc.stdout, proc.stderr),
        }

    return {
        "name": name,
        "status": "PASS",
        "exit_code": 0,
        "duration_s": duration_s,
        "required": required,
    }


def workspace_has_cpp_files(scope: Any) -> bool:
    """Check active component trees without descending into excluded directories or symlinks."""
    suffixes = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}
    for directory in scope.target_dirs():
        for folder, directories, filenames in os.walk(directory, followlinks=False):
            base = pathlib.Path(folder)
            directories[:] = [name for name in directories if not scope.is_excluded(base / name)]
            if any(pathlib.Path(name).suffix.lower() in suffixes and scope.filter([base / name]) for name in filenames):
                return True
    return False


def build_arg_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser for the gate runner."""
    parser = argparse.ArgumentParser(
        prog="gate.py",
        description=(
            "Fail-closed quality gate runner. Executes ordered [[check]] entries from a TOML "
            "configuration, writes a JSON receipt, and exits 0 only on GATE_OK."
        ),
    )
    parser.add_argument(
        "--config",
        default="gate.toml",
        help="Path to the TOML gate configuration file (default: gate.toml).",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Path to the repository root directory used as cwd for checks (default: current directory).",
    )
    parser.add_argument(
        "--receipt",
        default="gate_receipt.json",
        help="Path to write the JSON gate receipt (default: gate_receipt.json).",
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="NAME",
        help="Skip the named check (may be specified multiple times; requires --deviation <ID>).",
    )
    parser.add_argument(
        "--deviation",
        default=None,
        metavar="ID",
        help="Deviation identifier authorizing --skip <NAME>.",
    )
    parser.add_argument(
        "--components",
        default=None,
        help="Comma-separated workspace component names forwarded via DEVPACK_COMPONENTS.",
    )
    parser.add_argument(
        "--all-components",
        action="store_true",
        help="Run workspace-aware checks across all components (sets DEVPACK_COMPONENTS=all).",
    )
    parser.add_argument("--allow-unowned", action="store_true", help="Allow changed workspace files outside all components in project checks.")
    parser.add_argument("--allow-empty", action="store_true", help="Allow an explicitly empty C/C++ change set.")
    parser.add_argument(
        "--project-config",
        default="",
        help="Optional path to project.toml (default: <repo>/.devpack/project.toml).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the fail-closed gate runner."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    repo_root = pathlib.Path(args.repo).resolve()
    config_path = pathlib.Path(args.config)
    if not config_path.is_absolute():
        project_candidate = repo_root / config_path
        pack_candidate = pathlib.Path(_REPO_ROOT_STR) / config_path
        if project_candidate.is_file():
            config_path = project_candidate
        elif pack_candidate.is_file():
            config_path = pack_candidate
    receipt_path = pathlib.Path(args.receipt)

    skip_names: list[str] = []
    for raw_skip in args.skip:
        for item in raw_skip.split(","):
            stripped = item.strip()
            if stripped:
                skip_names.append(stripped)

    deviation_id = args.deviation.strip() if isinstance(args.deviation, str) else ""

    if skip_names and not deviation_id:
        first_skip = skip_names[0]
        reason = "--skip requires --deviation <ID>"
        print(f"FAIL {first_skip}: {reason}")
        print(f"GATE_FAIL {first_skip}")
        write_receipt(
            receipt_path=receipt_path,
            gate_status="GATE_FAIL",
            first_failed=first_skip,
            repo_root=repo_root,
            checks_results=[
                {
                    "name": first_skip,
                    "status": "FAIL",
                    "exit_code": None,
                    "duration_s": 0.0,
                    "required": True,
                    "reason": reason,
                }
            ],
            error=reason,
        )
        return 1

    if not repo_root.is_dir():
        reason = "repository root directory does not exist"
        print(f"FAIL config: {reason}")
        print("GATE_FAIL config")
        write_receipt(
            receipt_path=receipt_path,
            gate_status="GATE_FAIL",
            first_failed="config",
            repo_root=repo_root,
            checks_results=[],
            error=reason,
        )
        return 1

    checks, fail_target, error_reason = parse_checks_config(config_path)
    if checks is None:
        target = fail_target or "config"
        reason = error_reason or "invalid configuration"
        print(f"FAIL {target}: {reason}")
        print(f"GATE_FAIL {target}")
        write_receipt(
            receipt_path=receipt_path,
            gate_status="GATE_FAIL",
            first_failed=target,
            repo_root=repo_root,
            checks_results=[],
            error=reason,
        )
        return 1

    proj_cfg = pathlib.Path(args.project_config) if args.project_config else None
    try:
        ws_scope = resolve_workspace_scope(
            repo_root,
            project_config_path=proj_cfg,
            components=args.components,
            all_components=bool(args.all_components),
        )
    except ValueError as exc:
        reason = str(exc)
        print(f"FAIL workspace_scope: {reason}")
        print("GATE_FAIL workspace_scope")
        write_receipt(
            receipt_path=receipt_path,
            gate_status="GATE_FAIL",
            first_failed="workspace_scope",
            repo_root=repo_root,
            checks_results=[],
            error=reason,
        )
        return 1

    unchecked_components = [
        comp.name for comp in ws_scope.all_components if comp not in ws_scope.active_components
    ]
    if unchecked_components:
        print(f"WARNING: {len(unchecked_components)} configured components not checked: {', '.join(unchecked_components)}")
    if ws_scope.configured and not workspace_has_cpp_files(ws_scope):
        if args.allow_empty:
            print("SKIP: no C/C++ changes")
        else:
            reason = "NO_FILES_CHECKED: active workspace contains no C/C++ files"
            print(f"FAIL workspace_scope: {reason}")
            print("GATE_FAIL workspace_scope")
            write_receipt(receipt_path, "GATE_FAIL", "workspace_scope", repo_root, [],
                          error=reason, components=[comp.name for comp in ws_scope.active_components],
                          scan_mode=ws_scope.scan_mode, unchecked_components=unchecked_components)
            return 1

    child_env: dict[str, str] | None = None
    if args.all_components:
        child_env = dict(os.environ)
        child_env["DEVPACK_COMPONENTS"] = "all"
    elif args.components is not None:
        child_env = dict(os.environ)
        child_env["DEVPACK_COMPONENTS"] = ",".join(c.name for c in ws_scope.active_components)

    if args.project_config:
        child_env = dict(os.environ) if child_env is None else child_env
        child_env["DEVPACK_PROJECT_CONFIG"] = str(pathlib.Path(args.project_config).resolve()) if args.project_config != "none" else "none"

    receipt_components = (
        [c.name for c in ws_scope.active_components]
        if (ws_scope.configured or args.components is not None or args.all_components)
        else None
    )
    receipt_scan_mode = (
        ws_scope.scan_mode
        if (ws_scope.configured or args.components is not None or args.all_components)
        else None
    )

    skip_set = set(skip_names)
    results: list[dict[str, Any]] = []
    first_failed: str | None = None

    for check in checks:
        name = check["name"]
        if name in skip_set:
            skip_status = f"SKIPPED deviation={deviation_id}"
            print(f"PASS {name} ({skip_status})")
            results.append(
                {
                    "name": name,
                    "status": skip_status,
                    "exit_code": None,
                    "duration_s": 0.0,
                    "required": check["required"],
                    "deviation": deviation_id,
                }
            )
            continue

        check_env = child_env
        # Skill graders infer roots from fixture paths. Pin pack-owned suites so a
        # vendoring project's policy cannot silently exclude their fixture files.
        selftest_config = pathlib.Path(_REPO_ROOT_STR) / "enforcement/gate/gate.example.toml"
        if config_path.resolve() == selftest_config.resolve() and name in {
            "skilltest-unit-tests", "skilltest-lint", "skill-unit-tests",
        }:
            check_env = dict(os.environ) if child_env is None else dict(child_env)
            check_env["DEVPACK_PROJECT_CONFIG"] = "none"
        if any(pathlib.Path(str(arg)).name == "project_checks.py" for arg in check["cmd"]):
            check = dict(check)
            check["cmd"] = list(check["cmd"])
            if args.allow_empty:
                check["cmd"].append("--allow-empty")
            if args.allow_unowned:
                check["cmd"].append("--allow-unowned")
        result = run_single_check(check, repo_root, env=check_env)
        results.append(result)
        if result.get("output_tail"):
            print(result["output_tail"])
        if result["status"] == "PASS":
            print(f"PASS {name}")
        elif not result.get("required", True):
            reason = result.get("reason", "check failed")
            result["status"] = "WARN"
            print(f"WARN {name}: {reason}")
        else:
            reason = result.get("reason", "check failed")
            print(f"FAIL {name}: {reason}")
            if first_failed is None:
                first_failed = name

    if first_failed is None:
        write_receipt(
            receipt_path=receipt_path,
            gate_status="GATE_OK",
            first_failed=None,
            repo_root=repo_root,
            checks_results=results,
            components=receipt_components,
            scan_mode=receipt_scan_mode,
            unchecked_components=unchecked_components,
        )
        print("GATE_OK")
        return 0

    write_receipt(
        receipt_path=receipt_path,
        gate_status="GATE_FAIL",
        first_failed=first_failed,
        repo_root=repo_root,
        checks_results=results,
        components=receipt_components,
        scan_mode=receipt_scan_mode,
        unchecked_components=unchecked_components,
    )
    print(f"GATE_FAIL {first_failed}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
