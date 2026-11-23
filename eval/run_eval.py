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
import functools
import json
import os
from pathlib import Path
import shutil
import signal
import time
import stat
import subprocess
import sys
from typing import Any, Callable, Mapping, Sequence
import uuid

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "runner")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from runner import agent_exec  # noqa: E402
from runner import devpack_logs  # noqa: E402
from runner import devpack_run  # noqa: E402
from runner import repomap  # noqa: E402
from runner.agent_exec import (  # noqa: E402
    ALLOWED_TOOLS,
    DEFAULT_CLAUDE_VERSION,
    DISALLOWED_TOOLS,
    DOCKER_IMAGE_TAG,
    DOCKER_TASK_DIR,
    AgentRequest,
    EngineConfig,
    EnginesConfig,
    MemoryConfig,
    _minimal_toml_loads,
    build_claude_state,
    build_docker_agent_invocation,
    build_invocation,
    build_local_agent_invocation,
    build_permission_args,
    build_project_settings,
    check_local_isolation_allowed,
    cleanup_cfg_dir,
    compute_engine_config_hash,
    detect_harness_error,
    extract_agent_json_object,
    json_string_leaves,
    load_engines,
    load_qualification_data,
    new_run_dir,
    parse_agent_json_output,
    parse_stream,
    resolve_engine,
    resolve_engine_profile_path,
    run_agent,
    save_qualification_data,
    update_run_json,
)
from runner.devpack_dispatch import run_cppcheck_on_changed, run_preflight_lint_on_changed  # noqa: E402
from runner.memory import memory_root_for, process_memory_proposals  # noqa: E402
from runner.redaction import (  # noqa: E402
    collect_credential_secrets,
    redact_sensitive_text,
    redact_structure,
)
from runner.safe_git import (  # noqa: E402
    GitSecurityError,
    check_git_metadata_unchanged,
    run_git,
    snapshot_git_metadata,
)
from enforcement.checks.scope_check import get_changed_files  # noqa: E402

EXCERPT_MAX_CHARS = 4000
STDERR_TAIL_MAX_CHARS = 2000
ORCHESTRATOR_PACK_PATH = Path(".devpack/pack")

REQUIRED_TASK_FILES = ("task.md", "check.sh", "setup.patch", "solution.patch")


def _redact_eval_text(
    text: str,
    *,
    extra_secrets: Sequence[str],
    path_replacements: Sequence[tuple[str, str]],
) -> str:
    """Redact trial secrets, patterns, and workspace paths using ``runner.redaction``."""
    return redact_sensitive_text(
        text,
        extra_secrets=extra_secrets,
        path_replacements=path_replacements,
        home_placeholder="$HOME",
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


@dataclass(frozen=True)
class MemoryStepSpec:
    """Metadata for a single step inside a multi-step memory evaluation sequence."""

    step_id: str
    role: str
    engine: str
    prompt_file: Path
    allowed_files: tuple[str, ...]
    expected_ids: tuple[str, ...] = ()
    forbidden_ids: tuple[str, ...] = ()
    expect_proposals: bool = False
    expect_prior_proposals: bool = False


@dataclass(frozen=True)
class MemorySequenceSpec:
    """Metadata and filesystem paths for a multi-step memory evaluation sequence."""

    seq_id: str
    category: str
    tier: str
    title: str
    ab_compare: bool
    seq_dir: Path
    seed_memory_dir: Path | None
    check_sh: Path
    steps: tuple[MemoryStepSpec, ...]


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
    static_findings_first_attempt: int | None = None
    static_clean_final: bool | None = None
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
    init_model: str = ""
    init_digest: str = ""
    memory_arm: str = ""
    memory_recall_rate: float | None = None
    memory_over_recall_rate: float | None = None
    memory_injected_tokens: int | None = None
    memory_injected_ids: list[str] = field(default_factory=list)
    violation_reason: str = ""
    brief_variant: str = "standard"
    context_mode: str = "none"
    skills_enabled: bool = False
    skipped: bool = False
    skip_reason: str = ""
    suspected_cause: str | None = None
    attribution_secondary: str | None = None
    ablation_cause: str | None = None
    ablation_arms: dict[str, Any] = field(default_factory=dict)
    arm: str = "base"


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
    tier: str = "smoke"
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
    mean_static_findings_first_attempt: float | None = None
    static_clean_final_rate: float | None = None
    mean_local_tokens: float | None = None
    mean_local_wall_s: float | None = None
    mean_rounds_used: float | None = None
    escalate_rate: float | None = None
    false_approve_count: int = 0
    false_reject_count: int = 0
    false_approve_rate: float | None = None
    false_reject_rate: float | None = None
    memory_recall_rate: float | None = None
    memory_over_recall_rate: float | None = None
    mean_injected_tokens: float | None = None
    pass_with_memory: float | None = None
    pass_without_memory: float | None = None
    memory_ab_delta: float | None = None
    skipped_runs: int = 0
    ablation_cause: str | None = None
    ablation_arms: dict[str, Any] = field(default_factory=dict)


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


def load_and_validate_memory_sequences(
    memory_tasks_dir: Path,
    selector: str = "all",
    tier: str = "all",
) -> list[MemorySequenceSpec]:
    """Discover and validate multi-step memory evaluation sequences under ``memory_tasks_dir``."""
    if not memory_tasks_dir.is_dir():
        raise ValueError(f"Memory tasks directory does not exist: {memory_tasks_dir}")

    if selector.strip().lower() == "all":
        candidate_dirs = sorted(
            p for p in memory_tasks_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
        )
    else:
        requested = [item.strip() for item in selector.split(",") if item.strip()]
        if not requested:
            raise ValueError("No memory sequence IDs provided")
        candidate_dirs = [memory_tasks_dir / sid for sid in requested]

    sequences: list[MemorySequenceSpec] = []
    for sdir in candidate_dirs:
        if not sdir.is_dir():
            raise ValueError(f"Memory sequence directory not found: {sdir}")
        seq_toml = sdir / "sequence.toml"
        check_sh = sdir / "check.sh"
        if not seq_toml.is_file():
            raise ValueError(f"Memory sequence '{sdir.name}' is missing 'sequence.toml'")
        if not check_sh.is_file():
            raise ValueError(f"Memory sequence '{sdir.name}' is missing 'check.sh'")

        raw_text = seq_toml.read_text(encoding="utf-8")
        data = tomllib.loads(raw_text) if tomllib is not None else _minimal_toml_loads(raw_text)
        seq_tier = str(data.get("tier", "v2")).strip().lower() or "v2"
        if tier.strip().lower() not in ("", "all") and seq_tier != tier.strip().lower():
            continue

        step_keys = sorted(
            (k for k in data if k.startswith("step_") and isinstance(data[k], dict)),
            key=lambda s: int(s.split("_", 1)[1]),
        )
        if not step_keys:
            raise ValueError(f"Memory sequence '{sdir.name}' defines no [step_N] sections")

        steps: list[MemoryStepSpec] = []
        for skey in step_keys:
            sdata = data[skey]
            p_rel = str(sdata.get("prompt_file", "")).strip()
            if not p_rel:
                raise ValueError(f"Memory sequence '{sdir.name}' {skey} missing prompt_file")
            p_path = sdir / p_rel
            if not p_path.is_file():
                raise ValueError(f"Memory sequence '{sdir.name}' missing step prompt: {p_path}")
            if not sdata.get("allowed_files"):
                raise ValueError(f"Memory sequence '{sdir.name}' {skey} missing allowed_files")
            steps.append(
                MemoryStepSpec(
                    step_id=skey,
                    role=str(sdata.get("role", "implementer")).strip() or "implementer",
                    engine=str(sdata.get("engine", "")).strip(),
                    prompt_file=p_path,
                    allowed_files=tuple(str(x) for x in sdata.get("allowed_files", [])),
                    expected_ids=tuple(str(x) for x in sdata.get("expected_ids", [])),
                    forbidden_ids=tuple(str(x) for x in sdata.get("forbidden_ids", [])),
                    expect_proposals=bool(sdata.get("expect_proposals", False)),
                    expect_prior_proposals=bool(sdata.get("expect_prior_proposals", False)),
                )
            )

        seed_dir = sdir / "seed_memory"
        sequences.append(
            MemorySequenceSpec(
                seq_id=str(data.get("id", sdir.name)),
                category=str(data.get("category", "memory")).strip() or "memory",
                tier=seq_tier,
                title=str(data.get("title", sdir.name)),
                ab_compare=bool(data.get("ab_compare", True)),
                seq_dir=sdir,
                seed_memory_dir=seed_dir if seed_dir.is_dir() else None,
                check_sh=check_sh,
                steps=tuple(steps),
            )
        )
    if not sequences:
        raise ValueError(f"No memory sequences matched in {memory_tasks_dir}")
    return sequences


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


def extract_minimal_brief(task_md_text: str) -> str:
    """Return only the task heading and first goal paragraph from a task Markdown string."""
    lines = task_md_text.splitlines()
    title = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped
            break

    goal_lines: list[str] = []
    in_goal = False
    found_goal_heading = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower() == "## goal":
            found_goal_heading = True
            in_goal = True
            continue
        if in_goal:
            if stripped.startswith("## "):
                break
            if not stripped:
                if goal_lines:
                    break
                continue
            goal_lines.append(stripped)

    if not found_goal_heading:
        for line in lines:
            stripped = line.strip()
            if (
                not stripped
                or stripped.startswith("#")
                or stripped.startswith(("Category:", "Tier:", "Status:"))
            ):
                if goal_lines and not stripped:
                    break
                continue
            goal_lines.append(stripped)

    parts: list[str] = []
    if title:
        parts.append(title)
    if goal_lines:
        if found_goal_heading:
            parts.append("## Goal\n" + "\n".join(goal_lines))
        else:
            parts.append("\n".join(goal_lines))
    return ("\n\n".join(parts).strip() + "\n") if parts else task_md_text


def build_task_prompt_text(
    task: TaskSpec,
    *,
    brief_variant: str = "standard",
    context_mode: str = "none",
    work_dir: Path | None = None,
    repomap_budget: int = 1500,
) -> tuple[str, str | None]:
    """Assemble the worker prompt for ``task`` under the requested brief and context arms.

    When ``brief_variant == 'standard'`` and ``context_mode == 'none'``, returns the exact
    contents of ``task.task_md`` unchanged. Returns ``('', 'no_oracle')`` if
    ``brief_variant == 'oracle'`` and ``oracle.md`` is missing in ``task.task_dir``.
    """
    if brief_variant not in ("standard", "minimal", "oracle"):
        raise ValueError(f"Unsupported brief_variant: {brief_variant}")
    if context_mode not in ("none", "repomap"):
        raise ValueError(f"Unsupported context_mode: {context_mode}")

    raw_task = task.task_md.read_text(encoding="utf-8")
    if brief_variant == "standard":
        prompt_text = raw_task
    elif brief_variant == "minimal":
        prompt_text = extract_minimal_brief(raw_task)
    else:
        oracle_path = task.task_dir / "oracle.md"
        if not oracle_path.is_file():
            return "", "no_oracle"
        oracle_text = oracle_path.read_text(encoding="utf-8").strip()
        prompt_text = raw_task.rstrip() + "\n\n" + oracle_text + "\n"

    if context_mode == "repomap":
        target_repo = work_dir if work_dir is not None else task.task_dir
        map_md = repomap.generate_repomap(
            target_repo,
            task_text=prompt_text,
            budget=repomap_budget,
            include_title=False,
        )
        prompt_text = prompt_text.rstrip() + "\n\n## Code map\n\n" + map_md.strip() + "\n"

    return prompt_text, None


def _install_pack_skills(work_dir: Path, pack_root: Path = REPO_ROOT) -> list[str]:
    """Copy pack skills into ``<work_dir>/.claude/skills/`` for skill-on trials."""
    skills_src = pack_root / "knowledge" / "skills"
    installed: list[str] = []
    if not skills_src.is_dir():
        return installed
    for sdir in sorted(skills_src.iterdir()):
        if not sdir.is_dir() or not (sdir / "SKILL.md").is_file():
            continue
        dest = work_dir / ".claude" / "skills" / sdir.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            sdir,
            dest,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("tests", "__pycache__"),
        )
        installed.append(sdir.name)
    return installed


