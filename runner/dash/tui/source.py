"""Adapt bounded reader snapshots and the selected step stream for the TUI."""

from datetime import datetime, timedelta
import math
import time

from runner.dash.events import Stream
from runner.dash.files import DeferredRead, identifier
from runner.dash.reader import READ_ERRORS, TERMINAL, number, timestamp
from runner.dash.ui import poll_delay

ARTIFACT_ERRORS = (DeferredRead, *READ_ERRORS)
AD_HOC = "Ad-hoc runs"
UNAVAILABLE = "Brief unavailable."
RUN_STATES = {
    "running": "running", "succeeded": "done", "failed": "failed",
    "rejected": "failed", "refused": "failed", "waiting for approval": "awaiting_approval",
    "ready to resume": "blocked",
}
STEP_STATES = {
    **RUN_STATES, "pass": "done", "success": "done", "done": "done", "ok": "done",
    "fail": "failed", "error": "failed", "timeout": "failed", "blocked": "blocked",
    "retrying": "retrying", "queued": "queued",
    "agent_error": "failed", "harness_error": "failed", "engine_unavailable": "failed",
}


def find(rows, key, default=None):
    return next((row for row in rows if row["id"] == key), default)


def current_step(task):
    steps = task["steps"]
    return next((step for step in reversed(steps) if step["index"] == task["current_step"]),
                steps[0] if steps else None)


def selection(snapshot, keys):
    plan_id, task_id, _, step_id = keys
    plans = snapshot["plans"]
    plan = find(plans, plan_id, plans[0] if plans else None)
    tasks = plan["jobs"] if plan else []
    task = find(tasks, task_id, tasks[0] if tasks else None)
    step = find(task["steps"], step_id, current_step(task)) if task else None
    return plan, task, step


def empty_snapshot(message="Reading runs…"):
    return {
        "project": "Dashboard", "mode": "live", "plans": [], "jobs": [], "approvals": [],
        "pending_approvals": 0, "message": message, "health": "loading", "observed": None,
        "running": 0, "failed": 0, "awaiting": 0, "cost_today": None,
    }


def step_row(step, job, attempt):
    return {
        **step, "role": step.get("role") or "?", "engine": step.get("engine") or "?",
        "model": step.get("model") or "?", "duration_s": number(step.get("duration_s")),
        "status": STEP_STATES.get(step.get("status"), "unknown"),
        "started": job["started"], "finished": job["finished"],
        "attempt": attempt, "brief": UNAVAILABLE, "gates": [], "events": [], "files": {},
    }


def approval_row(approval, tasks):
    commands = approval["commands"]
    brief = "\n".join(str(approval[key]) for key in ("status", "summary", "note") if approval.get(key))
    return {
        **approval, "task_id": tasks.get(approval["run_id"], "?"),
        "brief": brief + ("\n\n" + "\n\n".join(commands.values()) if commands else ""),
    }


