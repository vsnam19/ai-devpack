#!/usr/bin/env python3
# devpack_dispatch.py - Deterministic single-job worker dispatcher with policy, gate, scope, and test-lock checks.
#
# Usage:
#   python3 runner/devpack_dispatch.py --engine NAME --brief FILE --workdir DIR
#       [--allow GLOB ...] [--protected GLOB ...] [--base REF] [--gate-cmd CMD]
#       [--no-gate] [--runs-root DIR] [--engines FILE] [--project-config FILE]
#       [--isolation docker|local] [--allow-unsandboxed-local] [--credentials PATH]
#       [--agent-bin PATH] [--role worker] [--no-checklist] [--profile FILE]
#       [--qualification-file FILE] [--require-qualification]
#       [--allow-unqualified --deviation ID] [--dry-run] [--help]
#
# Runs ONE isolated agent job via runner/agent_exec.py, then runs deterministic
# enforcement checks: scope_check.py against --allow, test_lock.py against
# --protected, preflight_lint.py on changed C/C++ files, and the configured gate
# command. Enforces .devpack/project.toml data-class bounds (missing file =
# "internal"), container-by-default sandbox policy, and qualification / served
# model-change detection (.devpack/qualification.json) before and after running.
# Writes a v0.2 run directory and prints ONE compact JSON line to stdout.
#
# Exit codes:
#   0 - Agent status ok, gate ok, and scope ok (or --dry-run validated)
#   1 - Agent error/timeout, gate/lint/test-lock fail, scope violation, policy
#       refusal, or unqualified engine / model-change refusal
#   2 - Invalid arguments (including --allow-unqualified without --deviation),
#       missing brief/config, or non-git workdir
"""Single-job deterministic worker dispatcher for ai-devpack."""

from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass, replace
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Any, Sequence

def _submission_requested(argv: Sequence[str]) -> bool:
    """Select queue mode before importing host execution and credential code."""
    probe = argparse.ArgumentParser(add_help=False)
    probe.add_argument("--workdir", default=".")
    options, _ = probe.parse_known_args(argv)
    return ("--submit" in argv or os.environ.get("DEVPACK_BROKER") == "1"
            or (Path(options.workdir) / ".devpack/broker.json").exists())


if __name__ == "__main__" and _submission_requested(sys.argv[1:]):
    from devpack_submit import main as _submit_main
    raise SystemExit(_submit_main())

