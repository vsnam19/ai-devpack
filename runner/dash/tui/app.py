"""Keyboard-first dashboard backed by the demo snapshot."""

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Input, Markdown, OptionList, RichLog, Static, Tabs, Tab
from textual.widgets.option_list import Option
from rich.text import Text

from .fixtures import LIVE_MESSAGES, demo_snapshot, event

PANELS = ("plans", "tasks", "agents", "approvals")
TABS = {"brief": "Brief", "log": "Log", "gate": "Gate", "graph": "Plan graph", "meta": "Meta"}
STATES = {
    "running": ("●", "primary", "Running"),
    "done": ("✓", "success", "Done"),
    "failed": ("✗", "error", "Failed"),
    "awaiting_approval": ("⏸", "warning", "Awaiting approval"),
    "blocked": ("⏸", "foreground", "Blocked"),
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
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m{seconds:02d}" if minutes else f"{seconds}s"


def amount(value):
    return "?" if value is None else f"${value:.3f}"


def started(plan):
    return sum(job["attempts"] > 0 for job in plan["jobs"])


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
        Binding("f", "follow", "Follow"),
        Binding("slash", "filter", "Filter"),
        Binding("escape", "clear_filter", "Clear filter"),
        Binding("a", "approvals", "Approvals"),
        Binding("question_mark", "help", "Help"),
        Binding("ctrl+t", "toggle_theme", "Theme"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, *, live=True):
        super().__init__()
        self.snapshot = demo_snapshot()
        self.plan = self.snapshot["plans"][0]
        self.selected_task = self.plan["jobs"][0]
        self.step = self.selected_task["steps"][0]
        self.approval = self.snapshot["approvals"][0]
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
                            yield Static(id=f"{panel}-title", classes="panel-title")
                        yield Panel(id=panel)
            with Vertical(id="main"):
                yield Static(id="context")
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
        if self.live:
            self.set_interval(1, self.tick)

    def on_resize(self, event):
        screen = self.default_screen
        previous = screen.has_class("compact")
        self.resize_layout(event.size)
        if self.visible_rows and previous != screen.has_class("compact"):
            for panel in PANELS:
                self.populate(panel)
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
            "tasks": self.plan["jobs"],
            "agents": self.selected_task["steps"],
            "approvals": self.snapshot["approvals"],
        }[panel]

    def selection(self, panel):
        return {"plans": self.plan, "tasks": self.selected_task, "agents": self.step,
                "approvals": self.approval}[panel]

    def row_text(self, panel, row):
        small = self.compact()
        if panel == "plans":
            count, total = started(row), len(row["jobs"])
            filled = round(7 * count / total)
            title = row["title"][:15] if small else row["title"]
            text = self.state_text(row["state"])
            text.append(f" {title:<15} ", style="bold")
            if not small:
                text.append("━" * filled, style=self.color("accent"))
                text.append("─" * (7 - filled), style="dim")
            text.append(f" {count}/{total}")
            if not small:
                text.append(f"  {STATES[row['state']][2]}", style="dim")
            return text
        if panel == "tasks":
            title = row["title"][:14] if small else row["title"]
            text = self.state_text(row["state"])
            text.append(f" {row['task_id']} ", style="bold")
            text.append(title)
            if not small:
                text.append(f"{' ' * (20 - len(title))}{row['attempts']}× {elapsed(row['age_s']):>5}",
                            style="dim")
                if row["depends_on"]:
                    text.append(f"  ‹ {','.join(row['depends_on'])}", style=self.color("secondary"))
            return text
        if panel == "agents":
            text = self.state_text(row["status"])
            text.append(f" {row['index']:02} {row['role']:<11}", style="bold")
            model = f"{row['engine']}:{row['model']}"
            if small:
                text.append(f"\n   {model}")
                return text
            text.append(model, style=self.color("secondary"))
            text.append(f" {elapsed(row['duration_s']):>5}", style="dim")
            tokens = row["tokens_in"]
            text.append(f"\n   Attempt {row['attempt']}   {tokens if tokens is not None else '?'} in"
                        f" / {row['tokens_out'] if tokens is not None else '?'} out"
                        f"   {amount(row['cost_usd'])}", style="dim")
            return text
        text = Text("⏸ ", style=self.color("warning"))
        text.append(f"{row['id']}  {row['task_id']}", style="bold")
        text.append(f"  {row['checkpoint']}" if not small else f"\n   {row['checkpoint']}")
        return text

    def populate(self, panel, *, choose_first=True):
        widget = self.query_one(f"#{panel}", Panel)
        query = self.filters[panel].casefold()
        rows = [
            row for row in self.rows(panel)
            if query in f"{row['id']} {self.row_text(panel, row).plain}".casefold()
        ]
        self.visible_rows[panel] = {row["id"]: row for row in rows}
        selected = self.selection(panel)
        index = next((i for i, row in enumerate(rows) if row is selected), None)
        widget.clear_options()
        widget.add_options(Option(self.row_text(panel, row), id=row["id"]) for row in rows)
        if not rows:
            widget.add_option(Option("No matches", disabled=True))
        elif index is None and choose_first:
            index = 0
        widget.highlighted = index
        suffix = f"  ·  {self.snapshot['pending_approvals']} pending" if panel == "approvals" else ""
        if self.filters[panel]:
            suffix += f"  /{self.filters[panel]}"
        self.query_one(f"#{panel}-title", Static).update(panel.title() + suffix)

    def update_top(self):
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
        row = self.visible_rows[panel].get(message.option.id)
        if row is None or row is self.selection(panel):
            return
        if panel == "plans":
            self.plan = row
            self.selected_task = row["jobs"][0]
            self.step = self.selected_task["steps"][0]
            self.populate("tasks")
            self.populate("agents")
        elif panel == "tasks":
            self.selected_task = row
            self.step = row["steps"][(row["current_step"] or 1) - 1]
            self.populate("agents")
        elif panel == "agents":
            self.step = row
        else:
            self.approval = row
        self.show_detail()

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
                self.populate(panel)
            self.log_step = None
            self.update_top()
            self.show_detail()

    @on(Tabs.TabActivated)
    def tab_changed(self):
        self.show_detail()
        if self.panel == "main" and self.focused is not self.query_one("#tabs"):
            self.action_panel("main")

    def plan_brief(self):
        finished = sum(job["state"] == "done" for job in self.plan["jobs"])
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
            "log": f"{'LIVE' if self.step['status'] == 'running' else 'EVENT STREAM'}"
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
        self.query_one("#keys", Static).update(Text(keymap))

    def show_detail(self):
        tab = self.query_one("#tabs", Tabs).active or "brief"
        source = self.detail_source
        if source == "plans":
            context = f"{self.plan['id']}  /  {self.plan['title']}"
        elif source == "approvals":
            context = f"{self.approval['id']}  /  {self.approval['checkpoint']}"
        elif source == "agents":
            context = f"{self.selected_task['task_id']}  /  {self.selected_task['title']}  /  {self.step['role']}"
        else:
            context = f"{self.selected_task['task_id']}  /  {self.selected_task['title']}"
        self.query_one("#context", Static).update(context)
        self.update_status()
        reading = self.query_one("#reading", VerticalScroll)
        brief = self.query_one("#brief", Markdown)
        detail = self.query_one("#detail", Static)
        log = self.query_one("#log", RichLog)
        reading.display = tab != "log"
        log.display = tab == "log"
        brief.display = tab == "brief"
        detail.display = tab in ("gate", "graph", "meta")
        reading.scroll_home(animate=False)
        if tab == "brief":
            brief.update(self.brief_body())
        elif tab == "log":
            self.render_log()
        else:
            detail.update({"gate": self.gates, "graph": self.graph, "meta": self.meta}[tab]())

    def brief_body(self):
        if self.detail_source == "plans":
            return self.plan_brief()
        if self.detail_source == "approvals":
            return self.approval["brief"]
        if self.detail_source == "agents":
            return self.step["brief"]
        return self.selected_task["brief"]

    def render_log(self):
        log = self.query_one("#log", RichLog)
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
            code = record["exit_code"]
            line.append(f"$ {record['text']} ", style=self.color("primary"))
            line.append(f"[exit {code}]", style=self.color("error" if code else "success"))
        else:
            line.append(f"> {record['text']}", style=self.color("accent"))
        self.query_one("#log", RichLog).write(line, scroll_end=self.follow)

    def gates(self):
        gates = self.step["gates"] or self.selected_task["gates"]
        if not gates:
            return Text("No gate results yet.", style="dim")
        text = Text()
        for gate in gates:
            failed = gate["status"] == "fail"
            text.append(f"{'✗ FAIL' if failed else '✓ PASS'}  {gate['name']}  ·  Attempt {gate['attempt']}\n",
                        style=f"bold {self.color('error' if failed else 'success')}")
            text.append(f"$ {gate['command']}  [exit {gate['returncode']}]\n\n",
                        style=self.color("primary"))
            text.append(f"{gate['output']}\n\n")
        return text

    def graph(self):
        text = Text(self.plan["title"] + "\n\n", style="bold")
        jobs = self.plan["jobs"]

        def branch(parent, indent):
            children = [job for job in jobs if (job["depends_on"] or (None,))[0] == parent]
            for index, job in enumerate(children, 1):
                last = index == len(children)
                text.append(indent + ("└─ " if last else "├─ "), style="dim")
                text.append_text(self.state_text(job["state"]))
                text.append(f"  {job['task_id']}  {job['title']}", style="bold")
                text.append(f"  ·  {STATES[job['state']][2]}\n", style="dim")
                for extra in job["depends_on"][1:]:
                    text.append(indent + ("   " if last else "│  ") + f"   also needs {extra}\n",
                                style="dim")
                branch(job["task_id"], indent + ("   " if last else "│  "))

        branch(None, "")
        return text

    def meta(self):
        text = Text()
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
