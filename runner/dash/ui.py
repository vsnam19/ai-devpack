"""Terminal presentation and navigation for dashboard snapshots."""

from datetime import datetime
import json
from queue import Empty, Queue
from threading import Event, Thread
import textwrap
import time
import unicodedata

from .reader import FILTER_HELP, terms


def display(value):
    return "unknown" if value is None or value == "" else str(value)


def elapsed(value):
    if value is None:
        return "?"
    seconds = int(value)
    for unit, scale in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= scale:
            return f"{seconds // scale}{unit}"
    return f"{seconds}s"


def cost(value):
    return "?" if value is None else f"${value:.4g}"


def rows(record):
    for key, value in record.items():
        if value is not None and value != [] and value != {}:
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            yield f"{key.replace('_', ' ')}: {value}"


def job_columns(width):
    columns = [("Job", 23), ("State", 9), ("Role", 10), ("Agent", 16),
               ("#", 2), ("Age", 5), ("Cost", 6)]
    remaining = width - sum(size for _, size in columns) - (len(columns) - 1)
    for label, size in (("Health", 7), ("Kind", 8)):
        if remaining < size + 1:
            break
        columns.append((label, size))
        remaining -= size + 1
    columns[3] = ("Agent", min(34, 16 + max(0, remaining)))
    return tuple(columns)


def job_line(job, width):
    state = {"waiting for approval": "awaiting", "ready to resume": "ready"}.get(job["state"], job["state"])
    agent = job["agent"] or "?"
    model = job["model"] or "?"
    age = job["duration_s"] if job["duration_s"] is not None else job["age_s"]
    values = {
        "Job": job["id"], "Kind": job["kind"], "State": state, "Role": job["role"],
        "Agent": agent if agent == model else f"{agent}:{model}",
        "#": "?" if job["current_step"] is None else job["current_step"],
        "Age": elapsed(age), "Cost": cost(job["cost_usd"]), "Health": job["process_health"],
    }
    fields = []
    for label, size in job_columns(width):
        value = display(values[label])
        if label == "Job":
            value = value[-size:]
        field = fit(value, size)
        if label in ("State", "Health", "Role") and field != value:
            field = fit(value, size - 1) + "…"
        fields.append(field + " " * (size - sum(cell_width(char) for char in field)))
    return " ".join(fields)


def job_heading(width):
    return " ".join(f"{label:<{size}}" for label, size in job_columns(width))


def job_lines(job, view):
    if view == "Gates":
        yield f"Overall gate: {display(job['gate'])}"
        for gate in job["gates"]:
            yield ""
            yield from rows(gate)
    elif view == "Diagnostics":
        yield from rows(job["diagnostics"])
        for step in job["steps"]:
            for key in ("error", "stderr"):
                if step.get(key):
                    yield f"{step['id']} {key}: {step[key]}"
        for gate in job["gates"]:
            for key in ("stderr", "error", "transcript", "artifact_notice"):
                if gate.get(key):
                    yield f"Gate {gate['step']} {key}: {gate[key]}"
    else:
        yield f"{job['id']}  {display(job['kind'])}  {display(job['state'])}"
        yield from rows({key: job[key] for key in (
            "aliases", "native_outcome", "task", "model", "model_source", "broker_disposition",
            "broker_response", "started", "finished", "approval_ids",
        )})
        yield f"Process health {display(job['process_health'])}"
        yield f"Wall duration {elapsed(job['duration_s'])}  Invocation time {elapsed(job['invocation_duration_s'])}"
        yield f"Cost {cost(job['cost_usd'])}  Tokens in/out {display(job['tokens_in'])}/{display(job['tokens_out'])}"
        yield f"Last event {event_time(job['last_event_time'])}  Age {elapsed(job['last_event_age_s'])}"
        yield f"Gate {display(job['gate'])}"
        if job["activity"]:
            yield f"Activity {job['activity']}"
        for step in job["steps"]:
            yield f"Step {step['index']:02}  {display(step['role'])}  {display(step['engine'])}:{display(step['model'])}  {display(step['status'])}"
    yield from job["notices"]


def approval_lines(record):
    yield f"{record['id']}  {display(record.get('checkpoint'))}  {record['status']}"
    yield f"Run {display(record.get('run_id'))}  Link {record['link']}"
    for key in ("subject", "summary", "note", "gate_detail", "gate_receipts", "hard_stop", "blocking"):
        yield from rows({key: record.get(key)})
    evidence = record.get("evidence") or {}
    if record.get("checkpoint") == "brief":
        yield f"Brief text {evidence.get('brief') or 'unavailable'}"
    yield from rows({key: evidence.get(key) for key in ("brief_lint", "brief_lint_error_count", "escalation_reason")})
    for excerpt in evidence.get("excerpts", []):
        yield f"{excerpt['name']}: {excerpt['text']}"
    if record["status"] == "pending":
        if record["commands"]:
            yield from record["commands"].values()
        else:
            yield "Operator commands unavailable"