RUNNER_DIR = Path(__file__).resolve().parent
PACK_ROOT = RUNNER_DIR.parent
for _p in (str(PACK_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from enforcement.checks.scope_check import (  # noqa: E402
    get_changed_files,
    is_gate_protected_path,
    normalize_rel_posix_path,
)
from runner import agent_exec  # noqa: E402
from runner.agent_exec import (  # noqa: E402
    AgentRequest,
    AgentResult,
    EngineConfig,
    EnginesConfig,
    WORKDIR_SANDBOX_OPT_OUT_WARNING,
    check_data_class_allowed,
    check_local_isolation_allowed,
    compute_engine_config_hash,
    load_engines,
    load_project_config,
    load_qualification_data,
    load_quality_checklist,
    new_run_dir,
    redact_sensitive_text,
    resolve_engine,
    resolve_engine_profile_path,
    run_agent,
    save_qualification_data,
    update_run_json,
)
from runner.brief_lint import lint_brief_text  # noqa: E402
from runner.devpack_logs import load_run_dir, write_rendered_outputs  # noqa: E402
from runner.memory import memory_root_for, process_memory_proposals  # noqa: E402
from runner.redaction import Redactor, collect_credential_secrets  # noqa: E402
from runner.safe_git import (  # noqa: E402
    GitSecurityError,
    check_git_metadata_unchanged,
    run_git,
    snapshot_git_metadata,
)
from runner.workspace_scope import WorkspaceScope, parse_toml_with_arrays, resolve_workspace_scope  # noqa: E402

CPP_SUFFIXES = (".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh")
_LINT_SUMMARY_RE = re.compile(r"Summary:\s+\d+\s+file\(s\)\s+checked,\s+(\d+)\s+finding\(s\)\.")
_CPPCHECK_SUMMARY_RE = re.compile(
    r"Summary:\s+\d+\s+file\(s\)\s+checked,\s+\d+\s+finding\(s\)\s+\((\d+)\s+blocking"
)
DEFAULT_TRUSTED_GATE_TOML = (
    '[general]\nstd = "c++17"\n\n'
    '[cppcheck]\nenable = ["warning", "style", "performance", "portability"]\n'
    'std = "c++17"\ninclude_dirs = ["include"]\nextra_args = ["--inline-suppr"]\n'
    'fail_on = ["error", "warning"]\n'
)


@dataclass(frozen=True)
class TrustedGateConfig:
    """Immutable gate configuration snapshot stored outside the worker's directory."""

    config_path: Path
    source: str
    sha256: str
    protected_rel_paths: tuple[str, ...] = ()
    gate_toml_path: Path | None = None
    suppressions_rel_path: str | None = None
    suppressions_sha256: str | None = None
    project_gate_path: Path | None = None
    project_gate_bytes: bytes | None = None


def _git_show_regular_blob(workdir: Path, ref: str, rel_path: str) -> bytes | None:
    """Return raw bytes for ``ref:rel_path`` in ``workdir`` if it is a regular git blob."""
    if not ref or not (workdir / ".git").exists():
        return None
    ls_proc = run_git(
        ["ls-tree", ref, "--", rel_path],
        repo_root=workdir,
        timeout=30,
        check=False,
    )
    if ls_proc.returncode != 0 or not ls_proc.stdout.strip():
        return None
    first_line = ls_proc.stdout.splitlines()[0].strip()
    mode = first_line.split(None, 1)[0]
    if mode == "120000":
        raise ValueError(f"{rel_path} in base commit {ref} must not be a symlink")
    show_proc = run_git(
        ["show", f"{ref}:{rel_path}"],
        repo_root=workdir,
        timeout=30,
        text=False,
        check=False,
    )
    if show_proc.returncode != 0:
        return None
    return show_proc.stdout


def _cppcheck_suppressions_ref(cfg_bytes: bytes) -> str | None:
    """Return the normalised relative ``[cppcheck].suppressions_file`` from TOML bytes, or None.

    Absolute paths and paths that escape the config directory are rejected (None).
    """
    try:
        parsed = parse_toml_with_arrays(cfg_bytes.decode("utf-8", errors="replace"))
    except Exception:
        return None
    cpp_tbl = parsed.get("cppcheck")
    if not isinstance(cpp_tbl, dict):
        return None
    raw = cpp_tbl.get("suppressions_file")
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    if raw.startswith(("/", "\\")) or Path(raw).is_absolute():
        return None
    norm = normalize_rel_posix_path(raw)
    if not norm or ".." in PurePosixPath(norm).parts:
        return None
    return norm


def _extract_referenced_config_paths(cfg_bytes: bytes) -> tuple[str, ...]:
    """Extract repo-relative suppressions/plugin paths from TOML bytes for gate-touched protection."""
    refs: list[str] = []
    suppr = _cppcheck_suppressions_ref(cfg_bytes)
    if suppr is not None:
        refs.append(suppr)
    try:
        parsed = parse_toml_with_arrays(cfg_bytes.decode("utf-8", errors="replace"))
    except Exception:
        return tuple(refs)
    plugins_tbl = parsed.get("plugins")
    if isinstance(plugins_tbl, dict) and isinstance(plugins_tbl.get("paths"), list):
        for item in plugins_tbl["paths"]:
            if isinstance(item, str) and item.strip() and not Path(item.strip()).is_absolute():
                norm = normalize_rel_posix_path(item)
                if norm:
                    refs.append(norm)
    return tuple(dict.fromkeys(refs))


def _read_trusted_sibling(
    workdir: Path,
    base_ref: str,
    source: str,
    source_fs_dir: Path | None,
    rel_ref: str,
) -> bytes | None:
    """Read ``rel_ref`` from trusted sources only, in the order ``_resolve_suppressions_file`` uses.

    Repo root first: the git blob at ``rel_ref`` from the repository root in ``base_ref``.
    Then the config's own directory: ``base:<path>`` sources read the blob beside the config in
    ``base_ref``; external project configs and pack configs read beside the file in their trusted
    directory. The working tree is never read. Returns None when no trusted copy exists (including
    pre-run snapshots, which hold no copy).
    """
    if base_ref:
        root_bytes = _git_show_regular_blob(workdir, base_ref, posixpath.normpath(rel_ref))
        if root_bytes is not None:
            return root_bytes
    if source.startswith("base:") and base_ref:
        cfg_dir = posixpath.dirname(source[len("base:"):])
        return _git_show_regular_blob(workdir, base_ref, posixpath.normpath(posixpath.join(cfg_dir, rel_ref)))
    if source_fs_dir is not None:
        candidate = source_fs_dir / rel_ref
        if candidate.is_file() and not candidate.is_symlink():
            return candidate.read_bytes()
    return None


def resolve_trusted_gate_config(
    workdir: Path,
    temp_dir: Path,
    *,
    base_ref: str = "",
    project_config_path: Path | None = None,
    pack_root: Path = PACK_ROOT,
    pre_run_project_bytes: bytes | None = None,
) -> TrustedGateConfig:
    """Resolve lint/cppcheck gate config from a trusted source outside the agent's working tree."""
    wd_res = workdir.resolve()
    eff_base = base_ref.strip() or (_resolve_git_head(wd_res) or "")

    cfg_bytes: bytes | None = None
    source = ""
    source_fs_dir: Path | None = None
    out_name = "preflight.toml"

    if eff_base:
        base_preflight = _git_show_regular_blob(wd_res, eff_base, "preflight.toml")
        if base_preflight is not None:
            cfg_bytes = base_preflight
            source = "base:preflight.toml"
            out_name = "preflight.toml"

    if cfg_bytes is None and project_config_path is not None:
        p_cfg = Path(project_config_path)
        if not p_cfg.is_file():
            raise ValueError(f"Requested project config does not exist: {p_cfg}")
        p_res = p_cfg.resolve()
        if not p_res.is_relative_to(wd_res):
            cfg_bytes = p_res.read_bytes()
            source = "project_config"
            source_fs_dir = p_res.parent
            out_name = "project.toml"
        else:
            rel_proj = p_res.relative_to(wd_res).as_posix()
            if eff_base:
                base_proj = _git_show_regular_blob(wd_res, eff_base, rel_proj)
                if base_proj is not None:
                    cfg_bytes = base_proj
                    source = f"base:{rel_proj}"
                    out_name = "project.toml"
            if cfg_bytes is None and pre_run_project_bytes is not None:
                cfg_bytes = pre_run_project_bytes
                source = f"pre_run:{rel_proj}"
                out_name = "project.toml"

    if cfg_bytes is None and eff_base:
        base_proj = _git_show_regular_blob(wd_res, eff_base, ".devpack/project.toml")
        if base_proj is not None:
            cfg_bytes = base_proj
            source = "base:.devpack/project.toml"
            out_name = "project.toml"

    if cfg_bytes is None and pre_run_project_bytes is not None:
        cfg_bytes = pre_run_project_bytes
        source = "pre_run:.devpack/project.toml"
        out_name = "project.toml"

    if cfg_bytes is None:
        pack_example = pack_root / ".devpack" / "project.example.toml"
        if pack_example.is_file():
            cfg_bytes = pack_example.read_bytes()
            source = "pack:.devpack/project.example.toml"
            source_fs_dir = pack_example.parent
            out_name = "project.example.toml"
        else:
            cfg_bytes = DEFAULT_TRUSTED_GATE_TOML.encode("utf-8")
            source = "pack:default"
            out_name = "default_preflight.toml"

    out_path = temp_dir / out_name
    out_path.write_bytes(cfg_bytes)
    sha256 = hashlib.sha256(cfg_bytes).hexdigest()
    protected = _extract_referenced_config_paths(cfg_bytes)

    suppr_rel = _cppcheck_suppressions_ref(cfg_bytes)
    suppr_sha: str | None = None
    if suppr_rel is not None:
        suppr_bytes = _read_trusted_sibling(wd_res, eff_base, source, source_fs_dir, suppr_rel)
        if suppr_bytes is not None:
            suppr_dest = temp_dir / suppr_rel
            suppr_dest.parent.mkdir(parents=True, exist_ok=True)
            suppr_dest.write_bytes(suppr_bytes)
            suppr_sha = hashlib.sha256(suppr_bytes).hexdigest()

    gate_toml_path: Path | None = None
    if eff_base:
        base_gate = _git_show_regular_blob(wd_res, eff_base, "gate.toml")
        if base_gate is not None:
            gate_toml_path = temp_dir / "gate.toml"
            gate_toml_path.write_bytes(base_gate)

    return TrustedGateConfig(
        config_path=out_path,
        source=source,
        sha256=sha256,
        protected_rel_paths=protected,
        gate_toml_path=gate_toml_path,
        suppressions_rel_path=suppr_rel,
        suppressions_sha256=suppr_sha,
    )


def _run_subprocess(cmd: Sequence[str], cwd: Path, timeout_s: int = 300) -> tuple[int, str, str]:
    """Run a subprocess in ``cwd`` and return ``(returncode, stdout, stderr)``."""
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout_s}s: {' '.join(cmd)}"
    except OSError as exc:
        return 127, "", f"Failed to execute command {' '.join(cmd)}: {exc}"


def _resolve_git_head(workdir: Path) -> str | None:
    """Return the HEAD commit SHA for ``workdir``, or None if not a valid git repo."""
    try:
        proc = run_git(["rev-parse", "--verify", "HEAD"], repo_root=workdir, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha if sha else None


def parse_lint_finding_count(lint_output: str, returncode: int) -> int:
    """Extract the finding count from ``preflight_lint.py`` output."""
    match = _LINT_SUMMARY_RE.search(lint_output)
    if match:
        return int(match.group(1))
    bullet_lines = [line for line in lint_output.splitlines() if line.lstrip().startswith("- [")]
    if bullet_lines:
        return len(bullet_lines)
    return 0 if returncode == 0 else 1


def run_preflight_lint_on_changed(
    workdir: Path,
    changed_files: Sequence[str],
    pack_root: Path = PACK_ROOT,
    *,
    components: str | Sequence[str] | None = None,
    all_components: bool = False,
    project_config_path: Path | None = None,
    scope: WorkspaceScope | None = None,
    config_path: Path | None = None,
    base_ref: str = "",
) -> tuple[bool, int, str]:
    """Run ``preflight_lint.py`` on changed C/C++ files inside ``workdir``.

    Returns ``(passed, finding_count, combined_output)``.
    """
    active_scope = scope
    if active_scope is None:
        active_scope = resolve_workspace_scope(
            workdir,
            project_config_path=project_config_path,
            components=components,
            all_components=all_components,
        )

    scoped_changed = (
        active_scope.filter(list(changed_files)) if active_scope.configured else list(changed_files)
    )
    dropped = (
        [rel for rel in changed_files if rel not in set(scoped_changed)]
        if active_scope.configured
        else []
    )
    lint_targets = [
        str(workdir / rel)
        for rel in scoped_changed
        if rel.lower().endswith(CPP_SUFFIXES) and (workdir / rel).is_file()
    ]
    if not lint_targets:
        if dropped:
            msg = "[out_of_scope_changes]\n" + "\n".join(
                f"OUT_OF_SCOPE_CHANGE: {p} is outside active component scope" for p in dropped
            )
            return False, 0, msg
        return True, 0, ""

    preflight_script = pack_root / "enforcement" / "lint" / "preflight_lint.py"
    if not preflight_script.is_file():
        return False, 1, f"Missing preflight linter: {preflight_script}"

    with contextlib.ExitStack() as stack:
        effective_cfg = config_path
        if effective_cfg is None:
            tmp_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="devpack-lint-cfg-")))
            effective_cfg = resolve_trusted_gate_config(
                workdir,
                tmp_dir,
                base_ref=base_ref,
                project_config_path=project_config_path,
                pack_root=pack_root,
            ).config_path

        cmd: list[str] = [
            "python3",
            str(preflight_script),
            "--no-cache",
            "--repo-root",
            str(workdir),
            "--config",
            str(effective_cfg),
        ]
        if project_config_path is not None:
            cmd.extend(["--project-config", str(project_config_path)])
        if active_scope.configured:
            if active_scope.scan_mode == "all":
                cmd.append("--all-components")
            elif active_scope.active_components:
                cmd.extend(["--components", ",".join(c.name for c in active_scope.active_components)])
        cmd.extend(lint_targets)

        rc, out, err = _run_subprocess(cmd, workdir, timeout_s=300)
    combined = "\n".join(part for part in (out.strip(), err.strip()) if part).strip()
    findings = parse_lint_finding_count(combined, rc)
    if dropped:
        drop_msg = "[out_of_scope_changes]\n" + "\n".join(
            f"OUT_OF_SCOPE_CHANGE: {p} is outside active component scope" for p in dropped
        )
        combined = f"{combined}\n{drop_msg}".strip()
        return False, findings, combined
    return rc == 0, findings, combined


