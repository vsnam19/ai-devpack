"""In-memory plans, runs and normalized stream events for the demo."""

STAMP = "2026-11-27T08:14:00Z"
ROLES = {
    "implementer": ("engine-a", "model-large"),
    "gate": ("local", "small"),
    "reviewer": ("engine-b", "model-mid"),
    "fixer": ("engine-a", "model-large"),
}
STEP_GOALS = {
    "implementer": "Add the regression test and implement the scoped change.",
    "gate": "Run the task checks and record the exit status and output.",
    "reviewer": "Check the brief, diff, and test evidence for regressions.",
    "fixer": "Restore the saved offset before replaying the first batch.",
}
LIVE_MESSAGES = (
    "Replay worker verified that acknowledged records are skipped.",
    "Checking cursor persistence across a simulated restart.",
    "Comparing the resumed batch against the expected offsets.",
    "The cursor holds after an empty batch. Checking the retry case.",
)


def event(sequence, text, *, exit_code=None):
    record = {
        "sequence": sequence,
        "kind": "message" if exit_code is None else "tool",
        "text": text,
        "timestamp": STAMP,
        "observed_at": 1795767240 + sequence,
    }
    if exit_code is not None:
        record.update(phase="result", call_id=f"cmd-{sequence}", tool="shell",
                      is_error=exit_code != 0, exit_code=exit_code)
    return record


def make_step(index, role, status, attempt, module, run_path):
    engine, model = ROLES[role]
    step_id = f"{index:02d}-{role}-{engine}"
    path = f"{run_path}/steps/{step_id}"
    waiting = status == "queued"
    command = f"python -m unittest tests.test_{module}"
    output = ("FAIL  test_replay_starts_at_saved_offset\n"
              "      expected offset 48, got 0\n\n"
              "Ran 12 tests · 1 failure") if status == "failed" else "Ran 12 tests · OK"
    events = []
    if not waiting:
        events = [
            event(1, f"Loaded the {role} brief for {module.replace('_', ' ')}."),
            event(2, "rg --files tests", exit_code=0),
            event(3, command, exit_code=int(status == "failed")),
            event(4, output),
        ]
        if status == "running":
            events[-1] = event(4, "Unit tests passed. Replay worker is checking the restart case.")
    measured = role != "gate" and status in ("done", "failed")
    return {
        "id": step_id, "index": index, "role": role, "engine": engine, "model": model,
        "status": status, "attempt": attempt,
        "duration_s": None if waiting else 9 + index * 7,
        "started": None if waiting else STAMP,
        "finished": STAMP if status in ("done", "failed") else None,
        "tokens_in": 1800 + index * 225 if measured else None,
        "tokens_out": 240 + index * 56 if measured else None,
        "cost_usd": round(0.009 + index * 0.003, 3) if measured else None,
        "files": {suffix: f"{path}.{suffix}" for suffix in ("prompt.md", "jsonl", "meta.json")},
        "events": events,
        "brief": (f"## {role.title()} · Attempt {attempt}\n\n"
                  f"{STEP_GOALS[role]}\n\n"
                  f"- **Task area** · {module.replace('_', ' ')}\n"
                  f"- **Check** · `{command}`\n"
                  "- **Stop** · Report any change needed outside the task scope."),
        "gates": [{
            "name": "Unit tests", "command": command, "attempt": attempt,
            "status": "fail" if status == "failed" else "pass",
            "returncode": int(status == "failed"), "output": output,
        }] if role == "gate" and not waiting else [],
    }


