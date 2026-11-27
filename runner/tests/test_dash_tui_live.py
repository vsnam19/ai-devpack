"""Headless checks for the read-only terminal dashboard."""

from contextlib import ExitStack
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
from time import monotonic
import unittest
from unittest.mock import patch

from runner.dash.reader import Reader, Settings
from runner.tests.test_dashboard_reader import write


class LiveDashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        try:
            from runner.dash.tui.app import Dashboard
        except ModuleNotFoundError as error:
            if error.name == "textual":
                self.skipTest("Textual is optional and is not installed")
            raise
        from runner.dash.tui.source import RunSource

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(self.root, self.root / "runs", self.root / "state")
        self.reader = Reader(self.settings)
        self.app = Dashboard(source=RunSource(self.reader, interval=0.05))

    def run_record(self, name, **fields):
        path = self.settings.runs / name
        write(path / "run.json", {"kind": "task", "outcome": "running", **fields})
        write(path / "steps" / "01-build-engine.meta.json", {"status": "running"})
        return path

    async def wait_for(self, pilot, condition):
        deadline = monotonic() + 4
        while monotonic() < deadline:
            if condition():
                return
            await pilot.pause()
        self.assertTrue(condition(), "Dashboard did not show the expected update")

    async def test_groups_attempts_and_propagates_selection_in_compact_layout(self):
        self.run_record("first", plan_id="p01", task_id="build", started="2026-01-01T01:00:00Z")
        self.run_record("second", plan_id="p01", task_id="build", started="2026-01-01T02:00:00Z")
        self.run_record("loose", task="review")
        async with self.app.run_test(size=(80, 24)) as pilot:
            await self.wait_for(pilot, lambda: len(self.app.snapshot["plans"]) == 2)
            groups = {plan["title"]: plan["jobs"] for plan in self.app.snapshot["plans"]}
            self.assertEqual(set(groups), {"p01", "Ad-hoc runs"})
            self.assertEqual(groups["p01"][0]["attempts"], 2)
            self.assertEqual(groups["Ad-hoc runs"][0]["task_id"], "review")
            if self.app.plan["title"] != "p01":
                await pilot.press("j")
            self.assertEqual(self.app.plan["title"], "p01")
            await pilot.press("enter")
            expected = self.app.plan["jobs"][0]["task_id"]
            self.assertEqual(self.app.selected_task["task_id"], expected)
            self.assertEqual(self.app.step["role"], "build")
            self.assertEqual(self.app.focused.id, "tasks")
            self.assertGreaterEqual(self.app.query_one("#main").region.width, 48)
            self.assertIn(expected, self.app.query_one("#context").content.plain)
            self.assertEqual(self.app.selected_task["run_id"], "second")
            await pilot.press("left")
            await self.wait_for(pilot, lambda: self.app.selected_task["run_id"] == "first")
            await pilot.press("enter")
            self.assertEqual(self.app.focused.id, "agents")

    async def test_selected_stream_redaction_and_follow(self):
        run = self.run_record("active", task="build")
        path = run / "steps" / "01-build-engine.jsonl"
        secret = "fake-secret-123456789"
        (run / "steps" / "01-build-engine.prompt.md").write_text(f"token={secret}\n", encoding="utf-8")
        write(run / "steps" / "02-review-engine.meta.json", {"status": "running", "duration_s": 1.5})
        (run / "steps" / "02-review-engine.jsonl").write_text(
            '{"type":"message","content":"other stream"}\n', encoding="utf-8",
        )

        def append(*messages):
            with path.open("a", encoding="utf-8") as output:
                for message in messages:
                    output.write(json.dumps({"type": "message", "content": message}) + "\n")

        append(f"token={secret}", *(f"record {i}" for i in range(55)))
        async with self.app.run_test(size=(120, 35)) as pilot:
            await self.wait_for(pilot, lambda: bool(self.app.plan))
            detail = self.app.query_one("#detail")
            self.assertIn("token=[REDACTED]", detail.content.plain)
            self.assertNotIn(secret, detail.content.plain)
            await pilot.press("3", "k", "]")
            log = self.app.query_one("#log")

            def log_text():
                return "\n".join(line.text for line in log.lines)

            def followed(message):
                # RichLog adds the line before its after-refresh scroll runs.
                return message in log_text() and 0 < log.scroll_y == log.max_scroll_y

            await self.wait_for(pilot, lambda: followed("record 54"))
            self.assertEqual(self.app.step["index"], 1)
            self.assertIn("token=[REDACTED]", log_text())
            self.assertNotIn(secret, log_text())
            self.assertNotIn("other stream", log_text())
            self.assertGreater(log.max_scroll_y, 0)
            self.assertEqual(log.scroll_y, log.max_scroll_y)
            previous_end = log.max_scroll_y
            append("new followed event")
            await self.wait_for(
                pilot, lambda: followed("new followed event") and log.max_scroll_y > previous_end,
            )
            self.assertEqual(log.scroll_y, log.max_scroll_y)
            await pilot.press("f")
            log.scroll_home(animate=False, immediate=True)
            await pilot.pause()
            position = log.scroll_y
            self.assertLess(position, log.max_scroll_y)
            append("new unfollowed event")
            await self.wait_for(pilot, lambda: "new unfollowed event" in log_text())
            self.assertEqual(log.scroll_y, position)
            self.assertEqual(self.app.step["index"], 1)
            await pilot.press("j")
            await self.wait_for(pilot, lambda: "other stream" in log_text())
            self.assertNotIn("new unfollowed event", log_text())
            self.assertEqual(self.app.step["index"], 2)

    async def test_gate_and_approval_commands_are_display_only(self):
        started = datetime.now(timezone.utc).isoformat()
        run = self.run_record("pause", task="review", outcome="awaiting_approval",
                              approval_id="vote", checkpoint_id="brief", started=started)
        write(run / "step_01_gate.json", {"gate_passed": False, "command": ["check"], "returncode": 1,
                                        "stderr": "check failed"})
        write(run / "steps" / "01-build-engine.meta.json", {"status": "running", "cost_usd": 0.25})
        pending = write(self.settings.state / "approvals" / "pending" / "vote.json",
                        {"id": "vote", "checkpoint": "brief", "status": "pending", "summary": "Review brief"})
        before = pending.read_bytes()
        commands = self.reader.commands("vote")

        def forbidden(*args, **kwargs):
            self.fail("Dashboard attempted an external action or filesystem mutation")

        original_os_open, original_io_open = os.open, io.open

        def read_only_fd(path, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                forbidden()
            return original_os_open(path, flags, *args, **kwargs)

        def read_only_file(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                forbidden()
            return original_io_open(file, mode, *args, **kwargs)

        with ExitStack() as guard:
            for target in ("subprocess.Popen", "os.system", "os.kill", "os.mkdir",
                           "os.unlink", "os.rename", "os.replace", "socket.create_connection"):
                guard.enter_context(patch(target, side_effect=forbidden))
            guard.enter_context(patch("os.open", side_effect=read_only_fd))
            guard.enter_context(patch("io.open", side_effect=read_only_file))
            guard.enter_context(patch("builtins.open", side_effect=read_only_file))
            async with self.app.run_test(size=(120, 35)) as pilot:
                await self.wait_for(pilot, lambda: bool(self.app.approval))
                self.assertIn("1 wait", self.app.query_one("#top").content.plain)
                self.assertIn("Today $0.250", self.app.query_one("#top").content.plain)
                await pilot.press("]", "]")
                self.assertIn("check failed", self.app.query_one("#detail").content.plain)
                await pilot.press("a", "enter")
                detail = self.app.query_one("#detail").content.plain
                self.assertIn(commands["approve"], detail.splitlines())
                self.assertIn(commands["reject"], detail.splitlines())
        self.assertEqual(pending.read_bytes(), before)

    async def test_empty_states(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            await self.wait_for(pilot, lambda: self.app.snapshot["health"] == "missing")
            detail = self.app.query_one("#detail")
            self.assertIn("Runs root missing", detail.content.plain)
            await pilot.press("a")
            self.assertEqual(detail.content.plain, "No approvals.")
            self.settings.runs.mkdir()
            self.app.next_poll = 0
            await self.wait_for(pilot, lambda: self.app.snapshot["health"] == "current")
            await pilot.press("1")
            self.assertEqual(detail.content.plain, "No runs found.")

    async def test_unreadable_selected_gates_leave_dashboard_current(self):
        self.run_record("active", task="build")
        with patch.object(self.reader, "gates", side_effect=PermissionError("steps unavailable")):
            async with self.app.run_test() as pilot:
                await self.wait_for(pilot, lambda: self.app.snapshot["observed"] is not None)
                self.assertEqual(self.app.snapshot["health"], "current")
                self.assertEqual(self.app.selected_task["task_id"], "build")
                self.assertEqual(self.app.selected_task["gates"], [])

    async def test_discovery_budget_does_not_starve_identity_or_details(self):
        run = self.run_record("active", plan_id="p01", task_id="build")
        (run / "steps" / "01-build-engine.prompt.md").write_text("Build brief", encoding="utf-8")
        write(run / "step_01_gate.json", {"gate_passed": True, "returncode": 0})
        snapshot = self.reader.snapshot

        def depleted_snapshot(**kwargs):
            result = snapshot(**kwargs)
            self.reader.files.remaining = 0
            return result

        with patch.object(self.reader, "snapshot", side_effect=depleted_snapshot):
            async with self.app.run_test() as pilot:
                await self.wait_for(pilot, lambda: bool(self.app.plan))
                self.assertEqual(self.app.plan["id"], "plan:p01")
                self.assertEqual(self.app.selected_task["brief"], "Build brief")
                self.assertTrue(self.app.selected_task["gates"])

    async def test_filter_title_displays_rich_tags_literally(self):
        async with self.app.run_test() as pilot:
            await pilot.press("slash", "[", "b", "]", "x")
            self.assertIn("/[b]x", str(self.app.query_one("#plans-title").render()))

    async def test_poll_retries_after_unexpected_exception(self):
        self.run_record("active", task="build")
        poll = self.app.source.poll

        def fail_once(keys):
            self.app.source.poll = poll
            raise KeyError("unexpected artifact field")

        with patch.object(self.app.source, "poll", side_effect=fail_once):
            async with self.app.run_test() as pilot:
                await self.wait_for(pilot, lambda: bool(self.app.selected_task))
                self.assertIn("Source current", self.app.query_one("#top").content.plain)
                self.assertEqual(self.app.selected_task["task_id"], "build")

    async def test_failed_summary_includes_rejected_and_refused_runs(self):
        for outcome in ("fail", "rejected", "refused"):
            self.run_record(outcome, task=outcome, outcome=outcome)
        async with self.app.run_test(size=(120, 35)) as pilot:
            await self.wait_for(pilot, lambda: bool(self.app.plan))
            self.assertEqual([task["state"] for task in self.app.plan["jobs"]], ["failed"] * 3)
            self.assertIn("3 fail", self.app.query_one("#top").content.plain)

    async def test_compact_row_fits_task_id_title_and_attempt(self):
        self.run_record("active", task_id="20261127-long-task-ending", task="Build")
        async with self.app.run_test(size=(80, 24)) as pilot:
            await self.wait_for(pilot, lambda: bool(self.app.selected_task))
            await pilot.press("2")
            panel = self.app.query_one("#tasks")
            prompt = panel.get_option_at_index(0).prompt
            self.assertIn("ending", prompt.plain)
            self.assertIn("Build", prompt.plain)
            self.assertTrue(prompt.plain.endswith("↻1"))
            self.assertLessEqual(prompt.cell_len, panel.content_region.width - 1)

    async def test_task_row_shows_id_once_when_title_matches(self):
        self.run_record("build")
        async with self.app.run_test(size=(120, 35)) as pilot:
            await self.wait_for(pilot, lambda: bool(self.app.selected_task))
            prompt = self.app.query_one("#tasks").get_option_at_index(0).prompt
            self.assertEqual(prompt.plain.count("build"), 1)

    async def test_repopulate_preserves_historical_attempt_and_step(self):
        from textual.widgets import OptionList

        first = self.run_record("first", plan_id="p02", task_id="build",
                                started="2026-01-01T01:00:00Z")
        write(first / "steps" / "02-review-engine.meta.json", {"status": "running"})
        self.run_record("second", plan_id="p02", task_id="build", started="2026-01-01T02:00:00Z")
        self.run_record("setup", plan_id="p02", task_id="setup", started="2026-01-01T03:00:00Z")
        self.run_record("other", plan_id="p01", task_id="other", started="2026-01-01T04:00:00Z")
        highlights = []

        def record_highlights(message):
            if isinstance(message, OptionList.OptionHighlighted):
                highlights.append(message.option.id)

        async with self.app.run_test(size=(120, 35), message_hook=record_highlights) as pilot:
            await self.wait_for(pilot, lambda: len(self.app.snapshot["plans"]) == 2)
            self.assertEqual(self.app.plan["id"], "plan:p01")
            await pilot.press("1", "j")
            self.assertEqual(self.app.plan["id"], "plan:p02")
            await pilot.press("2", "j")
            self.assertEqual(self.app.selected_task["task_id"], "build")
            await pilot.press("left")
            await self.wait_for(pilot, lambda: self.app.selected_task["run_id"] == "first")
            await pilot.press("3", "j")
            self.assertEqual(self.app.step["index"], 2)

            def assert_selection():
                self.assertEqual(self.app.plan["id"], "plan:p02")
                self.assertEqual(self.app.selected_task["task_id"], "build")
                self.assertEqual(self.app.attempt_id, "first")
                self.assertEqual(self.app.step["index"], 2)
                self.assertEqual(highlights, [])

            highlights.clear()
            await pilot.press("ctrl+t")
            assert_selection()
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertTrue(self.app.compact())
            assert_selection()

            await pilot.press("1", "slash", "p", "0", "2")
            plans = self.app.query_one("#plans")
            self.assertEqual([option.id for option in plans.options], ["plan:p02"])
            assert_selection()
            await pilot.press("escape")
            self.assertEqual(plans.highlighted_option.id, "plan:p02")
            assert_selection()

            await pilot.press("slash", "z")
            self.assertIsNone(plans.highlighted)
            await pilot.press("ctrl+t")
            await pilot.resize_terminal(120, 35)
            await pilot.pause()
            self.assertFalse(self.app.compact())
            self.assertIsNone(plans.highlighted)
            assert_selection()
            await pilot.press("escape")
            self.assertEqual(plans.highlighted_option.id, "plan:p02")
            assert_selection()

    async def test_sidebar_rows_bound_multiline_reader_text(self):
        from rich.cells import cell_len

        self.run_record("active", plan_id="p01", task_id="task\n" + "界" * 80,
                        task="Build\twide " + "界" * 80)
        write(self.settings.state / "approvals" / "pending" / "vote.json",
              {"id": "vote", "status": "pending", "checkpoint": "brief", "run_dir": "active"})
        with patch.object(self.app.source, "plan_title", return_value="Plan\n" + "界" * 80):
            async with self.app.run_test(size=(120, 35)) as pilot:
                await self.wait_for(pilot, lambda: bool(self.app.approval and self.app.plan))

                def assert_rows(compact):
                    for key, lines in (("plans", 1), ("tasks", 1), ("approvals", 2 if compact else 1)):
                        panel = self.app.query_one(f"#{key}")
                        prompt = panel.get_option_at_index(0).prompt.plain
                        self.assertEqual(len(prompt.splitlines()), lines, prompt)
                        self.assertNotIn("\t", prompt)
                        for line in prompt.splitlines():
                            self.assertLessEqual(cell_len(line), panel.content_region.width - 1)

                assert_rows(False)
                await pilot.resize_terminal(80, 24)
                await pilot.pause()
                assert_rows(True)
                plan = self.app.query_one("#plans").get_option_at_index(0).prompt
                self.assertLessEqual(cell_len(plan.plain.split(" 1/1")[0].strip("● ")), 15)