def parse_cppcheck_blocking_count(cppcheck_output: str, returncode: int) -> int:
    """Extract the blocking finding count from ``cppcheck_check.py`` output."""
    match = _CPPCHECK_SUMMARY_RE.search(cppcheck_output)
    if match:
        return int(match.group(1))
    return 0 if returncode == 0 else 1


def run_cppcheck_on_changed(
    workdir: Path,
    changed_files: Sequence[str],
    pack_root: Path = PACK_ROOT,
    *,
    components: str | Sequence[str] | None = None,
    all_components: bool = False,
    project_config_path: Path | None = None,
    scope: WorkspaceScope | None = None,
    config_path: Path | None = None,
    base_ref: str = "",
    trusted_snapshot_dir: Path | None = None,
) -> tuple[bool, int, str]:
    """Run ``cppcheck_check.py`` on changed C/C++ files inside ``workdir``.

    ``trusted_snapshot_dir`` is the directory holding the trusted config snapshot; relative
    ``suppressions_file`` entries are resolved only there. When ``config_path`` is None the
    temporary snapshot directory is used.

    Returns ``(passed, blocking_finding_count, combined_output)``.
    """
    active_scope = scope
    if active_scope is None:
        active_scope = resolve_workspace_scope(
            workdir,
            project_config_path=project_config_path,
            components=components,
            all_components=all_components,
        )

    scoped_changed = (
        active_scope.filter(list(changed_files)) if active_scope.configured else list(changed_files)
    )
    dropped = (
        [rel for rel in changed_files if rel not in set(scoped_changed)]
        if active_scope.configured
        else []
    )
    static_targets = [
        str(workdir / rel)
        for rel in scoped_changed
        if rel.lower().endswith(CPP_SUFFIXES) and (workdir / rel).is_file()
    ]
    if not static_targets:
        if dropped:
            msg = "[out_of_scope_changes]\n" + "\n".join(
                f"OUT_OF_SCOPE_CHANGE: {p} is outside active component scope" for p in dropped
            )
            return False, 0, msg
        return True, 0, ""

    cppcheck_script = pack_root / "enforcement" / "static" / "cppcheck_check.py"
    if not cppcheck_script.is_file():
        return False, 1, f"Missing cppcheck static analyzer: {cppcheck_script}"

    with contextlib.ExitStack() as stack:
        effective_cfg = config_path
        snapshot_dir = trusted_snapshot_dir
        if effective_cfg is None:
            tmp_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="devpack-cppcheck-cfg-")))
            effective_cfg = resolve_trusted_gate_config(
                workdir,
                tmp_dir,
                base_ref=base_ref,
                project_config_path=project_config_path,
                pack_root=pack_root,
            ).config_path
            snapshot_dir = tmp_dir

        cmd: list[str] = [
            "python3",
            str(cppcheck_script),
            "--repo-root",
            str(workdir),
            "--config",
            str(effective_cfg),
        ]
        if snapshot_dir is not None:
            cmd.extend(["--trusted-suppressions-dir", str(snapshot_dir)])
        if project_config_path is not None:
            cmd.extend(["--project-config", str(project_config_path)])
        if active_scope.configured:
            if active_scope.scan_mode == "all":
                cmd.append("--all-components")
            elif active_scope.active_components:
                cmd.extend(["--components", ",".join(c.name for c in active_scope.active_components)])
        cmd.extend(static_targets)

        rc, out, err = _run_subprocess(cmd, workdir, timeout_s=300)
    combined = "\n".join(part for part in (out.strip(), err.strip()) if part).strip()
    blocking = parse_cppcheck_blocking_count(combined, rc)
    if dropped:
        drop_msg = "[out_of_scope_changes]\n" + "\n".join(
            f"OUT_OF_SCOPE_CHANGE: {p} is outside active component scope" for p in dropped
        )
        combined = f"{combined}\n{drop_msg}".strip()
        return False, blocking, combined
    return rc == 0, blocking, combined


def _inject_quality_checklist(brief_text: str, pack_root: Path = PACK_ROOT) -> str:
    """Append ``runner/quality_checklist.md`` (read fresh from disk) to ``brief_text``."""
    checklist = load_quality_checklist(pack_root)
    if not checklist:
        return brief_text
    if "## Code Quality Checklist" in brief_text or checklist in brief_text:
        return brief_text
    return brief_text.rstrip() + "\n\n" + checklist + "\n"


def _resolve_gate_command(
    explicit_gate_cmd: str,
    engines_cfg: EnginesConfig | None,
    workdir: Path,
    run_dir: Path,
    pack_root: Path = PACK_ROOT,
    trusted_gate_toml: Path | None = None,
) -> tuple[list[str] | None, int]:
    """Determine the deterministic gate command and timeout for ``workdir``."""
    timeout_s = engines_cfg.gate_timeout_s if engines_cfg is not None else 300
    if explicit_gate_cmd.strip():
        return shlex.split(explicit_gate_cmd), timeout_s

    if trusted_gate_toml is not None and trusted_gate_toml.is_file():
        return [
            "python3",
            str(pack_root / "enforcement" / "gate" / "gate.py"),
            "--config",
            str(trusted_gate_toml),
            "--repo",
            str(workdir),
            "--receipt",
            str(run_dir / "steps" / "02-gate-receipt.json"),
        ], timeout_s

    if engines_cfg is not None and list(engines_cfg.gate_cmd) != list(agent_exec.DEFAULT_GATE_CMD):
        return list(engines_cfg.gate_cmd), timeout_s
    return agent_exec.resolve_project_gate_command(workdir), timeout_s


def _render_run_dir_quietly(
    run_dir: Path,
    credentials_path: Path | None = None,
) -> None:
    """Generate ``transcript.md`` and ``index.html`` for ``run_dir`` without failing dispatch."""
    try:
        redactor = Redactor(credentials_path=credentials_path)
        record = load_run_dir(run_dir, redactor)
        write_rendered_outputs(record, run_dir)
    except (OSError, ValueError):
        pass


def _emit_json_line(payload: dict[str, Any]) -> None:
    """Print exactly one compact JSON line to stdout."""
    print(json.dumps(payload, separators=(",", ":")))


def _control_snapshot(workdir: Path) -> dict[str, str]:
    """Fingerprint control files independently of Git ignore and allowed glob rules."""
    root = workdir / ".devpack"
    result = {}
    if root.is_symlink():
        return {".devpack": "symlink:" + os.readlink(root)}
    if root.exists():
        result[".devpack"] = "directory"
        for path in root.rglob("*"):
            name = path.relative_to(workdir).as_posix()
            if path.is_symlink():
                result[name] = "symlink:" + os.readlink(path)
            elif path.is_file():
                result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                result[name] = "directory"
    return result