def make_task(plan_id, number, title, state, depends_on, module, goal):
    task_id = f"T{number:02d}"
    run_id = f"{plan_id.lower()}-{task_id.lower()}"
    path = f".runs/{run_id}"
    roles = [("implementer", "done", 1), ("gate", "done", 1), ("reviewer", "done", 1)]
    current = 3
    if state in ("blocked", "queued"):
        roles = [(role, "queued", attempt) for role, _, attempt in roles[:2]]
        current = None
    elif state == "running":
        roles = [("implementer", "running", 1), ("gate", "queued", 1)]
        current = 1
    elif state == "retrying":
        roles = [
            ("implementer", "done", 1), ("gate", "failed", 1),
            ("fixer", "done", 2), ("gate", "done", 2), ("reviewer", "done", 2),
        ]
        current = 5

    steps = [make_step(index, *role, module, path) for index, role in enumerate(roles, 1)]
    latest = steps[(current or 1) - 1]
    started = current is not None
    age = 94 if state == "running" else 63 + number * 16 if started else None
    if state == "running":
        latest["duration_s"] = age
    cost = sum(step["cost_usd"] or 0 for step in steps)
    brief = f"""## {title}

{goal}

### Scope
- Files allowed: `src/{module}.py`, `tests/test_{module}.py`
- Size limit: 120 changed lines
- Depends on: {", ".join(depends_on) or "None"}

### RED → GREEN
- RED: `python -m unittest tests.test_{module}` must expose the missing behavior.
- GREEN: rerun the same command after the smallest change.

### Stop conditions
Stop after two fixes for the same failure or when the change needs a file outside scope.
"""
    return {
        "id": run_id, "native_id": run_id, "kind": "task", "task_id": task_id,
        "title": title, "state": state, "depends_on": depends_on, "size_limit": 120,
        "attempts": max(step["attempt"] for step in steps) if started else 0,
        "role": latest["role"], "engine": latest["engine"], "model": latest["model"],
        "current_step": current, "age_s": age, "duration_s": age,
        "started": STAMP if started else None, "finished": STAMP if state == "done" else None,
        "cost_usd": round(cost, 3) if state in ("done", "retrying", "awaiting_approval") else None,
        "tokens_in": sum(step["tokens_in"] or 0 for step in steps) if cost else None,
        "tokens_out": sum(step["tokens_out"] or 0 for step in steps) if cost else None,
        "health": "current", "source": path, "steps": steps,
        "gates": [gate for step in steps for gate in step["gates"]],
        "approval_ids": ["A-014"] if state == "awaiting_approval" else [],
        "brief": brief,
    }


def demo_snapshot():
    rows = [
        ("Define record format", "done", (), "record", "Document the stable fields for replay records."),
        ("Persist checkpoints", "done", ("T01",), "checkpoint", "Save the last acknowledged offset atomically."),
        ("Resume cursor", "running", ("T02",), "cursor", "Resume a replay from the last acknowledged offset after restart."),
        ("Replay batch", "retrying", ("T02",), "replay", "Preserve record order when retrying a partially acknowledged batch."),
        ("Approve retention", "awaiting_approval", ("T01",), "retention", "Confirm that archived records can be pruned after 30 days."),
        ("Wire recovery flow", "blocked", ("T03", "T04"), "recovery", "Connect cursor recovery to the startup path."),
        ("Document rollback", "queued", ("T01",), "rollback", "Describe how to restore the previous checkpoint format."),
    ]
    archive = [
        ("Add health probe", "done", (), "health", "Report the readiness of the record store."),
        ("Check probe output", "done", ("T01",), "probe", "Verify that callers can distinguish ready and degraded states."),
    ]
    plans = [
        {"id": plan_id, "title": title, "state": state, "jobs": [
            make_task(plan_id, number, *row) for number, row in enumerate(tasks, 1)
        ]}
        for plan_id, title, state, tasks in [
            ("P-021", "Reliable replay", "running", rows),
            ("P-018", "Health checks", "done", archive),
        ]
    ]
    return {
        "project": "Sample workspace", "mode": "hitl", "observed_at": STAMP,
        "plans": plans, "jobs": [job for plan in plans for job in plan["jobs"]],
        "pending_approvals": 1,
        "approvals": [{
            "id": "A-014", "plan_id": "P-021", "task_id": "T05", "status": "pending",
            "checkpoint": "Retention policy", "requested_at": STAMP,
            "brief": ("## Approve 30-day retention\n\n"
                      "The reviewer cleared the cleanup change. Human approval is required "
                      "before expired replay records can be removed.\n\n"
                      "- **Scope** · Replay archive only\n"
                      "- **Evidence** · Unit tests passed on attempt 1\n"
                      "- **Decision owner** · Maintainer"),
        }],
    }