def event_time(value):
    return datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if value is not None else "unknown"


def event_line(event):
    label = event["kind"]
    if label == "tool":
        label += f" {display(event.get('phase'))} {event.get('tool') or event.get('call_id') or ''}"
        if event.get("is_error"):
            label += " (error)"
    return f"{event.get('timestamp') or event_time(event['observed_at'])} [{label}] {event['text'] or ''}"


def plain(snapshot, command):
    if command == "approvals":
        for approval in snapshot["approvals"]:
            yield from approval_lines(approval)
        if not snapshot["approvals"]:
            yield "No approvals"
    elif command == "tail":
        for job in snapshot["jobs"]:
            yield from job["notices"]
        yield from (event_line(event) for event in snapshot["events"])
    elif command in ("show", "gate"):
        for job in snapshot["jobs"]:
            yield from job_lines(job, "Gates" if command == "gate" else "Job Detail")
            if command == "show":
                yield from job_lines(job, "Diagnostics")
    else:
        yield job_heading(120)
        yield from (job_line(job, 120) for job in snapshot["jobs"])
        scan = snapshot["scan"]
        yield f"{scan['returned']} of {scan['matched']} matched, {scan['unreadable']} unreadable"
    yield "Sources " + " | ".join(f"{name} {status}" for name, status in snapshot["sources"].items() if status)


def cell_width(char):
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def fit(line, width):
    result = []
    cells = 0
    for char in line.replace("\n", " ").expandtabs(4):
        size = cell_width(char)
        if cells + size > width:
            break
        result.append(char)
        cells += size
    return "".join(result)


