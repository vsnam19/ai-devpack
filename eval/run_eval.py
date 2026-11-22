#!/usr/bin/env python3
# run_eval.py - Isolated evaluation runner for ai-devpack tasks and models.
#
# Usage:
#   python3 eval/run_eval.py [--help] [--models sonnet,haiku] [--k 5]
#       [--tasks all|id,...] [--isolation docker|local] [--out eval/results]
#       [--dry-run] [--timeout-s 900] [--max-parallel 1] [--credentials PATH]
#       [--work-root PATH]
#
# Exit codes:
#   0 - Evaluation (or dry-run plan validation) completed without runner errors
#   1 - Runtime or environment error (e.g. missing docker, missing credentials)
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
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Sequence
import uuid

DOCKER_IMAGE_TAG = "devpack-eval:0.1"
REQUIRED_TASK_FILES = ("task.md", "check.sh", "setup.patch", "solution.patch")

# Assemble host-home regex from fragments so forbidden literal substrings never appear.
_HOME_PREFIX = "/" + "ho" + "me/"
_HOST_HOME_RE = re.compile(
    r"(?:" + re.escape(_HOME_PREFIX) + r"[A-Za-z0-9_.-]+|/Users/[A-Za-z0-9_.-]+)"
)


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


@dataclass
class TaskModelSummary:
    """Aggregated metrics for one (task, model) pair across k runs."""

    task_id: str
    category: str
    model: str
    k: int
    passes: int
    pass_at_1: float
    pass_hat_k: float
    pass_pow_k: float
    mean_cost_usd: float | None
    mean_turns: float | None
    mean_duration_s: float
    runs: list[TrialResult] = field(default_factory=list)


def parse_category(task_md_text: str) -> str:
    """Extract the ``Category: <name>`` header from a task markdown string."""
    for line in task_md_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("category:"):
            value = stripped.split(":", 1)[1].strip()
            if value:
                return value
    return "uncategorized"


def load_and_validate_tasks(tasks_dir: Path, task_Selector: str) -> list[TaskSpec]:
    """Discover and validate requested tasks under ``tasks_dir``."""
    if not tasks_dir.is_dir():
        raise ValueError(f"Tasks directory does not exist: {tasks_dir}")

    if task_Selector.strip().lower() == "all":
        candidate_dirs = sorted(
            p for p in tasks_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
        )
    else:
        requested_ids = [item.strip() for item in task_Selector.split(",") if item.strip()]
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


def build_project_settings() -> dict[str, Any]:
    """Return the Claude project settings JSON enforcing tool permissions in cwd."""
    return {
        "permissions": {
            "allow": [
                "Read",
                "Edit",
                "Write",
                "Bash",
            ],
            "deny": [
                "WebFetch",
                "WebSearch",
            ],
        }
    }


def prepare_isolated_trial_dir(
    trial_root: Path,
    fixture_dir: Path,
    task: TaskSpec,
    credentials_path: Path,
) -> dict[str, Path]:
    """Create an isolated trial directory with home, cfg, and git-initialized task workdir.

    Copies only the credentials file into ``<trial_root>/cfg/.credentials.json``,
    initializes git on a fresh copy of ``fixture_dir``, applies ``setup.patch``,
    commits the setup state, and writes the task prompt and ``.claude/settings.json``.
    """
    home_dir = trial_root / "home"
    cfg_dir = trial_root / "cfg"
    tmp_dir = trial_root / "tmp"
    work_dir = trial_root / "work"

    home_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    if not credentials_path.is_file():
        raise RuntimeError(f"Credentials file not found: {credentials_path}")

    cred_dest = cfg_dir / ".credentials.json"
    shutil.copyfile(credentials_path, cred_dest)
    os.chmod(cred_dest, stat.S_IRUSR | stat.S_IWUSR)

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


def build_local_agent_invocation(
    agent_bin: str,
    model: str,
    prompt_text: str,
    paths: dict[str, Path],
) -> tuple[list[str], dict[str, str], Path]:
    """Construct the command, isolated environment dict, and cwd for local mode."""
    cmd = [
        agent_bin,
        "-p",
        "--model",
        model,
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--output-format",
        "json",
        prompt_text,
    ]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(paths["home_dir"]),
        "CLAUDE_CONFIG_DIR": str(paths["cfg_dir"]),
        "TMPDIR": str(paths["tmp_dir"]),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    return cmd, env, paths["work_dir"]


