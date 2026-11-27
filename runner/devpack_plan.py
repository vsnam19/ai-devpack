#!/usr/bin/env python3
"""Validate, render, inspect, and run machine-readable task plans."""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner.brief_lint import brief_allowed_files, lint_brief_text  # noqa: E402
from runner.dash.files import Files, JSON_LIMIT, TEXT_LIMIT, identifier, regular  # noqa: E402
from runner.workspace_scope import parse_toml_with_arrays  # noqa: E402

TEMPLATE = Path(__file__).resolve().parents[1] / "workflow/templates/task_plan.md"
SUCCESS = {"pass", "APPROVED"}
ACTIVE = {"running", "awaiting_approval"}
READ_ERRORS = (OSError, ValueError, RecursionError)


def run_link(workdir, plan_id=None, plan_dir=None, task_id=None):
    """Return the optional fields written to run metadata."""
    if plan_id is None:
        if plan_dir is not None:
            raise ValueError("--plan-dir requires --plan-id")
        return {}
    if not identifier(plan_id) or not identifier(task_id):
        raise ValueError("invalid plan or task ID")
    root = Path(workdir).resolve()
    directory = (root / (plan_dir if plan_dir is not None else Path("plans") / plan_id)).resolve()
    return {"plan_id": plan_id, "plan_dir": os.path.relpath(directory, root), "task_id": task_id}


def _fields(record, required, optional, label):
    if not isinstance(record, dict) or required - record.keys() or record.keys() - (required | optional):
        raise ValueError(f"invalid fields in {label}")


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _strings(value):
    return isinstance(value, list) and all(_text(item) for item in value)


def _check_dependencies(tasks):
    dependents = defaultdict(list)
    pending = {}
    for task_id, task in tasks.items():
        pending[task_id] = len(task["depends_on"])
        for dependency in task["depends_on"]:
            if dependency not in tasks:
                raise ValueError(f"task {task_id} has unknown dependency {dependency}")
            dependents[dependency].append(task_id)
    ready = deque(task_id for task_id, count in pending.items() if count == 0)
    visited = 0
    while ready:
        visited += 1
        for dependent in dependents[ready.popleft()]:
            pending[dependent] -= 1
            if pending[dependent] == 0:
                ready.append(dependent)
    if visited != len(tasks):
        raise ValueError("plan contains a dependency cycle")


