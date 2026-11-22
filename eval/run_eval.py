#!/usr/bin/env python3
# run_eval.py - Isolated evaluation runner for ai-devpack tasks and engines.
#
# Usage:
#   python3 eval/run_eval.py [--help] [--mode single|runner|orchestrator]
#       [--engines sonnet,haiku] [--models sonnet,haiku] [--pairs haiku:sonnet]
#       [--orchestrator sonnet] [--workers haiku] [--tier smoke|v2|all]
#       [--k 5] [--tasks all|id,...] [--isolation docker|local] [--allow-unsandboxed-local]
#       [--out eval/results] [--runs-root eval/results/<date>/runs]
#       [--engines-config runner/engines.example.toml] [--dry-run] [--timeout-s 900]
#       [--max-parallel 1] [--credentials PATH] [--work-root PATH]
#       [--claude-version 2.1.330] [--allow-harness-errors]
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
import signal
import time
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
from runner import devpack_run  # noqa: E402
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
    check_local_isolation_allowed,
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
from runner.devpack_dispatch import run_preflight_lint_on_changed  # noqa: E402
from enforcement.checks.scope_check import get_changed_files  # noqa: E402

EXCERPT_MAX_CHARS = 4000
STDERR_TAIL_MAX_CHARS = 2000
ORCHESTRATOR_PACK_PATH = Path(".devpack/pack")

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
    hidden_dir: Path | None
    tier: str = "smoke"


@dataclass(frozen=True)
class OrchestratorGoalSpec:
    """Metadata and paths for a single orchestrator-mode evaluation goal."""

    goal_id: str
    category: str
    tier: str
    goal_dir: Path
    goal_md: Path
    check_sh: Path


@dataclass
class TrialResult:
    """Result record for a single evaluation trial across single, runner, or orchestrator mode."""

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
    timed_out: bool = False
    engine_class: str = "cloud"
    tokens_in: int = 0
    tokens_out: int = 0
    local_tokens: int = 0
    local_wall_s: float = 0.0
    lint_findings_first_attempt: int | None = None
    lint_clean_final: bool | None = None
    rounds_to_clean: int | None = None
    mode: str = "single"
    rounds_used: int | None = None
    escalated: bool = False
    reviewer_verdict: str = ""
    reviewer_false_approve: bool = False
    reviewer_false_reject: bool = False
    orchestrator_product_edits: int = 0
    dispatch_calls: int = 0
    orchestrator_compliant: bool = True
    disallowed_bash_commands: list[str] = field(default_factory=list)
    denied_tool_calls: dict[str, Any] = field(default_factory=lambda: {"count": 0, "commands": []})


@dataclass
class TaskModelSummary:
    """Aggregated metrics for one (task, engine/pair) across k runs."""

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
    timeouts: int = 0
    valid_runs: int = 0
    tool_protocol_errors: int = 0
    engine_class: str = "cloud"
    mean_tokens_in: float | None = None
    mean_tokens_out: float | None = None
    mean_lint_findings_first_attempt: float | None = None
    lint_clean_final_rate: float | None = None
    mean_rounds_to_clean: float | None = None
    mean_local_tokens: float | None = None
    mean_local_wall_s: float | None = None
    mean_rounds_used: float | None = None
    escalate_rate: float | None = None
    false_approve_count: int = 0
    false_reject_count: int = 0
    false_approve_rate: float | None = None
    false_reject_rate: float | None = None


def parse_category(task_md_text: str) -> str:
    """Extract the ``Category: <name>`` header from a task markdown string."""
    for line in task_md_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("category:"):
            value = stripped.split(":", 1)[1].strip()
            if value:
                return value
    return "uncategorized"


def parse_tier(task_md_text: str) -> str:
    """Extract the ``Tier: <name>`` header from a task markdown string (default: ``smoke``)."""
    for line in task_md_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("tier:"):
            value = stripped.split(":", 1)[1].strip().lower()
            if value:
                return value
    return "smoke"


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
        task_md_path = tdir / "task.md"
        task_text = task_md_path.read_text(encoding="utf-8")
        category = parse_category(task_text)
        tier = parse_tier(task_text)
        hidden_dir: Path | None = tdir / "hidden"
        if category.strip().lower() == "review":
            # Review tasks are graded on REVIEW.md and carry no hidden tests; they must ship
            # the seeded diff instead.
            if not (tdir / "review.patch").is_file():
                raise ValueError(
                    f"Review task '{tdir.name}' is missing required 'review.patch'"
                )
            if not hidden_dir.is_dir():
                hidden_dir = None
        elif not hidden_dir.is_dir():
            raise ValueError(f"Task '{tdir.name}' is missing required 'hidden/' directory")

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
                tier=tier,
            )
        )
    return specs


def filter_tasks_by_tier(tasks: Sequence[TaskSpec], tier: str) -> list[TaskSpec]:
    """Filter ``tasks`` by ``tier`` (``smoke``, ``v2``, or ``all``)."""
    normalized = tier.strip().lower() or "all"
    if normalized == "all":
        return list(tasks)
    filtered = [t for t in tasks if t.tier.lower() == normalized]
    if not filtered:
        raise ValueError(f"No tasks match --tier {tier}")
    return filtered


def load_and_validate_orchestrator_goals(
    goals_dir: Path,
    selector: str = "all",
    tier: str = "all",
) -> list[OrchestratorGoalSpec]:
    """Discover and validate orchestrator evaluation goals under ``goals_dir``."""
    if not goals_dir.is_dir():
        raise ValueError(f"Orchestrator goals directory does not exist: {goals_dir}")

    if selector.strip().lower() == "all":
        candidate_dirs = sorted(
            p for p in goals_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
        )
    else:
        requested = [item.strip() for item in selector.split(",") if item.strip()]
        if not requested:
            raise ValueError("No goal IDs provided")
        candidate_dirs = [goals_dir / gid for gid in requested]

    goals: list[OrchestratorGoalSpec] = []
    for gdir in candidate_dirs:
        if not gdir.is_dir():
            raise ValueError(f"Orchestrator goal directory not found: {gdir}")
        goal_md = gdir / "goal.md"
        check_sh = gdir / "check.sh"
        if not goal_md.is_file():
            raise ValueError(f"Orchestrator goal '{gdir.name}' is missing 'goal.md'")
        if not check_sh.is_file():
            raise ValueError(f"Orchestrator goal '{gdir.name}' is missing 'check.sh'")
        text = goal_md.read_text(encoding="utf-8")
        g_tier = parse_tier(text)
        if tier.strip().lower() not in ("", "all") and g_tier != tier.strip().lower():
            continue
        goals.append(
            OrchestratorGoalSpec(
                goal_id=gdir.name,
                category=parse_category(text) if "Category:" in text else "orchestrator-goal",
                tier=g_tier,
                goal_dir=gdir,
                goal_md=goal_md,
                check_sh=check_sh,
            )
        )
    if not goals:
        raise ValueError(f"No orchestrator goals matched in {goals_dir}")
    return goals


def _is_brief_or_runs_path(raw_path: str) -> bool:
    """Return True if ``raw_path`` points inside ``briefs/`` or ``runs/``."""
    norm = raw_path.strip().replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    parts = [p for p in norm.split("/") if p]
    return "briefs" in parts or "runs" in parts