def compute_ablation_cause(
    base_passed: bool,
    *,
    oracle_passed: bool | None,
    repomap_passed: bool | None,
    skill_passed: bool | None,
    analyze_cause: str | None = None,
    analyze_signals: Mapping[str, Any] | None = None,
) -> str | None:
    """Attribute failure cause from ablation arm outcomes and ``devpack_logs analyze`` signals.

    Rules (SPEC 5.6):
    - Base passed -> ``None``
    - Passes only with oracle brief -> ``BRIEF``
    - Passes only with repomap context -> ``CONTEXT``
    - Passes only with toggled skill -> ``SKILL``
    - Multi-arm passes -> minimal sufficient intervention (``CONTEXT``, then ``SKILL``, then ``BRIEF``)
    - None pass -> ``MODEL_OR_TOOL``, split via ``analyze`` signals into ``TOOL``, ``ENV``, or ``MODEL``
      when a deterministic signal is present (otherwise ``MODEL_OR_TOOL``).
    """
    if base_passed:
        return None
    o = oracle_passed is True
    r = repomap_passed is True
    s = skill_passed is True

    if o and not r and not s:
        return "BRIEF"
    if r and not o and not s:
        return "CONTEXT"
    if s and not o and not r:
        return "SKILL"
    if r:
        return "CONTEXT"
    if s:
        return "SKILL"
    if o:
        return "BRIEF"

    sigs = analyze_signals or {}
    if (
        analyze_cause == "TOOL"
        or bool(sigs.get("denied_calls"))
        or bool(sigs.get("prompt_over_context"))
        or int(sigs.get("tool_protocol_errors") or 0) > 0
    ):
        return "TOOL"
    if (
        analyze_cause == "ENV"
        or bool(sigs.get("timeout"))
        or bool(sigs.get("quota_or_network_error"))
    ):
        return "ENV"
    if analyze_cause == "MODEL":
        return "MODEL"
    return "MODEL_OR_TOOL"


def _analyze_failed_run_dir(
    trial_run_dir: Path,
    fixture_dir: Path,
    engines_config_path: Path | None = None,
) -> tuple[str | None, str | None, dict[str, Any]]:
    """Run ``devpack_logs.analyze_run_dir`` on a failed trial and return ``(cause, secondary, signals)``."""
    if not (trial_run_dir / "run.json").is_file():
        return None, None, {}
    try:
        attr = devpack_logs.analyze_run_dir(
            trial_run_dir,
            repo_root=fixture_dir,
            engines_config=engines_config_path,
            write=True,
        )
    except Exception:
        return None, None, {}
    cause = attr.get("suspected_cause")
    sec = attr.get("secondary")
    sigs = attr.get("signals")
    return (
        str(cause) if cause else None,
        str(sec) if sec else None,
        dict(sigs) if isinstance(sigs, Mapping) else {},
    )