class UI:
    def __init__(self):
        self.stack = ["Jobs"]
        self.query = ""
        self.edit = None
        self.message = ""
        self.job_key = None
        self.job_aliases = set()
        self.job_id = None
        self.approval_id = None
        self.kind = "all"
        self.follow = True
        self.offset = 0
        self.page = 18
        self.job_rows = []
        self.approval_rows = []
        self.visible = []
        self.previous = {}
        self.size = None
        self.event_anchor = None
        self.event_lines = []

    @property
    def view(self):
        return self.stack[-1]

    def request(self):
        job_view = self.view in ("Job Detail", "Events", "Gates", "Diagnostics")
        return {
            "query": self.query, "job_id": self.job_id if job_view else None,
            "event_kind": self.kind if self.view == "Events" else None,
            "approval_id": self.approval_id if self.view == "Evidence" else None,
            "include_evidence": self.view == "Evidence", "include_gates": self.view in ("Job Detail", "Gates", "Diagnostics"),
        }

    def push(self, view):
        self.stack.append(view)
        self.offset = 0
        self.event_anchor = None

    def select_job(self, job):
        self.job_key = job["key"]
        self.job_id = job["id"]
        self.job_aliases = {job["id"], *job["aliases"]}

    def reconcile(self, jobs):
        exact = next((job for job in jobs if job["key"] == self.job_key), None)
        aliases = [job for job in jobs if self.job_aliases.intersection({job["id"], *job["aliases"]})]
        if exact or len(aliases) == 1:
            self.select_job(exact or aliases[0])
        elif jobs and self.view == "Jobs":
            self.select_job(jobs[0])

    def key(self, key):
        if self.edit is not None:
            if key == "ESC":
                self.edit = None
                self.message = ""
            elif key == "ENTER":
                try:
                    terms(self.edit)
                except ValueError:
                    self.message = "Invalid filter"
                    return None
                self.query, self.edit, self.offset = self.edit, None, 0
                self.message = ""
            elif key == "BACKSPACE":
                self.edit = self.edit[:-1]
            elif len(key) == 1 and key.isprintable() and len(self.edit) < 512:
                self.edit += key
            return None
        if key == "q":
            return "quit"
        if key == "r":
            return "refresh"
        if key == "ESC" and len(self.stack) > 1:
            self.stack.pop()
            self.offset = 0
        elif key == "?":
            self.push("Help")
        elif self.view == "Jobs" and key == "/":
            self.edit = self.query
        elif self.view == "Jobs" and key == "C":
            self.query = ""
            self.offset = 0
        elif key == "a" and self.view in ("Jobs", "Job Detail"):
            self.push("Approvals")
        elif self.view == "Job Detail" and key in ("l", "g", "e"):
            self.push({"l": "Events", "g": "Gates", "e": "Diagnostics"}[key])
        elif key == "ENTER":
            if self.view == "Jobs" and self.job_rows:
                self.push("Job Detail")
            elif self.view == "Approvals" and self.approval_rows:
                self.push("Evidence")
        elif self.view == "Events" and key == "c":
            self.kind = {"all": "tool", "tool": "message", "message": "all"}[self.kind]
            self.event_anchor = None
        elif self.view == "Events" and key == "f":
            self.follow = not self.follow
            self.event_anchor = None
        elif key in ("UP", "DOWN", "j", "k", "PGUP", "PGDN"):
            delta = {"UP": -1, "k": -1, "DOWN": 1, "j": 1, "PGUP": -self.page, "PGDN": self.page}[key]
            if self.view == "Events":
                self.follow = False
            if self.view == "Jobs" and self.job_rows:
                index = next((i for i, job in enumerate(self.job_rows) if job["key"] == self.job_key), 0)
                self.select_job(self.job_rows[max(0, min(index + delta, len(self.job_rows) - 1))])
            elif self.view == "Approvals" and self.approval_rows:
                ids = [approval["id"] for approval in self.approval_rows]
                self.approval_id = ids[max(0, min(ids.index(self.approval_id) + delta, len(ids) - 1))]
            else:
                self.offset = max(0, min(self.offset + delta, max(0, len(self.visible) - self.page)))
                if self.view == "Events" and self.event_lines:
                    self.event_anchor = self.event_lines[self.offset]

    def content(self, snapshot, width):
        jobs = snapshot["jobs"]
        approvals = snapshot["approvals"]
        if self.view == "Jobs":
            self.job_rows = jobs
            self.reconcile(jobs)
            heading = "  " + job_heading(width - 2)
            lines = [("› " if job["key"] == self.job_key else "  ") + job_line(job, width - 2) for job in jobs]
            selected = next((i for i, job in enumerate(jobs) if job["key"] == self.job_key), None)
            return heading, lines or ["No matching jobs"], selected
        if self.view in ("Approvals", "Evidence"):
            self.approval_rows = approvals
            ids = [approval["id"] for approval in approvals]
            if self.approval_id not in ids:
                self.approval_id = ids[0] if ids else None
            if self.view == "Approvals":
                lines = [
                    ("› " if item["id"] == self.approval_id else "  ") +
                    f"{item['id']}  {item['status']}  {display(item.get('checkpoint'))}"
                    for item in approvals
                ]
                return "  Approval ID / Status / Checkpoint", lines or ["No approvals"], ids.index(self.approval_id) if ids else None
            for item in approvals:
                if item["id"] == self.approval_id:
                    return None, list(approval_lines(item)), None
            return None, ["Approval unavailable"], None
        if self.view == "Help":
            return None, [
                "↑ ↓ / j k move   PgUp/PgDn page   Enter open   Esc back",
                "r refresh   q quit   ? help   / edit filter   C clear",
                "Filter editor: Enter apply   Esc cancel",
                "a Approvals from Jobs or Detail   l Events   g Gates   e Diagnostics",
                "Events: c cycle all/tool/message   f toggle follow",
                "Copy full operator commands with approvals --plain.",
                FILTER_HELP,
            ], None
        if jobs:
            self.reconcile(jobs)
        if self.view == "Events":
            heading = f"{self.kind} | {'following' if self.follow else 'paused'} | {display(snapshot['sources'].get('stream'))}"
            events = snapshot["events"]
            lines = [event_line(event) for event in events]
            self.event_lines = [event["sequence"] for event in events]
            if self.event_anchor in self.event_lines and not self.follow:
                self.offset = self.event_lines.index(self.event_anchor)
            return heading, lines or (jobs[0]["notices"] if jobs else []) or ["No events"], None
        return None, list(job_lines(jobs[0], self.view)) if jobs else ["Job unavailable"], None

    def paint(self, screen, snapshot, now=None):
        height, width = screen.getmaxyx()
        if (height, width) != self.size:
            screen.erase()
            self.previous = {}
            self.size = height, width
        heading, lines, selected = self.content(snapshot, width - 1)
        first = 2 if heading else 1
        self.page = max(1, height - first - 1)
        if self.view not in ("Jobs", "Approvals", "Events"):
            lines = [
                line if value.startswith("python3 ") else wrapped
                for value in lines for line in value.split("\n")
                for wrapped in ([""] if value.startswith("python3 ") else textwrap.wrap(line, max(1, width - 1)) or [""])
            ]
        self.visible = lines
        if self.view == "Events" and self.follow:
            self.offset = max(0, len(lines) - self.page)
        elif selected is not None:
            self.offset = min(self.offset, selected)
            self.offset = max(self.offset, selected - self.page + 1)
        self.offset = min(self.offset, max(0, len(lines) - self.page))
        now = time.time() if now is None else now
        age = elapsed(max(0, now - snapshot.get("observed_epoch", now)))
        sources = snapshot["sources"]
        health = sources.get("runs", "unknown")
        issues = [name for name, status in sources.items() if status in ("stale", "partial", "unreadable", "inaccessible", "removed")]
        if issues:
            health += "/" + ",".join(issues)
        pending = snapshot["pending_approvals"]
        header = f"{self.view} | Pack {health} | Refresh {age} | {pending} pending"
        if snapshot.get("scan", {}).get("complete") is False:
            scan = snapshot["scan"]
            header += f" | Scan {scan['scanned']}/{scan['discovered']}"
        footer = {
            "Jobs": "/ Filter  C Clear  Enter Detail  a Approvals  ? Help  q Quit",
            "Job Detail": "l Events  g Gates  e Diagnostics  a Approvals  Esc Back  ? Help",
            "Approvals": "Enter Evidence  Esc Back  ? Help",
            "Events": "c Kind  f Follow  ↑↓ Scroll  Esc Back  ? Help",
            "Evidence": "Full commands available in approvals --plain  Esc Back",
        }.get(self.view, "Esc Back  r Refresh  q Quit")
        if self.edit is not None:
            footer = f"/ {self.edit}  {self.message}"
        elif self.query and self.view == "Jobs":
            header += f" | {self.query}"
        paint = [header, *([heading] if heading else []), *lines[self.offset:self.offset + self.page]]
        for y in range(height):
            value = footer if y == height - 1 else paint[y] if y < len(paint) else ""
            value = fit(value, max(0, width - 1))
            if self.previous.get(y) != value and width > 1:
                screen.addstr(y, 0, value + " " * max(0, width - 1 - cell_width(value)))
                self.previous[y] = value
        screen.refresh()