def orchestrator_bash_allowed(command: str) -> bool:
    """Accept only simple allowlisted commands, refusing shell composition and expansion."""
    import shlex
    if any(character in command for character in "\n\r;&|<>`$()\\"):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if words[:2] in (["git", "status"], ["git", "diff"], ["git", "log"]):
        return True
    return words[:3] == ["python3", ".devpack/pack/runner/devpack_dispatch.py", "--submit"]


def analyze_orchestrator_transcript(transcript_or_path: Path | str) -> dict[str, Any]:
    """Inspect an orchestrator stream-json transcript for product edits and dispatch usage.

    Verifies two invariants from SPEC section 5.2:
    1. The orchestrator made zero Edit/Write/NotebookEdit tool calls outside
       ``briefs/**`` and ``runs/**`` (i.e. zero direct edits to product/test files).
    Bash violations require a matching tool result without a tool-policy denial.
    Denied Bash attempts are reported separately as count and commands.
    2. Worker jobs were invoked via ``devpack_dispatch.py`` (and not via native
       ``Task`` / ``Agent`` subagent bypasses).
    """
    if isinstance(transcript_or_path, Path):
        raw_text = (
            transcript_or_path.read_text(encoding="utf-8")
            if transcript_or_path.is_file()
            else ""
        )
    else:
        candidate = Path(transcript_or_path)
        if "\n" not in transcript_or_path and not transcript_or_path.lstrip().startswith("{") and candidate.is_file():
            raw_text = candidate.read_text(encoding="utf-8")
        else:
            raw_text = str(transcript_or_path)

    product_edit_paths: list[str] = []
    dispatch_commands: list[str] = []
    subagent_calls = 0
    disallowed_bash_commands: list[str] = []
    bash_calls: dict[str, str] = {}
    tool_results: dict[str, dict[str, Any]] = {}
    denied_commands: list[str] = []

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") not in ("assistant", "user"):
            continue
        msg = event.get("message")
        if not isinstance(msg, dict):
            msg = event
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                tool_results[str(block.get("tool_use_id") or "")] = block
                continue
            if event.get("type") != "assistant" or block.get("type") != "tool_use":
                continue
            tool_name = str(block.get("name") or "")
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                tool_input = {}

            if tool_name in ("Edit", "Write", "NotebookEdit"):
                target = str(
                    tool_input.get("file_path")
                    or tool_input.get("path")
                    or tool_input.get("notebook_path")
                    or ""
                )
                if not target or not _is_brief_or_runs_path(target):
                    product_edit_paths.append(target or "<unknown>")
            elif tool_name == "Bash":
                cmd = str(tool_input.get("command") or tool_input.get("cmd") or "")
                allowed = orchestrator_bash_allowed(cmd)
                if block.get("id"):
                    bash_calls[str(block["id"])] = cmd
                if allowed and cmd.startswith("python3 .devpack/pack/runner/devpack_dispatch.py --submit"):
                    dispatch_commands.append(cmd)
            elif tool_name in ("Task", "Agent"):
                subagent_calls += 1

    for call_id, command in bash_calls.items():
        result = tool_results.get(call_id)
        if result is None:
            continue
        content_text = json.dumps(result.get("content", "")).lower()
        denied = any(marker in content_text for marker in (
            "permission to use bash has been denied",
            "permission request denied", "not allowed to use bash",
        ))
        if denied:
            denied_commands.append(command)
        elif not orchestrator_bash_allowed(command):
            disallowed_bash_commands.append(command)

    product_edits = len(product_edit_paths)
    dispatch_count = len(dispatch_commands)
    compliant = product_edits == 0 and dispatch_count >= 1 and subagent_calls == 0 and not disallowed_bash_commands
    return {
        "orchestrator_product_edits": product_edits,
        "product_edit_paths": product_edit_paths,
        "dispatch_calls": dispatch_count,
        "dispatch_commands": dispatch_commands,
        "disallowed_bash_commands": disallowed_bash_commands,
        "denied_tool_calls": {"count": len(denied_commands), "commands": denied_commands},
        "subagent_calls": subagent_calls,
        "compliant": compliant,
    }


def parse_runner_pairs(raw_pairs: str) -> list[tuple[str, str, str]]:
    """Parse ``--pairs`` (e.g. ``haiku:sonnet,sonnet:haiku``) into ``(impl, rev, fixer)`` tuples."""
    pairs: list[tuple[str, str, str]] = []
    for raw in raw_pairs.split(","):
        item = raw.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split(":")]
        if len(parts) == 2 and all(parts):
            pairs.append((parts[0], parts[1], parts[0]))
        elif len(parts) == 3 and all(parts):
            pairs.append((parts[0], parts[1], parts[2]))
        else:
            raise ValueError(
                f"Invalid --pairs entry '{item}': expected implementer:reviewer (or impl:rev:fixer)"
            )
    if not pairs:
        raise ValueError("--pairs must specify at least one implementer:reviewer pair")
    return pairs


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


