#!/usr/bin/env python3
# run_eval.py - Isolated evaluation runner for ai-devpack tasks and engines.
#
# Usage:
#   python3 eval/run_eval.py [--help] [--engines sonnet,haiku] [--models sonnet,haiku]
#       [--k 5] [--tasks all|id,...] [--isolation docker|local] [--out eval/results]
#       [--runs-root eval/results/<date>/runs] [--engines-config runner/engines.example.toml]
#       [--dry-run] [--timeout-s 900] [--max-parallel 1] [--credentials PATH]
#       [--work-root PATH] [--claude-version 2.1.330] [--allow-harness-errors]
#
# Exit codes:
#   0 - Evaluation (or dry-run plan validation) completed without runner errors
#   1 - Runtime or environment error (e.g. missing docker, missing credentials),
#       or at least one run was a harness error (unless --allow-harness-errors)
#   2 - Invalid command-line arguments or malformed task directory
"""Isolated evaluation runner for measuring agents across the ai-devpack task set."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Any, Callable, Sequence
import uuid

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "runner")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from runner import agent_exec  # noqa: E402
from runner.agent_exec import (  # noqa: E402
    ALLOWED_TOOLS,
    DEFAULT_CLAUDE_VERSION,
    DISALLOWED_TOOLS,
    DOCKER_IMAGE_TAG,
    DOCKER_TASK_DIR,
    AgentRequest,
    EngineConfig,
    EnginesConfig,
    build_claude_state,
    build_docker_agent_invocation,
    build_invocation,
    build_local_agent_invocation,
    build_permission_args,
    build_project_settings,
    cleanup_cfg_dir,
    detect_harness_error,
    extract_agent_json_object,
    json_string_leaves,
    load_engines,
    new_run_dir,
    parse_agent_json_output,
    parse_stream,
    redact_sensitive_text,
    redact_structure,
    resolve_engine,
    run_agent,
    update_run_json,
)

EXCERPT_MAX_CHARS = 4000
STDERR_TAIL_MAX_CHARS = 2000
REQUIRED_TASK_FILES = ("task.md", "check.sh", "setup.patch", "solution.patch")


@dataclass(frozen=True)
class TaskSpec:
    """Metadata and filesystem paths for a single evaluation task."""

    task_id: str
    category: str
    task_dir: Path
    task_md: Path
    check_sh: Path
    setup_patch: Path
    solution_patch: Path
    hidden_dir: Path


@dataclass
class TrialResult:
    """Result record for a single (task, model, run_index) evaluation trial."""

    task_id: str
    category: str
    model: str
    run_index: int
    passed: bool
    agent_returncode: int
    check_returncode: int
    cost_usd: float | None
    turns: int | None
    duration_s: float
    error: str = ""
    check_output: str = ""
    harness_error: bool = False
    harness_error_reason: str = ""
    agent_result_excerpt: str = ""
    permission_denials: list[Any] = field(default_factory=list)
    stderr_tail: str = ""
    tool_protocol_errors: int = 0
    run_dir: str = ""


@dataclass
class TaskModelSummary:
    """Aggregated metrics for one (task, model) pair across k runs."""

    task_id: str
    category: str
    model: str
    k: int
    passes: int
    pass_at_1: float | None
    pass_hat_k: float | None
    pass_pow_k: float | None
    mean_cost_usd: float | None
    mean_turns: float | None
    mean_duration_s: float
    runs: list[TrialResult] = field(default_factory=list)
    harness_errors: int = 0
    valid_runs: int = 0
    tool_protocol_errors: int = 0


def parse_category(task_md_text: str) -> str:
    """Extract the ``Category: <name>`` header from a task markdown string."""
    for line in task_md_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("category:"):
            value = stripped.split(":", 1)[1].strip()
            if value:
                return value
    return "uncategorized"


def load_and_validate_tasks(tasks_dir: Path, task_selector: str) -> list[TaskSpec]:
    """Discover and validate requested tasks under ``tasks_dir``."""
    if not tasks_dir.is_dir():
        raise ValueError(f"Tasks directory does not exist: {tasks_dir}")

    if task_selector.strip().lower() == "all":
        candidate_dirs = sorted(
            p for p in tasks_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
        )
    else:
        requested_ids = [item.strip() for item in task_selector.split(",") if item.strip()]
        if not requested_ids:
            raise ValueError("No task IDs provided in --tasks")
        candidate_dirs = [tasks_dir / tid for tid in requested_ids]

    if not candidate_dirs:
        raise ValueError(f"No task directories found in {tasks_dir}")

    specs: list[TaskSpec] = []
    for tdir in candidate_dirs:
        if not tdir.is_dir():
            raise ValueError(f"Task directory not found: {tdir}")
        for req_name in REQUIRED_TASK_FILES:
            req_path = tdir / req_name
            if not req_path.is_file():
                raise ValueError(f"Task '{tdir.name}' is missing required file '{req_name}'")
        hidden_dir = tdir / "hidden"
        if not hidden_dir.is_dir():
            raise ValueError(f"Task '{tdir.name}' is missing required 'hidden/' directory")

        task_md_path = tdir / "task.md"
        category = parse_category(task_md_path.read_text(encoding="utf-8"))
        specs.append(
            TaskSpec(
                task_id=tdir.name,
                category=category,
                task_dir=tdir,
                task_md=task_md_path,
                check_sh=tdir / "check.sh",
                setup_patch=tdir / "setup.patch",
                solution_patch=tdir / "solution.patch",
                hidden_dir=hidden_dir,
            )
        )
    return specs


def validate_fixture_dir(fixture_dir: Path) -> None:
    """Validate that the C++ evaluation fixture directory exists and has required files."""
    if not fixture_dir.is_dir():
        raise ValueError(f"Fixture directory does not exist: {fixture_dir}")
    for expected in ("CMakeLists.txt", "build_and_test.sh", "src", "include", "tests"):
        if not (fixture_dir / expected).exists():
            raise ValueError(f"Fixture directory is missing '{expected}': {fixture_dir}")


def docker_image_tag(claude_version: str) -> str:
    """Return the docker image tag for a given Claude Code version."""
    return f"devpack-eval:{claude_version}"


def prepare_isolated_trial_dir(
    trial_root: Path,
    fixture_dir: Path,
    task: TaskSpec,
    credentials_path: Path | None,
    agent_cwd: str | None = None,
) -> dict[str, Path]:
    """Create an isolated trial directory with home, cfg, and git-initialized task workdir.

    Copies only the credentials file into ``<trial_root>/cfg/.credentials.json``
    (when ``credentials_path`` is provided), initializes git on a fresh copy of
    ``fixture_dir``, applies ``setup.patch``, commits the setup state, and writes
    the task prompt and ``.claude/settings.json``. Also pre-writes
    ``<cfg>/.claude.json`` trusting ``agent_cwd``.
    """
    home_dir = trial_root / "home"
    cfg_dir = trial_root / "cfg"
    tmp_dir = trial_root / "tmp"
    work_dir = trial_root / "work"

    home_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    cred_dest = cfg_dir / ".credentials.json"
    if credentials_path is not None:
        if not credentials_path.is_file():
            raise RuntimeError(f"Credentials file not found: {credentials_path}")
        shutil.copyfile(credentials_path, cred_dest)
        os.chmod(cred_dest, stat.S_IRUSR | stat.S_IWUSR)

    state_path = cfg_dir / ".claude.json"
    state_path.write_text(
        json.dumps(build_claude_state(agent_cwd or str(work_dir)), indent=2) + "\n",
        encoding="utf-8",
    )

    shutil.copytree(fixture_dir, work_dir, dirs_exist_ok=True)

    git_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home_dir),
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C.UTF-8",
    }

    def _run_git(args: Sequence[str]) -> None:
        """Execute a git subcommand inside the isolated task workdir."""
        proc = subprocess.run(
            ["git", *args],
            cwd=str(work_dir),
            env=git_env,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed in {work_dir}: {proc.stderr.strip()}"
            )

    _run_git(["init", "-q"])
    _run_git(["config", "user.email", "eval@example.invalid"])
    _run_git(["config", "user.name", "Eval Runner"])
    _run_git(["add", "-A"])
    _run_git(["commit", "-q", "-m", "chore: base fixture"])
    _run_git(["apply", str(task.setup_patch)])
    _run_git(["add", "-A"])
    _run_git(["commit", "-q", "-m", "chore: apply setup.patch"])

    prompt_text = task.task_md.read_text(encoding="utf-8")
    prompt_file = trial_root / "prompt.md"
    prompt_file.write_text(prompt_text, encoding="utf-8")

    task_prompt_in_work = work_dir / "TASK_PROMPT.md"
    task_prompt_in_work.write_text(prompt_text, encoding="utf-8")

    claude_dir = work_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(
        json.dumps(build_project_settings(), indent=2) + "\n",
        encoding="utf-8",
    )

    exclude_file = work_dir / ".git" / "info" / "exclude"
    if exclude_file.parent.is_dir():
        existing = (
            exclude_file.read_text(encoding="utf-8") if exclude_file.is_file() else ""
        )
        additions = "\n.claude/\nTASK_PROMPT.md\n"
        exclude_file.write_text(existing + additions, encoding="utf-8")

    return {
        "trial_root": trial_root,
        "home_dir": home_dir,
        "cfg_dir": cfg_dir,
        "tmp_dir": tmp_dir,
        "work_dir": work_dir,
        "prompt_file": prompt_file,
        "settings_path": settings_path,
        "credentials_copy": cred_dest,
    }


def ensure_docker_image(
    dockerfile_path: Path,
    image_tag: str = DOCKER_IMAGE_TAG,
    claude_version: str = DEFAULT_CLAUDE_VERSION,
    which_fn: Callable[[str], str | None] = shutil.which,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Verify docker is installed and build the evaluation container image once."""
    docker_bin = which_fn("docker")
    if not docker_bin:
        raise RuntimeError(
            "Docker executable not found; cannot run with --isolation docker "
            "(no silent fallback to local)."
        )
    if not dockerfile_path.is_file():
        raise RuntimeError(f"Dockerfile not found at {dockerfile_path}")

    proc = run_fn(
        [
            docker_bin,
            "build",
            "--build-arg",
            f"CLAUDE_CODE_VERSION={claude_version}",
            "-t",
            image_tag,
            "-f",
            str(dockerfile_path),
            str(dockerfile_path.parent),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Docker image build failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return docker_bin


def compute_pass_at_1(passes: int, total: int) -> float:
    """Compute empirical pass@1 (c / n) for a task or category."""
    if total < 1:
        raise ValueError("total must be >= 1")
    if passes < 0 or passes > total:
        raise ValueError("passes must be between 0 and total")
    return passes / total


def compute_pass_hat_k(passes: int, total: int) -> float:
    """Compute strict pass^k indicator (1.0 iff all k runs passed, else 0.0)."""
    if total < 1:
        raise ValueError("total must be >= 1")
    if passes < 0 or passes > total:
        raise ValueError("passes must be between 0 and total")
    return 1.0 if passes == total else 0.0


def compute_pass_pow_k(passes: int, total: int, k: int | None = None) -> float:
    """Compute Bernoulli pass^k estimate ((c / n) ** k)."""
    if total < 1:
        raise ValueError("total must be >= 1")
    if passes < 0 or passes > total:
        raise ValueError("passes must be between 0 and total")
    exponent = total if k is None else k
    if exponent < 1:
        raise ValueError("k must be >= 1")
    return (passes / total) ** exponent


def run_single_trial(
    task: TaskSpec,
    model: str,
    run_index: int,
    run_root: Path,
    fixture_dir: Path,
    credentials_path: Path | None,
    isolation: str,
    agent_bin: str,
    timeout_s: int,
    docker_bin: str | None = None,
    extra_secrets: Sequence[str] = (),
    path_replacements: Sequence[tuple[str, str]] = (),
    image_tag: str = DOCKER_IMAGE_TAG,
    runs_root: Path | None = None,
    engine_config: EngineConfig | None = None,
) -> TrialResult:
    """Execute one (task, model, run_index) trial in an isolated workspace.

    Credentials and the config directory contents are always deleted afterwards,
    whether the trial succeeds, fails, times out, or raises.
    """
    trial_name = f"{task.task_id}__{model}__run{run_index}"
    trial_root = run_root / trial_name
    try:
        return _run_trial_body(
            task=task,
            model=model,
            run_index=run_index,
            trial_root=trial_root,
            fixture_dir=fixture_dir,
            credentials_path=credentials_path,
            isolation=isolation,
            agent_bin=agent_bin,
            timeout_s=timeout_s,
            docker_bin=docker_bin,
            extra_secrets=extra_secrets,
            path_replacements=path_replacements,
            image_tag=image_tag,
            runs_root=runs_root or (run_root / "runs"),
            engine_config=engine_config,
        )
    finally:
        cleanup_cfg_dir(trial_root / "cfg")


def _run_trial_body(
    task: TaskSpec,
    model: str,
    run_index: int,
    trial_root: Path,
    fixture_dir: Path,
    credentials_path: Path | None,
    isolation: str,
    agent_bin: str,
    timeout_s: int,
    docker_bin: str | None,
    extra_secrets: Sequence[str],
    path_replacements: Sequence[tuple[str, str]],
    image_tag: str,
    runs_root: Path | None = None,
    engine_config: EngineConfig | None = None,
) -> TrialResult:
    """Run the agent via agent_exec and execute check.sh for one trial."""
    if isolation not in ("local", "docker"):
        raise ValueError(f"Unsupported isolation mode: {isolation}")
    if isolation == "docker" and not docker_bin:
        raise RuntimeError("docker_bin is required for docker isolation mode")

    engine = engine_config or resolve_engine(model)
    paths = prepare_isolated_trial_dir(
        trial_root=trial_root,
        fixture_dir=fixture_dir,
        task=task,
        credentials_path=credentials_path if engine.is_cloud else None,
        agent_cwd=DOCKER_TASK_DIR if isolation == "docker" else None,
    )
    prompt_text = paths["prompt_file"].read_text(encoding="utf-8")
    work_dir = paths["work_dir"]

    effective_runs_root = runs_root if runs_root is not None else (trial_root / "runs")
    trial_run_dir = new_run_dir(
        effective_runs_root,
        kind="eval",
        task=task.task_id,
        engines=[engine.name],
    )

    request = AgentRequest(
        engine=engine,
        prompt=prompt_text,
        workdir=work_dir,
        run_dir=trial_run_dir,
        step_name=f"01-eval-{engine.name}",
        isolation=isolation,
        timeout_s=timeout_s,
        skills_enabled=False,
        credentials_path=credentials_path if engine.is_cloud else None,
        agent_bin=agent_bin,
        docker_bin=docker_bin or "docker",
        image_tag=image_tag,
        extra_secrets=extra_secrets,
        path_replacements=path_replacements,
        role="eval",
        step_index=1,
        home_dir=paths["home_dir"],
        cfg_dir=paths["cfg_dir"],
        tmp_dir=paths["tmp_dir"],
        home_placeholder="$HOME",
    )
    agent_res = run_agent(request)

    def _redact(text: str) -> str:
        """Redact a string using this run's secrets and path replacements."""
        return redact_sensitive_text(
            text,
            extra_secrets=extra_secrets,
            path_replacements=path_replacements,
            home_placeholder="$HOME",
        )

    excerpt = _redact(agent_res.final_text)[:EXCERPT_MAX_CHARS]
    denials_out = redact_structure(
        agent_res.permission_denials,
        extra_secrets=extra_secrets,
        path_replacements=path_replacements,
        home_placeholder="$HOME",
    )
    stderr_tail = _redact(agent_res.stderr)[-STDERR_TAIL_MAX_CHARS:]
    is_harness_err = agent_res.status in ("harness_error", "timeout")
    harness_reason = agent_res.error if is_harness_err else ""
    error_msg = agent_res.error if agent_res.status != "ok" else ""

    task_prompt_in_work = work_dir / "TASK_PROMPT.md"
    if task_prompt_in_work.exists():
        task_prompt_in_work.unlink()

    check_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(paths["home_dir"]),
        "TMPDIR": str(paths["tmp_dir"]),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    try:
        check_proc = subprocess.run(
            ["bash", str(task.check_sh), "--workdir", str(work_dir)],
            cwd=str(work_dir),
            env=check_env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        check_rc = check_proc.returncode
        check_out = (check_proc.stdout + "\n" + check_proc.stderr).strip()
    except subprocess.TimeoutExpired:
        check_rc = 124
        check_out = f"check.sh timed out after {timeout_s}s"

    redacted_check_out = _redact(check_out)
    gate_step_path = trial_run_dir / "steps" / "02-gate.txt"
    gate_step_path.write_text(
        (redacted_check_out + "\n") if redacted_check_out else f"check_rc={check_rc}\n",
        encoding="utf-8",
    )

    passed = check_rc == 0 and not is_harness_err
    outcome = "pass" if passed else ("harness_error" if is_harness_err else "fail")
    update_run_json(
        trial_run_dir,
        outcome=outcome,
        finished=datetime.now(timezone.utc).isoformat(),
    )

    return TrialResult(
        task_id=task.task_id,
        category=task.category,
        model=model,
        run_index=run_index,
        passed=passed,
        agent_returncode=agent_res.exit_code,
        check_returncode=check_rc,
        cost_usd=agent_res.cost_usd if not is_harness_err else None,
        turns=agent_res.num_turns if not is_harness_err else None,
        duration_s=float(agent_res.duration_s),
        error=_redact(error_msg),
        check_output=redacted_check_out,
        harness_error=is_harness_err,
        harness_error_reason=_redact(harness_reason),
        agent_result_excerpt=excerpt,
        permission_denials=denials_out if isinstance(denials_out, list) else [],
        stderr_tail=stderr_tail,
        tool_protocol_errors=int(agent_res.tool_protocol_errors),
        run_dir=_redact(str(trial_run_dir)),
    )


def aggregate_results(
    tasks: Sequence[TaskSpec],
    models: Sequence[str],
    k: int,
    trials: Sequence[TrialResult],
) -> tuple[list[TaskModelSummary], list[dict[str, Any]]]:
    """Aggregate trial results into per-task and per-category summaries."""
    by_task_model: dict[tuple[str, str], list[TrialResult]] = {}
    for trial in trials:
        by_task_model.setdefault((trial.task_id, trial.model), []).append(trial)

    task_summaries: list[TaskModelSummary] = []
    for task in tasks:
        for model in models:
            runs = sorted(
                by_task_model.get((task.task_id, model), []),
                key=lambda r: r.run_index,
            )
            harness_errors = sum(1 for r in runs if r.harness_error)
            protocol_errors = sum(int(r.tool_protocol_errors) for r in runs)
            valid = [r for r in runs if not r.harness_error]
            has_valid = bool(valid)
            total = len(valid) if has_valid else 0
            passes = sum(1 for r in valid if r.passed)
            costs = [r.cost_usd for r in valid if r.cost_usd is not None]
            turns_list = [r.turns for r in valid if r.turns is not None]
            durations = [r.duration_s for r in valid]

            task_summaries.append(
                TaskModelSummary(
                    task_id=task.task_id,
                    category=task.category,
                    model=model,
                    k=len(runs) if runs else k,
                    passes=passes,
                    pass_at_1=(
                        round(compute_pass_at_1(passes, total), 4) if has_valid else None
                    ),
                    pass_hat_k=(
                        round(compute_pass_hat_k(passes, total), 4) if has_valid else None
                    ),
                    pass_pow_k=(
                        round(compute_pass_pow_k(passes, total, total), 4)
                        if has_valid
                        else None
                    ),
                    mean_cost_usd=(
                        round(sum(costs) / len(costs), 6) if costs else None
                    ),
                    mean_turns=(
                        round(sum(turns_list) / len(turns_list), 2)
                        if turns_list
                        else None
                    ),
                    mean_duration_s=(
                        round(sum(durations) / len(durations), 4) if durations else 0.0
                    ),
                    runs=runs,
                    harness_errors=harness_errors,
                    valid_runs=len(valid),
                    tool_protocol_errors=protocol_errors,
                )
            )

    categories = sorted({t.category for t in tasks})
    category_summaries: list[dict[str, Any]] = []
    for category in categories:
        for model in models:
            matching = [
                s
                for s in task_summaries
                if s.category == category and s.model == model
            ]
            if not matching:
                continue
            n_tasks = len(matching)
            scored = [s for s in matching if s.pass_at_1 is not None]

            def _mean(values: list[float]) -> float | None:
                """Average ``values`` rounded to 4 places, or None when empty."""
                return round(sum(values) / len(values), 4) if values else None

            mean_pass_at_1 = _mean([s.pass_at_1 for s in scored if s.pass_at_1 is not None])
            mean_pass_hat_k = _mean([s.pass_hat_k for s in scored if s.pass_hat_k is not None])
            mean_pass_pow_k = _mean([s.pass_pow_k for s in scored if s.pass_pow_k is not None])
            cat_costs = [
                s.mean_cost_usd for s in matching if s.mean_cost_usd is not None
            ]
            cat_durations = [s.mean_duration_s for s in matching]
            category_summaries.append(
                {
                    "category": category,
                    "model": model,
                    "tasks_count": n_tasks,
                    "k": k,
                    "pass_at_1": mean_pass_at_1,
                    "pass_hat_k": mean_pass_hat_k,
                    "pass_pow_k": mean_pass_pow_k,
                    "harness_errors": sum(s.harness_errors for s in matching),
                    "tool_protocol_errors": sum(s.tool_protocol_errors for s in matching),
                    "mean_cost_usd": (
                        round(sum(cat_costs) / len(cat_costs), 6)
                        if cat_costs
                        else None
                    ),
                    "mean_duration_s": (
                        round(sum(cat_durations) / len(cat_durations), 4)
                        if cat_durations
                        else 0.0
                    ),
                }
            )

    return task_summaries, category_summaries


def _fmt(value: float | None) -> str:
    """Format a metric with two decimals, or ``n/a`` when there are no valid runs."""
    return "n/a" if value is None else f"{value:.2f}"


def render_summary_markdown(
    run_id: str,
    date_str: str,
    isolation: str,
    k: int,
    models: Sequence[str],
    task_summaries: Sequence[TaskModelSummary],
    category_summaries: Sequence[dict[str, Any]],
) -> str:
    """Format evaluation results into a human-readable Markdown report."""
    lines = [
        f"# Evaluation Summary ({run_id})",
        "",
        f"- **Date**: {date_str}",
        f"- **Isolation**: `{isolation}`",
        f"- **Engines**: `{', '.join(models)}`",
        f"- **k**: `{k}`",
        "",
        "## Per-Category Summary",
        "",
        "Pass metrics are computed over valid runs only (harness errors excluded).",
        "",
        "| Category | Engine | Tasks | Harness errors | Tool Protocol Errors | pass@1 | pass^k (all-k) | pass^k (p^k) | Mean Cost (USD) | Mean Duration (s) |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cat in category_summaries:
        cost_str = (
            f"{cat['mean_cost_usd']:.4f}"
            if cat["mean_cost_usd"] is not None
            else "n/a"
        )
        lines.append(
            f"| {cat['category']} | {cat['model']} | {cat['tasks_count']} | "
            f"{cat['harness_errors']} | {cat.get('tool_protocol_errors', 0)} | "
            f"{_fmt(cat['pass_at_1'])} | {_fmt(cat['pass_hat_k'])} | "
            f"{_fmt(cat['pass_pow_k'])} | {cost_str} | {cat['mean_duration_s']:.2f} |"
        )

    lines.extend(
        [
            "",
            "## Per-Task Summary",
            "",
            "| Task | Category | Engine | Passes (c/valid) | Harness errors | Tool Protocol Errors | pass@1 | pass^k (all-k) | pass^k (p^k) | Mean Cost (USD) | Mean Duration (s) |",
            "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for ts in task_summaries:
        cost_str = (
            f"{ts.mean_cost_usd:.4f}" if ts.mean_cost_usd is not None else "n/a"
        )
        lines.append(
            f"| {ts.task_id} | {ts.category} | {ts.model} | "
            f"{ts.passes}/{ts.valid_runs} | {ts.harness_errors} | {ts.tool_protocol_errors} | "
            f"{_fmt(ts.pass_at_1)} | {_fmt(ts.pass_hat_k)} | "
            f"{_fmt(ts.pass_pow_k)} | {cost_str} | {ts.mean_duration_s:.2f} |"
        )

    lines.extend(
        [
            "",
            "## Per-Trial Runs",
            "",
            "| Task | Engine | Run | Passed | Cost (USD) | Tool Protocol Errors | Run Dir |",
            "|---|---|---:|---|---:|---:|---|",
        ]
    )
    for ts in task_summaries:
        for run in ts.runs:
            run_cost = f"{run.cost_usd:.4f}" if run.cost_usd is not None else "n/a"
            lines.append(
                f"| {run.task_id} | {run.model} | {run.run_index} | "
                f"{'yes' if run.passed else 'no'} | {run_cost} | "
                f"{run.tool_protocol_errors} | `{run.run_dir}` |"
            )

    lines.append("")
    return "\n".join(lines)


class _EnginesModelsAction(argparse.Action):
    """Store both ``engines`` and ``models`` attributes on the parsed Namespace."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        """Assign ``values`` to both ``namespace.engines`` and ``namespace.models``."""
        setattr(namespace, "models", values)
        setattr(namespace, "engines", values)


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for ``eval/run_eval.py``."""
    repo_root = Path(__file__).resolve().parent.parent
    default_eval_dir = repo_root / "eval"
    default_engines_cfg = repo_root / "runner" / "engines.example.toml"

    parser = argparse.ArgumentParser(
        prog="run_eval.py",
        description=(
            "Run ai-devpack evaluation tasks across engines in an isolated environment "
            "and compute pass@1 and pass^k metrics."
        ),
    )
    parser.add_argument(
        "--engines",
        "--models",
        dest="models",
        action=_EnginesModelsAction,
        default="sonnet,haiku",
        help="Comma-separated engine/model names to evaluate (default: sonnet,haiku)",
    )
    parser.set_defaults(engines="sonnet,haiku", models="sonnet,haiku")
    parser.add_argument(
        "--k",
        type=int,
        default=5,
        help="Number of trials per (task, engine) pair (default: 5)",
    )
    parser.add_argument(
        "--tasks",
        default="all",
        help="Task selection: 'all' or comma-separated task IDs (default: all)",
    )
    parser.add_argument(
        "--isolation",
        choices=("docker", "local"),
        default="docker",
        help="Isolation mode: 'docker' or 'local' (default: docker)",
    )
    parser.add_argument(
        "--out",
        default="eval/results",
        help="Output directory for summary.md and results.json (default: eval/results)",
    )
    parser.add_argument(
        "--runs-root",
        default="",
        help="Directory for per-trial run directories (default: <out>/<date>/runs)",
    )
    parser.add_argument(
        "--engines-config",
        default=str(default_engines_cfg),
        help="Path to engines.toml configuration (default: runner/engines.example.toml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate tasks and print the execution plan without calling any model",
    )
    parser.add_argument(
        "--timeout-s",
        type=int,
        default=900,
        help="Per-trial timeout in seconds (default: 900)",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=1,
        help="Maximum number of parallel trials (default: 1)",
    )
    parser.add_argument(
        "--credentials",
        default=os.environ.get("DEVPACK_CLAUDE_CREDENTIALS", ""),
        help="Path to Claude credentials file (or set DEVPACK_CLAUDE_CREDENTIALS)",
    )
    parser.add_argument(
        "--work-root",
        default="/tmp/devpack-eval",
        help="Root directory for isolated per-run workspaces (default: /tmp/devpack-eval)",
    )
    parser.add_argument(
        "--fixture-dir",
        default=str(default_eval_dir / "fixture"),
        help="Path to the C++ evaluation fixture directory",
    )
    parser.add_argument(
        "--tasks-dir",
        default=str(default_eval_dir / "tasks"),
        help="Path to the evaluation tasks directory",
    )
    parser.add_argument(
        "--dockerfile",
        default=str(default_eval_dir / "Dockerfile"),
        help="Path to eval/Dockerfile for docker isolation mode",
    )
    parser.add_argument(
        "--agent-bin",
        default=os.environ.get("DEVPACK_EVAL_AGENT_BIN", "claude"),
        help="Agent CLI executable name or path (default: claude)",
    )
    parser.add_argument(
        "--claude-version",
        default=DEFAULT_CLAUDE_VERSION,
        help=(
            "Claude Code version baked into the docker image via --build-arg "
            f"(default: {DEFAULT_CLAUDE_VERSION})"
        ),
    )
    parser.add_argument(
        "--allow-harness-errors",
        action="store_true",
        help="Exit 0 even when some runs were harness errors (default: exit 1)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for ``eval/run_eval.py``."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.k < 1:
        print("error: --k must be >= 1", file=sys.stderr)
        return 2
    if args.timeout_s < 1:
        print("error: --timeout-s must be >= 1", file=sys.stderr)
        return 2
    if args.max_parallel < 1:
        print("error: --max-parallel must be >= 1", file=sys.stderr)
        return 2

    raw_engines = getattr(args, "engines", None) or args.models
    models = [m.strip() for m in raw_engines.split(",") if m.strip()]
    if not models:
        print("error: --engines/--models must specify at least one engine", file=sys.stderr)
        return 2

    fixture_dir = Path(args.fixture_dir).resolve()
    tasks_dir = Path(args.tasks_dir).resolve()
    dockerfile_path = Path(args.dockerfile).resolve()

    loaded_engines: EnginesConfig | None = None
    if args.engines_config:
        cfg_file = Path(args.engines_config)
        if cfg_file.is_file():
            try:
                loaded_engines = load_engines(cfg_file)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2

    engine_map: dict[str, EngineConfig] = {
        m: resolve_engine(m, loaded_engines) for m in models
    }

    try:
        validate_fixture_dir(fixture_dir)
        tasks = load_and_validate_tasks(tasks_dir, args.tasks)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    run_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    plan_items: list[tuple[TaskSpec, str, int]] = [
        (task, model, run_idx)
        for task in tasks
        for model in models
        for run_idx in range(1, args.k + 1)
    ]

    if args.dry_run:
        print(f"DRY-RUN plan ({len(plan_items)} total trials):")
        print(f"  isolation: {args.isolation}")
        print(f"  engines: {', '.join(models)}")
        print(f"  k: {args.k}")
        print(f"  tasks ({len(tasks)}): {', '.join(t.task_id for t in tasks)}")
        for idx, (task, model, run_idx) in enumerate(plan_items, start=1):
            print(
                f"  [{idx:03d}/{len(plan_items):03d}] "
                f"task={task.task_id} category={task.category} engine={model} run={run_idx}/{args.k}"
            )
        return 0

    needs_cloud_creds = any(eng.is_cloud for eng in engine_map.values())
    credentials_path: Path | None = None
    extra_secrets: list[str] = []

    if args.credentials:
        credentials_path = Path(args.credentials).resolve()
        if not credentials_path.is_file():
            print(
                f"error: credentials file does not exist: {args.credentials}",
                file=sys.stderr,
            )
            return 1
        cred_secret_text = credentials_path.read_text(encoding="utf-8").strip()
        extra_secrets.extend(
            s for s in (cred_secret_text, str(credentials_path), args.credentials) if s
        )
        try:
            extra_secrets.extend(json_string_leaves(json.loads(cred_secret_text)))
        except json.JSONDecodeError:
            pass
    elif needs_cloud_creds:
        print(
            "error: credentials file required via --credentials or DEVPACK_CLAUDE_CREDENTIALS",
            file=sys.stderr,
        )
        return 1

    repo_root = Path(__file__).resolve().parent.parent
    work_root = Path(args.work_root).resolve()
    path_replacements = [
        (str(work_root), "<WORK_ROOT>"),
        (str(repo_root), "<REPO_ROOT>"),
    ]

    image_tag = docker_image_tag(args.claude_version)
    docker_bin: str | None = None
    if args.isolation == "docker":
        try:
            docker_bin = ensure_docker_image(
                dockerfile_path=dockerfile_path,
                image_tag=image_tag,
                claude_version=args.claude_version,
            )
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    out_dir = Path(args.out)
    dated_dir = out_dir / date_str
    runs_root = Path(args.runs_root) if args.runs_root else (dated_dir / "runs")
    runs_root.mkdir(parents=True, exist_ok=True)

    run_root = work_root / run_id
    run_root.mkdir(parents=True, exist_ok=True)

    trials: list[TrialResult] = []
    try:
        if args.max_parallel == 1:
            for task, model, run_idx in plan_items:
                trials.append(
                    run_single_trial(
                        task=task,
                        model=model,
                        run_index=run_idx,
                        run_root=run_root,
                        fixture_dir=fixture_dir,
                        credentials_path=credentials_path,
                        isolation=args.isolation,
                        agent_bin=args.agent_bin,
                        timeout_s=args.timeout_s,
                        docker_bin=docker_bin,
                        extra_secrets=extra_secrets,
                        path_replacements=path_replacements,
                        image_tag=image_tag,
                        runs_root=runs_root,
                        engine_config=engine_map[model],
                    )
                )
        else:
            with ThreadPoolExecutor(max_workers=args.max_parallel) as pool:
                futures = [
                    pool.submit(
                        run_single_trial,
                        task,
                        model,
                        run_idx,
                        run_root,
                        fixture_dir,
                        credentials_path,
                        args.isolation,
                        args.agent_bin,
                        args.timeout_s,
                        docker_bin,
                        extra_secrets,
                        path_replacements,
                        image_tag,
                        runs_root,
                        engine_map[model],
                    )
                    for task, model, run_idx in plan_items
                ]
                for fut in as_completed(futures):
                    trials.append(fut.result())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    task_summaries, category_summaries = aggregate_results(
        tasks=tasks,
        models=models,
        k=args.k,
        trials=trials,
    )

    raw_payload: dict[str, Any] = {
        "run_id": run_id,
        "date": date_str,
        "isolation": args.isolation,
        "models": models,
        "engines": models,
        "k": args.k,
        "runs_root": str(runs_root),
        "tasks": [asdict(ts) for ts in task_summaries],
        "categories": category_summaries,
    }
    redacted_payload = redact_structure(
        raw_payload,
        extra_secrets=extra_secrets,
        path_replacements=path_replacements,
        home_placeholder="$HOME",
    )

    raw_markdown = render_summary_markdown(
        run_id=run_id,
        date_str=date_str,
        isolation=args.isolation,
        k=args.k,
        models=models,
        task_summaries=task_summaries,
        category_summaries=category_summaries,
    )
    redacted_markdown = redact_sensitive_text(
        raw_markdown,
        extra_secrets=extra_secrets,
        path_replacements=path_replacements,
        home_placeholder="$HOME",
    )

    for target_dir in (out_dir, dated_dir):
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "results.json").write_text(
            json.dumps(redacted_payload, indent=2) + "\n",
            encoding="utf-8",
        )
        (target_dir / "summary.md").write_text(
            redacted_markdown,
            encoding="utf-8",
        )

    harness_total = sum(1 for t in trials if t.harness_error)
    if harness_total:
        print(
            f"warning: {harness_total} run(s) were harness errors "
            "(excluded from pass metrics); see results.json",
            file=sys.stderr,
        )
        if not args.allow_harness_errors:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