def cell_width(text):
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def poll_delay(snapshot, request, interval):
    if not isinstance(snapshot, dict) or snapshot["active_jobs"]:
        return interval
    scan = snapshot["scan"]
    if scan["scanned"] < scan["discovered"] and snapshot["sources"]["runs"] == "current":
        return interval
    stream_pending = (
        request.get("event_kind") and not snapshot["stream_drained"]
        and snapshot["sources"]["stream"] not in (None, "missing")
    )
    return interval if stream_pending else 30


def interface(screen, curses, reader):
    ui = UI()
    screen.keypad(True)
    screen.timeout(100)
    updates = Queue(maxsize=1)
    wake = Event()
    stop = Event()
    request = ui.request(), 0

    def poll():
        generation = -1
        while not stop.is_set():
            args, current = request
            try:
                snapshot = reader.snapshot(interactive=True, force=current != generation, **args)
            except Exception as exc:
                update = (current, exc)
            else:
                update = (current, snapshot)
            try:
                updates.get_nowait()
            except Empty:
                pass
            updates.put_nowait(update)
            generation = current
            wake.wait(poll_delay(update[1], args, reader.settings.stream_poll_s))
            wake.clear()

    worker = Thread(target=poll, daemon=True)
    worker.start()
    snapshot = {"jobs": [], "approvals": [], "sources": {}, "pending_approvals": 0, "events": []}
    names = {
        curses.KEY_UP: "UP", curses.KEY_DOWN: "DOWN", curses.KEY_PPAGE: "PGUP", curses.KEY_NPAGE: "PGDN",
        curses.KEY_ENTER: "ENTER", curses.KEY_BACKSPACE: "BACKSPACE",
        "\n": "ENTER", "\r": "ENTER", "\x1b": "ESC", "\x7f": "BACKSPACE", "\b": "BACKSPACE",
    }
    try:
        while True:
            try:
                generation, update = updates.get_nowait()
            except Empty:
                pass
            else:
                if generation == request[1]:
                    if isinstance(update, Exception):
                        raise update
                    snapshot = update
            try:
                ui.paint(screen, snapshot)
            except curses.error:
                # A resize between getmaxyx and addstr invalidates this frame.
                ui.size = None
            try:
                raw = screen.get_wch()
            except curses.error:
                continue
            action = ui.key(names.get(raw, raw if isinstance(raw, str) else ""))
            if action == "quit":
                return 0
            args = ui.request()
            if action == "refresh" or args != request[0]:
                request = args, request[1] + 1
                wake.set()
    finally:
        stop.set()
        wake.set()