def _resolve_baseline_ref(work_dir: Path) -> str:
    """Return the post-setup baseline commit SHA for ``work_dir``."""
    for grep_pat in ("^chore: apply setup\\.patch$", "^chore: base fixture$"):
        proc = subprocess.run(
            ["git", "-C", str(work_dir), "log", "--format=%H", f"--grep={grep_pat}", "-n", "1"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return "HEAD"


def _run_check_script(
    check_sh: Path,
    work_dir: Path,
    home_dir: Path,
    tmp_dir: Path,
    timeout_s: int,
) -> tuple[int, str]:
    """Execute ``check_sh --workdir <work_dir>`` with ``DEVPACK_ROOT`` set."""
    check_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home_dir),
        "TMPDIR": str(tmp_dir),
        "DEVPACK_ROOT": str(REPO_ROOT),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    try:
        check_proc = subprocess.run(
            ["bash", str(check_sh), "--workdir", str(work_dir)],
            cwd=str(work_dir),
            env=check_env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return check_proc.returncode, (check_proc.stdout + "\n" + check_proc.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"check.sh timed out after {timeout_s}s"


def _parse_task_allowed_files(task_md_text: str) -> str:
    """Extract comma-separated allowed file globs from ``## Files allowed`` in task.md."""
    in_allowed = False
    items: list[str] = []
    for raw in task_md_text.splitlines():
        stripped = raw.strip()
        if stripped == "## Files allowed":
            in_allowed = True
            continue
        if in_allowed:
            if stripped.startswith("## "):
                break
            if stripped.startswith("- "):
                val = stripped[2:].strip().strip("`")
                if val:
                    items.append(val)
    return ", ".join(items) if items else "src/*, include/*, tests/*"


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
    allow_unsandboxed_local: bool = False,
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
            allow_unsandboxed_local=allow_unsandboxed_local,
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
    allow_unsandboxed_local: bool = False,
) -> TrialResult:
    """Run the agent via agent_exec and execute check.sh for one single-mode trial."""
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
        allow_unsandboxed_local=allow_unsandboxed_local,
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
    timed_out = agent_res.status == "timeout"
    is_harness_err = agent_res.status == "harness_error"
    harness_reason = agent_res.error if is_harness_err else ""
    error_msg = agent_res.error if agent_res.status != "ok" else ""

    task_prompt_in_work = work_dir / "TASK_PROMPT.md"
    if task_prompt_in_work.exists():
        task_prompt_in_work.unlink()

    lint_findings_first: int | None = None
    lint_clean_final: bool | None = None
    rounds_to_clean: int | None = None
    if not is_harness_err and not timed_out:
        base_ref = _resolve_baseline_ref(work_dir)
        changed_files, _ = get_changed_files(work_dir, base_ref)
        lint_ok, lint_count, _lint_out = run_preflight_lint_on_changed(
            work_dir, changed_files or [], pack_root=REPO_ROOT
        )
        lint_findings_first = lint_count
        lint_clean_final = lint_ok
        rounds_to_clean = 0 if lint_ok else None

    check_rc, check_out = _run_check_script(
        task.check_sh,
        work_dir,
        paths["home_dir"],
        paths["tmp_dir"],
        timeout_s,
    )
    if check_rc == 2 or "GRADER_MISCONFIGURED" in check_out:
        is_harness_err = True
        if not harness_reason:
            harness_reason = f"grader misconfigured (exit {check_rc}): {check_out}"

    redacted_check_out = _redact(check_out)
    gate_step_path = trial_run_dir / "steps" / "02-gate.txt"
    gate_step_path.write_text(
        (redacted_check_out + "\n") if redacted_check_out else f"check_rc={check_rc}\n",
        encoding="utf-8",
    )

    passed = check_rc == 0 and not is_harness_err and not timed_out
    outcome = (
        "pass"
        if passed
        else ("harness_error" if is_harness_err else ("timeout" if timed_out else "fail"))
    )
    update_run_json(
        trial_run_dir,
        outcome=outcome,
        timed_out=timed_out,
        finished=datetime.now(timezone.utc).isoformat(),
    )

    trial_cost: float | None = None
    if not is_harness_err and not engine.is_local:
        trial_cost = agent_res.cost_usd

    return TrialResult(
        task_id=task.task_id,
        category=task.category,
        model=model,
        run_index=run_index,
        passed=passed,
        agent_returncode=agent_res.exit_code,
        check_returncode=check_rc,
        cost_usd=trial_cost,
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
        timed_out=timed_out,
        engine_class=engine.engine_class,
        tokens_in=int(agent_res.tokens_in) if not is_harness_err else 0,
        tokens_out=int(agent_res.tokens_out) if not is_harness_err else 0,
        lint_findings_first_attempt=lint_findings_first,
        lint_clean_final=lint_clean_final,
        rounds_to_clean=rounds_to_clean,
        mode="single",
    )


def run_runner_trial(
    task: TaskSpec,
    pair_label: str,
    impl_engine: EngineConfig,
    rev_engine: EngineConfig,
    fixer_engine: EngineConfig,
    run_index: int,
    run_root: Path,
    fixture_dir: Path,
    credentials_path: Path | None,
    isolation: str,
    agent_bin: str,
    timeout_s: int,
    max_fix_rounds: int = 2,
    docker_bin: str | None = None,
    extra_secrets: Sequence[str] = (),
    path_replacements: Sequence[tuple[str, str]] = (),
    image_tag: str = DOCKER_IMAGE_TAG,
    runs_root: Path | None = None,
    allow_unsandboxed_local: bool = False,
) -> TrialResult:
    """Execute one runner-mode trial (implement -> gate -> review -> fix) and grade it."""
    safe_pair = pair_label.replace(":", "-")
    trial_name = f"{task.task_id}__{safe_pair}__run{run_index}"
    trial_root = run_root / trial_name
    try:
        if isolation not in ("local", "docker"):
            raise ValueError(f"Unsupported isolation mode: {isolation}")
        if isolation == "docker" and not docker_bin:
            raise RuntimeError("docker_bin is required for docker isolation mode")

        any_cloud = impl_engine.is_cloud or rev_engine.is_cloud or fixer_engine.is_cloud
        all_local = impl_engine.is_local and rev_engine.is_local and fixer_engine.is_local
        paths = prepare_isolated_trial_dir(
            trial_root=trial_root,
            fixture_dir=fixture_dir,
            task=task,
            credentials_path=credentials_path if any_cloud else None,
            agent_cwd=DOCKER_TASK_DIR if isolation == "docker" else None,
        )
        work_dir = paths["work_dir"]
        effective_runs_root = runs_root if runs_root is not None else (trial_root / "runs")
        trial_run_dir = new_run_dir(
            effective_runs_root,
            kind="eval",
            task=task.task_id,
            engines=[impl_engine.name, rev_engine.name],
        )

        impl_cfg = EngineConfig(
            name=impl_engine.name,
            model=impl_engine.model,
            engine_class=impl_engine.engine_class,
            max_data_class=impl_engine.max_data_class,
            base_url_env=impl_engine.base_url_env,
            auth_token_env=impl_engine.auth_token_env,
            min_context_tokens=impl_engine.min_context_tokens,
            timeout_s=timeout_s,
            cmd=impl_engine.cmd,
        )
        rev_cfg = EngineConfig(
            name=rev_engine.name,
            model=rev_engine.model,
            engine_class=rev_engine.engine_class,
            max_data_class=rev_engine.max_data_class,
            base_url_env=rev_engine.base_url_env,
            auth_token_env=rev_engine.auth_token_env,
            min_context_tokens=rev_engine.min_context_tokens,
            timeout_s=timeout_s,
            cmd=rev_engine.cmd,
        )
        fix_cfg = EngineConfig(
            name=fixer_engine.name,
            model=fixer_engine.model,
            engine_class=fixer_engine.engine_class,
            max_data_class=fixer_engine.max_data_class,
            base_url_env=fixer_engine.base_url_env,
            auth_token_env=fixer_engine.auth_token_env,
            min_context_tokens=fixer_engine.min_context_tokens,
            timeout_s=timeout_s,
            cmd=fixer_engine.cmd,
        )
        for target, source in ((impl_cfg, impl_engine), (rev_cfg, rev_engine), (fix_cfg, fixer_engine)):
            target.role_permissions = source.role_permissions
        cfg_obj = EnginesConfig(
            version=2,
            engines={
                impl_cfg.name: impl_cfg,
                rev_cfg.name: rev_cfg,
                fix_cfg.name: fix_cfg,
            },
            roles={
                "implementer": impl_cfg.name,
                "reviewer": rev_cfg.name,
                "fixer": fix_cfg.name,
            },
            limits={"max_fix_rounds": max_fix_rounds},
            max_fix_rounds=max_fix_rounds,
            gate_cmd=["bash", str(work_dir / "build_and_test.sh"), "--no-cmake"],
            gate_timeout_s=timeout_s,
            role_engines={
                "implementer": impl_cfg,
                "reviewer": rev_cfg,
                "fixer": fix_cfg,
            },
        )
        task_text = task.task_md.read_text(encoding="utf-8")
        allowed_str = _parse_task_allowed_files(task_text)
        context = devpack_run.load_task_data(
            task_file=paths["prompt_file"],
            task_id=task.task_id,
            title=task.task_id,
            goal=task_text,
            allowed_files=allowed_str,
        )
        _rc, summary = devpack_run.run_task_workflow(
            config=cfg_obj,
            context=context,
            worktree=work_dir,
            log_dir=trial_run_dir,
            repo_root=REPO_ROOT,
            max_fix_rounds_override=max_fix_rounds,
            dry_run=False,
            run_dir=trial_run_dir,
            isolation=isolation,
            allow_unsandboxed_local=allow_unsandboxed_local,
            credentials_path=credentials_path if any_cloud else None,
            agent_bin=agent_bin,
            docker_bin=docker_bin or "docker",
            image_tag=image_tag,
            home_dir=paths["home_dir"],
            cfg_dir=paths["cfg_dir"],
            tmp_dir=paths["tmp_dir"],
            extra_secrets=extra_secrets,
            path_replacements=path_replacements,
            home_placeholder="$HOME",
        )

        task_prompt_in_work = work_dir / "TASK_PROMPT.md"
        if task_prompt_in_work.exists():
            task_prompt_in_work.unlink()

        timed_out = bool(summary.get("timed_out", False))
        is_harness_err = bool(summary.get("harness_error", False))
        harness_reason = str(summary.get("harness_error_reason", ""))

        check_rc, check_out = _run_check_script(
            task.check_sh,
            work_dir,
            paths["home_dir"],
            paths["tmp_dir"],
            timeout_s,
        )
        if check_rc == 2 or "GRADER_MISCONFIGURED" in check_out:
            is_harness_err = True
            if not harness_reason:
                harness_reason = f"grader misconfigured (exit {check_rc}): {check_out}"

        def _redact(text: str) -> str:
            """Redact secrets and paths from text."""
            return redact_sensitive_text(
                text,
                extra_secrets=extra_secrets,
                path_replacements=path_replacements,
                home_placeholder="$HOME",
            )

        redacted_check_out = _redact(check_out)
        passed = check_rc == 0 and not is_harness_err and not timed_out
        verdict = str(summary.get("verdict", ""))
        escalated = summary.get("status") == devpack_run.ESCALATE_TO_HUMAN
        false_approve = (not is_harness_err) and (not timed_out) and (verdict == "APPROVE") and (check_rc != 0)
        false_reject = (not is_harness_err) and (not timed_out) and (verdict == "REQUEST_CHANGES") and (check_rc == 0)

        outcome = (
            "pass"
            if passed
            else ("harness_error" if is_harness_err else ("timeout" if timed_out else "fail"))
        )
        update_run_json(
            trial_run_dir,
            outcome=outcome,
            timed_out=timed_out,
            finished=datetime.now(timezone.utc).isoformat(),
        )

        trial_cost: float | None = None
        if not is_harness_err and not all_local:
            trial_cost = float(summary.get("cost_usd", 0.0) or 0.0)

        return TrialResult(
            task_id=task.task_id,
            category=task.category,
            model=pair_label,
            run_index=run_index,
            passed=passed,
            agent_returncode=0 if not is_harness_err and not timed_out else 1,
            check_returncode=check_rc,
            cost_usd=trial_cost,
            turns=None,
            duration_s=float(summary.get("duration_s", 0.0) or 0.0),
            error=_redact(harness_reason or str(summary.get("escalation_reason", ""))),
            check_output=redacted_check_out,
            harness_error=is_harness_err,
            harness_error_reason=_redact(harness_reason),
            tool_protocol_errors=int(summary.get("tool_protocol_errors", 0) or 0),
            run_dir=_redact(str(trial_run_dir)),
            timed_out=timed_out,
            engine_class="local" if all_local else "cloud",
            tokens_in=int(summary.get("tokens_in", 0) or 0),
            tokens_out=int(summary.get("tokens_out", 0) or 0),
            lint_findings_first_attempt=summary.get("lint_findings_first_attempt"),
            lint_clean_final=summary.get("lint_clean_final"),
            rounds_to_clean=summary.get("rounds_to_clean"),
            local_tokens=int(summary.get("local_tokens", 0) or 0),
            local_wall_s=float(summary.get("local_wall_s", 0.0) or 0.0),
            mode="runner",
            rounds_used=int(summary.get("fix_rounds_used", 0) or 0),
            escalated=escalated,
            reviewer_verdict=verdict,
            reviewer_false_approve=false_approve,
            reviewer_false_reject=false_reject,
        )
    finally:
        cleanup_cfg_dir(trial_root / "cfg")


def run_orchestrator_trial(
    goal: OrchestratorGoalSpec,
    orch_label: str,
    orch_engine: EngineConfig,
    worker_engines_str: str,
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
    allow_unsandboxed_local: bool = False,
    engines_config: Path | None = None,
) -> TrialResult:
    """Execute one orchestrator-mode trial, analyze transcript compliance, and run check.sh.

    ``engines_config`` is forwarded to the broker as an absolute ``--engines`` path so that
    custom engine files (for example local-model workers) are honoured by the broker.
    """
    safe_label = orch_label.replace(":", "-")
    trial_name = f"{goal.goal_id}__{safe_label}__run{run_index}"
    trial_root = run_root / trial_name
    try:
        if isolation != "local" or not allow_unsandboxed_local:
            raise ValueError(
                "Orchestrator sessions require --isolation local --allow-unsandboxed-local; "
                "dispatched workers use Docker by default"
            )

        home_dir = trial_root / "home"
        cfg_dir = trial_root / "cfg"
        tmp_dir = trial_root / "tmp"
        work_dir = trial_root / "work"
        for d in (home_dir, cfg_dir, tmp_dir, work_dir):
            d.mkdir(parents=True, exist_ok=True)

        if orch_engine.is_cloud and credentials_path is not None:
            cred_dest = cfg_dir / ".credentials.json"
            shutil.copyfile(credentials_path, cred_dest)
            os.chmod(cred_dest, stat.S_IRUSR | stat.S_IWUSR)

        agent_cwd = DOCKER_TASK_DIR if isolation == "docker" else str(work_dir)
        (cfg_dir / ".claude.json").write_text(
            json.dumps(build_claude_state(agent_cwd, include_onboarding=True), indent=2) + "\n",
            encoding="utf-8",
        )

        shutil.copytree(fixture_dir, work_dir, dirs_exist_ok=True)
        git_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home_dir),
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C.UTF-8",
        }
        for args_git in (
            ["init", "-q"],
            ["config", "user.email", "eval@example.invalid"],
            ["config", "user.name", "Eval Runner"],
            ["add", "-A"],
            ["commit", "-q", "-m", "chore: base fixture"],
        ):
            proc = subprocess.run(
                ["git", *args_git],
                cwd=str(work_dir),
                env=git_env,
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"git {' '.join(args_git)} failed: {proc.stderr.strip()}")

        # The broker installs only a credential-free client under this path.
        (work_dir / ".devpack").mkdir(exist_ok=True)
        (work_dir / ".devpack/project.toml").write_text(
            'data_class = "internal"\n', encoding="utf-8",
        )
        orch_src = REPO_ROOT / "orchestrator"
        if (orch_src / "CLAUDE.md").is_file():
            shutil.copyfile(orch_src / "CLAUDE.md", work_dir / "CLAUDE.md")
        if (orch_src / ".claude").is_dir():
            shutil.copytree(orch_src / ".claude", work_dir / ".claude", dirs_exist_ok=True)
        (work_dir / "briefs").mkdir(parents=True, exist_ok=True)

        exclude_file = work_dir / ".git" / "info" / "exclude"
        if exclude_file.parent.is_dir():
            existing = exclude_file.read_text(encoding="utf-8") if exclude_file.is_file() else ""
            exclude_file.write_text(
                existing + "\n.claude/\nCLAUDE.md\nbriefs/\nruns/\n",
                encoding="utf-8",
            )

        goal_text = goal.goal_md.read_text(encoding="utf-8")
        dispatch_script = str(ORCHESTRATOR_PACK_PATH / "runner/devpack_dispatch.py")
        prompt_text = (
            (orch_src / "CLAUDE.md").read_text(encoding="utf-8").split("\n\n# Sonnet")[0]
            + "\n\n" + f"{goal_text.strip()}\n\n"
            f"## Dispatch Environment\n"
            f"- Worker engine(s): `{worker_engines_str}`\n"
            f"- Dispatch command: `python3 {dispatch_script} --submit --engine <worker> --brief briefs/<file>.md "
            "--workdir . --allow '<glob>'...`\n"
            "- Never use Edit or Write on files outside `briefs/**`.\n"
        )

        effective_runs_root = runs_root if runs_root is not None else (trial_root / "runs")
        trial_run_dir = new_run_dir(
            effective_runs_root,
            kind="eval",
            task=goal.goal_id,
            engines=[orch_engine.name],
        )

        request = AgentRequest(
            engine=orch_engine,
            prompt=prompt_text,
            workdir=work_dir,
            run_dir=trial_run_dir,
            step_name=f"01-orchestrator-{orch_engine.name}",
            isolation="local",
            allow_unsandboxed_local=allow_unsandboxed_local,
            timeout_s=timeout_s,
            skills_enabled=False,
            credentials_path=credentials_path if orch_engine.is_cloud else None,
            agent_bin=agent_bin,
            docker_bin=docker_bin or "docker",
            image_tag=image_tag,
            extra_secrets=extra_secrets,
            path_replacements=path_replacements,
            role="orchestrator",
            step_index=1,
            home_dir=home_dir,
            cfg_dir=cfg_dir,
            tmp_dir=tmp_dir,
            home_placeholder="$HOME",
        )
        broker_command = [
            sys.executable, str(REPO_ROOT / "runner/devpack_broker.py"),
            "--workdir", str(work_dir), "--runs-root", str(trial_run_dir / "workers"),
            "--workers", worker_engines_str, "--gate-cmd", "true",
            "--docker-bin", docker_bin or "docker", "--image-tag", image_tag,
        ]
        if credentials_path is not None:
            broker_command.extend(["--credentials", str(credentials_path)])
        if engines_config is not None:
            broker_command.extend(["--engines", str(Path(engines_config).resolve())])
        broker_log = (trial_run_dir / "broker.txt").open("w", encoding="utf-8")
        broker = subprocess.Popen(broker_command, stdout=broker_log, stderr=broker_log,
                                  start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            marker = work_dir / ".devpack/broker.json"
            while not marker.is_file():
                if broker.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("broker failed to start; see broker.txt")
                time.sleep(0.05)
            agent_res = run_agent(request)
        finally:
            if broker.poll() is None:
                os.killpg(broker.pid, signal.SIGTERM)
                try:
                    broker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(broker.pid, signal.SIGKILL)
                    broker.wait(timeout=5)
            broker_log.close()
            (work_dir / ".devpack/broker.json").unlink(missing_ok=True)

        def _redact(text: str) -> str:
            """Redact secrets and paths from text."""
            return redact_sensitive_text(
                text,
                extra_secrets=extra_secrets,
                path_replacements=path_replacements,
                home_placeholder="$HOME",
            )

        timed_out = agent_res.status == "timeout"
        is_harness_err = agent_res.status == "harness_error"
        harness_reason = agent_res.error if is_harness_err else ""
        error_msg = agent_res.error if agent_res.status != "ok" else ""

        analysis = analyze_orchestrator_transcript(agent_res.transcript_path)

        # Grade the same product tree without host-generated queue/client artifacts.
        # Workers have their own clean Git baseline and an explicit control-file check.
        grading_work = trial_root / "grading-work"
        shutil.copytree(work_dir, grading_work, symlinks=True,
                        ignore=shutil.ignore_patterns(".devpack"))
        check_rc, check_out = _run_check_script(
            goal.check_sh,
            grading_work,
            home_dir,
            tmp_dir,
            timeout_s,
        )
        if check_rc == 2 or "GRADER_MISCONFIGURED" in check_out:
            is_harness_err = True
            if not harness_reason:
                harness_reason = f"grader misconfigured (exit {check_rc}): {check_out}"

        redacted_check_out = _redact(check_out)
        (trial_run_dir / "steps" / "02-gate.txt").write_text(
            (redacted_check_out + "\n") if redacted_check_out else f"check_rc={check_rc}\n",
            encoding="utf-8",
        )

        passed = (
            check_rc == 0
            and not is_harness_err
            and not timed_out
            and bool(analysis["compliant"])
        )
        outcome = (
            "pass"
            if passed
            else ("harness_error" if is_harness_err else ("timeout" if timed_out else "fail"))
        )
        update_run_json(
            trial_run_dir,
            outcome=outcome,
            timed_out=timed_out,
            orchestrator_analysis=analysis,
            finished=datetime.now(timezone.utc).isoformat(),
        )

        trial_cost: float | None = None
        if not is_harness_err and not orch_engine.is_local:
            trial_cost = agent_res.cost_usd

        return TrialResult(
            task_id=goal.goal_id,
            category=goal.category,
            model=orch_label,
            run_index=run_index,
            passed=passed,
            agent_returncode=agent_res.exit_code,
            check_returncode=check_rc,
            cost_usd=trial_cost,
            turns=agent_res.num_turns if not is_harness_err else None,
            duration_s=float(agent_res.duration_s),
            error=_redact(error_msg),
            check_output=redacted_check_out,
            harness_error=is_harness_err,
            harness_error_reason=_redact(harness_reason),
            agent_result_excerpt=_redact(agent_res.final_text)[:EXCERPT_MAX_CHARS],
            stderr_tail=_redact(agent_res.stderr)[-STDERR_TAIL_MAX_CHARS:],
            tool_protocol_errors=int(agent_res.tool_protocol_errors),
            run_dir=_redact(str(trial_run_dir)),
            timed_out=timed_out,
            engine_class=orch_engine.engine_class,
            tokens_in=int(agent_res.tokens_in) if not is_harness_err else 0,
            tokens_out=int(agent_res.tokens_out) if not is_harness_err else 0,
            mode="orchestrator",
            orchestrator_product_edits=int(analysis["orchestrator_product_edits"]),
            dispatch_calls=int(analysis["dispatch_calls"]),
            orchestrator_compliant=bool(analysis["compliant"]),
            disallowed_bash_commands=analysis["disallowed_bash_commands"],
            denied_tool_calls=analysis["denied_tool_calls"],
        )
    finally:
        cleanup_cfg_dir(trial_root / "cfg")


def aggregate_results(
    tasks: Sequence[TaskSpec | OrchestratorGoalSpec],
    models: Sequence[str],
    k: int,
    trials: Sequence[TrialResult],
) -> tuple[list[TaskModelSummary], list[dict[str, Any]]]:
    """Aggregate trial results into per-task and per-category summaries."""
    by_task_model: dict[tuple[str, str], list[TrialResult]] = {}
    for trial in trials:
        by_task_model.setdefault((trial.task_id, trial.model), []).append(trial)

    def _task_id_of(item: TaskSpec | OrchestratorGoalSpec) -> str:
        """Return task_id or goal_id for a spec item."""
        return item.task_id if isinstance(item, TaskSpec) else item.goal_id

    task_summaries: list[TaskModelSummary] = []
    for task in tasks:
        tid = _task_id_of(task)
        for model in models:
            runs = sorted(
                by_task_model.get((tid, model), []),
                key=lambda r: r.run_index,
            )
            harness_errors = sum(1 for r in runs if r.harness_error)
            timeouts = sum(1 for r in runs if r.timed_out and not r.harness_error)
            protocol_errors = sum(int(r.tool_protocol_errors) for r in runs)
            valid = [r for r in runs if not r.harness_error]
            has_valid = bool(valid)
            total = len(valid) if has_valid else 0
            passes = sum(1 for r in valid if r.passed)
            is_local = any(r.engine_class == "local" for r in runs) and all(
                r.engine_class == "local" for r in runs
            )
            costs = (
                []
                if is_local
                else [r.cost_usd for r in valid if r.cost_usd is not None]
            )
            turns_list = [r.turns for r in valid if r.turns is not None]
            durations = [r.duration_s for r in valid]
            tokens_in_list = [r.tokens_in for r in valid]
            tokens_out_list = [r.tokens_out for r in valid]

            lint_first = [
                r.lint_findings_first_attempt
                for r in valid
                if r.lint_findings_first_attempt is not None
            ]
            lint_clean = [r.lint_clean_final for r in valid if r.lint_clean_final is not None]
            lint_rounds = [r.rounds_to_clean for r in valid if r.rounds_to_clean is not None]

            rounds_used_list = [r.rounds_used for r in valid if r.rounds_used is not None]
            runner_runs = [r for r in valid if r.mode == "runner"]
            escalations = sum(1 for r in runner_runs if r.escalated)
            false_approves = sum(1 for r in runner_runs if r.reviewer_false_approve)
            false_rejects = sum(1 for r in runner_runs if r.reviewer_false_reject)

            task_summaries.append(
                TaskModelSummary(
                    task_id=tid,
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
                    timeouts=timeouts,
                    valid_runs=len(valid),
                    tool_protocol_errors=protocol_errors,
                    engine_class="local" if is_local else "cloud",
                    mean_tokens_in=(
                        round(sum(tokens_in_list) / len(tokens_in_list), 1)
                        if tokens_in_list
                        else None
                    ),
                    mean_tokens_out=(
                        round(sum(tokens_out_list) / len(tokens_out_list), 1)
                        if tokens_out_list
                        else None
                    ),
                    mean_lint_findings_first_attempt=(
                        round(sum(lint_first) / len(lint_first), 4) if lint_first else None
                    ),
                    lint_clean_final_rate=(
                        round(sum(1 for x in lint_clean if x) / len(lint_clean), 4)
                        if lint_clean
                        else None
                    ),
                    mean_rounds_to_clean=(
                        round(sum(lint_rounds) / len(lint_rounds), 4) if lint_rounds else None
                    ),
                    mean_local_tokens=(sum(r.local_tokens for r in valid) / len(valid) if valid else None),
                    mean_local_wall_s=(sum(r.local_wall_s for r in valid) / len(valid) if valid else None),
                    mean_rounds_used=(
                        round(sum(rounds_used_list) / len(rounds_used_list), 4)
                        if rounds_used_list
                        else None
                    ),
                    escalate_rate=(
                        round(escalations / len(runner_runs), 4) if runner_runs else None
                    ),
                    false_approve_count=false_approves,
                    false_reject_count=false_rejects,
                    false_approve_rate=(
                        round(false_approves / len(runner_runs), 4) if runner_runs else None
                    ),
                    false_reject_rate=(
                        round(false_rejects / len(runner_runs), 4) if runner_runs else None
                    ),
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
            cat_is_local = all(s.engine_class == "local" for s in matching)
            cat_costs = (
                []
                if cat_is_local
                else [s.mean_cost_usd for s in matching if s.mean_cost_usd is not None]
            )
            cat_durations = [s.mean_duration_s for s in matching]
            cat_tokens_in = [s.mean_tokens_in for s in matching if s.mean_tokens_in is not None]
            cat_tokens_out = [s.mean_tokens_out for s in matching if s.mean_tokens_out is not None]
            cat_lint_first = [
                s.mean_lint_findings_first_attempt
                for s in matching
                if s.mean_lint_findings_first_attempt is not None
            ]
            cat_lint_clean = [
                s.lint_clean_final_rate
                for s in matching
                if s.lint_clean_final_rate is not None
            ]
            cat_rounds_clean = [
                s.mean_rounds_to_clean
                for s in matching
                if s.mean_rounds_to_clean is not None
            ]
            cat_rounds_used = [
                s.mean_rounds_used for s in matching if s.mean_rounds_used is not None
            ]
            cat_escalate = [s.escalate_rate for s in matching if s.escalate_rate is not None]
            cat_false_app = [
                s.false_approve_rate for s in matching if s.false_approve_rate is not None
            ]
            cat_false_rej = [
                s.false_reject_rate for s in matching if s.false_reject_rate is not None
            ]
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
                    "timeouts": sum(s.timeouts for s in matching),
                    "tool_protocol_errors": sum(s.tool_protocol_errors for s in matching),
                    "engine_class": "local" if cat_is_local else "cloud",
                    "mean_cost_usd": (
                        round(sum(cat_costs) / len(cat_costs), 6)
                        if cat_costs
                        else None
                    ),
                    "mean_local_tokens": _mean([s.mean_local_tokens for s in matching if s.mean_local_tokens is not None]),
                    "mean_local_wall_s": _mean([s.mean_local_wall_s for s in matching if s.mean_local_wall_s is not None]),
                    "mean_tokens_in": _mean(cat_tokens_in),
                    "mean_tokens_out": _mean(cat_tokens_out),
                    "mean_duration_s": (
                        round(sum(cat_durations) / len(cat_durations), 4)
                        if cat_durations
                        else 0.0
                    ),
                    "mean_lint_findings_first_attempt": _mean(cat_lint_first),
                    "lint_clean_final_rate": _mean(cat_lint_clean),
                    "mean_rounds_to_clean": _mean(cat_rounds_clean),
                    "mean_rounds_used": _mean(cat_rounds_used),
                    "escalate_rate": _mean(cat_escalate),
                    "false_approve_count": sum(s.false_approve_count for s in matching),
                    "false_reject_count": sum(s.false_reject_count for s in matching),
                    "false_approve_rate": _mean(cat_false_app),
                    "false_reject_rate": _mean(cat_false_rej),
                }
            )

    return task_summaries, category_summaries


def _fmt(value: float | None) -> str:
    """Format a metric with two decimals, or ``n/a`` when there are no valid runs."""
    return "n/a" if value is None else f"{value:.2f}"


def _fmt_tokens(tok_in: float | int | None, tok_out: float | int | None) -> str:
    """Format input/output token counts or ``n/a``."""
    if tok_in is None and tok_out is None:
        return "n/a"
    return f"{int(round(tok_in or 0))}/{int(round(tok_out or 0))}"


def render_summary_markdown(
    run_id: str,
    date_str: str,
    isolation: str,
    k: int,
    models: Sequence[str],
    task_summaries: Sequence[TaskModelSummary],
    category_summaries: Sequence[dict[str, Any]],
    mode: str = "single",
) -> str:
    """Format evaluation results into a human-readable Markdown report."""
    lines = [
        f"# Evaluation Summary ({run_id})",
        "",
        f"- **Date**: {date_str}",
        f"- **Mode**: `{mode}`",
        f"- **Isolation**: `{isolation}`",
        f"- **Engines**: `{', '.join(models)}`",
        f"- **k**: `{k}`",
        "",
        "## Per-Category Summary",
        "",
        "Pass metrics include timeouts as not passed; harness errors are excluded.",
        "",
        "| Category | Engine | Tasks | Harness errors | Timeouts | Tool Protocol Errors | pass@1 | pass^k (all-k) | pass^k (p^k) | Mean Cost (USD) | Mean Tokens (in/out) | Mean Duration (s) | Lint 1st | Lint Clean |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cat in category_summaries:
        cost_str = (
            f"{cat['mean_cost_usd']:.4f}"
            if cat.get("mean_cost_usd") is not None
            else "n/a"
        )
        tok_str = _fmt_tokens(cat.get("mean_tokens_in"), cat.get("mean_tokens_out"))
        lines.append(
            f"| {cat['category']} | {cat['model']} | {cat['tasks_count']} | "
            f"{cat['harness_errors']} | {cat.get('timeouts', 0)} | {cat.get('tool_protocol_errors', 0)} | "
            f"{_fmt(cat['pass_at_1'])} | {_fmt(cat['pass_hat_k'])} | "
            f"{_fmt(cat['pass_pow_k'])} | {cost_str} | {tok_str} | {cat['mean_duration_s']:.2f} | "
            f"{_fmt(cat.get('mean_lint_findings_first_attempt'))} | {_fmt(cat.get('lint_clean_final_rate'))} |"
        )

    lines.extend(
        [
            "",
            "## Per-Task Summary",
            "",
            "| Task | Category | Engine | Passes (c/valid) | Harness errors | Timeouts | Tool Protocol Errors | pass@1 | pass^k (all-k) | pass^k (p^k) | Mean Cost (USD) | Mean Tokens (in/out) | Mean Duration (s) | Lint 1st | Lint Clean | Rounds Clean |",
            "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for ts in task_summaries:
        cost_str = (
            f"{ts.mean_cost_usd:.4f}" if ts.mean_cost_usd is not None else "n/a"
        )
        tok_str = _fmt_tokens(ts.mean_tokens_in, ts.mean_tokens_out)
        lines.append(
            f"| {ts.task_id} | {ts.category} | {ts.model} | "
            f"{ts.passes}/{ts.valid_runs} | {ts.harness_errors} | {ts.timeouts} | {ts.tool_protocol_errors} | "
            f"{_fmt(ts.pass_at_1)} | {_fmt(ts.pass_hat_k)} | "
            f"{_fmt(ts.pass_pow_k)} | {cost_str} | {tok_str} | {ts.mean_duration_s:.2f} | "
            f"{_fmt(ts.mean_lint_findings_first_attempt)} | {_fmt(ts.lint_clean_final_rate)} | {_fmt(ts.mean_rounds_to_clean)} |"
        )

    if any(r.mode == "runner" for ts in task_summaries for r in ts.runs):
        lines.extend(
            [
                "",
                "## Runner Metrics",
                "",
                "| Task | Pair | Mean Rounds | Escalate Rate | False-Approve | False-Reject | Local Tokens | Local Wall (s) |",
                "|---|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for ts in task_summaries:
            lines.append(
                f"| {ts.task_id} | {ts.model} | {_fmt(ts.mean_rounds_used)} | "
                f"{_fmt(ts.escalate_rate)} | {ts.false_approve_count} ({_fmt(ts.false_approve_rate)}) | "
                f"{ts.false_reject_count} ({_fmt(ts.false_reject_rate)}) | "
                f"{_fmt(ts.mean_local_tokens)} | {_fmt(ts.mean_local_wall_s)} |"
            )

    lines.extend(
        [
            "",
            "## Per-Trial Runs",
            "",
            "| Task | Engine | Run | Passed | Timed Out | Cost (USD) | Tokens (in/out) | Duration (s) | Tool Protocol Errors | Run Dir |",
            "|---|---|---:|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for ts in task_summaries:
        for run in ts.runs:
            run_cost = f"{run.cost_usd:.4f}" if run.cost_usd is not None else "n/a"
            tok_str = _fmt_tokens(run.tokens_in, run.tokens_out)
            lines.append(
                f"| {run.task_id} | {run.model} | {run.run_index} | "
                f"{'yes' if run.passed else 'no'} | {'yes' if run.timed_out else 'no'} | {run_cost} | "
                f"{tok_str} | {run.duration_s:.2f} | {run.tool_protocol_errors} | `{run.run_dir}` |"
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
            "Run ai-devpack evaluation tasks across engines in single, runner, or "
            "orchestrator mode and compute pass@1, pass^k, lint, and reviewer metrics."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("single", "runner", "orchestrator"),
        default="single",
        help="Evaluation topology mode: single, runner, or orchestrator (default: single)",
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
        "--pairs",
        default="haiku:sonnet",
        help="Comma-separated implementer:reviewer pairs for --mode runner (default: haiku:sonnet)",
    )
    parser.add_argument(
        "--orchestrator",
        default="sonnet",
        help="Orchestrator engine name for --mode orchestrator (default: sonnet)",
    )
    parser.add_argument(
        "--workers",
        default="haiku",
        help="Comma-separated worker engine(s) for --mode orchestrator (default: haiku)",
    )
    parser.add_argument(
        "--tier",
        choices=("smoke", "v2", "all"),
        default="all",
        help="Task tier filter: smoke, v2, or all (default: all)",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=5,
        help="Number of trials per (task, engine) pair (default: 5)",
    )
    parser.add_argument(
        "--tasks",
        default="all",
        help="Task or goal selection: 'all' or comma-separated IDs (default: all)",
    )
    parser.add_argument(
        "--goals-dir",
        default=str(default_eval_dir / "orchestrator_goals"),
        help="Path to orchestrator goals directory for --mode orchestrator",
    )
    parser.add_argument(
        "--max-fix-rounds",
        type=int,
        default=2,
        help="Maximum fix rounds for --mode runner (default: 2)",
    )
    parser.add_argument(
        "--isolation",
        choices=("docker", "local"),
        default="docker",
        help="Isolation mode: 'docker' or 'local' (default: docker)",
    )
    parser.add_argument(
        "--allow-unsandboxed-local",
        action="store_true",
        help="Explicitly authorize unsandboxed --isolation local (sandbox deviation)",
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

    fixture_dir = Path(args.fixture_dir).resolve()
    tasks_dir = Path(args.tasks_dir).resolve()
    goals_dir = Path(args.goals_dir).resolve()
    dockerfile_path = Path(args.dockerfile).resolve()
    repo_root = Path(__file__).resolve().parent.parent

    loaded_engines: EnginesConfig | None = None
    if args.engines_config:
        cfg_file = Path(args.engines_config)
        if cfg_file.is_file():
            try:
                loaded_engines = load_engines(cfg_file)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2

    engines_config_path: Path | None = (
        Path(args.engines_config).resolve() if args.engines_config else None
    )

    mode = args.mode
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    run_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    try:
        validate_fixture_dir(fixture_dir)
        if mode == "orchestrator":
            goals = load_and_validate_orchestrator_goals(goals_dir, args.tasks, args.tier)
            eval_units: list[TaskSpec | OrchestratorGoalSpec] = list(goals)
            orch_name = args.orchestrator.strip()
            workers_list = [w.strip() for w in args.workers.split(",") if w.strip()]
            if not orch_name or not workers_list:
                raise ValueError("--orchestrator and --workers must be non-empty")
            orch_label = f"{orch_name}->{','.join(workers_list)}"
            engine_names_for_creds = [orch_name, *workers_list]
            unit_labels = [orch_label]
        else:
            tasks = filter_tasks_by_tier(
                load_and_validate_tasks(tasks_dir, args.tasks), args.tier
            )
            eval_units = list(tasks)
            if mode == "runner":
                parsed_pairs = parse_runner_pairs(args.pairs)
                unit_labels = [f"{imp}:{rev}" for imp, rev, _ in parsed_pairs]
                engine_names_for_creds = [
                    name for imp, rev, fix in parsed_pairs for name in (imp, rev, fix)
                ]
            else:
                raw_engines = getattr(args, "engines", None) or args.models
                unit_labels = [m.strip() for m in raw_engines.split(",") if m.strip()]
                if not unit_labels:
                    raise ValueError("--engines/--models must specify at least one engine")
                engine_names_for_creds = list(unit_labels)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    engine_map: dict[str, EngineConfig] = {
        name: resolve_engine(name, loaded_engines) for name in dict.fromkeys(engine_names_for_creds)
    }

    if args.dry_run:
        if mode == "orchestrator":
            plan_orch = [
                (g, lbl, run_idx)
                for g in eval_units
                for lbl in unit_labels
                for run_idx in range(1, args.k + 1)
            ]
            print(f"DRY-RUN orchestrator plan ({len(plan_orch)} total trials):")
            print(f"  mode: orchestrator")
            print(f"  isolation: {args.isolation}")
            print(f"  orchestrator: {args.orchestrator.strip()}")
            print(f"  workers: {args.workers.strip()}")
            print(f"  tier: {args.tier}")
            print(f"  k: {args.k}")
            print(f"  goals ({len(eval_units)}): {', '.join(g.goal_id for g in eval_units)}")  # type: ignore[union-attr]
            for idx, (g, lbl, run_idx) in enumerate(plan_orch, start=1):
                print(
                    f"  [{idx:03d}/{len(plan_orch):03d}] "
                    f"goal={g.goal_id} topology={lbl} run={run_idx}/{args.k}"  # type: ignore[union-attr]
                )
            return 0

        plan_items: list[tuple[TaskSpec, str, int]] = [
            (task, lbl, run_idx)
            for task in eval_units  # type: ignore[misc]
            for lbl in unit_labels
            for run_idx in range(1, args.k + 1)
        ]
        kind_label = "pair" if mode == "runner" else "engine"
        print(f"DRY-RUN {mode} plan ({len(plan_items)} total trials):")
        print(f"  mode: {mode}")
        print(f"  isolation: {args.isolation}")
        print(f"  tier: {args.tier}")
        print(f"  {kind_label}s: {', '.join(unit_labels)}")
        print(f"  k: {args.k}")
        print(f"  tasks ({len(eval_units)}): {', '.join(t.task_id for t in eval_units)}")  # type: ignore[union-attr]
        for idx, (task, lbl, run_idx) in enumerate(plan_items, start=1):
            print(
                f"  [{idx:03d}/{len(plan_items):03d}] "
                f"task={task.task_id} category={task.category} {kind_label}={lbl} run={run_idx}/{args.k}"
            )
        return 0

    if mode == "orchestrator" and (args.isolation != "local" or not args.allow_unsandboxed_local):
        print("error: orchestrator requires --isolation local --allow-unsandboxed-local", file=sys.stderr)
        return 1

    iso_ok, iso_msg = check_local_isolation_allowed(
        args.isolation,
        allow_unsandboxed_local=args.allow_unsandboxed_local,
        workdir=repo_root,
    )
    if not iso_ok:
        print(f"error: {iso_msg}", file=sys.stderr)
        return 1

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
        if mode == "orchestrator":
            orch_name = args.orchestrator.strip()
            orch_eng = engine_map[orch_name]
            orch_lbl = unit_labels[0]
            orch_jobs = [
                (g, run_idx)
                for g in eval_units
                for run_idx in range(1, args.k + 1)
            ]
            if args.max_parallel == 1:
                for g, run_idx in orch_jobs:
                    trials.append(
                        run_orchestrator_trial(
                            goal=g,  # type: ignore[arg-type]
                            orch_label=orch_lbl,
                            orch_engine=orch_eng,
                            worker_engines_str=args.workers.strip(),
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
                            allow_unsandboxed_local=args.allow_unsandboxed_local,
                            engines_config=engines_config_path,
                        )
                    )
            else:
                with ThreadPoolExecutor(max_workers=args.max_parallel) as pool:
                    futures = [
                        pool.submit(
                            run_orchestrator_trial,
                            g,  # type: ignore[arg-type]
                            orch_lbl,
                            orch_eng,
                            args.workers.strip(),
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
                            args.allow_unsandboxed_local,
                            engines_config_path,
                        )
                        for g, run_idx in orch_jobs
                    ]
                    for fut in as_completed(futures):
                        trials.append(fut.result())
        elif mode == "runner":
            parsed_pairs = parse_runner_pairs(args.pairs)
            runner_jobs = [
                (t, f"{imp}:{rev}", engine_map[imp], engine_map[rev], engine_map[fix], run_idx)
                for t in eval_units
                for imp, rev, fix in parsed_pairs
                for run_idx in range(1, args.k + 1)
            ]
            if args.max_parallel == 1:
                for t, lbl, imp_e, rev_e, fix_e, run_idx in runner_jobs:
                    trials.append(
                        run_runner_trial(
                            task=t,  # type: ignore[arg-type]
                            pair_label=lbl,
                            impl_engine=imp_e,
                            rev_engine=rev_e,
                            fixer_engine=fix_e,
                            run_index=run_idx,
                            run_root=run_root,
                            fixture_dir=fixture_dir,
                            credentials_path=credentials_path,
                            isolation=args.isolation,
                            agent_bin=args.agent_bin,
                            timeout_s=args.timeout_s,
                            max_fix_rounds=args.max_fix_rounds,
                            docker_bin=docker_bin,
                            extra_secrets=extra_secrets,
                            path_replacements=path_replacements,
                            image_tag=image_tag,
                            runs_root=runs_root,
                            allow_unsandboxed_local=args.allow_unsandboxed_local,
                        )
                    )
            else:
                with ThreadPoolExecutor(max_workers=args.max_parallel) as pool:
                    futures = [
                        pool.submit(
                            run_runner_trial,
                            t,  # type: ignore[arg-type]
                            lbl,
                            imp_e,
                            rev_e,
                            fix_e,
                            run_idx,
                            run_root,
                            fixture_dir,
                            credentials_path,
                            args.isolation,
                            args.agent_bin,
                            args.timeout_s,
                            args.max_fix_rounds,
                            docker_bin,
                            extra_secrets,
                            path_replacements,
                            image_tag,
                            runs_root,
                            args.allow_unsandboxed_local,
                        )
                        for t, lbl, imp_e, rev_e, fix_e, run_idx in runner_jobs
                    ]
                    for fut in as_completed(futures):
                        trials.append(fut.result())
        else:
            plan_items = [
                (task, model, run_idx)
                for task in eval_units  # type: ignore[misc]
                for model in unit_labels
                for run_idx in range(1, args.k + 1)
            ]
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
                            allow_unsandboxed_local=args.allow_unsandboxed_local,
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
                            args.allow_unsandboxed_local,
                        )
                        for task, model, run_idx in plan_items
                    ]
                    for fut in as_completed(futures):
                        trials.append(fut.result())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    task_summaries, category_summaries = aggregate_results(
        tasks=eval_units,
        models=unit_labels,
        k=args.k,
        trials=trials,
    )

    raw_payload: dict[str, Any] = {
        "run_id": run_id,
        "date": date_str,
        "mode": mode,
        "tier": args.tier,
        "isolation": args.isolation,
        "models": unit_labels,
        "engines": unit_labels,
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
        models=unit_labels,
        task_summaries=task_summaries,
        category_summaries=category_summaries,
        mode=mode,
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
