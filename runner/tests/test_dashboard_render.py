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
            self.assertIn("Role", screen.lines[1])
            self.assertNotIn("Health", screen.lines[1])
            ui.key("j")
            selected = ui.job_id
            snapshot["jobs"].reverse()
            screen.width = 160
            ui.paint(screen, snapshot)
            self.assertIn("Health", screen.lines[1])
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

    def test_job_columns_at_80_100_and_160(self):
        from runner.dash.ui import job_columns, job_line

        run_id = "20261126T223127Z-a540cc"
        job = {"id": run_id, "key": ["task"], "aliases": [],
               "kind": "dispatch", "state": "succeeded", "role": "worker",
               "agent": "claude", "model": "haiku", "current_step": None, "age_s": 0,
               "duration_s": None, "cost_usd": 0.03, "process_health": None}
        mandatory = (("Job", 23), ("State", 9), ("Role", 10), ("Agent", 16),
                     ("#", 2), ("Age", 5), ("Cost", 6))
        expected = {
            80: mandatory,
            100: (*mandatory[:3], ("Agent", 19), *mandatory[4:], ("Health", 7), ("Kind", 8)),
            160: (*mandatory[:3], ("Agent", 34), *mandatory[4:], ("Health", 7), ("Kind", 8)),
        }
        self.assertEqual(len(run_id), 23)
        for width, columns in expected.items():
            with self.subTest(width=width):
                available = width - 3
                screen = Screen(24, width)
                UI().paint(screen, {"jobs": [job], "approvals": [], "sources": {}, "pending_approvals": 0})
                self.assertEqual(job_columns(available), columns)
                self.assertEqual(screen.lines[1].split(), [label for label, _ in columns])
                line = screen.lines[2]
                fields = {}
                offset = 2
                for label, size in columns:
                    fields[label] = line[offset:offset + size].strip()
                    offset += size + 1
                self.assertTrue(all(len(line) < width for line in screen.lines.values()))
                self.assertEqual(fields["Job"], run_id)
                self.assertEqual(fields["State"], "succeeded")
                self.assertEqual(fields["Role"], "worker")
                self.assertEqual(fields["Agent"], "claude:haiku")
                self.assertEqual(fields["#"], "?")
                self.assertEqual(fields["Age"], "0s")
                self.assertEqual(fields["Cost"], "$0.03")
                if width >= 100:
                    self.assertEqual(fields["Health"], "unknown")
                if width == 160:
                    self.assertEqual(fields["Kind"], "dispatch")
                    self.assertIn("engine:model-standard-v123", job_line(
                        {**job, "agent": "engine", "model": "model-standard-v123"}, available))

        broker_id = "0123456789abcdef0123456789abcdef"
        row = job_line({**job, "id": broker_id, "state": "waiting for approval",
                        "role": "broker-worker", "agent": "haiku", "process_health": "unavailable"}, 117)
        self.assertEqual(row.split(), [broker_id[-23:], "awaiting", "broker-wo…",
                                      "haiku", "?", "0s", "$0.03", "unavai…", "dispatch"])
        custom = job_line({**job, "state": "custom long state"}, 77)
        self.assertEqual(custom[24:33], "custom l…")

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