class RunSource:
    """Single-worker adapter. The UI receives fresh view rows, never reader state."""

    def __init__(self, reader, *, interval=2):
        self.reader = reader
        self.interval = interval
        self.identities = {}
        self.stream_key = None
        self.stream = None
        self.stream_generation = 0
        self.gate_key = None
        self.gates = []

    def identity(self, job):
        """Older snapshots omit the plan and task IDs. Read them through Files."""
        native = job["native_id"]
        plan, task = job.get("plan_id"), job.get("task_id")
        signature, _, health, _ = self.reader.runs.get(native, (None, None, None, None))
        # Only changes to run.json invalidate its identity.
        signature = signature[1] if signature else None
        previous = self.identities.get(native)
        if health == "current" and not ("plan_id" in job and "task_id" in job):
            if previous is None or previous[0] != signature:
                try:
                    raw = self.reader.files.json(self.reader.settings.runs / native / "run.json")
                except ARTIFACT_ERRORS:
                    pass
                else:
                    previous = (signature, self.reader.safe_id(raw.get("plan_id")),
                                self.reader.text(raw.get("task_id")))
                    self.identities[native] = previous
        if previous:
            plan = job.get("plan_id", previous[1])
            task = job.get("task_id", previous[2])
        task_id = self.reader.text(task) or self.reader.text(job["task"]) or job["id"]
        return self.reader.safe_id(plan), task_id

    def plan_title(self, plan_id):
        method = "get_plan" if hasattr(self.reader, "get_plan") else "plan"
        if hasattr(self.reader, method):
            try:
                plan = getattr(self.reader, method)(plan_id)
            except ARTIFACT_ERRORS:
                plan = None
            if isinstance(plan, dict) and (title := self.reader.text(plan.get("title"))):
                return title
        return plan_id

    def poll(self, keys):
        reader = self.reader
        raw = reader.snapshot(interactive=True, include_gates=False)
        jobs = raw["jobs"]
        groups = {}
        task_names = {}
        for job in jobs:
            if not job["native_id"]:
                continue
            plan_id, task_id = self.identity(job)
            task_names[job["native_id"]] = task_id
            groups.setdefault(plan_id, {}).setdefault(task_id, []).append(job)
        self.identities = {key: value for key, value in self.identities.items() if key in reader.runs}

        plans = []
        for plan_id, tasks in groups.items():
            key = f"plan:{plan_id}" if plan_id else "adhoc"
            rows = []
            for task_id, attempts in tasks.items():
                attempts.sort(key=lambda job: (timestamp(job["started"]) or 0, job["id"]))
                run = attempts[-1]
                if keys[:2] == (key, task_id):
                    run = find(attempts, keys[2], run)
                attempt = attempts.index(run) + 1
                rows.append({
                    **run, "id": task_id, "run_id": run["id"], "task_id": task_id,
                    "terminal": run["state"] in TERMINAL,
                    "title": reader.text(run["task"]) or task_id,
                    "state": RUN_STATES.get(run["state"], "unknown"),
                    "attempts": len(attempts), "attempt": attempt, "runs": [job["id"] for job in attempts],
                    "depends_on": [], "brief": UNAVAILABLE,
                    "steps": [step_row(step, run, attempt) for step in run["steps"]],
                })
            states = {row["state"] for row in rows}
            state = next((value for value in ("running", "awaiting_approval", "failed", "blocked")
                          if value in states), next(iter(states)) if len(states) == 1 else "unknown")
            plans.append({"id": key, "title": self.plan_title(plan_id) if plan_id else AD_HOC,
                          "state": state, "jobs": rows})

        health = raw["sources"]["runs"]
        message = {
            "missing": "Runs root missing. Check --runs-root.",
            "removed": "Runs root removed. Check --runs-root.",
            "inaccessible": "Runs root unavailable. Check --runs-root.",
        }.get(health, "No runs found.")
        approvals_health = raw["sources"]["approvals"]
        if raw["scan"]["unreadable"] or (health == "current" and approvals_health not in ("missing", "current")):
            health = "partial"
        if raw["scan"]["scanned"] < raw["scan"]["discovered"] and health == "current":
            health = "scanning"
            message = "Scanning runs…"

        now = datetime.fromtimestamp(raw["observed_epoch"])
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = midnight.timestamp(), (midnight + timedelta(days=1)).timestamp()
        dates = [(job, timestamp(job["started"])) for job in jobs if job["native_id"]]
        today = [job for job, date in dates if date is not None and start <= date < end]
        incomplete = any(
            job["cost_usd"] is None or (
                (coverage := job["cost_usd_coverage"])["source"] == "steps"
                and coverage["known"] < coverage["eligible"]
            )
            for job in today
        )
        cost = None
        if (not incomplete and raw["scan"]["complete"] and all(date is not None for _, date in dates)
                and raw["scan"]["returned"] == raw["scan"]["matched"]):
            total = sum(job["cost_usd"] for job in today)
            cost = total if math.isfinite(total) else None
        snapshot = {
            **empty_snapshot(message), "plans": plans, "jobs": jobs, "health": health,
            "approvals": [approval_row(approval, task_names) for approval in raw["approvals"]],
            "pending_approvals": raw["pending_approvals"],
            "running": sum(job["state"] == "running" for job in jobs),
            "failed": sum(job["state"] == "failed" for job in jobs),
            "awaiting": sum(job["state"] == "waiting for approval" for job in jobs),
            "cost_today": cost,
        }
        _, task, step = selection(snapshot, keys)
        if task:
            self.details(task, step, raw)
        else:
            self.stream_key, self.stream = None, None
        snapshot["observed"] = time.monotonic()
        return snapshot, max(self.interval, poll_delay(raw, {"event_kind": "all"}, self.interval))

    def details(self, task, selected, raw):
        reader = self.reader
        native = task["native_id"]
        _, run, _, _ = reader.runs.get(native, (None, None, None, None))
        if not run:
            return
        base = reader.settings.runs / native
        steps = run["steps"]
        first_prompt = next((step["files"]["prompt.md"] for step in steps
                             if step["index"] == 1 and "prompt.md" in step["files"]), None)
        try:
            task["brief"] = (reader.excerpt(base / "steps" / first_prompt) if first_prompt else None) or UNAVAILABLE
            self.gates, _ = reader.gates(native)
            self.gate_key = native
        except DeferredRead:
            pass
        task["gates"] = self.gates if self.gate_key == native else []
        if not selected:
            self.stream_key, self.stream = None, None
            return
        info = find(steps, selected["id"], {})
        files = info.get("files", {})
        selected["files"] = files.copy()
        selected["brief"] = task["brief"]
        selected["gates"] = [gate for gate in task["gates"] if gate["step"] == selected["index"]]
        key = (native, selected["id"])
        if key != self.stream_key:
            self.stream_key = key
            self.stream = Stream(reader.redactor, reader.clock)
            self.stream_generation += 1
        selected["stream_key"] = (*key, self.stream_generation)
        # The reader validated the native ID and step filename. Stream enforces bounded no-follow reads.
        if identifier(native):
            path = base / "steps" / files.get("jsonl", selected["id"] + ".jsonl")
            terminal = task["terminal"]
            health, _ = self.stream.poll(path, terminal=terminal, root=reader.files.root_for(path))
            selected["events"] = list(self.stream.events)
            raw["sources"]["stream"] = health
            raw["stream_drained"] = terminal and self.stream.stable >= 2