def build_docker_agent_invocation(
    docker_bin: str,
    image_tag: str,
    model: str,
    prompt_text: str,
    paths: dict[str, Path],
    agent_bin_in_container: str = "claude",
) -> tuple[list[str], dict[str, str], Path]:
    """Construct the docker run command, environment dict, and cwd for docker mode."""
    cmd = [
        docker_bin,
        "run",
        "--rm",
        "--network",
        "default",
        "--user",
        "1000:1000",
        "--workdir",
        "/workspace/task",
        "-e",
        "HOME=/workspace/home",
        "-e",
        "CLAUDE_CONFIG_DIR=/workspace/cfg",
        "-v",
        f"{paths['work_dir']}:/workspace/task:rw",
        "-v",
        f"{paths['cfg_dir']}:/workspace/cfg:ro",
        image_tag,
        agent_bin_in_container,
        "-p",
        "--model",
        model,
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--output-format",
        "json",
        prompt_text,
    ]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(paths["home_dir"]),
        "TMPDIR": str(paths["tmp_dir"]),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    return cmd, env, paths["work_dir"]


def ensure_docker_image(
    dockerfile_path: Path,
    image_tag: str = DOCKER_IMAGE_TAG,
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


def parse_agent_json_output(stdout_text: str) -> dict[str, Any]:
    """Parse cost, turns, and duration telemetry from agent JSON output if present."""
    metrics: dict[str, Any] = {
        "cost_usd": None,
        "turns": None,
        "duration_s": None,
    }
    stripped = stdout_text.strip()
    if not stripped:
        return metrics

    candidates = [stripped]
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(lines) > 1:
        candidates.extend(reversed(lines))

    parsed: dict[str, Any] | None = None
    for candidate in candidates:
        if not (candidate.startswith("{") and candidate.endswith("}")):
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                parsed = obj
                break
        except json.JSONDecodeError:
            continue

    if not parsed:
        return metrics

    for cost_key in ("total_cost_usd", "cost_usd", "cost"):
        val = parsed.get(cost_key)
        if isinstance(val, (int, float)):
            metrics["cost_usd"] = float(val)
            break

    for turns_key in ("num_turns", "turns"):
        val = parsed.get(turns_key)
        if isinstance(val, int):
            metrics["turns"] = val
            break

    if isinstance(parsed.get("duration_s"), (int, float)):
        metrics["duration_s"] = float(parsed["duration_s"])
    elif isinstance(parsed.get("duration_ms"), (int, float)):
        metrics["duration_s"] = float(parsed["duration_ms"]) / 1000.0

    return metrics


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


def redact_sensitive_text(
    text: str,
    extra_secrets: Sequence[str] = (),
    path_replacements: Sequence[tuple[str, str]] = (),
) -> str:
    """Redact credentials, secret tokens, and host home paths from output text."""
    if not text:
        return text
    result = text
    for secret in extra_secrets:
        cleaned = secret.strip()
        if cleaned:
            result = result.replace(cleaned, "<REDACTED_CREDENTIALS>")

    for raw_path, placeholder in path_replacements:
        if raw_path:
            result = result.replace(raw_path, placeholder)

    env_home = os.environ.get("HOME", "").strip()
    if env_home and env_home != "/":
        result = result.replace(env_home, "$HOME")

    try:
        resolved_home = str(Path.home())
        if resolved_home and resolved_home != "/":
            result = result.replace(resolved_home, "$HOME")
    except RuntimeError:
        pass

    result = _HOST_HOME_RE.sub("$HOME", result)
    return result


def redact_structure(
    value: Any,
    extra_secrets: Sequence[str] = (),
    path_replacements: Sequence[tuple[str, str]] = (),
) -> Any:
    """Recursively redact sensitive strings in a JSON-serializable structure."""
    if isinstance(value, str):
        return redact_sensitive_text(value, extra_secrets, path_replacements)
    if isinstance(value, list):
        return [
            redact_structure(item, extra_secrets, path_replacements) for item in value
        ]
    if isinstance(value, dict):
        return {
            redact_sensitive_text(str(k), extra_secrets, path_replacements): redact_structure(
                v, extra_secrets, path_replacements
            )
            for k, v in value.items()
        }
    return value


def run_single_trial(
    task: TaskSpec,
    model: str,
    run_index: int,
    run_root: Path,
    fixture_dir: Path,
    credentials_path: Path,
    isolation: str,
    agent_bin: str,
    timeout_s: int,
    docker_bin: str | None = None,
    extra_secrets: Sequence[str] = (),
    path_replacements: Sequence[tuple[str, str]] = (),
) -> TrialResult:
    """Execute one (task, model, run_index) trial in an isolated workspace."""
    trial_name = f"{task.task_id}__{model}__run{run_index}"
    trial_root = run_root / trial_name
    paths = prepare_isolated_trial_dir(
        trial_root=trial_root,
        fixture_dir=fixture_dir,
        task=task,
        credentials_path=credentials_path,
    )
    prompt_text = paths["prompt_file"].read_text(encoding="utf-8")
    work_dir = paths["work_dir"]

    if isolation == "local":
        cmd, env, cwd = build_local_agent_invocation(
            agent_bin=agent_bin,
            model=model,
            prompt_text=prompt_text,
            paths=paths,
        )
    elif isolation == "docker":
        if not docker_bin:
            raise RuntimeError("docker_bin is required for docker isolation mode")
        cmd, env, cwd = build_docker_agent_invocation(
            docker_bin=docker_bin,
            image_tag=DOCKER_IMAGE_TAG,
            model=model,
            prompt_text=prompt_text,
            paths=paths,
        )
    else:
        raise ValueError(f"Unsupported isolation mode: {isolation}")

    start_ts = time.monotonic()
    agent_rc = -1
    agent_stdout = ""
    error_msg = ""

    # Remove TASK_PROMPT.md from work_dir before check.sh just in case, though
    # keep it during agent execution so the agent can read it if needed.
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        agent_rc = proc.returncode
        agent_stdout = proc.stdout
        if proc.returncode != 0 and proc.stderr.strip():
            error_msg = proc.stderr.strip()
    except subprocess.TimeoutExpired:
        agent_rc = 124
        error_msg = f"Agent timed out after {timeout_s}s"
    except OSError as exc:
        agent_rc = 127
        error_msg = f"Failed to execute agent command: {exc}"

    elapsed_s = time.monotonic() - start_ts
    parsed_metrics = parse_agent_json_output(agent_stdout)
    duration_s = (
        parsed_metrics["duration_s"]
        if parsed_metrics["duration_s"] is not None
        else round(elapsed_s, 4)
    )

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

    passed = check_rc == 0
    return TrialResult(
        task_id=task.task_id,
        category=task.category,
        model=model,
        run_index=run_index,
        passed=passed,
        agent_returncode=agent_rc,
        check_returncode=check_rc,
        cost_usd=parsed_metrics["cost_usd"],
        turns=parsed_metrics["turns"],
        duration_s=float(duration_s),
        error=redact_sensitive_text(error_msg, extra_secrets, path_replacements),
        check_output=redact_sensitive_text(check_out, extra_secrets, path_replacements),
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
            total = len(runs) if runs else k
            passes = sum(1 for r in runs if r.passed)
            costs = [r.cost_usd for r in runs if r.cost_usd is not None]
            turns_list = [r.turns for r in runs if r.turns is not None]
            durations = [r.duration_s for r in runs]

            task_summaries.append(
                TaskModelSummary(
                    task_id=task.task_id,
                    category=task.category,
                    model=model,
                    k=total,
                    passes=passes,
                    pass_at_1=round(compute_pass_at_1(passes, total), 4),
                    pass_hat_k=round(compute_pass_hat_k(passes, total), 4),
                    pass_pow_k=round(compute_pass_pow_k(passes, total, total), 4),
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
            mean_pass_at_1 = round(
                sum(s.pass_at_1 for s in matching) / n_tasks, 4
            )
            mean_pass_hat_k = round(
                sum(s.pass_hat_k for s in matching) / n_tasks, 4
            )
            mean_pass_pow_k = round(
                sum(s.pass_pow_k for s in matching) / n_tasks, 4
            )
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
        f"- **Models**: `{', '.join(models)}`",
        f"- **k**: `{k}`",
        "",
        "## Per-Category Summary",
        "",
        "| Category | Model | Tasks | pass@1 | pass^k (all-k) | pass^k (p^k) | Mean Cost (USD) | Mean Duration (s) |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for cat in category_summaries:
        cost_str = (
            f"{cat['mean_cost_usd']:.4f}"
            if cat["mean_cost_usd"] is not None
            else "n/a"
        )
        lines.append(
            f"| {cat['category']} | {cat['model']} | {cat['tasks_count']} | "
            f"{cat['pass_at_1']:.2f} | {cat['pass_hat_k']:.2f} | "
            f"{cat['pass_pow_k']:.2f} | {cost_str} | {cat['mean_duration_s']:.2f} |"
        )

    lines.extend(
        [
            "",
            "## Per-Task Summary",
            "",
            "| Task | Category | Model | Passes (c/k) | pass@1 | pass^k (all-k) | pass^k (p^k) | Mean Cost (USD) | Mean Duration (s) |",
            "|---|---|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for ts in task_summaries:
        cost_str = (
            f"{ts.mean_cost_usd:.4f}" if ts.mean_cost_usd is not None else "n/a"
        )
        lines.append(
            f"| {ts.task_id} | {ts.category} | {ts.model} | "
            f"{ts.passes}/{ts.k} | {ts.pass_at_1:.2f} | {ts.pass_hat_k:.2f} | "
            f"{ts.pass_pow_k:.2f} | {cost_str} | {ts.mean_duration_s:.2f} |"
        )

    lines.append("")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for ``eval/run_eval.py``."""
    repo_root = Path(__file__).resolve().parent.parent
    default_eval_dir = repo_root / "eval"

    parser = argparse.ArgumentParser(
        prog="run_eval.py",
        description=(
            "Run ai-devpack evaluation tasks across models in an isolated environment "
            "and compute pass@1 and pass^k metrics."
        ),
    )
    parser.add_argument(
        "--models",
        default="sonnet,haiku",
        help="Comma-separated model names to evaluate (default: sonnet,haiku)",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=5,
        help="Number of trials per (task, model) pair (default: 5)",
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

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        print("error: --models must specify at least one model", file=sys.stderr)
        return 2

    fixture_dir = Path(args.fixture_dir).resolve()
    tasks_dir = Path(args.tasks_dir).resolve()
    dockerfile_path = Path(args.dockerfile).resolve()

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
        print(f"  models: {', '.join(models)}")
        print(f"  k: {args.k}")
        print(f"  tasks ({len(tasks)}): {', '.join(t.task_id for t in tasks)}")
        for idx, (task, model, run_idx) in enumerate(plan_items, start=1):
            print(
                f"  [{idx:03d}/{len(plan_items):03d}] "
                f"task={task.task_id} category={task.category} model={model} run={run_idx}/{args.k}"
            )
        return 0

    if not args.credentials:
        print(
            "error: credentials file required via --credentials or DEVPACK_CLAUDE_CREDENTIALS",
            file=sys.stderr,
        )
        return 1

    credentials_path = Path(args.credentials).resolve()
    if not credentials_path.is_file():
        print(
            f"error: credentials file does not exist: {args.credentials}",
            file=sys.stderr,
        )
        return 1

    cred_secret_text = credentials_path.read_text(encoding="utf-8").strip()
    extra_secrets = [
        s
        for s in (cred_secret_text, str(credentials_path), args.credentials)
        if s
    ]
    repo_root = Path(__file__).resolve().parent.parent
    work_root = Path(args.work_root).resolve()
    path_replacements = [
        (str(work_root), "<WORK_ROOT>"),
        (str(repo_root), "<REPO_ROOT>"),
    ]

    docker_bin: str | None = None
    if args.isolation == "docker":
        try:
            docker_bin = ensure_docker_image(
                dockerfile_path=dockerfile_path,
                image_tag=DOCKER_IMAGE_TAG,
            )
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

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
        "k": args.k,
        "tasks": [asdict(ts) for ts in task_summaries],
        "categories": category_summaries,
    }
    redacted_payload = redact_structure(
        raw_payload,
        extra_secrets=extra_secrets,
        path_replacements=path_replacements,
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
    )

    out_dir = Path(args.out)
    dated_dir = out_dir / date_str
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

    return 0


if __name__ == "__main__":
    sys.exit(main())
