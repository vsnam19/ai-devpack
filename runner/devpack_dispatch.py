#!/usr/bin/env python3
# devpack_dispatch.py - Deterministic single-job worker dispatcher with policy, gate, scope, and test-lock checks.
#
# Usage:
#   python3 runner/devpack_dispatch.py --engine NAME --brief FILE --workdir DIR
#       [--allow GLOB ...] [--protected GLOB ...] [--base REF] [--gate-cmd CMD]
#       [--no-gate] [--runs-root DIR] [--engines FILE] [--project-config FILE]
#       [--isolation docker|local] [--allow-unsandboxed-local] [--credentials PATH]
#       [--agent-bin PATH] [--role worker] [--no-checklist] [--dry-run] [--help]
#
# Runs ONE isolated agent job via runner/agent_exec.py, then runs deterministic
# enforcement checks: scope_check.py against --allow, test_lock.py against
# --protected, preflight_lint.py on changed C/C++ files, and the configured gate
# command. Enforces .devpack/project.toml data-class bounds (missing file =
# "internal") and container-by-default sandbox policy before launching an agent.
# Writes a v0.2 run directory and prints ONE compact JSON line to stdout.
#
# Exit codes:
#   0 - Agent status ok, gate ok, and scope ok (or --dry-run validated)
#   1 - Agent error/timeout, gate/lint/test-lock fail, scope violation, or policy refusal
#   2 - Invalid arguments, missing brief/config, or non-git workdir
"""Single-job deterministic worker dispatcher for ai-devpack."""

from __future__ import annotations

import argparse
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

RUNNER_DIR = Path(__file__).resolve().parent
PACK_ROOT = RUNNER_DIR.parent
for _p in (str(PACK_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from enforcement.checks.scope_check import get_changed_files  # noqa: E402
from runner import agent_exec  # noqa: E402
from runner.agent_exec import (  # noqa: E402
    AgentRequest,
    EngineConfig,
    EnginesConfig,
    check_data_class_allowed,
    check_local_isolation_allowed,
    load_engines,
    load_project_config,
    load_quality_checklist,
    new_run_dir,
    redact_sensitive_text,
    resolve_engine,
    run_agent,
    update_run_json,
)
from runner.devpack_logs import Redactor, load_run_dir, write_rendered_outputs  # noqa: E402

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
    dry_run: bool = False,
    pack_root: Path = PACK_ROOT,
) -> tuple[int, dict[str, Any]]:
    """Execute one dispatched worker job with policy checks, gate, scope, and test-lock.

    Returns ``(exit_code, result_payload)``.
    """
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
    prompt_text = brief_raw if no_checklist else _inject_quality_checklist(brief_raw, pack_root)

    # Check data-class and sandbox policy before launching any agent
    data_ok, data_reason = check_data_class_allowed(engine, str(project_cfg["data_class"]))
    iso_ok, iso_msg = check_local_isolation_allowed(
        isolation,
        allow_unsandboxed_local=allow_unsandboxed_local,
        workdir=workdir,
        project_config=project_cfg,
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
        plan = {
            "mode": "dry-run",
            "engine": engine.name,
            "engine_class": engine.engine_class,
            "data_class": project_cfg["data_class"],
            "isolation": isolation,
            "workdir": str(workdir),
            "base_ref": effective_base,
            "allow": list(allow_globs),
            "protected": list(protected_globs),
            "brief": str(brief_path),
        }
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
        role=role,
        step_index=1,
    )
    agent_res = run_agent(req)

    changed_files, changed_err = get_changed_files(workdir, effective_base)
    if changed_files is None:
        changed_files = []

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
    outcome = "pass" if passed else ("timeout" if agent_res.status == "timeout" else "fail")
    update_run_json(
        run_dir,
        outcome=outcome,
        gate=gate_status,
        scope=scope_status,
        test_lock=test_lock_status,
        lint_findings=lint_findings,
        data_class=project_cfg["data_class"],
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
    }
    if agent_res.error:
        payload["error"] = agent_res.error
    if scope_out and scope_status != "ok":
        payload["scope_detail"] = scope_out

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
        "--dry-run",
        action="store_true",
        help="Validate configuration, data-class, and sandbox policy without running an agent",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for ``runner/devpack_dispatch.py``."""
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

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
            dry_run=args.dry_run,
        )
        return rc
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