def prepare_isolated_trial_dir(
    trial_root: Path,
    fixture_dir: Path,
    task: TaskSpec,
    credentials_path: Path | None,
    agent_cwd: str | None = None,
    *,
    brief_variant: str = "standard",
    context_mode: str = "none",
    repomap_budget: int = 1500,
    skills_enabled: bool = False,
    pack_root: Path = REPO_ROOT,
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
        proc = run_git(
            args,
            repo_root=work_dir,
            cwd=work_dir,
            env=git_env,
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
    rev_proc = run_git(["rev-parse", "HEAD"], repo_root=work_dir, cwd=work_dir, env=git_env, check=True)
    baseline_sha = rev_proc.stdout.strip()
    baseline_file = trial_root / "baseline.sha"
    baseline_file.write_text(baseline_sha + "\n", encoding="utf-8")

    prompt_text, skip_reason = build_task_prompt_text(
        task,
        brief_variant=brief_variant,
        context_mode=context_mode,
        work_dir=work_dir,
        repomap_budget=repomap_budget,
    )
    if skip_reason is not None:
        raise RuntimeError(f"Task '{task.task_id}' skipped: {skip_reason}")
    prompt_file = trial_root / "prompt.md"
    prompt_file.write_text(prompt_text, encoding="utf-8")

    task_prompt_in_work = work_dir / "TASK_PROMPT.md"
    task_prompt_in_work.write_text(prompt_text, encoding="utf-8")

    claude_dir = work_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    if skills_enabled:
        _install_pack_skills(work_dir, pack_root)
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
        "trial_root": trial_root,
        "baseline_sha": baseline_sha,
        "baseline_file": baseline_file,
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


def _resolve_baseline_ref(
    work_dir: Path,
    trial_dir: Path | None = None,
    *,
    baseline_sha: str = "",
) -> str:
    """Return the external baseline commit SHA recorded by the harness; never search commit messages."""
    sha = (baseline_sha or "").strip()
    if not sha and trial_dir is not None:
        bfile = trial_dir / "baseline.sha"
        if bfile.is_file():
            sha = bfile.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    if not sha:
        sha = os.environ.get("DEVPACK_BASELINE_SHA", "").strip()
    if not sha:
        raise RuntimeError("GRADER_MISCONFIGURED: missing external DEVPACK_BASELINE_SHA / baseline.sha")
    proc = run_git(["cat-file", "-e", f"{sha}^{{commit}}"], repo_root=work_dir, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"GRADER_MISCONFIGURED: baseline SHA '{sha}' not found in {work_dir}")
    return sha


def _run_check_script(
    check_sh: Path,
    work_dir: Path,
    home_dir: Path,
    tmp_dir: Path,
    timeout_s: int,
    *,
    trial_dir: Path | None = None,
    baseline_sha: str = "",
) -> tuple[int, str]:
    """Execute ``check_sh --workdir <work_dir>`` with ``DEVPACK_ROOT`` and ``DEVPACK_BASELINE_SHA`` set."""
    resolved_sha = (baseline_sha or "").strip()
    if not resolved_sha and trial_dir is not None:
        bfile = trial_dir / "baseline.sha"
        if bfile.is_file():
            resolved_sha = bfile.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    if not resolved_sha:
        resolved_sha = os.environ.get("DEVPACK_BASELINE_SHA", "").strip()
    if not resolved_sha:
        return 2, "GRADER_MISCONFIGURED: missing external DEVPACK_BASELINE_SHA / baseline.sha"
    check_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home_dir),
        "TMPDIR": str(tmp_dir),
        "DEVPACK_ROOT": str(REPO_ROOT),
        "DEVPACK_BASELINE_SHA": resolved_sha,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    if trial_dir is not None:
        check_env["TRIAL_DIR"] = str(trial_dir)
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
    profile_overrides: Mapping[str, Path | None] | None = None,
    brief_variant: str = "standard",
    context_mode: str = "none",
    repomap_budget: int = 1500,
    skills_enabled: bool = False,
    engines_config_path: Path | None = None,
    arm: str = "base",
) -> TrialResult:
    """Execute one (task, model, run_index) trial in an isolated workspace.

    Credentials and the config directory contents are always deleted afterwards,
    whether the trial succeeds, fails, times out, or raises.
    """
    if brief_variant == "oracle" and not (task.task_dir / "oracle.md").is_file():
        engine = engine_config or resolve_engine(model)
        return TrialResult(
            task_id=task.task_id,
            category=task.category,
            model=model,
            run_index=run_index,
            passed=False,
            agent_returncode=0,
            check_returncode=0,
            cost_usd=None,
            turns=None,
            duration_s=0.0,
            engine_class=engine.engine_class,
            mode="single",
            brief_variant=brief_variant,
            context_mode=context_mode,
            skills_enabled=skills_enabled,
            skipped=True,
            skip_reason="no_oracle",
            arm=arm,
        )
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
            profile_overrides=profile_overrides,
            brief_variant=brief_variant,
            context_mode=context_mode,
            repomap_budget=repomap_budget,
            skills_enabled=skills_enabled,
            engines_config_path=engines_config_path,
            arm=arm,
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
    profile_overrides: Mapping[str, Path | None] | None = None,
    brief_variant: str = "standard",
    context_mode: str = "none",
    repomap_budget: int = 1500,
    skills_enabled: bool = False,
    engines_config_path: Path | None = None,
    arm: str = "base",
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
        brief_variant=brief_variant,
        context_mode=context_mode,
        repomap_budget=repomap_budget,
        skills_enabled=skills_enabled,
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

    if profile_overrides is not None and engine.name in profile_overrides:
        resolved_profile = profile_overrides[engine.name]
    else:
        resolved_profile = resolve_engine_profile_path(engine.name, REPO_ROOT)

    request = AgentRequest(
        engine=engine,
        prompt=prompt_text,
        workdir=work_dir,
        run_dir=trial_run_dir,
        step_name=f"01-eval-{engine.name}",
        isolation=isolation,
        allow_unsandboxed_local=allow_unsandboxed_local,
        timeout_s=timeout_s,
        skills_enabled=skills_enabled,
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
        profile_path=resolved_profile,
    )
    git_snap = snapshot_git_metadata(work_dir)
    update_run_json(
        trial_run_dir,
        git_config_sha256=git_snap.config_sha256,
        git_hooks_listing=git_snap.hooks_entries_for_log(),
    )
    agent_res = run_agent(request)

    _redact = functools.partial(
        _redact_eval_text,
        extra_secrets=extra_secrets,
        path_replacements=path_replacements,
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

    git_ok, git_tamper_reason = check_git_metadata_unchanged(git_snap)
    if not git_ok:
        tamper_out = _redact(f"SCOPE_VIOLATION: {git_tamper_reason}")
        (trial_run_dir / "steps" / "02-gate.txt").write_text(
            tamper_out + "\n", encoding="utf-8"
        )
        update_run_json(
            trial_run_dir,
            outcome="fail",
            scope="violation",
            scope_violation="git_metadata_tampered",
            git_metadata_tampered=True,
            refusal_reason=git_tamper_reason,
            timed_out=timed_out,
            context=context_mode,
            brief_variant=brief_variant,
            skills_enabled=skills_enabled,
            arm=arm,
            finished=datetime.now(timezone.utc).isoformat(),
        )
        trial_cost_tamper: float | None = None
        if not is_harness_err and not engine.is_local:
            trial_cost_tamper = agent_res.cost_usd
        return TrialResult(
            task_id=task.task_id,
            category=task.category,
            model=model,
            run_index=run_index,
            passed=False,
            agent_returncode=agent_res.exit_code,
            check_returncode=1,
            cost_usd=trial_cost_tamper,
            turns=agent_res.num_turns if not is_harness_err else None,
            duration_s=float(agent_res.duration_s),
            error=_redact(git_tamper_reason),
            check_output=tamper_out,
            harness_error=False,
            harness_error_reason="",
            agent_result_excerpt=excerpt,
            permission_denials=denials_out if isinstance(denials_out, list) else [],
            stderr_tail=stderr_tail,
            tool_protocol_errors=int(agent_res.tool_protocol_errors),
            run_dir=_redact(str(trial_run_dir)),
            timed_out=timed_out,
            engine_class=engine.engine_class,
            tokens_in=int(agent_res.tokens_in) if not is_harness_err else 0,
            tokens_out=int(agent_res.tokens_out) if not is_harness_err else 0,
            mode="single",
            init_model=agent_res.init_model,
            init_digest=agent_res.init_digest,
            brief_variant=brief_variant,
            context_mode=context_mode,
            skills_enabled=skills_enabled,
            arm=arm,
        )

    lint_findings_first: int | None = None
    lint_clean_final: bool | None = None
    rounds_to_clean: int | None = None
    static_findings_first: int | None = None
    static_clean_final: bool | None = None
    if not is_harness_err and not timed_out:
        base_ref = _resolve_baseline_ref(
            work_dir,
            trial_dir=paths["trial_root"],
            baseline_sha=str(paths.get("baseline_sha", "")),
        )
        changed_files, _ = get_changed_files(work_dir, base_ref)
        lint_ok, lint_count, _lint_out = run_preflight_lint_on_changed(
            work_dir, changed_files or [], pack_root=REPO_ROOT
        )
        lint_findings_first = lint_count
        lint_clean_final = lint_ok
        rounds_to_clean = 0 if lint_ok else None

        static_ok, static_count, _static_out = run_cppcheck_on_changed(
            work_dir, changed_files or [], pack_root=REPO_ROOT
        )
        static_findings_first = static_count
        static_clean_final = static_ok

    check_rc, check_out = _run_check_script(
        task.check_sh,
        work_dir,
        paths["home_dir"],
        paths["tmp_dir"],
        timeout_s,
        trial_dir=paths["trial_root"],
        baseline_sha=str(paths.get("baseline_sha", "")),
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
        lint_findings_first_attempt=lint_findings_first,
        lint_clean_final=lint_clean_final,
        static_findings_first_attempt=static_findings_first,
        static_clean_final=static_clean_final,
        context=context_mode,
        brief_variant=brief_variant,
        skills_enabled=skills_enabled,
        arm=arm,
        finished=datetime.now(timezone.utc).isoformat(),
    )

    suspected_cause: str | None = None
    attr_secondary: str | None = None
    if not passed:
        suspected_cause, attr_secondary, _ = _analyze_failed_run_dir(
            trial_run_dir, fixture_dir, engines_config_path
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
        static_findings_first_attempt=static_findings_first,
        static_clean_final=static_clean_final,
        mode="single",
        init_model=agent_res.init_model,
        init_digest=agent_res.init_digest,
        brief_variant=brief_variant,
        context_mode=context_mode,
        skills_enabled=skills_enabled,
        suspected_cause=suspected_cause,
        attribution_secondary=attr_secondary,
        arm=arm,
    )


def _implementer_init_info(run_dir: Path, step_logs: Sequence[str]) -> tuple[str, str]:
    """Return (init_model, init_digest) of the first implementer step that reports an init model.

    Reads the workflow step logs in ``run_dir``, follows each implementer step's
    ``transcript_path`` and parses its stream ``init`` event. Returns empty strings when
    no implementer transcript exposes init info; nothing is invented.
    """
    for name in step_logs:
        log_path = run_dir / name
        if not log_path.is_file():
            continue
        try:
            payload = json.loads(log_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("role") != "implementer":
            continue
        transcript = Path(str(payload.get("transcript_path") or ""))
        if not transcript.is_file():
            continue
        parsed = agent_exec.parse_stream(transcript)
        if parsed.init_model:
            return parsed.init_model, parsed.init_digest
    return "", ""


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
    profile_overrides: Mapping[str, Path | None] | None = None,
    brief_variant: str = "standard",
    context_mode: str = "none",
    repomap_budget: int = 1500,
    skills_enabled: bool = False,
    engines_config_path: Path | None = None,
    arm: str = "base",
) -> TrialResult:
    """Execute one runner-mode trial (implement -> gate -> review -> fix) and grade it."""
    if brief_variant == "oracle" and not (task.task_dir / "oracle.md").is_file():
        all_local = impl_engine.is_local and rev_engine.is_local and fixer_engine.is_local
        return TrialResult(
            task_id=task.task_id,
            category=task.category,
            model=pair_label,
            run_index=run_index,
            passed=False,
            agent_returncode=0,
            check_returncode=0,
            cost_usd=None,
            turns=None,
            duration_s=0.0,
            engine_class="local" if all_local else "cloud",
            mode="runner",
            brief_variant=brief_variant,
            context_mode=context_mode,
            skills_enabled=skills_enabled,
            skipped=True,
            skip_reason="no_oracle",
            arm=arm,
        )
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
            brief_variant=brief_variant,
            context_mode=context_mode,
            repomap_budget=repomap_budget,
            skills_enabled=skills_enabled,
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
        task_raw_text = task.task_md.read_text(encoding="utf-8")
        task_prompt_text = paths["prompt_file"].read_text(encoding="utf-8")
        allowed_str = _parse_task_allowed_files(task_raw_text)
        context = devpack_run.load_task_data(
            task_file=paths["prompt_file"],
            task_id=task.task_id,
            title=task.task_id,
            goal=task_prompt_text,
            allowed_files=allowed_str,
        )
        git_snap = snapshot_git_metadata(work_dir)
        update_run_json(
            trial_run_dir,
            git_config_sha256=git_snap.config_sha256,
            git_hooks_listing=git_snap.hooks_entries_for_log(),
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
            profile_overrides=profile_overrides,
            skills_enabled=skills_enabled,
        )

        task_prompt_in_work = work_dir / "TASK_PROMPT.md"
        if task_prompt_in_work.exists():
            task_prompt_in_work.unlink()

        timed_out = bool(summary.get("timed_out", False))
        is_harness_err = bool(summary.get("harness_error", False))
        harness_reason = str(summary.get("harness_error_reason", ""))

        git_ok, git_tamper_reason = check_git_metadata_unchanged(git_snap)
        git_tampered = bool(summary.get("git_metadata_tampered", False)) or (not git_ok)
        if git_tampered:
            tamper_detail = git_tamper_reason or str(summary.get("escalation_reason", "git_metadata_tampered"))
            check_rc = 1
            check_out = f"SCOPE_VIOLATION: {tamper_detail}"
            update_run_json(
                trial_run_dir,
                outcome="fail",
                scope="violation",
                scope_violation="git_metadata_tampered",
                git_metadata_tampered=True,
            )
        else:
            check_rc, check_out = _run_check_script(
                task.check_sh,
                work_dir,
                paths["home_dir"],
                paths["tmp_dir"],
                timeout_s,
                trial_dir=paths["trial_root"],
                baseline_sha=str(paths.get("baseline_sha", "")),
            )
            if check_rc == 2 or "GRADER_MISCONFIGURED" in check_out:
                is_harness_err = True
                if not harness_reason:
                    harness_reason = f"grader misconfigured (exit {check_rc}): {check_out}"

        _redact = functools.partial(
            _redact_eval_text,
            extra_secrets=extra_secrets,
            path_replacements=path_replacements,
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
            context=context_mode,
            brief_variant=brief_variant,
            skills_enabled=skills_enabled,
            arm=arm,
            finished=datetime.now(timezone.utc).isoformat(),
        )

        suspected_cause: str | None = None
        attr_secondary: str | None = None
        if not passed:
            suspected_cause, attr_secondary, _ = _analyze_failed_run_dir(
                trial_run_dir, fixture_dir, engines_config_path
            )

        trial_cost: float | None = None
        if not is_harness_err and not all_local:
            trial_cost = float(summary.get("cost_usd", 0.0) or 0.0)
        runner_init_model, runner_init_digest = _implementer_init_info(
            trial_run_dir, summary.get("step_logs", []) or []
        )

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
            static_findings_first_attempt=summary.get("static_findings_first_attempt"),
            static_clean_final=summary.get("static_clean_final"),
            local_tokens=int(summary.get("local_tokens", 0) or 0),
            local_wall_s=float(summary.get("local_wall_s", 0.0) or 0.0),
            mode="runner",
            rounds_used=int(summary.get("fix_rounds_used", 0) or 0),
            escalated=escalated,
            reviewer_verdict=verdict,
            reviewer_false_approve=false_approve,
            reviewer_false_reject=false_reject,
            init_model=runner_init_model,
            init_digest=runner_init_digest,
            brief_variant=brief_variant,
            context_mode=context_mode,
            skills_enabled=skills_enabled,
            suspected_cause=suspected_cause,
            attribution_secondary=attr_secondary,
            arm=arm,
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
            proc = run_git(
                args_git,
                repo_root=work_dir,
                cwd=work_dir,
                env=git_env,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"git {' '.join(args_git)} failed: {proc.stderr.strip()}")
        orch_base_sha = run_git(
            ["rev-parse", "HEAD"], repo_root=work_dir, cwd=work_dir, env=git_env, check=True
        ).stdout.strip()
        (trial_root / "baseline.sha").write_text(orch_base_sha + "\n", encoding="utf-8")

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
        git_snap = snapshot_git_metadata(work_dir)
        update_run_json(
            trial_run_dir,
            git_config_sha256=git_snap.config_sha256,
            git_hooks_listing=git_snap.hooks_entries_for_log(),
        )
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

        _redact = functools.partial(
            _redact_eval_text,
            extra_secrets=extra_secrets,
            path_replacements=path_replacements,
        )

        timed_out = agent_res.status == "timeout"
        is_harness_err = agent_res.status == "harness_error"
        harness_reason = agent_res.error if is_harness_err else ""
        error_msg = agent_res.error if agent_res.status != "ok" else ""

        analysis = analyze_orchestrator_transcript(agent_res.transcript_path)

        git_ok, git_tamper_reason = check_git_metadata_unchanged(git_snap)
        if not git_ok:
            check_rc = 1
            check_out = f"SCOPE_VIOLATION: {git_tamper_reason}"
            update_run_json(
                trial_run_dir,
                scope="violation",
                scope_violation="git_metadata_tampered",
                git_metadata_tampered=True,
            )
        else:
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
                trial_dir=trial_root,
                baseline_sha=orch_base_sha,
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
            context="none",
            brief_variant="standard",
            skills_enabled=False,
            finished=datetime.now(timezone.utc).isoformat(),
        )

        suspected_cause: str | None = None
        attr_secondary: str | None = None
        if not passed:
            suspected_cause, attr_secondary, _ = _analyze_failed_run_dir(
                trial_run_dir, fixture_dir, engines_config
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
            suspected_cause=suspected_cause,
            attribution_secondary=attr_secondary,
        )
    finally:
        cleanup_cfg_dir(trial_root / "cfg")


