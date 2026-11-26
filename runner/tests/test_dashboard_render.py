"""Render dashboard views on a fake terminal."""

import unittest

from runner.dash.ui import UI


class Screen:
    def __init__(self, height, width):
        self.height = height
        self.width = width
        self.lines = {}

    def getmaxyx(self):
        return self.height, self.width

    def addstr(self, y, x, value):
        assert 0 <= y < self.height
        assert x + len(value) < self.width
        self.lines[y] = value

    def refresh(self):
        pass

    def erase(self):
        self.lines.clear()


class RenderTest(unittest.TestCase):
    def test_empty_jobs_approvals_and_filter(self):
        for size in ((24, 80), (48, 160)):
            screen = Screen(*size)
            ui = UI()
            ui.paint(screen, {"jobs": [], "approvals": [], "sources": {}, "pending_approvals": 0})
            self.assertIn("Jobs", screen.lines[0])
            ui.key("/")
            ui.key("x")
            ui.key("ENTER")
            self.assertEqual(ui.query, "x")
            ui.key("/")
            ui.key("y")
            ui.key("ESC")
            self.assertEqual(ui.query, "x")
            ui.key("C")
            self.assertEqual(ui.query, "")
            ui.key("a")
            self.assertEqual(ui.view, "Approvals")

    def test_drill_down_follow_and_stable_selection(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from runner.dash.reader import Reader, Settings
        from runner.tests.test_dashboard_reader import write

        with TemporaryDirectory() as temp:
            root = Path(temp)
            runs = root / "runs"
            for name in ("first", "second"):
                write(runs / name / "run.json", {"kind": "dispatch", "outcome": "running"})
            reader = Reader(Settings(root, runs, root / "state"))
            ui = UI()
            screen = Screen(24, 80)
            snapshot = reader.snapshot()
            ui.paint(screen, snapshot)
            self.assertIn("Health", screen.lines[1])
            ui.key("j")
            selected = ui.job_id
            snapshot["jobs"].reverse()
            screen.width = 160
            ui.paint(screen, snapshot)
            self.assertEqual(ui.job_id, selected)
            ui.key("ENTER")
            self.assertEqual(ui.view, "Job Detail")
            ui.key("a")
            self.assertEqual(ui.view, "Approvals")
            ui.key("ESC")
            ui.job_id = None
            ui.key("a")
            self.assertEqual(ui.view, "Approvals")
            ui.key("ESC")
            ui.key("l")
            ui.key("c")
            self.assertEqual(ui.kind, "tool")
            ui.key("PGUP")
            self.assertFalse(ui.follow)
            ui.key("f")
            self.assertTrue(ui.follow)

    def test_job_columns_at_80_and_120(self):
        from runner.dash.ui import job_columns, job_line

        job = {"id": "20261126T223127Z-a540cc", "key": ["task"], "aliases": [],
               "kind": "task", "state": "running", "role": "worker",
               "agent": "engine", "model": "model-standard-v123", "current_step": None, "age_s": 0,
               "duration_s": None, "cost_usd": None, "process_health": None}
        for width in (80, 120):
            with self.subTest(width=width):
                available = width - 3
                screen = Screen(24, width)
                UI().paint(screen, {"jobs": [job], "approvals": [], "sources": {}, "pending_approvals": 0})
                line = screen.lines[2]
                fields = {}
                offset = 2
                for label, size in job_columns(available):
                    fields[label] = line[offset:offset + size].strip()
                    offset += size + 1
                self.assertLess(len(line), width)
                self.assertEqual(fields["Job"], job["id"])
                self.assertEqual(fields["Agent"], "engine:model-standard-v123")
                self.assertEqual(fields["#"], "?")
                if width == 120:
                    self.assertIn("a" * 32, job_line({**job, "id": "a" * 32}, available))

    def test_idle_poll_delay(self):
        from runner.dash.ui import poll_delay

        idle = {"scan": {"scanned": 1, "discovered": 1}, "active_jobs": False, "stream_drained": False,
                "sources": {"runs": "current", "stream": "jsonl"}}
        self.assertEqual(poll_delay(idle, {}, 1), 30)
        self.assertEqual(poll_delay(idle, {"event_kind": "all"}, 1), 1)
        self.assertEqual(poll_delay({**idle, "stream_drained": True}, {"event_kind": "all"}, 1), 30)
        self.assertEqual(poll_delay({**idle, "active_jobs": True}, {}, 1), 1)
        missing = {**idle, "sources": {"runs": "missing", "stream": "missing"}}
        self.assertEqual(poll_delay(missing, {"event_kind": "all"}, 1), 30)
        backlog = {**idle, "scan": {"scanned": 1, "discovered": 2}}
        self.assertEqual(poll_delay(backlog, {}, 1), 1)