def check_qualification_pre_run(
    engine: EngineConfig,
    qual_path: Path,
    *,
    enforce_if_missing: bool = False,
) -> tuple[bool, str]:
    """Verify engine qualification status and config hash before launching an agent."""
    if not qual_path.is_file():
        if enforce_if_missing:
            return (
                False,
                f"Refused: engine '{engine.name}' is unqualified "
                f"(missing {qual_path.name}; run eval/run_eval.py --record-qualification "
                "or pass --allow-unqualified --deviation <ID>).",
            )
        return True, ""

    qual_data = load_qualification_data(qual_path)
    entry = qual_data.get("engines", {}).get(engine.name)
    if not isinstance(entry, dict):
        return (
            False,
            f"Refused: engine '{engine.name}' is unqualified "
            f"(not recorded in {qual_path.name}; run eval/run_eval.py --record-qualification "
            "or pass --allow-unqualified --deviation <ID>).",
        )
    status = str(entry.get("status", "")).strip().lower()
    if status != "qualified":
        reason = str(entry.get("unqualified_reason") or f"status='{status or 'unknown'}'")
        return (
            False,
            f"Refused: engine '{engine.name}' is unqualified ({reason}).",
        )

    current_hash = compute_engine_config_hash(engine)
    recorded_hash = str(entry.get("config_hash") or "")
    if recorded_hash and recorded_hash != current_hash:
        entry["status"] = "unqualified"
        entry["unqualified_reason"] = "engine config hash changed"
        entry["updated"] = datetime.now(timezone.utc).isoformat()
        save_qualification_data(qual_path, qual_data)
        return (
            False,
            f"Refused: engine '{engine.name}' config changed since qualification (marked unqualified).",
        )
    return True, ""


def check_model_change_post_run(
    engine: EngineConfig,
    agent_res: AgentResult,
    qual_path: Path,
) -> tuple[bool, str]:
    """Compare served stream init model ID/digest against qualification.json."""
    observed_model = str(agent_res.init_model or "").strip()
    observed_digest = str(agent_res.init_digest or "").strip()
    if not qual_path.is_file() or not observed_model:
        return True, ""

    qual_data = load_qualification_data(qual_path)
    entry = qual_data.get("engines", {}).get(engine.name)
    if not isinstance(entry, dict) or str(entry.get("status", "")).lower() != "qualified":
        return True, ""

    expected_model = str(entry.get("model_id") or "").strip()
    expected_digest = str(entry.get("model_digest") or "").strip()
    model_diff = bool(expected_model and observed_model != expected_model)
    digest_diff = bool(expected_digest and observed_digest and observed_digest != expected_digest)
    if not model_diff and not digest_diff:
        return True, ""

    reason = (
        f"served model changed from '{expected_model}' to '{observed_model}'"
        if model_diff
        else f"served model digest changed from '{expected_digest}' to '{observed_digest}'"
    )
    entry["status"] = "unqualified"
    entry["unqualified_reason"] = reason
    entry["detected_model_id"] = observed_model
    if observed_digest:
        entry["detected_model_digest"] = observed_digest
    entry["updated"] = datetime.now(timezone.utc).isoformat()
    save_qualification_data(qual_path, qual_data)
    return (
        False,
        f"Refused: engine '{engine.name}' {reason} (marked unqualified; re-run qualification).",
    )


def dispatch_job(
    *,
    engine_name: str,
    brief_path: Path,
    workdir: Path,
    allow_globs: Sequence[str] = (),
    protected_globs: Sequence[str] = ("tests/**",),
    base_ref: str = "",
    gate_cmd: str = "",
    no_gate: bool = False,
    runs_root: Path | None = None,
    engines_config_path: Path | None = None,
    project_config_path: Path | None = None,
    isolation: str = "docker",
    allow_unsandboxed_local: bool = False,
    credentials_path: Path | None = None,
    agent_bin: str = "claude",
    role: str = "worker",
    no_checklist: bool = False,
    profile_path: Path | None = None,
    qualification_file: Path | None = None,
    allow_unqualified: bool = False,
    deviation_id: str = "",
    require_qualification: bool = False,
    dry_run: bool = False,
    docker_bin: str = "docker",
    image_tag: str = agent_exec.DOCKER_IMAGE_TAG,
    pack_root: Path = PACK_ROOT,
    memory_root: Path | None = None,
    memory_enabled: bool | None = None,
    memory_top_k: int | None = None,
    memory_budget_tokens: int | None = None,
    brief_lint_findings: Sequence[dict[str, Any]] | None = None,
    components: str | Sequence[str] | None = None,
    all_components: bool = False,
    task_id: str = "",
) -> tuple[int, dict[str, Any]]:
    """Execute one dispatched worker job with policy checks, gate, scope, and test-lock.

    Returns ``(exit_code, result_payload)``.
    """
    cleaned_deviation = deviation_id.strip()
    if allow_unqualified and not cleaned_deviation:
        raise ValueError("--allow-unqualified requires --deviation <ID>")
    if project_config_path is not None and not Path(project_config_path).is_file():
        raise ValueError(f"Requested project config does not exist: {project_config_path}")

    project_cfg = load_project_config(workdir, project_config_path)
    ws_scope = resolve_workspace_scope(
        workdir,
        project_config_path=project_config_path,
        components=components,
        all_components=all_components,
    )
    if ws_scope.configured and allow_globs:
        ws_scope = ws_scope.with_allow_globs(allow_globs)
    active_component_names = [c.name for c in ws_scope.active_components]
    if ws_scope.selection_warning():
        print(ws_scope.selection_warning(), file=sys.stderr)

    effective_engines_path = engines_config_path
    if effective_engines_path is None:
        effective_engines_path = Path(project_cfg["engines_config"])

    engines_cfg: EnginesConfig | None = None
    if effective_engines_path.is_file():
        engines_cfg = load_engines(effective_engines_path)

    engine: EngineConfig = resolve_engine(engine_name, engines_cfg)

    effective_runs_root = runs_root
    if effective_runs_root is None:
        raw_runs = Path(str(project_cfg.get("runs_root", "runs")))
        effective_runs_root = raw_runs if raw_runs.is_absolute() else (workdir / raw_runs)

    head_sha = _resolve_git_head(workdir)
    if head_sha is None:
        raise ValueError(f"Workdir must be a git worktree with at least one commit: {workdir}")
    effective_base = base_ref.strip() or head_sha

    brief_raw = brief_path.read_text(encoding="utf-8")
    effective_brief_lint = (
        list(brief_lint_findings)
        if brief_lint_findings is not None
        else lint_brief_text(
            brief_raw,
            repo_root=workdir,
            engine=engine.name,
            engines_config=effective_engines_path,
            author="human",
        )
    )
    prompt_text = brief_raw if no_checklist else _inject_quality_checklist(brief_raw, pack_root)
    resolved_profile = (
        profile_path
        if profile_path is not None
        else resolve_engine_profile_path(engine.name, workdir, pack_root)
    )
    qual_path = qualification_file or (workdir / ".devpack" / "qualification.json")
    enforce_qual = require_qualification or bool(project_cfg.get("require_qualification", False))
    engine_cfg_hash = compute_engine_config_hash(engine)

    # Check data-class, sandbox, and qualification policy before launching any agent
    data_ok, data_reason = check_data_class_allowed(engine, str(project_cfg["data_class"]))
    iso_ok, iso_msg = check_local_isolation_allowed(
        isolation,
        allow_unsandboxed_local=allow_unsandboxed_local,
        workdir=workdir,
        project_config=project_cfg,
    )
    qual_ok, qual_reason = check_qualification_pre_run(
        engine,
        qual_path,
        enforce_if_missing=enforce_qual,
    )
    qual_deviation_logged = False
    if not qual_ok and allow_unqualified:
        qual_deviation_logged = True
        sys.stderr.write(
            f"WARNING: deviation {cleaned_deviation}: allowing unqualified engine '{engine.name}': {qual_reason}\n"
        )

    pre_run_project_bytes: bytes | None = None
    default_proj = workdir / ".devpack" / "project.toml"
    if project_config_path is None and default_proj.is_file() and not default_proj.is_symlink():
        pre_run_project_bytes = default_proj.read_bytes()

    with tempfile.TemporaryDirectory(prefix="devpack-gate-cfg-") as gate_cfg_tmp:
        trusted_cfg = resolve_trusted_gate_config(
            workdir,
            Path(gate_cfg_tmp),
            base_ref=effective_base,
            project_config_path=project_config_path,
            pack_root=pack_root,
            pre_run_project_bytes=pre_run_project_bytes,
        )
        if (not no_gate and not gate_cmd.strip()
                and (engines_cfg is None or list(engines_cfg.gate_cmd) == list(agent_exec.DEFAULT_GATE_CMD))
                and str(project_cfg.get("gate_config", "")).strip()):
            project_gate_cmd = agent_exec.resolve_project_gate_command(workdir, project_config_path=project_config_path)
            original_gate = Path(project_gate_cmd[project_gate_cmd.index("--config") + 1])
            if original_gate.is_symlink():
                raise ValueError("Project gate_config must be a regular file, not a symlink")
            original_bytes = original_gate.read_bytes()
            snapshot_gate = Path(gate_cfg_tmp) / "project-gate.toml"
            snapshot_gate.write_bytes(original_bytes)
            gate_refs = trusted_cfg.protected_rel_paths
            if original_gate.is_relative_to(workdir):
                gate_refs = (*gate_refs, original_gate.relative_to(workdir).as_posix())
            trusted_cfg = replace(trusted_cfg, gate_toml_path=snapshot_gate,
                                  protected_rel_paths=gate_refs,
                                  project_gate_path=original_gate, project_gate_bytes=original_bytes)
        return _dispatch_job_with_trusted_cfg(
            trusted_cfg=trusted_cfg,
            engine=engine,
            engines_cfg=engines_cfg,
            project_cfg=project_cfg,
            ws_scope=ws_scope,
            active_component_names=active_component_names,
            effective_runs_root=effective_runs_root,
            effective_base=effective_base,
            brief_path=brief_path,
            task_id=(task_id or brief_path.stem),
            brief_raw=brief_raw,
            effective_brief_lint=effective_brief_lint,
            prompt_text=prompt_text,
            resolved_profile=resolved_profile,
            qual_path=qual_path,
            enforce_qual=enforce_qual,
            engine_cfg_hash=engine_cfg_hash,
            data_ok=data_ok,
            data_reason=data_reason,
            iso_ok=iso_ok,
            iso_msg=iso_msg,
            qual_ok=qual_ok,
            qual_reason=qual_reason,
            qual_deviation_logged=qual_deviation_logged,
            cleaned_deviation=cleaned_deviation,
            allow_unqualified=allow_unqualified,
            dry_run=dry_run,
            workdir=workdir,
            allow_globs=allow_globs,
            protected_globs=protected_globs,
            gate_cmd=gate_cmd,
            no_gate=no_gate,
            project_config_path=project_config_path,
            isolation=isolation,
            allow_unsandboxed_local=allow_unsandboxed_local,
            credentials_path=credentials_path,
            agent_bin=agent_bin,
            docker_bin=docker_bin,
            image_tag=image_tag,
            role=role,
            pack_root=pack_root,
            memory_root=memory_root,
            memory_enabled=memory_enabled,
            memory_top_k=memory_top_k,
            memory_budget_tokens=memory_budget_tokens,
        )


