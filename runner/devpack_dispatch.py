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
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
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

from enforcement.checks.scope_check import get_changed_files  # noqa: E402
from runner import agent_exec  # noqa: E402
from runner.agent_exec import (  # noqa: E402
    AgentRequest,
    AgentResult,
    EngineConfig,
    EnginesConfig,
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
from runner.devpack_logs import Redactor, load_run_dir, write_rendered_outputs  # noqa: E402
from runner.memory import memory_root_for, process_memory_proposals  # noqa: E402

CPP_SUFFIXES = (".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh")
_LINT_SUMMARY_RE = re.compile(r"Summary:\s+\d+\s+file\(s\)\s+checked,\s+(\d+)\s+finding\(s\)\.")


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
    rc, out, _ = _run_subprocess(["git", "-C", str(workdir), "rev-parse", "--verify", "HEAD"], workdir, 30)
    if rc != 0:
        return None
    sha = out.strip()
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
) -> tuple[bool, int, str]:
    """Run ``preflight_lint.py`` on changed C/C++ files inside ``workdir``.

    Returns ``(passed, finding_count, combined_output)``.
    """
    lint_targets = [
        str(workdir / rel)
        for rel in changed_files
        if rel.lower().endswith(CPP_SUFFIXES) and (workdir / rel).is_file()
    ]
    if not lint_targets:
        return True, 0, ""

    preflight_script = pack_root / "enforcement" / "lint" / "preflight_lint.py"
    if not preflight_script.is_file():
        return False, 1, f"Missing preflight linter: {preflight_script}"

    cmd: list[str] = [
        "python3",
        str(preflight_script),
        "--no-cache",
        "--repo-root",
        str(workdir),
    ]
    cfg_candidate = workdir / "preflight.toml"
    if cfg_candidate.is_file():
        cmd.extend(["--config", str(cfg_candidate)])
    cmd.extend(lint_targets)

    rc, out, err = _run_subprocess(cmd, workdir, timeout_s=300)
    combined = "\n".join(part for part in (out.strip(), err.strip()) if part).strip()
    findings = parse_lint_finding_count(combined, rc)
    return rc == 0, findings, combined


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
) -> tuple[list[str] | None, int]:
    """Determine the deterministic gate command and timeout for ``workdir``."""
    timeout_s = engines_cfg.gate_timeout_s if engines_cfg is not None else 300
    if explicit_gate_cmd.strip():
        return shlex.split(explicit_gate_cmd), timeout_s

    local_gate_toml = workdir / "gate.toml"
    if local_gate_toml.is_file():
        return [
            "python3",
            str(pack_root / "enforcement" / "gate" / "gate.py"),
            "--config",
            str(local_gate_toml),
            "--repo",
            str(workdir),
            "--receipt",
            str(run_dir / "steps" / "02-gate-receipt.json"),
        ], timeout_s

    if engines_cfg is not None:
        cfg_cmd = list(engines_cfg.gate_cmd)
        if cfg_cmd != list(agent_exec.DEFAULT_GATE_CMD):
            return cfg_cmd, timeout_s
        if (workdir / "enforcement" / "gate" / "gate.example.toml").is_file():
            return cfg_cmd, timeout_s

    return None, timeout_s