MEMORY_PROTECTED_GLOBS: tuple[str, ...] = ("tests/**",)
MEMORY_STEP_BASELINE_MSG = "chore: memory eval step baseline"
SCOPE_CHECK_SCRIPT = REPO_ROOT / "enforcement" / "checks" / "scope_check.py"
TEST_LOCK_SCRIPT = REPO_ROOT / "enforcement" / "checks" / "test_lock.py"


def _commit_step_baseline(work_dir: Path, git_env: Mapping[str, str]) -> str:
    """Commit the worktree as it stands before a step and return that commit SHA.

    The SHA is the base for the step's enforcement checks, so each step is judged only on
    the edits it made (earlier accepted steps are part of its baseline).
    """
    env = dict(git_env)
    run_git(["add", "-A"], repo_root=work_dir, cwd=work_dir, env=env, check=True)
    run_git(
        ["commit", "-q", "--allow-empty", "-m", MEMORY_STEP_BASELINE_MSG],
        repo_root=work_dir,
        cwd=work_dir,
        env=env,
        check=True,
    )
    head = run_git(
        ["rev-parse", "HEAD"],
        repo_root=work_dir,
        cwd=work_dir,
        env=env,
        check=True,
    )
    return head.stdout.strip()


def _classify_enforcement_proc(
    proc: subprocess.CompletedProcess[str],
    violation_tag: str,
    violation_kind: str,
    label: str,
) -> tuple[str, str]:
    """Map a check result to ``("", "")`` (clean), ``(violation_kind, detail)`` or ``("harness_error", detail)``."""
    if proc.returncode == 0:
        return "", ""
    violation_lines = [line for line in (proc.stdout or "").splitlines() if violation_tag in line]
    if proc.returncode == 1 and violation_lines:
        return violation_kind, "; ".join(violation_lines)
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-500:]
    return "harness_error", f"{label} failed (exit {proc.returncode}): {tail}"


