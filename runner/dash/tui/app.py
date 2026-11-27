"""Keyboard-first dashboard for pack runs and demo fixtures."""

from collections import defaultdict
import time

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Input, Markdown, OptionList, RichLog, Static, Tabs, Tab
from textual.widgets.option_list import Option
from rich.cells import cell_len
from rich.text import Text

from .fixtures import LIVE_MESSAGES, demo_snapshot, event
from .source import current_step, empty_snapshot, find, selection

PANELS = ("plans", "tasks", "agents", "approvals")
TABS = {"brief": "Brief", "log": "Log", "gate": "Gate", "graph": "Plan graph", "meta": "Meta"}
STATES = {
    "running": ("●", "primary", "Running"),
    "done": ("✓", "success", "Done"),
    "failed": ("✗", "error", "Failed"),
    "awaiting_approval": ("⏸", "warning", "Awaiting approval"),
    "blocked": ("⏸", "foreground", "Blocked"),
    "unknown": ("?", "foreground", "Unknown"),
    "queued": ("○", "foreground", "Queued"),
    "retrying": ("↻", "warning", "Retried"),
}
KEYMAP = (
    ("1–4", "Focus Plans, Tasks, Agents or Approvals"),
    ("Tab / Shift+Tab", "Move forward or backward through panels"),
    ("h / l", "Move to the previous or next panel"),
    ("j / k or ↑ / ↓", "Move through the focused list or detail"),
    ("Enter", "Drill into the highlighted row"),
    ("[ / ]", "Switch detail tabs"),
    ("f", "Toggle log follow"),
    ("/", "Filter the current list"),
    ("Esc", "Close help or clear the current filter"),
    ("a", "Open Approvals"),
    ("Ctrl+T", "Switch dark or light theme"),
    ("?", "Open this key map"),
    ("q", "Quit"),
)


def elapsed(seconds):
    if seconds is None:
        return "–"
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}m{seconds:02d}" if minutes else f"{seconds}s"


def amount(value):
    return "?" if value is None else f"${value:.3f}"


def started(plan):
    return sum(job["attempts"] > 0 for job in plan["jobs"])


def row_field(value, width):
    text = Text(" ".join(value.split()))
    text.truncate(max(0, width))
    return text.plain


