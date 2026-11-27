"""Headless navigation and layout checks for the optional dashboard."""

import unittest


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    def dashboard(self):
        try:
            from runner.dash.tui.app import Dashboard
        except ModuleNotFoundError as error:
            if error.name == "textual":
                self.skipTest("Textual is optional and is not installed")
            raise
        return Dashboard(live=False)

    async def test_focus_and_drill(self):
        app = self.dashboard()
        async with app.run_test(size=(120, 35)) as pilot:
            for key, panel in zip("4321", ("approvals", "agents", "tasks", "plans")):
                await pilot.press(key)
                self.assertEqual(app.focused.id, panel)
            await pilot.press("enter", "j", "j")
            self.assertEqual(app.selected_task["task_id"], "T03")
            self.assertEqual(app.query_one("#context").content, "T03  /  Resume cursor")
            await pilot.press("enter")
            self.assertEqual(app.focused.id, "agents")

    async def test_tabs_follow_and_tick(self):
        app = self.dashboard()
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.press("2", "j", "j", "3", "]")
            self.assertEqual(app.query_one("#tabs").active, "log")
            self.assertTrue(app.follow)
            await pilot.press("f")
            self.assertFalse(app.follow)
            events = len(app.step["events"])
            age = app.selected_task["age_s"]
            app.tick()
            self.assertEqual(len(app.step["events"]), events + 1)
            self.assertEqual(app.selected_task["age_s"], age + 1)
            await pilot.press("[")
            self.assertEqual(app.query_one("#tabs").active, "brief")

    async def test_compact_and_light_theme(self):
        app = self.dashboard()
        async with app.run_test(size=(80, 24)) as pilot:
            self.assertEqual(app.query_one("#tasks-panel").region.height, 1)
            self.assertGreaterEqual(app.query_one("#main").region.width, 48)
            await pilot.press("2", "ctrl+t")
            self.assertGreater(app.query_one("#tasks-panel").region.height, 1)
            self.assertFalse(app.current_theme.dark)

    async def test_mouse_filter_and_help_during_tick(self):
        app = self.dashboard()
        async with app.run_test(size=(80, 24)) as pilot:
            title = app.query_one("#tasks-title")
            hit, _ = app.screen.get_widget_at(title.region.x, title.region.y)
            self.assertIs(hit, title)
            await pilot.click(title)
            self.assertEqual(app.focused.id, "tasks")
            await pilot.press("/", "r", "e", "s", "u", "m", "e", "enter")
            self.assertEqual(list(app.visible_rows["tasks"]), ["p-021-t03"])
            self.assertEqual(app.selected_task["task_id"], "T03")
            await pilot.press("escape", "tab")
            self.assertEqual(app.focused.id, "agents")
            await pilot.press("]")
            await pilot.press("?")
            self.assertEqual(app.screen.query_one("#keymap").border_title,
                             "Keyboard shortcuts · Esc to close")
            app.tick()
            await pilot.press("escape")
            self.assertEqual(app.focused.id, "agents")