def _run_step_enforcement(
    work_dir: Path,
    base_ref: str,
    allowed_files: Sequence[str],
    git_env: Mapping[str, str],
    timeout_s: int,
) -> tuple[str, str]:
    """Run the test lock then the scope check for one step, using the pack-root scripts.

    Returns ``("", "")`` when the step is clean, ``("test_lock_violation" | "scope_violation", detail)``
    on a violation, or ``("harness_error", detail)`` when a check could not run. A checker that exceeds
    ``timeout_s`` yields ``("harness_error", "enforcement_timeout: ...")`` instead of raising.
    """
    if not allowed_files:
        return "harness_error", "memory step has no allowed_files; cannot run scope check"
    env = dict(git_env)
    try:
        lock = subprocess.run(
            [
                sys.executable,
                "-B",  # never write __pycache__ into the pack root
                str(TEST_LOCK_SCRIPT),
                "--repo",
                str(work_dir),
                "--base",
                base_ref,
                "--protected",
                *MEMORY_PROTECTED_GLOBS,
                "--no-deviation",  # the agent under test may commit its own Deviation trailer
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_s,
            check=False,
        )
        kind, detail = _classify_enforcement_proc(lock, "TEST_LOCK_VIOLATION", "test_lock_violation", "test_lock")
        if kind:
            return kind, detail
        scope = subprocess.run(
            [
                sys.executable,
                "-B",  # never write __pycache__ into the pack root
                str(SCOPE_CHECK_SCRIPT),
                "--repo",
                str(work_dir),
                "--base",
                base_ref,
                "--allow",
                *allowed_files,
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "harness_error", f"enforcement_timeout: checker timed out after {timeout_s}s"
    return _classify_enforcement_proc(scope, "SCOPE_VIOLATION", "scope_violation", "scope_check")


def run_memory_trial(
    seq: MemorySequenceSpec,
    model: str,
    arm: str,
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
    profile_overrides: Mapping[str, Path | None] | None = None,
    memory_config: MemoryConfig | None = None,
) -> TrialResult:
    """Execute one multi-step memory evaluation trial (fresh agent per step) for ``with_memory`` or ``without_memory``."""
    trial_name = f"{seq.seq_id}__{model}__{arm}__run{run_index}"
    trial_root = run_root / trial_name
    engine = engine_config or resolve_engine(model)
    mem_enabled = arm == "with_memory"
    try:
        if isolation not in ("local", "docker"):
            raise ValueError(f"Unsupported isolation mode: {isolation}")
        if isolation == "docker" and not docker_bin:
            raise RuntimeError("docker_bin is required for docker isolation mode")

        home_dir = trial_root / "home"
        cfg_dir = trial_root / "cfg"
        tmp_dir = trial_root / "tmp"
        work_dir = trial_root / "work"
        for d in (home_dir, cfg_dir, tmp_dir, work_dir):
            d.mkdir(parents=True, exist_ok=True)

        if credentials_path is not None and engine.is_cloud:
            if not credentials_path.is_file():
                raise RuntimeError(f"Credentials file not found: {credentials_path}")
            cred_dest = cfg_dir / ".credentials.json"
            shutil.copyfile(credentials_path, cred_dest)
            os.chmod(cred_dest, stat.S_IRUSR | stat.S_IWUSR)

        agent_cwd = DOCKER_TASK_DIR if isolation == "docker" else str(work_dir)
        (cfg_dir / ".claude.json").write_text(
            json.dumps(build_claude_state(agent_cwd), indent=2) + "\n",
            encoding="utf-8",
        )

        shutil.copytree(fixture_dir, work_dir, dirs_exist_ok=True)
        git_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home_dir),
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C.UTF-8",
        }
        for args in (
            ["init", "-q"],
            ["config", "user.email", "eval@example.invalid"],
            ["config", "user.name", "Eval Runner"],
            ["add", "-A"],
            ["commit", "-q", "-m", "chore: base fixture"],
        ):
            run_git(args, repo_root=work_dir, cwd=work_dir, env=git_env, check=True)
        mem_base_sha = run_git(
            ["rev-parse", "HEAD"], repo_root=work_dir, cwd=work_dir, env=git_env, check=True
        ).stdout.strip()
        (trial_root / "baseline.sha").write_text(mem_base_sha + "\n", encoding="utf-8")

        (work_dir / ".claude").mkdir(parents=True, exist_ok=True)
        (work_dir / ".claude" / "settings.json").write_text(
            json.dumps(build_project_settings(), indent=2) + "\n",
            encoding="utf-8",
        )
        exclude_file = work_dir / ".git" / "info" / "exclude"
        if exclude_file.parent.is_dir():
            existing = exclude_file.read_text(encoding="utf-8") if exclude_file.is_file() else ""
            exclude_file.write_text(existing + "\n.claude/\n.devpack/\n", encoding="utf-8")

        mem_root = memory_root_for(work_dir)
        if mem_enabled and seq.seed_memory_dir is not None and seq.seed_memory_dir.is_dir():
            shutil.copytree(seq.seed_memory_dir, mem_root, dirs_exist_ok=True)

        effective_runs_root = runs_root if runs_root is not None else (trial_root / "runs")
        trial_run_dir = new_run_dir(
            effective_runs_root,
            kind="eval",
            task=f"{seq.seq_id}:{arm}",
            engines=[engine.name],
        )

        if profile_overrides is not None and engine.name in profile_overrides:
            resolved_profile = profile_overrides[engine.name]
        else:
            resolved_profile = resolve_engine_profile_path(engine.name, REPO_ROOT)

        _redact = functools.partial(
            _redact_eval_text,
            extra_secrets=extra_secrets,
            path_replacements=path_replacements,
        )

        total_cost = 0.0
        total_turns = 0
        total_duration = 0.0
        total_tok_in = 0
        total_tok_out = 0
        total_proto_errors = 0
        total_injected_tokens = 0
        all_injected_ids: list[str] = []
        prior_proposed_ids: list[str] = []
        total_expected = 0
        total_recalled = 0
        total_over_recall_hits = 0
        total_over_recall_denom = 0
        any_timeout = False
        any_harness_err = False
        harness_reason = ""
        last_rc = 0
        last_error = ""
        last_excerpt = ""
        last_stderr = ""
        last_init_model = ""
        last_init_digest = ""
        violation_reason = ""
        violation_detail = ""

        for step_idx, step in enumerate(seq.steps, start=1):
            step_prompt = step.prompt_file.read_text(encoding="utf-8")
            req = AgentRequest(
                engine=engine,
                prompt=step_prompt,
                workdir=work_dir,
                run_dir=trial_run_dir,
                step_name=f"{step_idx:02d}-{step.role}-{engine.name}",
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
                role=step.role,
                step_index=step_idx,
                home_dir=home_dir,
                cfg_dir=cfg_dir,
                tmp_dir=tmp_dir,
                home_placeholder="$HOME",
                profile_path=resolved_profile,
                memory_enabled=mem_enabled,
                memory_root=mem_root,
                memory_config=memory_config,
                memory_task_id=seq.seq_id,
            )
            step_base_ref = _commit_step_baseline(work_dir, git_env)
            git_snap = snapshot_git_metadata(work_dir)
            update_run_json(
                trial_run_dir,
                git_config_sha256=git_snap.config_sha256,
                git_hooks_listing=git_snap.hooks_entries_for_log(),
            )
            agent_res = run_agent(req)
            last_rc = agent_res.exit_code
            last_excerpt = _redact(agent_res.final_text)[:EXCERPT_MAX_CHARS]
            last_stderr = _redact(agent_res.stderr)[-STDERR_TAIL_MAX_CHARS:]
            if agent_res.init_model:
                last_init_model = agent_res.init_model
                last_init_digest = agent_res.init_digest
            if not engine.is_local:
                total_cost += float(agent_res.cost_usd or 0.0)
            total_turns += int(agent_res.num_turns or 0)
            total_duration += float(agent_res.duration_s or 0.0)
            total_tok_in += int(agent_res.tokens_in or 0)
            total_tok_out += int(agent_res.tokens_out or 0)
            total_proto_errors += int(agent_res.tool_protocol_errors or 0)
            total_injected_tokens += int(agent_res.memory_tokens or 0)
            for mid in agent_res.memory_ids:
                if mid not in all_injected_ids:
                    all_injected_ids.append(mid)

            if agent_res.status == "timeout":
                any_timeout = True
            if agent_res.status == "harness_error":
                any_harness_err = True
                if not harness_reason:
                    harness_reason = agent_res.error
            if agent_res.status != "ok" and not last_error:
                last_error = agent_res.error

            # Recall and over-recall are counted for every step that ran, including a step that
            # then violates enforcement, so the violating step is reflected in the trial metrics.
            if mem_enabled:
                expected_set = set(step.expected_ids)
                if step.expect_prior_proposals:
                    expected_set.update(prior_proposed_ids)
                forbidden_set = set(step.forbidden_ids)
                injected_set = set(agent_res.memory_ids)
                total_expected += len(expected_set)
                total_recalled += len(injected_set & expected_set)

                bad_injected = set(injected_set & forbidden_set)
                if expected_set:
                    bad_injected |= (injected_set - expected_set)
                cited_forbidden = {
                    fid for fid in forbidden_set if fid and fid in agent_res.final_text
                }
                bad_total = bad_injected | cited_forbidden
                total_over_recall_hits += len(bad_total)
                total_over_recall_denom += max(1, len(injected_set | cited_forbidden))

            git_ok, git_tamper_reason = check_git_metadata_unchanged(git_snap)
            if not git_ok:
                violation_reason = "git_metadata_tampered"
                violation_detail = _redact(f"step {step_idx}: {git_tamper_reason}")
                update_run_json(
                    trial_run_dir,
                    scope="violation",
                    scope_violation="git_metadata_tampered",
                    git_metadata_tampered=True,
                )
                break

            # Enforce scope and the test lock before accepting this step's proposals.
            enforce_kind, enforce_detail = _run_step_enforcement(
                work_dir, step_base_ref, step.allowed_files, git_env, timeout_s
            )
            if enforce_kind == "harness_error":
                any_harness_err = True
                if not harness_reason:
                    harness_reason = _redact(f"step {step_idx}: {enforce_detail}")
                break
            if enforce_kind:
                violation_reason = enforce_kind
                violation_detail = _redact(f"step {step_idx}: {enforce_detail}")
                break

            # Auto-accept deterministic handoff/known-gap proposals after step build gate passes
            if mem_enabled and agent_res.status == "ok":
                try:
                    build_proc = subprocess.run(
                        ["bash", str(work_dir / "build_and_test.sh"), "--no-cmake"],
                        cwd=str(work_dir),
                        capture_output=True,
                        text=True,
                        timeout=timeout_s,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    any_harness_err = True
                    if not harness_reason:
                        harness_reason = _redact(
                            f"step {step_idx}: build_timeout: build_and_test.sh timed out after {timeout_s}s"
                        )
                    break
                if build_proc.returncode == 0:
                    prop_res = process_memory_proposals(
                        mem_root,
                        agent_res.final_text,
                        source=f"run:{trial_run_dir.name}-step{step_idx}",
                        task_id=seq.seq_id,
                        extra_secrets=extra_secrets,
                    )
                    prior_proposed_ids.extend(e.id for e in prop_res.accepted)

            if any_harness_err or any_timeout:
                break

        if violation_reason == "git_metadata_tampered":
            check_rc = 1
            check_out = f"SCOPE_VIOLATION: {violation_detail}"
        else:
            check_rc, check_out = _run_check_script(
                seq.check_sh,
                work_dir,
                home_dir,
                tmp_dir,
                timeout_s,
                trial_dir=trial_root,
                baseline_sha=mem_base_sha,
            )
            if check_rc == 2 or "GRADER_MISCONFIGURED" in check_out:
                any_harness_err = True
                if not harness_reason:
                    harness_reason = f"grader misconfigured (exit {check_rc}): {check_out}"

        redacted_check_out = _redact(check_out)
        gate_step_num = len(seq.steps) + 1
        (trial_run_dir / "steps" / f"{gate_step_num:02d}-gate.txt").write_text(
            (redacted_check_out + "\n") if redacted_check_out else f"check_rc={check_rc}\n",
            encoding="utf-8",
        )

        recall_rate: float | None = None
        over_recall_rate: float | None = None
        if mem_enabled:
            recall_rate = (
                round(total_recalled / total_expected, 4) if total_expected > 0 else 1.0
            )
            over_recall_rate = (
                round(total_over_recall_hits / total_over_recall_denom, 4)
                if total_over_recall_denom > 0
                else 0.0
            )

        passed = check_rc == 0 and not any_harness_err and not any_timeout and not violation_reason
        outcome = (
            "pass"
            if passed
            else (
                "harness_error"
                if any_harness_err
                else ("timeout" if any_timeout else (violation_reason or "fail"))
            )
        )
        update_run_json(
            trial_run_dir,
            outcome=outcome,
            timed_out=any_timeout,
            violation_reason=violation_reason,
            memory_arm=arm,
            memory_recall_rate=recall_rate,
            memory_over_recall_rate=over_recall_rate,
            memory_injected_tokens=total_injected_tokens,
            context="none",
            brief_variant="standard",
            skills_enabled=False,
            finished=datetime.now(timezone.utc).isoformat(),
        )

        suspected_cause: str | None = None
        attr_secondary: str | None = None
        if not passed:
            suspected_cause, attr_secondary, _ = _analyze_failed_run_dir(
                trial_run_dir, fixture_dir, None
            )

        return TrialResult(
            task_id=seq.seq_id,
            category=seq.category,
            model=model,
            run_index=run_index,
            passed=passed,
            agent_returncode=last_rc,
            check_returncode=check_rc,
            cost_usd=None if (any_harness_err or engine.is_local) else round(total_cost, 6),
            turns=total_turns if not any_harness_err else None,
            duration_s=round(total_duration, 4),
            error=_redact(last_error) or violation_detail,
            check_output=redacted_check_out,
            harness_error=any_harness_err,
            harness_error_reason=_redact(harness_reason),
            agent_result_excerpt=last_excerpt,
            stderr_tail=last_stderr,
            tool_protocol_errors=total_proto_errors,
            run_dir=_redact(str(trial_run_dir)),
            timed_out=any_timeout,
            engine_class=engine.engine_class,
            tokens_in=total_tok_in if not any_harness_err else 0,
            tokens_out=total_tok_out if not any_harness_err else 0,
            mode="memory",
            init_model=last_init_model,
            init_digest=last_init_digest,
            memory_arm=arm,
            memory_recall_rate=recall_rate,
            memory_over_recall_rate=over_recall_rate,
            memory_injected_tokens=total_injected_tokens,
            memory_injected_ids=all_injected_ids,
            violation_reason=violation_reason,
            suspected_cause=suspected_cause,
            attribution_secondary=attr_secondary,
        )
    finally:
        cleanup_cfg_dir(trial_root / "cfg")


def aggregate_results(
    tasks: Sequence[TaskSpec | OrchestratorGoalSpec | MemorySequenceSpec],
    models: Sequence[str],
    k: int,
    trials: Sequence[TrialResult],
) -> tuple[list[TaskModelSummary], list[dict[str, Any]]]:
    """Aggregate trial results into per-task and per-category summaries."""
    by_task_model: dict[tuple[str, str], list[TrialResult]] = {}
    for trial in trials:
        by_task_model.setdefault((trial.task_id, trial.model), []).append(trial)

    def _task_id_of(item: TaskSpec | OrchestratorGoalSpec | MemorySequenceSpec) -> str:
        """Return task_id, goal_id, or seq_id for a spec item."""
        if isinstance(item, TaskSpec):
            return item.task_id
        if isinstance(item, MemorySequenceSpec):
            return item.seq_id
        return item.goal_id

    task_summaries: list[TaskModelSummary] = []
    for task in tasks:
        tid = _task_id_of(task)
        for model in models:
            runs = sorted(
                by_task_model.get((tid, model), []),
                key=lambda r: r.run_index,
            )
            harness_errors = sum(1 for r in runs if r.harness_error)
            skipped_runs = sum(1 for r in runs if r.skipped)
            timeouts = sum(1 for r in runs if r.timed_out and not r.harness_error)
            protocol_errors = sum(int(r.tool_protocol_errors) for r in runs)
            valid = [r for r in runs if not r.harness_error and not r.skipped]
            memory_runs = [r for r in valid if r.mode == "memory"]
            with_mem_runs = [r for r in memory_runs if r.memory_arm == "with_memory"]
            without_mem_runs = [r for r in memory_runs if r.memory_arm == "without_memory"]
            scored_valid = with_mem_runs if with_mem_runs else valid
            has_valid = bool(scored_valid)
            total = len(scored_valid) if has_valid else 0
            passes = sum(1 for r in scored_valid if r.passed)
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
            static_first = [
                r.static_findings_first_attempt
                for r in valid
                if r.static_findings_first_attempt is not None
            ]
            static_clean = [r.static_clean_final for r in valid if r.static_clean_final is not None]

            rounds_used_list = [r.rounds_used for r in valid if r.rounds_used is not None]
            runner_runs = [r for r in valid if r.mode == "runner"]
            escalations = sum(1 for r in runner_runs if r.escalated)
            false_approves = sum(1 for r in runner_runs if r.reviewer_false_approve)
            false_rejects = sum(1 for r in runner_runs if r.reviewer_false_reject)

            recall_vals = [r.memory_recall_rate for r in with_mem_runs if r.memory_recall_rate is not None]
            over_recall_vals = [r.memory_over_recall_rate for r in with_mem_runs if r.memory_over_recall_rate is not None]
            inj_tok_vals = [r.memory_injected_tokens for r in with_mem_runs if r.memory_injected_tokens is not None]
            pass_with_mem = (
                round(sum(1 for r in with_mem_runs if r.passed) / len(with_mem_runs), 4)
                if with_mem_runs
                else None
            )
            pass_without_mem = (
                round(sum(1 for r in without_mem_runs if r.passed) / len(without_mem_runs), 4)
                if without_mem_runs
                else None
            )
            mem_ab_delta = (
                round(pass_with_mem - pass_without_mem, 4)
                if pass_with_mem is not None and pass_without_mem is not None
                else None
            )

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
                    tier=getattr(task, "tier", "smoke"),
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
                    mean_static_findings_first_attempt=(
                        round(sum(static_first) / len(static_first), 4) if static_first else None
                    ),
                    static_clean_final_rate=(
                        round(sum(1 for x in static_clean if x) / len(static_clean), 4)
                        if static_clean
                        else None
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
                    memory_recall_rate=(
                        round(sum(recall_vals) / len(recall_vals), 4) if recall_vals else None
                    ),
                    memory_over_recall_rate=(
                        round(sum(over_recall_vals) / len(over_recall_vals), 4)
                        if over_recall_vals
                        else None
                    ),
                    mean_injected_tokens=(
                        round(sum(inj_tok_vals) / len(inj_tok_vals), 1) if inj_tok_vals else None
                    ),
                    pass_with_memory=pass_with_mem,
                    pass_without_memory=pass_without_mem,
                    memory_ab_delta=mem_ab_delta,
                    skipped_runs=skipped_runs,
                    ablation_cause=next((r.ablation_cause for r in runs if r.ablation_cause), None),
                    ablation_arms=next((dict(r.ablation_arms) for r in runs if r.ablation_arms), {}),
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
            cat_static_first = [
                s.mean_static_findings_first_attempt
                for s in matching
                if s.mean_static_findings_first_attempt is not None
            ]
            cat_static_clean = [
                s.static_clean_final_rate
                for s in matching
                if s.static_clean_final_rate is not None
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
                    "mean_static_findings_first_attempt": _mean(cat_static_first),
                    "static_clean_final_rate": _mean(cat_static_clean),
                    "mean_rounds_used": _mean(cat_rounds_used),
                    "escalate_rate": _mean(cat_escalate),
                    "false_approve_count": sum(s.false_approve_count for s in matching),
                    "false_reject_count": sum(s.false_reject_count for s in matching),
                    "false_approve_rate": _mean(cat_false_app),
                    "false_reject_rate": _mean(cat_false_rej),
                    "memory_recall_rate": _mean([s.memory_recall_rate for s in matching if s.memory_recall_rate is not None]),
                    "memory_over_recall_rate": _mean([s.memory_over_recall_rate for s in matching if s.memory_over_recall_rate is not None]),
                    "mean_injected_tokens": _mean([s.mean_injected_tokens for s in matching if s.mean_injected_tokens is not None]),
                    "pass_with_memory": _mean([s.pass_with_memory for s in matching if s.pass_with_memory is not None]),
                    "pass_without_memory": _mean([s.pass_without_memory for s in matching if s.pass_without_memory is not None]),
                    "memory_ab_delta": _mean([s.memory_ab_delta for s in matching if s.memory_ab_delta is not None]),
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


def _compute_failure_cause_rows(
    models: Sequence[str],
    task_summaries: Sequence[TaskModelSummary],
) -> list[dict[str, Any]]:
    """Aggregate per-engine failure counts, shares, and example tasks for summary reporting."""
    rows: list[dict[str, Any]] = []
    for eng in models:
        failed_runs = [
            r
            for ts in task_summaries
            if ts.model == eng
            for r in ts.runs
            if not r.passed and not r.skipped
        ]
        total_fail = len(failed_runs)
        if total_fail == 0:
            continue
        counts: dict[str, int] = {}
        examples: dict[str, str] = {}
        for r in failed_runs:
            cause = (
                r.ablation_cause
                or r.suspected_cause
                or ("ENV" if r.timed_out else ("TOOL" if r.harness_error else "MODEL"))
            )
            counts[cause] = counts.get(cause, 0) + 1
            if cause not in examples or r.task_id < examples[cause]:
                examples[cause] = r.task_id
        for cause, cnt in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            share = round((cnt / total_fail) * 100.0, 1)
            rows.append(
                {
                    "engine": eng,
                    "cause": cause,
                    "count": cnt,
                    "share_pct": share,
                    "example_task": examples[cause],
                }
            )
    return rows


def render_summary_markdown(
    run_id: str,
    date_str: str,
    isolation: str,
    k: int,
    models: Sequence[str],
    task_summaries: Sequence[TaskModelSummary],
    category_summaries: Sequence[dict[str, Any]],
    mode: str = "single",
    ablation_records: Sequence[Mapping[str, Any]] = (),
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
        "| Category | Engine | Tasks | Harness errors | Timeouts | Tool Protocol Errors | pass@1 | pass^k (all-k) | pass^k (p^k) | Mean Cost (USD) | Mean Tokens (in/out) | Mean Duration (s) | Lint 1st | Lint Clean | Static 1st | Static Clean |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
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
            f"{_fmt(cat.get('mean_lint_findings_first_attempt'))} | {_fmt(cat.get('lint_clean_final_rate'))} | "
            f"{_fmt(cat.get('mean_static_findings_first_attempt'))} | {_fmt(cat.get('static_clean_final_rate'))} |"
        )

    lines.extend(
        [
            "",
            "## Per-Task Summary",
            "",
            "| Task | Category | Engine | Passes (c/valid) | Harness errors | Timeouts | Tool Protocol Errors | pass@1 | pass^k (all-k) | pass^k (p^k) | Mean Cost (USD) | Mean Tokens (in/out) | Mean Duration (s) | Lint 1st | Lint Clean | Rounds Clean | Static 1st | Static Clean |",
            "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
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
            f"{_fmt(ts.mean_lint_findings_first_attempt)} | {_fmt(ts.lint_clean_final_rate)} | {_fmt(ts.mean_rounds_to_clean)} | "
            f"{_fmt(ts.mean_static_findings_first_attempt)} | {_fmt(ts.static_clean_final_rate)} |"
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

    if mode == "memory" or any(r.mode == "memory" for ts in task_summaries for r in ts.runs):
        lines.extend(
            [
                "",
                "## Memory Metrics",
                "",
                "| Sequence | Category | Engine | Pass (With Mem) | Pass (No Mem) | A/B Delta | Recall Rate | Over-Recall Rate | Injected Tokens |",
                "|---|---|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for ts in task_summaries:
            lines.append(
                f"| {ts.task_id} | {ts.category} | {ts.model} | "
                f"{_fmt(ts.pass_with_memory)} | {_fmt(ts.pass_without_memory)} | {_fmt(ts.memory_ab_delta)} | "
                f"{_fmt(ts.memory_recall_rate)} | {_fmt(ts.memory_over_recall_rate)} | {_fmt(ts.mean_injected_tokens)} |"
            )

    cause_rows = _compute_failure_cause_rows(models, task_summaries)
    lines.extend(["", "## Failure causes", ""])
    if cause_rows:
        lines.extend(
            [
                "| Engine | Cause | Count | Share | Example task |",
                "|---|---|---:|---:|---|",
            ]
        )
        for row in cause_rows:
            lines.append(
                f"| {row['engine']} | {row['cause']} | {row['count']} | "
                f"{row['share_pct']:.1f}% | {row['example_task']} |"
            )
    else:
        lines.append("_No failures recorded._")

    if ablation_records:
        lines.extend(
            [
                "",
                "## Ablation Arms",
                "",
                "| Task | Engine | Base | Oracle Brief | Repomap | Skill Toggle | Ablation Cause |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for rec in ablation_records:
            arms_map = rec.get("arms", {}) if isinstance(rec, Mapping) else {}
            def _arm_cell(arm_dict: Any) -> str:
                if not isinstance(arm_dict, Mapping):
                    return "n/a"
                if arm_dict.get("skipped"):
                    return f"skipped ({arm_dict.get('skip_reason', 'skipped')})"
                return "pass" if arm_dict.get("passed") else "fail"
            skill_cell = _arm_cell(arms_map.get("skill-on") or arms_map.get("skill-off"))
            lines.append(
                f"| {rec.get('task_id', '')} | {rec.get('model', '')} | fail | "
                f"{_arm_cell(arms_map.get('oracle-brief'))} | "
                f"{_arm_cell(arms_map.get('repomap'))} | "
                f"{skill_cell} | {rec.get('ablation_cause') or 'n/a'} |"
            )

    lines.extend(
        [
            "",
            "## Per-Trial Runs",
            "",
            "| Task | Engine | Run | Passed | Timed Out | Cause | Cost (USD) | Tokens (in/out) | Duration (s) | Tool Protocol Errors | Run Dir |",
            "|---|---|---:|---|---|---|---|---:|---:|---:|---|",
        ]
    )
    for ts in task_summaries:
        for run in ts.runs:
            run_cost = f"{run.cost_usd:.4f}" if run.cost_usd is not None else "n/a"
            tok_str = _fmt_tokens(run.tokens_in, run.tokens_out)
            pass_str = f"skipped ({run.skip_reason})" if run.skipped else ("yes" if run.passed else "no")
            cause_str = (
                run.ablation_cause or run.suspected_cause or "-"
            ) if not run.passed and not run.skipped else "-"
            lines.append(
                f"| {run.task_id} | {run.model} | {run.run_index} | "
                f"{pass_str} | {'yes' if run.timed_out else 'no'} | {cause_str} | {run_cost} | "
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
        choices=("single", "runner", "orchestrator", "memory"),
        default="single",
        help="Evaluation topology mode: single, runner, orchestrator, or memory (default: single)",
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
        "--memory-tasks-dir",
        default=str(default_eval_dir / "memory_tasks"),
        help="Path to memory sequences directory for --mode memory",
    )
    parser.add_argument(
        "--no-memory-ab",
        action="store_true",
        help="Run only the with_memory arm in --mode memory (skip without_memory A/B arm)",
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
    parser.add_argument(
        "--record-qualification",
        action="store_true",
        help="Evaluate SPEC 5.3 criteria and write .devpack/qualification.json when met",
    )
    parser.add_argument(
        "--qualification-file",
        default="",
        help="Path to qualification.json (default: <repo_root>/.devpack/qualification.json)",
    )
    parser.add_argument(
        "--profile-override",
        action="append",
        default=[],
        metavar="ENGINE=PATH",
        help="Override engine profile path for an engine during evaluation (repeatable)",
    )
    parser.add_argument(
        "--context",
        choices=("none", "repomap"),
        default="none",
        help="Code context injection mode: none or repomap (default: none)",
    )
    parser.add_argument(
        "--brief-variant",
        choices=("standard", "minimal", "oracle"),
        default="standard",
        help="Task brief variant: standard, minimal, or oracle (default: standard)",
    )
    parser.add_argument(
        "--repomap-budget",
        type=int,
        default=1500,
        help="Estimated token budget for --context repomap (default: 1500)",
    )
    parser.add_argument(
        "--skills",
        action="store_true",
        help="Install pack skills into isolated workspaces and enable skills in agent invocations",
    )
    parser.add_argument(
        "--ablate",
        action="store_true",
        help="Rerun failing base-arm tasks across oracle-brief, repomap, and toggled-skill arms",
    )
    return parser


def _parse_profile_overrides(raw_items: Sequence[str]) -> dict[str, Path | None]:
    """Parse ``ENGINE=PATH`` CLI flags into a mapping of engine name to profile Path."""
    overrides: dict[str, Path | None] = {}
    for item in raw_items:
        if "=" not in item:
            raise ValueError(f"Invalid --profile-override '{item}': expected ENGINE=PATH")
        eng_name, raw_path = item.split("=", 1)
        eng_name = eng_name.strip()
        raw_path = raw_path.strip()
        if not eng_name:
            raise ValueError(f"Invalid --profile-override '{item}': empty engine name")
        overrides[eng_name] = Path(raw_path).resolve() if raw_path else None
    return overrides


def evaluate_and_record_qualification(
    *,
    tier: str,
    mode: str,
    unit_labels: Sequence[str],
    engine_map: Mapping[str, EngineConfig],
    task_summaries: Sequence[TaskModelSummary],
    qualification_file: Path,
    eval_summary_path: Path,
    repo_root: Path,
) -> tuple[bool, list[str]]:
    """Check SPEC 5.3 qualification criteria and write ``qualification.json`` when met.

    Returns ``(all_qualified, messages)``.
    """
    if tier not in ("smoke", "all"):
        return False, [f"Qualification requires --tier smoke or --tier all (got '{tier}')."]
    if mode not in ("single", "runner"):
        return False, [f"Qualification requires --mode single or --mode runner (got '{mode}')."]

    try:
        summary_rel = str(eval_summary_path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        summary_rel = redact_sensitive_text(str(eval_summary_path), home_placeholder="$HOME")

    qual_data = load_qualification_data(qualification_file) if qualification_file.is_file() else {"version": 1, "engines": {}}
    all_ok = True
    messages: list[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    changed_existing = False

    for label in unit_labels:
        eng_name = label.split(":", 1)[0] if mode == "runner" else label
        engine_cfg = engine_map.get(eng_name)
        if engine_cfg is None:
            all_ok = False
            messages.append(f"{eng_name}: engine configuration not found")
            continue

        smoke_sums = [
            ts for ts in task_summaries if ts.model == label and ts.tier == "smoke"
        ]
        if not smoke_sums:
            all_ok = False
            messages.append(f"{eng_name}: no smoke-tier tasks evaluated")
            continue

        pow3_values = [
            compute_pass_pow_k(ts.passes, ts.valid_runs, k=3) if ts.valid_runs > 0 else 0.0
            for ts in smoke_sums
        ]
        mean_pass_pow_3 = sum(pow3_values) / len(pow3_values)
        total_proto_errors = sum(ts.tool_protocol_errors for ts in smoke_sums)
        passing_trials = [
            r for ts in smoke_sums for r in ts.runs if not r.harness_error and r.passed
        ]
        lint_clean_rate = (
            sum(1 for r in passing_trials if r.lint_clean_final is True) / len(passing_trials)
            if passing_trials
            else 0.0
        )

        failures: list[str] = []
        if mean_pass_pow_3 < 0.8:
            failures.append(f"smoke pass^3={mean_pass_pow_3:.4f} < 0.8")
        if total_proto_errors != 0:
            failures.append(f"tool_protocol_errors={total_proto_errors} != 0")
        if not passing_trials:
            failures.append("no passing smoke trials for lint_clean_final check")
        elif lint_clean_rate < 0.9:
            failures.append(f"lint_clean_final_rate={lint_clean_rate:.4f} < 0.9")

        if failures:
            all_ok = False
            reason = "; ".join(failures)
            messages.append(f"{eng_name}: unqualified ({reason})")
            if eng_name in qual_data["engines"] and isinstance(qual_data["engines"][eng_name], dict):
                qual_data["engines"][eng_name]["status"] = "unqualified"
                qual_data["engines"][eng_name]["unqualified_reason"] = reason
                qual_data["engines"][eng_name]["updated"] = now_iso
                changed_existing = True
            continue

        all_trials = [r for ts in smoke_sums for r in ts.runs]
        init_model = next((r.init_model for r in all_trials if r.init_model), "") or engine_cfg.model
        init_digest = next((r.init_digest for r in all_trials if r.init_digest), "")
        qual_data["engines"][eng_name] = {
            "engine": eng_name,
            "status": "qualified",
            "model_id": init_model,
            "model_digest": init_digest,
            "config_hash": compute_engine_config_hash(engine_cfg),
            "date": now_iso,
            "eval_summary_path": summary_rel,
            "metrics": {
                "pass_pow_3": round(mean_pass_pow_3, 4),
                "tool_protocol_errors": total_proto_errors,
                "lint_clean_final_rate": round(lint_clean_rate, 4),
            },
        }
        changed_existing = True
        messages.append(
            f"{eng_name}: qualified (model_id={init_model}, pass^3={mean_pass_pow_3:.4f}, "
            f"lint_clean={lint_clean_rate:.4f})"
        )

    if changed_existing:
        save_qualification_data(qualification_file, qual_data)
    return all_ok, messages


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
    if args.repomap_budget < 1:
        print("error: --repomap-budget must be >= 1", file=sys.stderr)
        return 2

    fixture_dir = Path(args.fixture_dir).resolve()
    tasks_dir = Path(args.tasks_dir).resolve()
    goals_dir = Path(args.goals_dir).resolve()
    memory_tasks_dir = Path(args.memory_tasks_dir).resolve()
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
        if mode == "memory":
            sequences = load_and_validate_memory_sequences(memory_tasks_dir, args.tasks, args.tier)
            eval_units: list[TaskSpec | OrchestratorGoalSpec | MemorySequenceSpec] = list(sequences)
            raw_engines = getattr(args, "engines", None) or args.models
            unit_labels = [m.strip() for m in raw_engines.split(",") if m.strip()]
            if not unit_labels:
                raise ValueError("--engines/--models must specify at least one engine")
            engine_names_for_creds = list(unit_labels)
        elif mode == "orchestrator":
            goals = load_and_validate_orchestrator_goals(goals_dir, args.tasks, args.tier)
            eval_units = list(goals)
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

    try:
        profile_overrides = _parse_profile_overrides(args.profile_override)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        if mode == "memory":
            arms = ("with_memory",) if args.no_memory_ab else ("with_memory", "without_memory")
            plan_mem = [
                (seq, lbl, arm, run_idx)
                for seq in eval_units
                for lbl in unit_labels
                for arm in arms
                for run_idx in range(1, args.k + 1)
            ]
            print(f"DRY-RUN memory plan ({len(plan_mem)} total trials):")
            print("  mode: memory")
            print(f"  isolation: {args.isolation}")
            print(f"  engines: {', '.join(unit_labels)}")
            print(f"  arms: {', '.join(arms)}")
            print(f"  tier: {args.tier}")
            print(f"  k: {args.k}")
            print(f"  sequences ({len(eval_units)}): {', '.join(s.seq_id for s in eval_units)}")  # type: ignore[union-attr]
            for idx, (seq, lbl, arm, run_idx) in enumerate(plan_mem, start=1):
                print(
                    f"  [{idx:03d}/{len(plan_mem):03d}] "
                    f"seq={seq.seq_id} category={seq.category} engine={lbl} arm={arm} run={run_idx}/{args.k}"  # type: ignore[union-attr]
                )
            return 0
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
        skill_arm_name = "skill-off" if args.skills else "skill-on"
        ablation_arms_list = ("oracle-brief", "repomap", skill_arm_name)
        print(f"DRY-RUN {mode} plan ({len(plan_items)} total trials):")
        print(f"  mode: {mode}")
        print(f"  isolation: {args.isolation}")
        print(f"  tier: {args.tier}")
        print(f"  {kind_label}s: {', '.join(unit_labels)}")
        print(f"  brief_variant: {args.brief_variant}")
        print(f"  context: {args.context}")
        print(f"  skills: {'on' if args.skills else 'off'}")
        if args.ablate:
            print(f"  ablate: enabled (arms on failure: {', '.join(ablation_arms_list)})")
        print(f"  k: {args.k}")
        print(f"  tasks ({len(eval_units)}): {', '.join(t.task_id for t in eval_units)}")  # type: ignore[union-attr]
        for idx, (task, lbl, run_idx) in enumerate(plan_items, start=1):
            oracle_tag = "yes" if (task.task_dir / "oracle.md").is_file() else "no"
            extra_ablate = (
                f" ablation_arms=[{', '.join(ablation_arms_list)}] oracle={oracle_tag}"
                if args.ablate
                else ""
            )
            print(
                f"  [{idx:03d}/{len(plan_items):03d}] "
                f"task={task.task_id} category={task.category} {kind_label}={lbl} "
                f"brief={args.brief_variant} context={args.context} run={run_idx}/{args.k}"
                f"{extra_ablate}"
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
        if mode == "memory":
            arms = ("with_memory",) if args.no_memory_ab else ("with_memory", "without_memory")
            mem_cfg = loaded_engines.memory if loaded_engines is not None else MemoryConfig()
            mem_jobs = [
                (seq, model, arm, run_idx)
                for seq in eval_units
                for model in unit_labels
                for arm in arms
                for run_idx in range(1, args.k + 1)
            ]
            if args.max_parallel == 1:
                for seq, model, arm, run_idx in mem_jobs:
                    trials.append(
                        run_memory_trial(
                            seq=seq,  # type: ignore[arg-type]
                            model=model,
                            arm=arm,
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
                            profile_overrides=profile_overrides,
                            memory_config=mem_cfg,
                        )
                    )
            else:
                with ThreadPoolExecutor(max_workers=args.max_parallel) as pool:
                    futures = [
                        pool.submit(
                            run_memory_trial,
                            seq,  # type: ignore[arg-type]
                            model,
                            arm,
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
                            profile_overrides,
                            mem_cfg,
                        )
                        for seq, model, arm, run_idx in mem_jobs
                    ]
                    for fut in as_completed(futures):
                        trials.append(fut.result())
        elif mode == "orchestrator":
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
                            profile_overrides=profile_overrides,
                            brief_variant=args.brief_variant,
                            context_mode=args.context,
                            repomap_budget=args.repomap_budget,
                            skills_enabled=args.skills,
                            engines_config_path=engines_config_path,
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
                            profile_overrides,
                            args.brief_variant,
                            args.context,
                            args.repomap_budget,
                            args.skills,
                            engines_config_path,
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
                            profile_overrides=profile_overrides,
                            brief_variant=args.brief_variant,
                            context_mode=args.context,
                            repomap_budget=args.repomap_budget,
                            skills_enabled=args.skills,
                            engines_config_path=engines_config_path,
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
                            profile_overrides,
                            args.brief_variant,
                            args.context,
                            args.repomap_budget,
                            args.skills,
                            engines_config_path,
                        )
                        for task, model, run_idx in plan_items
                    ]
                    for fut in as_completed(futures):
                        trials.append(fut.result())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    ablation_records: list[dict[str, Any]] = []
    if args.ablate and mode in ("single", "runner"):
        pair_map = (
            {f"{imp}:{rev}": (engine_map[imp], engine_map[rev], engine_map[fix]) for imp, rev, fix in parse_runner_pairs(args.pairs)}
            if mode == "runner"
            else {}
        )
        skill_arm_name = "skill-off" if args.skills else "skill-on"
        arm_specs = (
            ("oracle-brief", "oracle", args.context, args.skills),
            ("repomap", args.brief_variant, "repomap", args.skills),
            (skill_arm_name, args.brief_variant, args.context, not args.skills),
        )
        for task in eval_units:  # type: ignore[assignment]
            for label in unit_labels:
                base_group = [
                    t for t in trials if t.task_id == task.task_id and t.model == label  # type: ignore[union-attr]
                ]
                failing_base = [
                    t for t in base_group if not t.passed and not t.harness_error and not t.skipped
                ]
                if not failing_base:
                    continue
                rep_trial = failing_base[0]
                analyze_signals: dict[str, Any] = {}
                if rep_trial.run_dir:
                    attr_file = runs_root / Path(rep_trial.run_dir).name / "attribution.json"
                    if attr_file.is_file():
                        try:
                            attr_obj = json.loads(attr_file.read_text(encoding="utf-8"))
                            if isinstance(attr_obj, dict) and isinstance(attr_obj.get("signals"), dict):
                                analyze_signals = attr_obj["signals"]
                        except (OSError, json.JSONDecodeError):
                            pass

                arm_results: dict[str, TrialResult] = {}
                for arm_name, a_brief, a_ctx, a_skill in arm_specs:
                    arm_run_root = run_root / "ablation" / arm_name
                    if mode == "runner":
                        imp_e, rev_e, fix_e = pair_map[label]
                        arm_res = run_runner_trial(
                            task=task,  # type: ignore[arg-type]
                            pair_label=label,
                            impl_engine=imp_e,
                            rev_engine=rev_e,
                            fixer_engine=fix_e,
                            run_index=1,
                            run_root=arm_run_root,
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
                            profile_overrides=profile_overrides,
                            brief_variant=a_brief,
                            context_mode=a_ctx,
                            repomap_budget=args.repomap_budget,
                            skills_enabled=a_skill,
                            engines_config_path=engines_config_path,
                            arm=arm_name,
                        )
                    else:
                        arm_res = run_single_trial(
                            task=task,  # type: ignore[arg-type]
                            model=label,
                            run_index=1,
                            run_root=arm_run_root,
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
                            engine_config=engine_map[label],
                            allow_unsandboxed_local=args.allow_unsandboxed_local,
                            profile_overrides=profile_overrides,
                            brief_variant=a_brief,
                            context_mode=a_ctx,
                            repomap_budget=args.repomap_budget,
                            skills_enabled=a_skill,
                            engines_config_path=engines_config_path,
                            arm=arm_name,
                        )
                    arm_results[arm_name] = arm_res

                oracle_res = arm_results["oracle-brief"]
                repomap_res = arm_results["repomap"]
                skill_res = arm_results[skill_arm_name]
                ab_cause = compute_ablation_cause(
                    False,
                    oracle_passed=(None if (oracle_res.skipped or oracle_res.harness_error) else oracle_res.passed),
                    repomap_passed=(None if (repomap_res.skipped or repomap_res.harness_error) else repomap_res.passed),
                    skill_passed=(None if (skill_res.skipped or skill_res.harness_error) else skill_res.passed),
                    analyze_cause=rep_trial.suspected_cause,
                    analyze_signals=analyze_signals,
                )
                arm_summary = {
                    a_name: {
                        "passed": r.passed,
                        "skipped": r.skipped,
                        "skip_reason": r.skip_reason,
                        "harness_error": r.harness_error,
                        "run_dir": r.run_dir,
                    }
                    for a_name, r in arm_results.items()
                }
                for ft in failing_base:
                    ft.ablation_cause = ab_cause
                    ft.ablation_arms = arm_summary
                ablation_records.append(
                    {
                        "task_id": task.task_id,  # type: ignore[union-attr]
                        "model": label,
                        "suspected_cause": rep_trial.suspected_cause,
                        "ablation_cause": ab_cause,
                        "arms": arm_summary,
                    }
                )

    task_summaries, category_summaries = aggregate_results(
        tasks=eval_units,
        models=unit_labels,
        k=args.k,
        trials=trials,
    )
    failure_causes = _compute_failure_cause_rows(unit_labels, task_summaries)

    raw_payload: dict[str, Any] = {
        "run_id": run_id,
        "date": date_str,
        "mode": mode,
        "tier": args.tier,
        "isolation": args.isolation,
        "brief_variant": args.brief_variant,
        "context": args.context,
        "skills": bool(args.skills),
        "ablate": bool(args.ablate),
        "models": unit_labels,
        "engines": unit_labels,
        "k": args.k,
        "runs_root": str(runs_root),
        "tasks": [asdict(ts) for ts in task_summaries],
        "categories": category_summaries,
        "failure_causes": failure_causes,
        "ablation": ablation_records,
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
        ablation_records=ablation_records,
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

    if args.record_qualification:
        qual_path = (
            Path(args.qualification_file).resolve()
            if args.qualification_file
            else (repo_root / ".devpack" / "qualification.json")
        )
        qual_ok, qual_messages = evaluate_and_record_qualification(
            tier=args.tier,
            mode=mode,
            unit_labels=unit_labels,
            engine_map=engine_map,
            task_summaries=task_summaries,
            qualification_file=qual_path,
            eval_summary_path=dated_dir / "summary.md",
            repo_root=repo_root,
        )
        for msg in qual_messages:
            stream = sys.stdout if qual_ok else sys.stderr
            print(msg, file=stream)
        if not qual_ok:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