def _dispatch_job_with_trusted_cfg(
    *,
    trusted_cfg: TrustedGateConfig,
    engine: EngineConfig,
    engines_cfg: EnginesConfig | None,
    project_cfg: dict[str, Any],
    ws_scope: WorkspaceScope,
    active_component_names: list[str],
    effective_runs_root: Path,
    effective_base: str,
    brief_path: Path,
    task_id: str,
    brief_raw: str,
    effective_brief_lint: list[dict[str, Any]],
    prompt_text: str,
    resolved_profile: Path | None,
    qual_path: Path,
    enforce_qual: bool,
    engine_cfg_hash: str,
    data_ok: bool,
    data_reason: str,
    iso_ok: bool,
    iso_msg: str,
    qual_ok: bool,
    qual_reason: str,
    qual_deviation_logged: bool,
    cleaned_deviation: str,
    allow_unqualified: bool,
    dry_run: bool,
    workdir: Path,
    allow_globs: Sequence[str],
    protected_globs: Sequence[str],
    gate_cmd: str,
    no_gate: bool,
    project_config_path: Path | None,
    isolation: str,
    allow_unsandboxed_local: bool,
    credentials_path: Path | None,
    agent_bin: str,
    docker_bin: str,
    image_tag: str,
    role: str,
    pack_root: Path,
    memory_root: Path | None,
    memory_enabled: bool | None,
    memory_top_k: int | None,
    memory_budget_tokens: int | None,
) -> tuple[int, dict[str, Any]]:
    """Run one dispatched worker job using the pre-snapshotted ``trusted_cfg``."""
    if dry_run:
        if not data_ok:
            sys.stderr.write(data_reason + "\n")
            return 1, {
                "run_dir": "",
                "status": "refused",
                "gate": "skipped",
                "scope": "ok",
                "changed_files": [],
                "final_text_tail": "",
                "cost_usd": None if engine.is_local else 0.0,
                "error": data_reason,
            }
        if not iso_ok:
            sys.stderr.write(iso_msg + "\n")
            return 1, {
                "run_dir": "",
                "status": "refused",
                "gate": "skipped",
                "scope": "ok",
                "changed_files": [],
                "final_text_tail": "",
                "cost_usd": None if engine.is_local else 0.0,
                "error": iso_msg,
            }
        if not qual_ok and not allow_unqualified:
            sys.stderr.write(qual_reason + "\n")
            return 1, {
                "run_dir": "",
                "status": "refused",
                "gate": "skipped",
                "scope": "ok",
                "changed_files": [],
                "final_text_tail": "",
                "cost_usd": None if engine.is_local else 0.0,
                "error": qual_reason,
            }
        plan = {
            "mode": "dry-run",
            "engine": engine.name,
            "engine_class": engine.engine_class,
            "engine_config_hash": engine_cfg_hash,
            "data_class": project_cfg["data_class"],
            "isolation": isolation,
            "qualification": "deviation" if qual_deviation_logged else "ok",
            "workdir": str(workdir),
            "base_ref": effective_base,
            "allow": list(allow_globs),
            "protected": list(protected_globs),
            "brief": str(brief_path),
            "profile_path": str(resolved_profile) if resolved_profile else "",
            "components": active_component_names,
            "scan_mode": ws_scope.scan_mode,
            "unchecked_components": ws_scope.unchecked_components(),
        }
        if qual_deviation_logged:
            plan["deviation_id"] = cleaned_deviation
        _emit_json_line(plan)
        return 0, plan

    run_dir = new_run_dir(
        effective_runs_root,
        kind="dispatch",
        task=brief_path.stem,
        engines=[engine.name],
        outcome="running",
    )

    update_run_json(
        run_dir,
        isolation=isolation,
        components=active_component_names,
        scan_mode=ws_scope.scan_mode,
        unchecked_components=ws_scope.unchecked_components(),
    )

    if not data_ok:
        sys.stderr.write(data_reason + "\n")
        update_run_json(
            run_dir,
            outcome="refused",
            data_class=project_cfg["data_class"],
            refusal_reason=data_reason,
            finished=datetime.now(timezone.utc).isoformat(),
        )
        payload = {
            "run_dir": str(run_dir),
            "status": "refused",
            "gate": "skipped",
            "scope": "ok",
            "changed_files": [],
            "final_text_tail": "",
            "cost_usd": None if engine.is_local else 0.0,
            "error": data_reason,
        }
        _render_run_dir_quietly(run_dir, credentials_path=credentials_path)
        _emit_json_line(payload)
        return 1, payload

    if not iso_ok:
        sys.stderr.write(iso_msg + "\n")
        refused_iso: dict[str, Any] = {
            "outcome": "refused",
            "isolation": isolation,
            "refusal_reason": iso_msg,
            "finished": datetime.now(timezone.utc).isoformat(),
        }
        if WORKDIR_SANDBOX_OPT_OUT_WARNING in iso_msg:
            refused_iso["sandbox_warning"] = iso_msg
            refused_iso["ignored_workdir_sandbox_opt_out"] = True
        update_run_json(run_dir, **refused_iso)
        payload = {
            "run_dir": str(run_dir),
            "status": "refused",
            "gate": "skipped",
            "scope": "ok",
            "changed_files": [],
            "final_text_tail": "",
            "cost_usd": None if engine.is_local else 0.0,
            "error": iso_msg,
        }
        _render_run_dir_quietly(run_dir, credentials_path=credentials_path)
        _emit_json_line(payload)
        return 1, payload

    if iso_msg:
        warn_iso: dict[str, Any] = {"sandbox_warning": iso_msg}
        if WORKDIR_SANDBOX_OPT_OUT_WARNING in iso_msg:
            warn_iso["ignored_workdir_sandbox_opt_out"] = True
        update_run_json(run_dir, **warn_iso)

    if not qual_ok and not allow_unqualified:
        sys.stderr.write(qual_reason + "\n")
        update_run_json(
            run_dir,
            outcome="refused",
            qualification_status="unqualified",
            engine_config_hash=engine_cfg_hash,
            refusal_reason=qual_reason,
            finished=datetime.now(timezone.utc).isoformat(),
        )
        payload = {
            "run_dir": str(run_dir),
            "status": "refused",
            "gate": "skipped",
            "scope": "ok",
            "changed_files": [],
            "final_text_tail": "",
            "cost_usd": None if engine.is_local else 0.0,
            "error": qual_reason,
            "qualification": "unqualified",
        }
        _render_run_dir_quietly(run_dir, credentials_path=credentials_path)
        _emit_json_line(payload)
        return 1, payload

    if qual_deviation_logged:
        update_run_json(
            run_dir,
            allow_unqualified=True,
            deviation_id=cleaned_deviation,
            qualification_warning=qual_reason,
            engine_config_hash=engine_cfg_hash,
        )

    req = AgentRequest(
        engine=engine,
        prompt=prompt_text,
        workdir=workdir,
        run_dir=run_dir,
        step_name=f"01-{role}-{engine.name}",
        isolation=isolation,
        allow_unsandboxed_local=allow_unsandboxed_local,
        timeout_s=engine.timeout_s,
        credentials_path=credentials_path,
        agent_bin=agent_bin,
        docker_bin=docker_bin,
        image_tag=image_tag,
        role=role,
        step_index=1,
        profile_path=resolved_profile,
        memory_enabled=memory_enabled,
        memory_root=memory_root,
        memory_top_k=memory_top_k,
        memory_budget_tokens=memory_budget_tokens,
        memory_config=engines_cfg.memory if engines_cfg is not None else None,
        memory_task_text=brief_raw,
        memory_task_id=task_id,
    )
    control_before = _control_snapshot(workdir)
    git_snap = snapshot_git_metadata(workdir)
    update_run_json(
        run_dir,
        git_config_sha256=git_snap.config_sha256,
        git_hooks_listing=git_snap.hooks_entries_for_log(),
    )
    agent_res = run_agent(req)

    git_ok, git_tamper_reason = check_git_metadata_unchanged(git_snap)
    if not git_ok:
        tamper_msg = f"SCOPE_VIOLATION: {git_tamper_reason}"
        sys.stderr.write(tamper_msg + "\n")
        update_run_json(
            run_dir,
            task=brief_path.stem,
            outcome="fail",
            gate="skipped",
            scope="violation",
            scope_violation="git_metadata_tampered",
            git_metadata_tampered=True,
            refusal_reason=git_tamper_reason,
            finished=datetime.now(timezone.utc).isoformat(),
        )
        _render_run_dir_quietly(run_dir, credentials_path=credentials_path)
        cost_val_tamper: float | None = None if engine.is_local else round(float(agent_res.cost_usd or 0.0), 6)
        payload = {
            "run_dir": str(run_dir),
            "status": agent_res.status,
            "gate": "skipped",
            "scope": "violation",
            "scope_violation": "git_metadata_tampered",
            "git_metadata_tampered": True,
            "scope_detail": tamper_msg,
            "changed_files": [],
            "final_text_tail": agent_res.final_text[-800:],
            "cost_usd": cost_val_tamper,
            "error": git_tamper_reason,
        }
        _emit_json_line(payload)
        return 1, payload

    model_ok, model_reason = check_model_change_post_run(engine, agent_res, qual_path)
    if not model_ok:
        if allow_unqualified:
            qual_deviation_logged = True
            sys.stderr.write(
                f"WARNING: deviation {cleaned_deviation}: model changed on '{engine.name}': {model_reason}\n"
            )
            update_run_json(
                run_dir,
                allow_unqualified=True,
                deviation_id=cleaned_deviation,
                model_changed=True,
                served_model_id=agent_res.init_model,
                engine_config_hash=engine_cfg_hash,
            )
        else:
            sys.stderr.write(model_reason + "\n")
            update_run_json(
                run_dir,
                outcome="refused",
                qualification_status="unqualified",
                model_changed=True,
                served_model_id=agent_res.init_model,
                engine_config_hash=engine_cfg_hash,
                refusal_reason=model_reason,
                finished=datetime.now(timezone.utc).isoformat(),
            )
            payload = {
                "run_dir": str(run_dir),
                "status": "refused",
                "gate": "skipped",
                "scope": "ok",
                "changed_files": [],
                "final_text_tail": agent_res.final_text[-800:],
                "cost_usd": None if engine.is_local else round(float(agent_res.cost_usd or 0.0), 6),
                "error": model_reason,
                "qualification": "unqualified",
                "model_changed": True,
                "served_model_id": agent_res.init_model,
            }
            _render_run_dir_quietly(run_dir, credentials_path=credentials_path)
            _emit_json_line(payload)
            return 1, payload
    control_after = _control_snapshot(workdir)
    control_changes = sorted(name for name in control_before.keys() | control_after.keys()
                             if control_before.get(name) != control_after.get(name))

    changed_files, changed_err = get_changed_files(workdir, effective_base)
    if changed_files is None:
        changed_files = []

    changed_files = sorted(set(changed_files) | set(control_changes))
    gate_touched = sorted(
        {
            name
            for name in changed_files
            if not (name == ".devpack" or name.startswith(".devpack/"))
            and is_gate_protected_path(name, trusted_cfg.protected_rel_paths)
        }
        | {
            name
            for name in control_changes
            if is_gate_protected_path(name, trusted_cfg.protected_rel_paths)
        }
    )
    if trusted_cfg.project_gate_path is not None and (
        trusted_cfg.project_gate_path.is_symlink()
        or not trusted_cfg.project_gate_path.is_file()
        or trusted_cfg.project_gate_path.read_bytes() != trusted_cfg.project_gate_bytes
    ):
        original = trusted_cfg.project_gate_path
        gate_label = original.relative_to(workdir).as_posix() if original.is_relative_to(workdir) else original.name
        gate_touched = sorted(set(gate_touched) | {gate_label})
    out_of_scope_changes: list[str] = []
    scoped_changed_files: list[str] = list(changed_files)
    if ws_scope.configured:
        scoped_changed_files = ws_scope.filter(list(changed_files))
        scoped_set = set(scoped_changed_files)
        out_of_scope_changes = [p for p in changed_files if p not in scoped_set]

    # Scope check
    scope_status = "ok"
    scope_msgs: list[str] = []
    if changed_err:
        scope_status = "violation"
        scope_msgs.append(f"SCOPE_ERROR: {changed_err}")
    else:
        if gate_touched:
            scope_status = "violation"
            for name in gate_touched:
                scope_msgs.append(
                    f"SCOPE_VIOLATION: {name} is gate-touched "
                    "(gate/lint/cppcheck configuration cannot be modified by workers)"
                )
        if ws_scope.configured:
            outside_ws = [p for p in changed_files if ws_scope.outside_workspace(p)]
            outside_active = [
                p
                for p in changed_files
                if not ws_scope.outside_workspace(p) and not ws_scope.is_in_active_components(p)
            ]
            if outside_ws or outside_active:
                scope_status = "violation"
                for name in outside_ws:
                    scope_msgs.append(
                        f"SCOPE_VIOLATION: {name} is outside workspace root ({ws_scope.workspace_root_rel})"
                    )
                active_label = ", ".join(active_component_names) if active_component_names else "(none)"
                for name in outside_active:
                    scope_msgs.append(
                        f"SCOPE_VIOLATION: {name} is outside active components ({active_label})"
                    )
        if scope_status == "ok" and allow_globs:
            scope_script = pack_root / "enforcement" / "checks" / "scope_check.py"
            scope_cmd = [
                "python3",
                str(scope_script),
                "--repo",
                str(workdir),
                "--base",
                effective_base,
                "--allow",
                *allow_globs,
            ]
            if ws_scope.configured:
                if ws_scope.scan_mode == "all":
                    scope_cmd.append("--all-components")
                else:
                    scope_cmd.extend(["--components", ",".join(c.name for c in ws_scope.active_components)])
            for extra_gate in trusted_cfg.protected_rel_paths:
                scope_cmd.extend(["--gate-config-file", extra_gate])
            if project_config_path is not None:
                scope_cmd.extend(["--project-config", str(project_config_path)])
            s_rc, s_out, s_err = _run_subprocess(
                scope_cmd,
                workdir,
                timeout_s=60,
            )
            s_combined = (s_out + "\n" + s_err).strip()
            if s_combined:
                scope_msgs.append(s_combined)
            if s_rc != 0:
                scope_status = "violation"

    if control_changes and not any(
        name == ".devpack" or name.startswith(".devpack/") for name in gate_touched
    ):
        scope_status = "violation"
        for name in control_changes:
            scope_msgs.append(f"SCOPE_VIOLATION: {name} is broker control data")
    scope_out = "\n".join(part for part in scope_msgs if part).strip()

    # Gate + test_lock + preflight_lint + cppcheck
    gate_status = "skipped"
    lint_findings = 0
    static_findings = 0
    static_clean = True
    test_lock_status = "skipped"
    gate_logs: list[str] = []

    if agent_res.status == "ok":
        if no_gate:
            gate_status = "skipped"
        else:
            gate_ok = True
            if out_of_scope_changes:
                gate_ok = False
                gate_logs.append(
                    "[out_of_scope_changes]\n"
                    + "\n".join(
                        f"OUT_OF_SCOPE_CHANGE: {p} is outside active component scope"
                        for p in out_of_scope_changes
                    )
                )

            # 1. Protected test lock check (always --no-deviation for agent-produced commits)
            if protected_globs:
                tl_script = pack_root / "enforcement" / "checks" / "test_lock.py"
                tl_rc, tl_out, tl_err = _run_subprocess(
                    [
                        "python3",
                        str(tl_script),
                        "--repo",
                        str(workdir),
                        "--base",
                        effective_base,
                        "--no-deviation",
                        "--protected",
                        *protected_globs,
                    ],
                    workdir,
                    timeout_s=60,
                )
                tl_combined = (tl_out + "\n" + tl_err).strip()
                if tl_combined:
                    gate_logs.append(f"[test_lock]\n{tl_combined}")
                if tl_rc != 0:
                    gate_ok = False
                    test_lock_status = "fail"
                else:
                    test_lock_status = "ok"

            # 2. Preflight lint on changed C/C++ files (using trusted pre-run config)
            lint_ok, lint_findings, lint_out = run_preflight_lint_on_changed(
                workdir,
                scoped_changed_files,
                pack_root=pack_root,
                project_config_path=project_config_path,
                scope=ws_scope,
                config_path=trusted_cfg.config_path,
                base_ref=effective_base,
            )
            if lint_out:
                gate_logs.append(f"[preflight_lint]\n{lint_out}")
            if not lint_ok:
                gate_ok = False

            # 2b. Static analysis (cppcheck) on changed C/C++ files (using trusted pre-run config)
            static_clean, static_findings, static_out = run_cppcheck_on_changed(
                workdir,
                scoped_changed_files,
                pack_root=pack_root,
                project_config_path=project_config_path,
                scope=ws_scope,
                config_path=trusted_cfg.config_path,
                base_ref=effective_base,
                trusted_snapshot_dir=trusted_cfg.config_path.parent,
            )
            if static_out:
                gate_logs.append(f"[cppcheck]\n{static_out}")
            if not static_clean:
                gate_ok = False

            # 3. Configured gate command (if any)
            resolved_gate_cmd, gate_timeout_s = _resolve_gate_command(
                gate_cmd,
                engines_cfg,
                workdir,
                run_dir,
                pack_root=pack_root,
                trusted_gate_toml=trusted_cfg.gate_toml_path,
            )
            if resolved_gate_cmd is not None:
                g_rc, g_out, g_err = _run_subprocess(resolved_gate_cmd, workdir, timeout_s=gate_timeout_s)
                g_combined = (g_out + "\n" + g_err).strip()
                if g_combined:
                    gate_logs.append(f"[gate_cmd]\n{g_combined}")
                if g_rc != 0:
                    gate_ok = False

            gate_status = "ok" if gate_ok else "fail"
            if scope_out and scope_status != "ok":
                gate_logs.append(f"[scope_check]\n{scope_out}")

            steps_dir = run_dir / "steps"
            steps_dir.mkdir(parents=True, exist_ok=True)
            gate_text = "\n\n".join(gate_logs).strip() or f"GATE_{gate_status.upper()}"
            cred_secrets = collect_credential_secrets(credentials_path)
            redacted_gate = redact_sensitive_text(
                gate_text,
                extra_secrets=cred_secrets,
                credentials_path=credentials_path,
            )
            (steps_dir / "02-gate.txt").write_text(redacted_gate + "\n", encoding="utf-8")

    passed = agent_res.status == "ok" and gate_status == "ok" and scope_status == "ok"

    mem_proposal_summary: dict[str, Any] = {"accepted": [], "pending": [], "rejected": []}
    if agent_res.status == "ok" and gate_status == "ok" and scope_status == "ok":
        effective_mem_root = memory_root if memory_root is not None else memory_root_for(workdir)
        stop_report_path = workdir / "STOP_REPORT.md"
        stop_report_text = (
            stop_report_path.read_text(encoding="utf-8", errors="replace")
            if stop_report_path.is_file()
            else ""
        )
        prop_res = process_memory_proposals(
            effective_mem_root,
            agent_res.final_text,
            source=f"run:{run_dir.name}",
            task_id=task_id,
            stop_report_text=stop_report_text,
        )
        mem_proposal_summary = prop_res.to_dict()

    outcome = "pass" if passed else ("timeout" if agent_res.status == "timeout" else "fail")
    suppressions_meta: dict[str, Any] = {}
    if trusted_cfg.suppressions_rel_path is not None:
        suppressions_meta["gate_suppressions_path"] = trusted_cfg.suppressions_rel_path
        suppressions_meta["gate_suppressions_sha256"] = trusted_cfg.suppressions_sha256
    run_extra: dict[str, Any] = {}
    if ws_scope.configured or out_of_scope_changes:
        run_extra["out_of_scope_changes"] = out_of_scope_changes
    if gate_touched:
        run_extra["gate_touched"] = gate_touched
    update_run_json(
        run_dir,
        task=brief_path.stem,
        outcome=outcome,
        gate=gate_status,
        scope=scope_status,
        test_lock=test_lock_status,
        lint_findings=lint_findings,
        static_findings=static_findings,
        static_findings_first_attempt=static_findings,
        static_clean_final=static_clean,
        brief_lint=effective_brief_lint,
        data_class=project_cfg["data_class"],
        memory_ids=list(agent_res.memory_ids),
        memory_hashes=dict(agent_res.memory_hashes),
        memory_tokens=int(agent_res.memory_tokens),
        memory_proposals=mem_proposal_summary,
        components=active_component_names,
        scan_mode=ws_scope.scan_mode,
        unchecked_components=ws_scope.unchecked_components(),
        gate_config_source=trusted_cfg.source,
        gate_config_sha256=trusted_cfg.sha256,
        **suppressions_meta,
        finished=datetime.now(timezone.utc).isoformat(),
        **run_extra,
    )
    _render_run_dir_quietly(run_dir, credentials_path=credentials_path)

    cost_val: float | None = None if engine.is_local else round(float(agent_res.cost_usd or 0.0), 6)
    payload: dict[str, Any] = {
        "run_dir": str(run_dir),
        "status": agent_res.status,
        "gate": gate_status,
        "scope": scope_status,
        "changed_files": changed_files,
        "final_text_tail": agent_res.final_text[-800:],
        "cost_usd": cost_val,
        "tokens_in": agent_res.tokens_in,
        "tokens_out": agent_res.tokens_out,
        "duration_s": agent_res.duration_s,
        "tool_protocol_errors": agent_res.tool_protocol_errors,
        "test_lock": test_lock_status,
        "lint_findings": lint_findings,
        "static_findings": static_findings,
        "static_clean": static_clean,
        "memory_ids": list(agent_res.memory_ids),
        "memory_hashes": dict(agent_res.memory_hashes),
        "memory_tokens": int(agent_res.memory_tokens),
        "components": active_component_names,
        "scan_mode": ws_scope.scan_mode,
        "unchecked_components": ws_scope.unchecked_components(),
        "gate_config_source": trusted_cfg.source,
        "gate_config_sha256": trusted_cfg.sha256,
        **suppressions_meta,
    }
    if ws_scope.configured or out_of_scope_changes:
        payload["out_of_scope_changes"] = out_of_scope_changes
    if gate_touched:
        payload["gate_touched"] = gate_touched
    if any(mem_proposal_summary.values()):
        payload["memory_proposals"] = mem_proposal_summary
    if agent_res.error:
        payload["error"] = agent_res.error
    if scope_out and scope_status != "ok":
        payload["scope_detail"] = scope_out
    if agent_res.profile_sha256:
        payload["profile_sha256"] = agent_res.profile_sha256
        payload["profile_version"] = agent_res.profile_version
    if agent_res.init_model:
        payload["served_model_id"] = agent_res.init_model
    if qual_deviation_logged:
        payload["deviation_id"] = cleaned_deviation
        payload["allow_unqualified"] = True

    _emit_json_line(payload)
    return (0 if passed else 1), payload


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for ``runner/devpack_dispatch.py``."""
    parser = argparse.ArgumentParser(
        prog="devpack_dispatch.py",
        description=(
            "Dispatch one worker job through agent_exec with data-class refusal, "
            "sandbox enforcement, deterministic gate, preflight lint, scope_check, "
            "and test_lock."
        ),
    )
    parser.add_argument("--submit", action="store_true", help="Submit through the host broker")
    parser.add_argument(
        "--engine",
        required=True,
        help="Engine name from engines.toml (e.g. haiku, sonnet, local)",
    )
    parser.add_argument(
        "--brief",
        required=True,
        help="Path to the worker brief markdown file",
    )
    parser.add_argument(
        "--workdir",
        required=True,
        help="Path to the target git worktree",
    )
    parser.add_argument(
        "--allow",
        action="extend",
        nargs="+",
        default=[],
        metavar="GLOB",
        help="Allowed file glob(s) checked via scope_check.py",
    )
    parser.add_argument(
        "--protected",
        action="extend",
        nargs="+",
        default=["tests/**"],
        metavar="GLOB",
        help="Protected test glob(s) checked via test_lock.py (default: tests/**)",
    )
    parser.add_argument(
        "--base",
        default="",
        help="Base git ref for scope_check/test_lock (default: pre-run workdir HEAD)",
    )
    parser.add_argument(
        "--gate-cmd",
        default="",
        help="Override gate command to run in workdir after the agent completes",
    )
    parser.add_argument(
        "--no-gate",
        action="store_true",
        help="Skip gate execution (note: gate='skipped' causes non-zero exit per SPEC)",
    )
    parser.add_argument(
        "--runs-root",
        default="",
        help="Root directory for runs/<run-id>/ logs (default: from .devpack/project.toml or runs)",
    )
    parser.add_argument(
        "--engines",
        default="",
        help="Path to engines.toml (default: from .devpack/project.toml or runner/engines.example.toml)",
    )
    parser.add_argument(
        "--project-config",
        default="",
        help="Optional path to project.toml (default: <workdir>/.devpack/project.toml)",
    )
    parser.add_argument(
        "--isolation",
        choices=("docker", "local"),
        default="docker",
        help="Agent isolation mode (default: docker)",
    )
    parser.add_argument(
        "--allow-unsandboxed-local",
        action="store_true",
        help="Explicitly authorize unsandboxed --isolation local (sandbox deviation)",
    )
    parser.add_argument(
        "--credentials",
        default=os.environ.get("DEVPACK_CLAUDE_CREDENTIALS", ""),
        help="Optional path to Claude credentials file for cloud engines",
    )
    parser.add_argument(
        "--agent-bin",
        default=os.environ.get("DEVPACK_AGENT_BIN", "claude"),
        help="Agent CLI binary name or path (default: claude)",
    )
    parser.add_argument(
        "--role",
        default="worker",
        help="Step role label in run directory (default: worker)",
    )
    parser.add_argument(
        "--no-checklist",
        action="store_true",
        help="Do not inject runner/quality_checklist.md into the worker prompt",
    )
    parser.add_argument(
        "--profile",
        default="",
        help="Optional engine profile markdown path (default: knowledge/engine_profiles/<engine>.md)",
    )
    parser.add_argument(
        "--qualification-file",
        default="",
        help="Optional qualification JSON path (default: <workdir>/.devpack/qualification.json)",
    )
    parser.add_argument(
        "--require-qualification",
        action="store_true",
        help="Refuse engines without a valid qualified entry even if qualification.json is absent",
    )
    parser.add_argument(
        "--allow-unqualified",
        action="store_true",
        help="Permit running an unqualified engine (requires --deviation <ID>)",
    )
    parser.add_argument(
        "--deviation",
        default="",
        help="Deviation ID (e.g. DEV-001) authorizing --allow-unqualified",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate configuration, data-class, and sandbox policy without running an agent",
    )
    parser.add_argument(
        "--no-memory",
        action="store_true",
        help="Disable project memory retrieval and injection for this dispatch",
    )
    parser.add_argument(
        "--components",
        default=None,
        help="Comma-separated workspace component names to scope checks to",
    )
    parser.add_argument(
        "--all-components",
        action="store_true",
        help="Run workspace-scoped checks across all configured/discovered components",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for ``runner/devpack_dispatch.py``."""
    effective_argv = list(argv) if argv is not None else sys.argv[1:]
    if _submission_requested(effective_argv):
        from runner.devpack_submit import main as submit_main
        return submit_main(effective_argv)
    parser = build_arg_parser()
    args = parser.parse_args(effective_argv)

    brief_path = Path(args.brief).resolve()
    workdir = Path(args.workdir).resolve()
    if not brief_path.is_file():
        print(f"error: brief file not found: {brief_path}", file=sys.stderr)
        return 2
    if not workdir.is_dir():
        print(f"error: workdir not found: {workdir}", file=sys.stderr)
        return 2

    engines_path = Path(args.engines).resolve() if args.engines else None
    if engines_path is not None and not engines_path.is_file():
        print(f"error: engines config not found: {engines_path}", file=sys.stderr)
        return 2

    project_cfg_path = Path(args.project_config).resolve() if args.project_config else None
    if project_cfg_path is not None and not project_cfg_path.is_file():
        print(f"error: project config not found: {project_cfg_path}", file=sys.stderr)
        return 2

    runs_root = Path(args.runs_root).resolve() if args.runs_root else None
    credentials_path = Path(args.credentials).resolve() if args.credentials else None
    profile_path = Path(args.profile).resolve() if args.profile else None
    if profile_path is not None and not profile_path.is_file():
        print(f"error: profile file not found: {profile_path}", file=sys.stderr)
        return 2
    qual_file = Path(args.qualification_file).resolve() if args.qualification_file else None

    try:
        rc, _payload = dispatch_job(
            engine_name=args.engine,
            brief_path=brief_path,
            workdir=workdir,
            allow_globs=args.allow,
            protected_globs=args.protected,
            base_ref=args.base,
            gate_cmd=args.gate_cmd,
            no_gate=args.no_gate,
            runs_root=runs_root,
            engines_config_path=engines_path,
            project_config_path=project_cfg_path,
            isolation=args.isolation,
            allow_unsandboxed_local=args.allow_unsandboxed_local,
            credentials_path=credentials_path,
            agent_bin=args.agent_bin,
            role=args.role,
            no_checklist=args.no_checklist,
            profile_path=profile_path,
            qualification_file=qual_file,
            allow_unqualified=args.allow_unqualified,
            deviation_id=args.deviation,
            require_qualification=args.require_qualification,
            dry_run=args.dry_run,
            memory_enabled=False if args.no_memory else None,
            components=args.components,
            all_components=bool(args.all_components),
        )
        return rc
    except (ValueError, GitSecurityError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1 if isinstance(exc, GitSecurityError) else 2


if __name__ == "__main__":
    sys.exit(main())
