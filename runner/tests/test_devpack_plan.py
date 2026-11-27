"""Offline coverage for plan validation, progress, and runner linkage."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from shutil import copytree
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from runner import checkpoints, devpack_dispatch, devpack_plan, devpack_run
from runner.agent_exec import new_run_dir


class PlanTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        example = Path(__file__).resolve().parents[2] / "workflow/templates/plan.example"
        self.plan = copytree(example, self.root / "plans" / "example")
        self.manifest = self.plan / "plan.toml"

    def test_invalid_plans(self):
        original = self.manifest.read_text()
        for broken, message in (
            (original.replace("depends_on = []", 'depends_on = ["T2"]', 1), "cycle"),
            (original.replace('brief = "briefs/T1.md"', 'brief = "../T1.md"'), "relative"),
            (original.replace('id = "T2"', 'id = "T1"'), "duplicate"),
            (original.replace('depends_on = ["T1"]', 'depends_on = ["missing"]'), "unknown"),
        ):
            with self.subTest(message=message):
                self.manifest.write_text(broken)
                with self.assertRaisesRegex(ValueError, message):
                    devpack_plan.load_plan(self.plan, workdir=self.root)
        self.manifest.write_text(original)
        brief = self.plan / "briefs/T1.md"
        brief.write_text(brief.read_text().replace("docs/**", "`--help`"))
        with self.assertRaisesRegex(ValueError, "invalid allowed-files glob"):
            devpack_plan.load_plan(self.plan, workdir=self.root)
        brief.write_text("# Incomplete")
        with self.assertRaisesRegex(ValueError, "BRIEF-HEADINGS"):
            devpack_plan.load_plan(self.plan, workdir=self.root)

    def test_status_joins_only_matching_plan_and_selects_ready_task(self):
        runs = self.root / "runs"
        legacy = new_run_dir(runs, task="T1", outcome="pass", run_id="legacy")
        self.assertNotIn("plan_id", json.loads((legacy / "run.json").read_text()))
        for run_id, plan_id, outcome, started in (
            ("first", "example", "fail", "2026-01-01T00:00:00+00:00"),
            ("second", "example", "APPROVED", "2026-01-02T00:00:00+00:00"),
            ("other", "other-plan", "fail", "2026-01-03T00:00:00+00:00"),
        ):
            path = runs / run_id
            path.mkdir()
            (path / "run.json").write_text(json.dumps({
                "plan_id": plan_id, "task": "T1", "outcome": outcome, "started": started,
            }))
        plan, _ = devpack_plan.load_plan(self.plan, workdir=self.root)
        status = devpack_plan.progress(plan, devpack_plan.read_runs(runs))
        self.assertEqual((status["done"], status["total"]), (1, 3))
        self.assertEqual([(task["state"], task["attempts"]) for task in status["tasks"]],
                         [("done", 2), ("ready", 0), ("blocked", 0)])
        with patch("runner.devpack_run.main", return_value=20) as runner:
            self.assertEqual(devpack_plan.main(["run-next", str(self.plan), "--worktree", str(self.root)]), 20)
        args = runner.call_args.args[0]
        for key, value in (("--plan-id", "example"), ("--task-id", "T2"),
                           ("--task-file", str(self.plan / "briefs/T2.md"))):
            self.assertEqual(args[args.index(key) + 1], value)

    def test_fallback_parser_and_markdown_render(self):
        with patch("runner.workspace_scope.tomllib", None):
            plan, briefs = devpack_plan.load_plan(self.plan, workdir=self.root)
        self.assertEqual(list(briefs), ["T1", "T2", "T3"])
        markdown = devpack_plan.render_markdown(plan)
        self.assertIn("Created: 2026-01-01", markdown)
        self.assertIn("| T2 | Add a worked example | T1 | project default | DOC-1 |", markdown)
        self.assertIn("- [Brief](<briefs/T2.md>)", markdown)

    def engines(self):
        path = self.root / "engines.toml"
        path.write_text(
            'version = 2\ngate_cmd = ["true"]\n'
            '[engines.worker]\nmodel = "worker"\nclass = "cloud"\n'
            '[roles]\nimplementer = "worker"\nreviewer = "worker"\nfixer = "worker"\n'
        )
        return path

    def test_linked_task_pauses_and_resumes_with_exact_brief(self):
        worktree = self.root / "target"
        worktree.mkdir()
        subprocess.run(["git", "init", "-q", "--template="], cwd=worktree, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "Fixture", "--allow-empty"], cwd=worktree, check=True)
        state = self.root / "state"
        args = ["--worktree", str(worktree), "--state-dir", str(state), "--config", str(self.engines()),
                "--isolation", "local", "--allow-unsandboxed-local"]
        with redirect_stdout(io.StringIO()), patch("runner.devpack_run.run_agent") as worker:
            result = devpack_plan.main(["run-next", str(self.plan), "--", *args])
            self.assertEqual(result, checkpoints.AWAITING_APPROVAL_EXIT_CODE)
            worker.assert_not_called()
            self.assertEqual(devpack_plan.main(["run-next", str(self.plan), *args]), 1)
        records = list((worktree / "runs").glob("*/run.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertEqual((record["plan_id"], record["task_id"], record["plan_dir"]),
                         ("example", "T1", "../plans/example"))
        self.assertEqual(record["outcome"], "awaiting_approval")
        approval = record["approval_id"]
        checkpoints.decide_approval(state, approval, decision_status=checkpoints.APPROVED)
        with patch("runner.devpack_run.run_task_workflow", return_value=(0, {})) as workflow:
            self.assertEqual(devpack_run.main([*args, "--resume", approval]), 0)
        resumed = workflow.call_args.kwargs
        self.assertEqual(resumed["run_dir"], records[0].parent)
        self.assertEqual(resumed["plan_brief"], (self.plan / "briefs/T1.md").read_text())
        self.assertEqual(resumed["plan_link"]["plan_id"], "example")

    def test_dispatch_links_the_paused_attempt(self):
        subprocess.run(["git", "init", "-q", "--template="], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "Fixture", "--allow-empty"], cwd=self.root, check=True)
        with (TemporaryDirectory() as state, redirect_stdout(io.StringIO()),
              patch("runner.devpack_dispatch.run_agent") as worker):
            result = devpack_dispatch.main([
                "--workdir", str(self.root), "--engine", "worker", "--engines", str(self.engines()),
                "--brief", str(self.plan / "briefs/T1.md"), "--gate-cmd", "true",
                "--plan-id", "example", "--task-id", "T1", "--allow", "docs/**",
                "--isolation", "local", "--allow-unsandboxed-local", "--state-dir", state,
            ])
            worker.assert_not_called()
        self.assertEqual(result, checkpoints.AWAITING_APPROVAL_EXIT_CODE)
        records = list((self.root / "runs").glob("*/run.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertEqual((record["plan_id"], record["task_id"], record["plan_dir"]),
                         ("example", "T1", "plans/example"))