class Help(ModalScreen):
    BINDINGS = [("escape,question_mark,q", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        keys = Text()
        for key, description in KEYMAP:
            keys.append(f"{key:<20}", style=f"bold {self.app.current_theme.primary}")
            keys.append(f"{description}\n")
        card = Static(keys, id="keymap")
        card.border_title = "Keyboard shortcuts · Esc to close"
        yield card


class Panel(OptionList):
    def on_click(self):
        self.focus()


class Polled(Message):
    def __init__(self, keys, snapshot, delay):
        super().__init__()
        self.keys = keys
        self.snapshot = snapshot
        self.delay = delay


class Dashboard(App):
    TITLE = "Work dashboard"
    CSS_PATH = "dashboard.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        *(Binding(str(index), f"panel('{panel}')", panel.title())
          for index, panel in enumerate(PANELS, 1)),
        Binding("tab,l", "move_panel(1)", "Next panel"),
        Binding("shift+tab,h", "move_panel(-1)", "Previous panel"),
        Binding("j", "move_row(1)", "Down"),
        Binding("k", "move_row(-1)", "Up"),
        Binding("left_square_bracket", "change_tab(-1)", "Previous tab"),
        Binding("right_square_bracket", "change_tab(1)", "Next tab"),
        Binding("left", "attempt(-1)", show=False),
        Binding("right", "attempt(1)", show=False),
        Binding("f", "follow", "Follow"),
        Binding("slash", "filter", "Filter"),
        Binding("escape", "clear_filter", "Clear filter"),
        Binding("a", "approvals", "Approvals"),
        Binding("question_mark", "help", "Help"),
        Binding("ctrl+t", "toggle_theme", "Theme"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, *, live=True, source=None):
        super().__init__()
        self.source = source
        self.snapshot = empty_snapshot() if source else demo_snapshot()
        self.plan = self.snapshot["plans"][0] if not source else None
        self.selected_task = self.plan["jobs"][0] if self.plan else None
        self.step = self.selected_task["steps"][0] if self.selected_task else None
        self.approval = self.snapshot["approvals"][0] if not source else None
        self.attempt_id = None
        self.pending_poll = False
        self.next_poll = 0
        self.next_age = 0
        self.log_sequence = 0
        self.panel = "plans"
        self.detail_source = "plans"
        self.filters = dict.fromkeys(PANELS, "")
        self.visible_rows = {}
        self.follow = True
        self.live = live
        self.ticks = 0
        self.log_step = None
        self.register_theme(Theme(
            "pack-dark", primary="#76bdff", secondary="#95a8c8", accent="#74dbc5",
            foreground="#d4deee", background="#101623", surface="#151e2e",
            panel="#20304a", success="#78d9a1", warning="#f4c876", error="#ff8490",
            dark=True,
        ))
        self.register_theme(Theme(
            "pack-light", primary="#145995", secondary="#435779", accent="#087363",
            foreground="#1d3047", background="#f0f4fa", surface="#fafcff",
            panel="#d6e2f2", success="#177649", warning="#805100", error="#a52237",
            dark=False,
        ))
        self.theme = "pack-dark"

    def compose(self) -> ComposeResult:
        yield Static(id="top")
        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
                for index, panel in enumerate(PANELS, 1):
                    with Vertical(id=f"{panel}-panel", classes="panel"):
                        with Horizontal(classes="panel-heading"):
                            yield Static(f"[{index}]", classes="panel-number")
                            yield Static(id=f"{panel}-title", classes="panel-title", markup=False)
                        yield Panel(id=panel)
            with Vertical(id="main"):
                yield Static(id="context", markup=False)
                yield Tabs(*(Tab(label, id=key) for key, label in TABS.items()), id="tabs")
                yield Static(id="detail-status", markup=False)
                with VerticalScroll(id="reading"):
                    yield Markdown(id="brief")
                    yield Static(id="detail")
                yield RichLog(id="log", wrap=True, min_width=1, auto_scroll=False, max_lines=1000)
        yield Input(placeholder="Filter rows", id="filter")
        yield Static(id="keys")

    def on_mount(self):
        self.resize_layout(self.size)
        self.query_one("#filter").display = False
        for panel in PANELS:
            self.populate(panel)
        self.action_panel("plans")
        self.update_top()
        self.show_detail()
        if self.source:
            self.poll_if_due()
            self.set_interval(min(0.2, self.source.interval), self.poll_if_due)
        elif self.live:
            self.set_interval(1, self.tick)

    def selection_keys(self):
        return (self.plan["id"] if self.plan else None,
                self.selected_task["id"] if self.selected_task else None,
                self.attempt_id, self.step["id"] if self.step else None)

    def poll_if_due(self):
        if not self.is_running:
            return
        now = time.monotonic()
        if now >= self.next_age:
            self.next_age = now + 1
            self.update_top()
        if not self.pending_poll and now >= self.next_poll:
            self.pending_poll = True
            keys = self.selection_keys()

            def read():
                try:
                    snapshot, delay = self.source.poll(keys)
                except (OSError, ValueError, RecursionError):
                    snapshot, delay = None, self.source.interval
                except Exception as error:
                    self.log.error("Dashboard poll failed", error)
                    snapshot, delay = None, self.source.interval
                self.post_message(Polled(keys, snapshot, delay))

            self.run_worker(read, thread=True)

    @on(Polled)
    def apply_poll(self, message):
        self.pending_poll = False
        if not self.is_running:
            return
        if message.keys != self.selection_keys():
            self.next_poll = 0
            return
        self.next_poll = time.monotonic() + message.delay
        if message.snapshot is None:
            self.snapshot = {**self.snapshot, "health": "unavailable"}
            self.update_top()
            return
        approval_id = self.approval["id"] if self.approval else None
        self.plan, self.selected_task, self.step = selection(message.snapshot, message.keys)
        self.attempt_id = self.selected_task["run_id"] if self.selected_task else None
        self.snapshot = message.snapshot
        approvals = self.snapshot["approvals"]
        self.approval = find(approvals, approval_id, approvals[0] if approvals else None)
        for panel in PANELS:
            self.populate(panel, choose_first=False)
        self.update_top()
        self.show_detail(preserve_scroll=True)

    def on_resize(self, event):
        screen = self.default_screen
        previous = screen.has_class("compact")
        self.resize_layout(event.size)
        if self.visible_rows and previous != screen.has_class("compact"):
            for panel in PANELS:
                self.populate(panel, choose_first=False)
            self.update_top()
            self.log_step = None
            self.show_detail()

    def resize_layout(self, size):
        self.default_screen.set_class(size.width < 100 or size.height < 31, "compact")

    def on_click(self, message: events.Click):
        widget = message.widget
        for ancestor in (widget, *widget.ancestors) if widget is not None else ():
            if ancestor.has_class("panel-heading"):
                self.action_panel(ancestor.parent.id.removesuffix("-panel"))
                break

    def color(self, name):
        return getattr(self.current_theme, name)

    def state_text(self, state, *, label=False):
        icon, color, name = STATES[state]
        style = f"bold {self.color(color)}" if state != "queued" else "dim"
        return Text(f"{icon}  {name}" if label else icon, style=style)

    def compact(self):
        return self.default_screen.has_class("compact")

    def rows(self, panel):
        return {
            "plans": self.snapshot["plans"],
            "tasks": self.plan["jobs"] if self.plan else [],
            "agents": self.selected_task["steps"] if self.selected_task else [],
            "approvals": self.snapshot["approvals"],
        }[panel]

    def selection(self, panel):
        return {"plans": self.plan, "tasks": self.selected_task, "agents": self.step,
                "approvals": self.approval}[panel]

    def row_text(self, panel, row):
        small = self.compact()
        # Sidebar width minus panel borders, padding, and scrollbar.
        width = 23 if small else 46
        if panel == "plans":
            count, total = (row["done"], row["total"]) if "done" in row else (started(row), len(row["jobs"]))
            filled = round(7 * count / total) if total else 0
            text = self.state_text(row["state"])
            progress = f" {count}/{total}"
            label = "" if small else f"  {STATES[row['state']][2]}"
            available = width - text.cell_len - cell_len(progress + label) - (2 if small else 9)
            title = row_field(row["title"], min(15, available) if small else available)
            text.append(f" {title}" + " " * (1 + max(0, min(15, available) - cell_len(title))), style="bold")
            if not small:
                text.append("━" * filled, style=self.color("accent"))
                text.append("─" * (7 - filled), style="dim")
            text.append(progress)
            text.append(label, style="dim")
            return text
        if panel == "tasks":
            task_id = " ".join(row["task_id"].split())
            title = " ".join(row["title"].split())
            if title == task_id:
                title = ""
            text = self.state_text(row["state"])
            attempt_text = ""
            if self.source and row["attempts"]:
                attempt = row["attempt"]
                total = row["attempts"]
                attempt_text = f"  ↻{attempt}/{total}" if total > 1 else "  ↻1"
            if small:
                task_id = task_id[-9:]
                while cell_len(task_id) > 9:
                    task_id = task_id[1:]
            else:
                task_id = row_field(task_id, 20)
            blockers = row.get("blocked_by")
            summary = ""
            if blockers:
                label = "‹ " if small else "Blocked by "
                space = width - text.cell_len - cell_len(task_id + attempt_text) - 5
                summary = row_field(label + ", ".join(blockers), space)
                summary = "  " + summary if summary else ""
            elif self.source and not row["attempts"]:
                summary = "  Queued"
            elif not small and not self.source:
                summary = f"{row['attempts']}× {elapsed(row['age_s']):>5}"
            available = width - text.cell_len - cell_len(attempt_text + summary) - 3
            task_id = row_field(task_id, available)
            title_width = available - cell_len(task_id)
            title = row_field(title, min(14, title_width) if small else title_width)
            text.append(f" {task_id}", style="bold")
            if title:
                text.append(f" {title}")
            text.append(attempt_text, style="dim")
            if self.source:
                text.append(summary, style=self.color("secondary") if blockers else "dim")
            elif summary:
                padding = min(max(1, 21 - cell_len(title)), width - text.cell_len - cell_len(summary))
                text.append(f"{' ' * padding}{summary}", style="dim")
                if row["depends_on"] and width - text.cell_len > 4:
                    dependency = row_field(",".join(row["depends_on"]), width - text.cell_len - 4)
                    text.append(f"  ‹ {dependency}", style=self.color("secondary"))
            return text
        if panel == "agents":
            text = self.state_text(row["status"])
            role = row_field(row["role"], 11)
            text.append(f" {row['index']:02} {role}" + " " * (11 - cell_len(role)), style="bold")
            model = f"{row['engine']}:{row['model']}"
            if small:
                text.append(f"\n   {row_field(model, width - 3)}")
                if self.source:
                    duration = elapsed(row["duration_s"]) if row["duration_s"] is not None else "?"
                    tokens_in, tokens_out = (row[key] if row[key] is not None else "?"
                                             for key in ("tokens_in", "tokens_out"))
                    text.append(f"\n   {duration}  {tokens_in}/{tokens_out}  {amount(row['cost_usd'])}", style="dim")
                return text
            duration = "?" if self.source and row["duration_s"] is None else elapsed(row["duration_s"])
            model = row_field(model, width - text.cell_len - max(5, cell_len(duration)) - 1)
            text.append(model, style=self.color("secondary"))
            text.append(f" {duration:>5}", style="dim")
            tokens = row["tokens_in"]
            text.append(f"\n   Attempt {row['attempt']}   {tokens if tokens is not None else '?'} in"
                        f" / {row['tokens_out'] if row['tokens_out'] is not None else '?'} out"
                        f"   {amount(row['cost_usd'])}", style="dim")
            return text
        if self.source:
            status = {"approved": "done", "rejected": "failed"}.get(row["status"], "awaiting_approval")
            text = self.state_text(status)
            text.append(" ")
        else:
            text = Text("⏸ ", style=self.color("warning"))
        available = width - text.cell_len - (2 if small else 14)
        approval_id = row_field(row["id"], min(12 if small else 16, available))
        task_id = row_field(row["task_id"], available - cell_len(approval_id))
        text.append(f"{approval_id}  {task_id}", style="bold")
        if small:
            text.append(f"\n   {row_field(row['checkpoint'], width - 3)}")
        else:
            text.append(f"  {row_field(row['checkpoint'], width - text.cell_len - 2)}")
        return text

    def populate(self, panel, *, choose_first=True):
        widget = self.query_one(f"#{panel}", Panel)
        query = self.filters[panel].casefold()
        rows, options = [], []
        for row in self.rows(panel):
            prompt = self.row_text(panel, row)
            fields = ("title", "task_id", "checkpoint", "role", "engine", "model")
            full_text = " ".join(row.get(field) or "" for field in fields)
            if query in f"{row['id']} {prompt.plain} {' '.join(full_text.split())}".casefold():
                rows.append(row)
                options.append(Option(prompt, id=row["id"]))
        self.visible_rows[panel] = {row["id"]: row for row in rows}
        selected = self.selection(panel)
        index = next((i for i, row in enumerate(rows) if selected and row["id"] == selected["id"]), None)
        if not rows:
            message = "No matches"
            if not query:
                message = {
                    "plans": self.snapshot.get("message", "No plans."),
                    "tasks": "No tasks.", "agents": "No agent steps.", "approvals": "No approvals.",
                }[panel]
            options.append(Option(message, disabled=True))
        if index is None and rows and choose_first:
            # A filter or parent selection can require a genuine selection change.
            widget.set_options(options)
            widget.highlighted = 0
        else:
            self.replace_rows(widget, options, index)
        suffix = f"  ·  {self.snapshot['pending_approvals']} pending" if panel == "approvals" else ""
        if self.filters[panel]:
            suffix += f"  /{self.filters[panel]}"
        self.query_one(f"#{panel}-title", Static).update(panel.title() + suffix)

    def replace_rows(self, widget, options, index):
        old_options = widget.options
        with widget.prevent(OptionList.OptionHighlighted):
            if [option.id for option in old_options] != [option.id for option in options]:
                widget.set_options(options)
            else:
                for i, (old, new) in enumerate(zip(old_options, options)):
                    if old.prompt != new.prompt:
                        widget.replace_option_prompt_at_index(i, new.prompt)
            widget.highlighted = index

    def update_top(self):
        if self.source:
            data = self.snapshot
            observed = data["observed"]
            age = "?" if observed is None else f"{int(time.monotonic() - observed)}s"
            text = Text("LIVE", style=f"bold {self.color('accent')}")
            text.append(f"   ● {data['running']} run", style=self.color("primary"))
            text.append(f"   ✗ {data['failed']} fail", style=self.color("error"))
            text.append(f"   ⏸ {data['awaiting']} wait", style=self.color("warning"))
            text.append(f"   Today {amount(data['cost_today'])}")
            text.append(f"   Source {data['health']}   Age {age}", style="dim")
            self.query_one("#top", Static).update(text)
            return
        jobs = self.snapshot["jobs"]
        running = sum(job["state"] == "running" for job in jobs)
        failed = sum(step["status"] == "failed" for job in jobs for step in job["steps"])
        unknown = any(job["attempts"] and job["cost_usd"] is None for job in jobs)
        known_cost = sum(job["cost_usd"] or 0 for job in jobs)
        text = Text(self.snapshot["project"], style="bold")
        text.append(f"  {self.snapshot['mode'].upper()}", style=self.color("accent"))
        text.append(f"   ● {running} run", style=self.color("primary"))
        text.append(f"   ✗ {failed} fail", style=self.color("error"))
        approval_label = "appr" if self.compact() else "approval"
        text.append(f"   ⏸ {self.snapshot['pending_approvals']} {approval_label}", style=self.color("warning"))
        text.append(f"   Today {amount(known_cost)}{' + ?' if unknown else ''}")
        # Fixture snapshots refresh every five ticks independently of stream events.
        refresh_age = self.ticks % 5
        text.append(f"   {'Age' if self.compact() else 'Refresh'} {refresh_age}s", style="dim")
        self.query_one("#top", Static).update(text)

    def on_descendant_focus(self, message):
        target = message.widget.id
        if isinstance(message.widget, Panel):
            self.panel = self.detail_source = target
        elif target in ("tabs", "reading", "log"):
            self.panel = "main"
        else:
            return
        for panel in PANELS:
            self.query_one(f"#{panel}-panel").set_class(panel == self.panel, "active-panel")
        self.show_detail()

    @on(OptionList.OptionHighlighted)
    def highlight(self, message):
        panel = message.option_list.id
        row = self.visible_rows.get(panel, {}).get(message.option.id)
        if row is None or row is self.selection(panel):
            return
        if panel == "plans":
            self.plan = row
            self.selected_task = row["jobs"][0] if row["jobs"] else None
            if self.selected_task:
                self.step = current_step(self.selected_task) if self.source else self.selected_task["steps"][0]
            else:
                self.step = None
            self.attempt_id = None
            self.populate("tasks")
            self.populate("agents")
        elif panel == "tasks":
            self.selected_task = row
            self.step = current_step(row)
            self.attempt_id = None
            self.populate("agents")
        elif panel == "agents":
            self.step = row
        else:
            self.approval = row
        self.show_detail()
        if self.source:
            self.next_poll = 0

    def action_attempt(self, direction):
        task = self.selected_task
        if not self.source or self.panel != "tasks" or not task:
            return
        index = task["attempt"] - 1 + direction
        if 0 <= index < len(task["runs"]):
            self.attempt_id = task["runs"][index]
            self.next_poll = 0

    @on(OptionList.OptionSelected)
    def drill(self, message):
        panel = message.option_list.id
        if panel == "approvals":
            self.action_approvals()
        else:
            self.action_panel(("tasks", "agents", "main")[PANELS.index(panel)])

    def action_panel(self, panel):
        if panel == "main":
            target = "#log" if self.query_one("#tabs", Tabs).active == "log" else "#reading"
        else:
            target = f"#{panel}"
        self.query_one(target).focus()

    def action_move_panel(self, direction):
        order = (*PANELS, "main")
        self.action_panel(order[(order.index(self.panel) + direction) % len(order)])

    def action_move_row(self, direction):
        if isinstance(self.focused, Panel):
            action = self.focused.action_cursor_down if direction > 0 else self.focused.action_cursor_up
            action()
        elif self.panel == "main":
            self.focused.scroll_relative(y=direction, animate=False)

    def action_change_tab(self, direction):
        tabs = self.query_one("#tabs", Tabs)
        action = tabs.action_next_tab if direction > 0 else tabs.action_previous_tab
        action()

    def action_follow(self):
        self.follow = not self.follow
        if self.follow:
            self.query_one("#log", RichLog).scroll_end(animate=False, x_axis=False)
        self.update_status()

    def action_filter(self):
        if self.panel in PANELS:
            field = self.query_one("#filter", Input)
            field.value = self.filters[self.panel]
            field.display = True
            field.focus()

    @on(Input.Changed, "#filter")
    def filter_changed(self, message):
        if message.input.has_focus:
            self.filters[self.panel] = message.value
            self.populate(self.panel)
            self.show_detail()

    @on(Input.Submitted, "#filter")
    def filter_submitted(self):
        self.query_one("#filter").display = False
        self.action_panel(self.panel)

    def action_clear_filter(self):
        if self.panel in PANELS:
            self.filters[self.panel] = ""
            self.query_one("#filter").display = False
            self.populate(self.panel)
            self.action_panel(self.panel)
            self.show_detail()

    def check_action(self, action, parameters):
        if action == "attempt" and (not self.source or self.focused is not self.query_one("#tasks")):
            return False
        if isinstance(self.screen, Help):
            return False
        return super().check_action(action, parameters)

    def action_approvals(self):
        self.action_panel("approvals")
        self.query_one("#tabs", Tabs).active = "brief"

    def action_help(self):
        self.push_screen(Help())

    def action_toggle_theme(self):
        self.theme = "pack-light" if self.current_theme.dark else "pack-dark"
        if not isinstance(self.screen, Help):
            for panel in PANELS:
                self.populate(panel, choose_first=False)
            self.log_step = None
            self.update_top()
            self.show_detail()

    @on(Tabs.TabActivated)
    def tab_changed(self):
        self.show_detail()
        if self.panel == "main" and self.focused is not self.query_one("#tabs"):
            self.action_panel("main")

    def plan_brief(self):
        if not self.plan:
            return self.snapshot["message"]
        finished = sum(job["state"] == "done" for job in self.plan["jobs"])
        if self.source:
            brief = self.task_brief()
            if "done" in self.plan:
                return f"{self.plan['title']}\n\n{self.plan['done']}/{self.plan['total']} complete\n\n{brief}"
            return (f"{self.plan['title']}\n\n{started(self.plan)}/{len(self.plan['jobs'])} started"
                    f" · {finished} complete\n\n{brief}")
        return (
            f"## {self.plan['title']}\n\n"
            f"**{started(self.plan)}/{len(self.plan['jobs'])} started** · {finished} complete\n\n"
            "### Task brief\n\n" + self.selected_task["brief"]
        )

    def update_status(self):
        tab = self.query_one("#tabs", Tabs).active
        source = self.detail_source
        status = {
            "brief": {"plans": "PLAN BRIEF", "approvals": "APPROVAL BRIEF",
                      "agents": "STEP BRIEF", "tasks": "TASK BRIEF"}[source],
            "log": f"{'LIVE' if self.step and self.step['status'] == 'running' else 'EVENT STREAM'}"
                   f"    ·    Follow {'ON' if self.follow else 'OFF'}  [f]",
            "gate": "GATE RESULTS",
            "graph": "TASK DEPENDENCIES",
            "meta": "RUN / STEP METADATA",
        }
        self.query_one("#detail-status", Static).update(status.get(tab, ""))
        hint = "Enter drill" if self.panel != "main" else "↑↓ scroll"
        keymap = f" 1–4 panels   j/k move   {hint}   [ ] tabs"
        if self.compact():
            keymap += "   ? help   q quit\n f follow   / filter   a approvals"
        else:
            keymap += "   f follow   / filter   a approvals   ? help   q quit"
        if self.source and self.panel == "tasks" and self.selected_task and self.selected_task["attempts"] > 1:
            keymap += "   ←/→ attempts"
        self.query_one("#keys", Static).update(Text(keymap))

    def show_detail(self, *, preserve_scroll=False):
        tab = self.query_one("#tabs", Tabs).active or "brief"
        source = self.detail_source
        if source == "plans":
            context = f"{self.plan['id']}  /  {self.plan['title']}" if self.plan else "Runs"
        elif source == "approvals":
            context = f"{self.approval['id']}  /  {self.approval['checkpoint']}" if self.approval else "Approvals"
        elif self.selected_task:
            context = f"{self.selected_task['task_id']}  /  {self.selected_task['title']}"
            if source == "agents" and self.step:
                context += f"  /  {self.step['role']}"
        else:
            context = "Runs"
        self.query_one("#context", Static).update(Text(context) if self.source else context)
        self.update_status()
        reading = self.query_one("#reading", VerticalScroll)
        brief = self.query_one("#brief", Markdown)
        detail = self.query_one("#detail", Static)
        log = self.query_one("#log", RichLog)
        reading.display = tab != "log"
        log.display = tab == "log"
        brief.display = tab == "brief" and not self.source
        detail.display = tab in ("gate", "graph", "meta") or (tab == "brief" and self.source is not None)
        if not preserve_scroll:
            reading.scroll_home(animate=False)
        if tab == "brief":
            if self.source:
                detail.update(Text(self.brief_body()))
            else:
                brief.update(self.brief_body())
        elif tab == "log":
            self.render_log()
        else:
            detail.update({"gate": self.gates, "graph": self.graph, "meta": self.meta}[tab]())

    def brief_body(self):
        if self.detail_source == "plans":
            return self.plan_brief()
        if self.detail_source == "approvals":
            return self.approval["brief"] if self.approval else "No approvals."
        if self.detail_source == "agents":
            return self.step["brief"] if self.step else "No agent steps."
        return self.task_brief()

    def task_brief(self):
        task = self.selected_task
        if not task:
            return self.snapshot["message"]
        if blockers := task.get("blocked_by"):
            return f"Blocked by {', '.join(blockers)}\n\n{task['brief']}"
        return task["brief"]

    def render_log(self):
        log = self.query_one("#log", RichLog)
        if self.source:
            records = self.step["events"] if self.step else []
            key = self.step.get("stream_key") if self.step else None
            key = key or (*self.selection_keys(), "empty")
            if self.log_step != key or (records and not self.log_sequence):
                log.clear()
                self.log_step, self.log_sequence = key, 0
                if not records:
                    log.write(Text("Waiting for the first event." if self.step else "No agent steps.", style="dim"),
                              scroll_end=False)
            for record in records:
                if record["sequence"] > self.log_sequence:
                    self.write_event(record)
                    self.log_sequence = record["sequence"]
            return
        if self.log_step is self.step:
            return
        self.log_step = self.step
        log.clear()
        if not self.step["events"]:
            log.write(Text("Waiting for the first event.", style="dim"))
        for record in self.step["events"]:
            self.write_event(record)

    def write_event(self, record):
        line = Text(f"{record['sequence']:03d}  ", style="dim", overflow="fold")
        if record["kind"] == "tool":
            code = record.get("exit_code")
            line.append(f"$ {record['text']} ", style=self.color("primary"))
            if code is not None:
                line.append(f"[exit {code}]", style=self.color("error" if code else "success"))
        else:
            line.append(f"> {record['text']}", style=self.color("accent"))
        self.query_one("#log", RichLog).write(line, scroll_end=self.follow)

    def gates(self):
        task_gates = self.selected_task["gates"] if self.selected_task else []
        gates = (self.step["gates"] if self.step else []) or task_gates
        if not gates:
            return Text("No gate results yet.", style="dim")
        text = Text()
        for gate in gates:
            if self.source:
                status = str(gate.get("status") or "unknown")
                text.append(f"Step {gate['step']:02}  {status.upper()}\n", style="bold")
                if command := gate.get("command"):
                    text.append(f"$ {command}\n", style=self.color("primary"))
                if gate.get("returncode") is not None:
                    text.append(f"Exit {gate['returncode']}\n")
                for field in ("stdout", "stderr", "transcript", "error", "artifact_notice"):
                    if gate.get(field):
                        text.append(f"{gate[field]}\n")
                for field in ("checks", "lint_findings", "static_findings"):
                    for finding in gate.get(field, []):
                        text.append("  ".join(str(value) for value in finding.values()) + "\n")
                text.append("\n")
                continue
            failed = gate["status"] == "fail"
            text.append(f"{'✗ FAIL' if failed else '✓ PASS'}  {gate['name']}  ·  Attempt {gate['attempt']}\n",
                        style=f"bold {self.color('error' if failed else 'success')}")
            text.append(f"$ {gate['command']}  [exit {gate['returncode']}]\n\n",
                        style=self.color("primary"))
            text.append(f"{gate['output']}\n\n")
        return text

    def graph(self):
        if not self.plan:
            return Text(self.snapshot["message"])
        text = Text(self.plan["title"] + "\n\n", style="bold")
        children = defaultdict(list)
        for job in self.plan["jobs"]:
            parent = job["depends_on"][0] if job["depends_on"] else None
            children[parent].append(job)

        # Follow the first dependency for the tree and label the remaining edges.
        roots = children.pop(None, [])
        stack = [(iter(roots), "", len(roots))]
        while stack:
            siblings, indent, remaining = stack.pop()
            job = next(siblings, None)
            if job is None:
                continue
            remaining -= 1
            last = remaining == 0
            stack.append((siblings, indent, remaining))
            text.append(indent + ("└─ " if last else "├─ "), style="dim")
            text.append_text(self.state_text(job["state"]))
            text.append(f"  {job['task_id']}  {job['title']}", style="bold")
            text.append(f"  ·  {STATES[job['state']][2]}\n", style="dim")
            next_indent = indent + ("   " if last else "│  ")
            for extra in job["depends_on"][1:]:
                text.append(next_indent + f"   also needs {extra}\n", style="dim")
            descendants = children.pop(job["task_id"], [])
            stack.append((iter(descendants), next_indent, len(descendants)))
        return text

    def meta(self):
        if not self.selected_task:
            return Text(self.snapshot["message"])
        text = Text()
        if self.source:
            task = self.selected_task
            if not task["attempts"]:
                return Text(f"{task['task_id']}  ·  No attempts yet.")
            fields = {
                "Run ID": task["run_id"], "Kind": task["kind"], "Task": task["task_id"],
                "Health": task["health"], "Attempt": f"{task['attempt']}/{task['attempts']}",
                "Attempts": ", ".join(task["runs"]), "Started": task["started"], "Finished": task["finished"],
            }
            if self.step:
                step = self.step
                fields.update({
                    "Step ID": step["id"], "Engine": f"{step['engine']}:{step['model']}",
                    "Elapsed": elapsed(step["duration_s"]) if step["duration_s"] is not None else None,
                    "Tokens in": step["tokens_in"], "Tokens out": step["tokens_out"],
                    "Cost": amount(step["cost_usd"]), **step["files"],
                })
        else:
            fields = {
                "Job ID": self.selected_task["id"], "Kind": self.selected_task["kind"], "Task": self.selected_task["task_id"],
                "Health": self.selected_task["health"], "Step ID": self.step["id"],
                "Engine": f"{self.step['engine']}:{self.step['model']}",
                "Started": self.step["started"], "Finished": self.step["finished"],
                "Elapsed": elapsed(self.step["duration_s"]), "Tokens in": self.step["tokens_in"],
                "Tokens out": self.step["tokens_out"], "Cost": amount(self.step["cost_usd"]),
                **self.step["files"],
            }
        for label, value in fields.items():
            text.append(f"{label:<13}", style=self.color("secondary"))
            text.append(f"{value if value is not None else '?'}\n")
        return text

    def tick(self):
        running = next(job for job in self.snapshot["jobs"] if job["state"] == "running")
        step = running["steps"][running["current_step"] - 1]
        running["age_s"] += 1
        running["duration_s"] += 1
        step["duration_s"] += 1
        record = event(step["events"][-1]["sequence"] + 1, LIVE_MESSAGES[self.ticks % len(LIVE_MESSAGES)])
        step["events"].append(record)
        del step["events"][:-1000]
        self.ticks += 1
        self.update_top()
        tasks = self.query_one("#tasks", Panel)
        agents = self.query_one("#agents", Panel)
        if running["id"] in self.visible_rows["tasks"]:
            tasks.replace_option_prompt(running["id"], self.row_text("tasks", running))
        if running is self.selected_task and step["id"] in self.visible_rows["agents"]:
            agents.replace_option_prompt(step["id"], self.row_text("agents", step))
        if self.log_step is step:
            self.write_event(record)
        if self.step is step and self.query_one("#tabs", Tabs).active == "meta":
            self.query_one("#detail", Static).update(self.meta())