def _render_run_dir_quietly(run_dir: Path) -> None:
    """Generate ``transcript.md`` and ``index.html`` for ``run_dir`` without failing dispatch."""
    try:
        redactor = Redactor()
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
) -> tuple[int, dict[str, Any]]:
    """Execute one dispatched worker job with policy checks, gate, scope, and test-lock.

    Returns ``(exit_code, result_payload)``.
    """
    cleaned_deviation = deviation_id.strip()
    if allow_unqualified and not cleaned_deviation:
        raise ValueError("--allow-unqualified requires --deviation <ID>")

    project_cfg = load_project_config(workdir, project_config_path)

    effective_engines_path = engines_config_path
    if effective_engines_path is None:
        rel_eng = project_cfg.get("engines_config", "runner/engines.example.toml")
        candidate = workdir / rel_eng
        effective_engines_path = candidate if candidate.is_file() else (pack_root / rel_eng)

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

    update_run_json(run_dir, isolation=isolation)

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
        _render_run_dir_quietly(run_dir)
        _emit_json_line(payload)
        return 1, payload

    if not iso_ok:
        sys.stderr.write(iso_msg + "\n")
        update_run_json(
            run_dir,
            outcome="refused",
            isolation=isolation,
            refusal_reason=iso_msg,
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
            "error": iso_msg,
        }
        _render_run_dir_quietly(run_dir)
        _emit_json_line(payload)
        return 1, payload

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
        _render_run_dir_quietly(run_dir)
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
        allow_unsandboxed_local=allow_unsandboxed_local or bool(project_cfg.get("allow_unsandboxed_local", False)),
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
    )
    control_before = _control_snapshot(workdir)
    agent_res = run_agent(req)

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
            _render_run_dir_quietly(run_dir)
            _emit_json_line(payload)
            return 1, payload
    control_after = _control_snapshot(workdir)
    control_changes = sorted(name for name in control_before.keys() | control_after.keys()
                             if control_before.get(name) != control_after.get(name))

    changed_files, changed_err = get_changed_files(workdir, effective_base)
    if changed_files is None:
        changed_files = []

    changed_files = sorted(set(changed_files) | set(control_changes))

    # Scope check
    scope_status = "ok"
    scope_out = ""
    if changed_err:
        scope_status = "violation"
        scope_out = f"SCOPE_ERROR: {changed_err}"
    elif allow_globs:
        scope_script = pack_root / "enforcement" / "checks" / "scope_check.py"
        s_rc, s_out, s_err = _run_subprocess(
            ["python3", str(scope_script), "--repo", str(workdir), "--base", effective_base, "--allow", *allow_globs],
            workdir,
            timeout_s=60,
        )
        scope_out = (s_out + "\n" + s_err).strip()
        if s_rc != 0:
            scope_status = "violation"

    if control_changes:
        scope_status = "violation"
        scope_out = "\n".join(f"SCOPE_VIOLATION: {name} is broker control data" for name in control_changes)

    # Gate + test_lock + preflight_lint
    gate_status = "skipped"
    lint_findings = 0
    test_lock_status = "skipped"
    gate_logs: list[str] = []

    if agent_res.status == "ok":
        if no_gate:
            gate_status = "skipped"
        else:
            gate_ok = True
            # 1. Protected test lock check
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

            # 2. Preflight lint on changed C/C++ files
            lint_ok, lint_findings, lint_out = run_preflight_lint_on_changed(
                workdir, changed_files, pack_root=pack_root
            )
            if lint_out:
                gate_logs.append(f"[preflight_lint]\n{lint_out}")
            if not lint_ok:
                gate_ok = False

            # 3. Configured gate command (if any)
            resolved_gate_cmd, gate_timeout_s = _resolve_gate_command(
                gate_cmd, engines_cfg, workdir, run_dir, pack_root=pack_root
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
            redacted_gate = redact_sensitive_text(gate_text)
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
            stop_report_text=stop_report_text,
        )
        mem_proposal_summary = prop_res.to_dict()

    outcome = "pass" if passed else ("timeout" if agent_res.status == "timeout" else "fail")
    update_run_json(
        run_dir,
        task=brief_path.stem,
        outcome=outcome,
        gate=gate_status,
        scope=scope_status,
        test_lock=test_lock_status,
        lint_findings=lint_findings,
        brief_lint=effective_brief_lint,
        data_class=project_cfg["data_class"],
        memory_ids=list(agent_res.memory_ids),
        memory_hashes=dict(agent_res.memory_hashes),
        memory_tokens=int(agent_res.memory_tokens),
        memory_proposals=mem_proposal_summary,
        finished=datetime.now(timezone.utc).isoformat(),
    )
    _render_run_dir_quietly(run_dir)

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
        "memory_ids": list(agent_res.memory_ids),
        "memory_hashes": dict(agent_res.memory_hashes),
        "memory_tokens": int(agent_res.memory_tokens),
    }
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
        )
        return rc
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