def load_plan(directory, *, workdir=None, files=None):
    """Read a plan and its briefs. Supply workdir to lint briefs against the project."""
    directory = Path(os.path.abspath(directory))
    files = files if files is not None else Files()
    plan = parse_toml_with_arrays(files.read(directory / "plan.toml", JSON_LIMIT).decode("utf-8"))
    _fields(plan, {"id", "title", "goal", "created", "tasks"}, {"owner_role"}, "plan")
    created = plan["created"]
    if isinstance(created, date):
        plan["created"] = created.isoformat()
    try:
        datetime.fromisoformat(plan["created"].replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ValueError("created must be an ISO date or timestamp") from None
    if not identifier(plan["id"]) or not all(_text(plan[key]) for key in ("title", "goal")):
        raise ValueError("invalid plan id, title, or goal")
    if "owner_role" in plan and not _text(plan["owner_role"]):
        raise ValueError("invalid owner_role")
    if not isinstance(plan["tasks"], list):
        raise ValueError("tasks must be an array of tables")

    tasks = {}
    for task in plan["tasks"]:
        _fields(task, {"id", "title", "brief", "depends_on"}, {"role", "size_limit", "requirement_ids"}, "task")
        task_id = task["id"]
        if not identifier(task_id) or not _text(task["title"]):
            raise ValueError("invalid task id or title")
        if task_id in tasks:
            raise ValueError(f"duplicate task id {task_id}")
        if not _strings(task["depends_on"]) or not _strings(task.get("requirement_ids", [])):
            raise ValueError(f"invalid dependency or requirement list for {task_id}")
        if len(set(task["depends_on"])) != len(task["depends_on"]):
            raise ValueError(f"duplicate dependency for {task_id}")
        if "role" in task and not _text(task["role"]):
            raise ValueError(f"invalid role for {task_id}")
        if "size_limit" in task and (type(task["size_limit"]) is not int or task["size_limit"] <= 0):
            raise ValueError(f"invalid size_limit for {task_id}")
        brief = task["brief"]
        if (not _text(brief) or any(part in ("", ".", "..") for part in brief.split("/"))
                or "\\" in brief or "\x00" in brief or Path(brief).suffix.lower() != ".md"):
            raise ValueError(f"brief for {task_id} must be a relative Markdown path inside the plan")
        tasks[task_id] = task
    _check_dependencies(tasks)

    briefs = {}
    for task_id, task in tasks.items():
        text = files.read(directory / task["brief"], TEXT_LIMIT).decode("utf-8")
        if workdir is not None:
            findings = lint_brief_text(text, repo_root=workdir)
            errors = [f'{item["rule"]}: {item["message"]}' for item in findings if item["severity"] == "error"]
            if errors:
                raise ValueError(f"invalid brief for {task_id}: " + "; ".join(errors))
            brief_allowed_files(text)
        briefs[task_id] = text
    return plan, briefs


def _started(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    except (AttributeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def progress(plan, runs):
    """Join attempts by plan and task ID, ordered newest first."""
    attempts = defaultdict(list)
    for run in runs:
        task_id = run.get("task_id", run.get("task"))
        if run.get("plan_id") == plan["id"] and identifier(task_id):
            attempts[task_id].append({key: run.get(key) for key in ("run_id", "outcome", "started", "finished")})
    latest = {}
    for task_id, records in attempts.items():
        records.sort(key=lambda run: (_started(run["started"]), run["run_id"] or ""), reverse=True)
        latest[task_id] = records[0]["outcome"]
    done = {task_id for task_id, outcome in latest.items() if isinstance(outcome, str) and outcome in SUCCESS}
    tasks = []
    for task in plan["tasks"]:
        task_id = task["id"]
        blocked = [dependency for dependency in task["depends_on"] if dependency not in done]
        outcome = latest.get(task_id)
        if task_id in done:
            state = "done"
        elif isinstance(outcome, str) and outcome in ACTIVE:
            state = "in_progress"
        elif blocked:
            state = "blocked"
        else:
            state = "ready"
        records = attempts[task_id]
        tasks.append({**task, "state": state, "blocked_by": blocked, "attempts": len(records),
                      "latest_outcome": outcome, "runs": records})
    return {**plan, "tasks": tasks, "done": sum(task["state"] == "done" for task in tasks), "total": len(tasks)}


def read_runs(root):
    files = Files()
    try:
        names = files.names(root, folders=True)
    except FileNotFoundError:
        return
    for name in names:
        if not identifier(name):
            continue
        try:
            record = files.json(root / name / "run.json")
        except FileNotFoundError:
            continue
        if record.get("run_id") not in (None, "", name):
            continue
        if "plan_id" in record:
            yield {**record, "run_id": name}


def render_markdown(plan):
    """Fill the existing plan template, repeating its row and task block."""
    template = TEMPLATE.read_text(encoding="utf-8")
    before, row, after = template.partition("| {{TASK_ID}} | {{TITLE}} | {{DEPENDS_ON}} | {{LIMIT}} | {{REQ_IDS}} |")
    between, _, block = after.partition("### Task {{TASK_ID}}:")

    def fill(text, values):
        return re.sub(r"\{\{([A-Z0-9_]+)\}\}", lambda match: str(values.get(match[1], match[0])), text)

    def table(value):
        return " ".join(value.splitlines()).replace("|", "\\|")

    header = fill(before, {
        "PLAN_TITLE": plan["title"], "PLAN_ID": plan["id"], "TECH_LEAD_ROLE": plan.get("owner_role", "Unassigned"),
        "DRAFTER": "Unspecified", "DATE": "Unrecorded", "DEFAULT_MAX_CHANGED_LINES": "project default",
    })
    header = header.replace("## 0. Common conventions", f"Created: {plan['created']}\n\n{plan['goal']}\n\n## 0. Common conventions", 1)
    rows, blocks = [], []
    for task in plan["tasks"]:
        values = {
            "TASK_ID": task["id"], "TITLE": task["title"], "DEPENDS_ON": ", ".join(task["depends_on"]) or "—",
            "LIMIT": task.get("size_limit", "project default"), "REQ_IDS": ", ".join(task.get("requirement_ids", [])) or "—",
        }
        rows.append(fill(row, {key: table(str(value)) for key, value in values.items()}))
        # The brief is the source for task details beyond the manifest fields.
        heading = fill("### Task {{TASK_ID}}:" + block.splitlines()[0], values)
        details = fill("- Requirement IDs: {{REQ_IDS}}\n- Size limit: {{LIMIT}}", values)
        role = f"\n- Role: {task['role']}" if "role" in task else ""
        destination = task["brief"].replace("%", "%25").replace("<", "%3C").replace(">", "%3E")
        blocks.append(f"{heading}\n- [Brief](<{destination}>)\n- Depends on: {values['DEPENDS_ON']}\n{details}{role}")
    return header + "\n".join(rows) + between + "\n\n".join(blocks) + "\n"


def run_next(directory, forwarded):
    import fcntl

    from runner import devpack_run

    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    if forwarded[-1:] == ["--"]:
        forwarded = forwarded[:-1]
    args = devpack_run.build_arg_parser().parse_args(forwarded)
    if args.resume or args.log_dir:
        raise ValueError("run-next requires a new attempt under the runs root")
    workdir = Path(args.worktree).resolve(strict=True)
    runs_root = Path(args.runs_root).resolve() if args.runs_root else workdir / "runs"
    # Serialize run-next invocations for this plan even before a run record exists.
    with regular(directory / "plan.toml") as (fd, _):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("a run-next invocation already holds this plan") from None
        plan, _ = load_plan(directory, workdir=workdir)
        tasks = progress(plan, read_runs(runs_root))["tasks"]
        if any(task["state"] == "in_progress" for task in tasks):
            print("Plan already has a running or paused task.")
            return 1
        task = next((task for task in tasks if task["state"] == "ready"), None)
        if task is None:
            print("No ready tasks.")
            return 0 if all(task["state"] == "done" for task in tasks) else 1
        return devpack_run.main([
            *forwarded, "--plan-id", plan["id"], "--plan-dir", str(directory), "--task-id", task["id"],
            "--task-file", str(directory / task["brief"]), "--title", task["title"],
            "--runs-root", str(runs_root), "--brief-author", "orchestrator",
        ])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "render-md", "status", "run-next"):
        command = commands.add_parser(name)
        command.add_argument("plan_dir", type=Path)
        if name == "run-next":
            command.add_argument("runner_args", nargs=argparse.REMAINDER)
        else:
            command.add_argument("--workdir", type=Path, default=Path.cwd())
        if name == "status":
            command.add_argument("--runs-root", type=Path)
            command.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    directory = args.plan_dir.absolute()
    try:
        if args.command == "run-next":
            return run_next(directory, args.runner_args)
        plan, _ = load_plan(directory, workdir=args.workdir)
        if args.command == "render-md":
            print(render_markdown(plan), end="")
        elif args.command == "status":
            status = progress(plan, read_runs(args.runs_root or args.workdir / "runs"))
            if args.json:
                print(json.dumps(status, indent=2))
            else:
                print(f"{plan['id']}  {status['done']}/{status['total']} done")
                for task in status["tasks"]:
                    blockers = ", ".join(task["blocked_by"])
                    detail = f" (waiting for {blockers})" if blockers else ""
                    print(f"{task['id']}  {task['state']}{detail}  attempts={task['attempts']}  "
                          f"outcome={task['latest_outcome'] or '—'}  {task['title']}")
        else:
            print(f"Valid plan {plan['id']} ({len(plan['tasks'])} tasks)")
    except READ_ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
