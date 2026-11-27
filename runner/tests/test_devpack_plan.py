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
from runner.agent_exec import AgentResult, new_run_dir


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
        args = devpack_run.build_arg_parser().parse_args(runner.call_args.args[0])
        self.assertEqual(args.plan_id, "example")
        self.assertEqual(args.task_id, "T2")
        self.assertEqual(args.task_file, str(self.plan / "briefs/T2.md"))

    def test_run_next_preserves_option_like_title(self):
        self.manifest.write_text(self.manifest.read_text().replace(
            'title = "Draft the guide"', 'title = "--dry-run"',
        ))
        args = ["run-next", str(self.plan), "--", "--worktree", str(self.root)]
        with patch("runner.devpack_run.main", return_value=0) as runner:
            self.assertEqual(devpack_plan.main(args), 0)
        parsed = devpack_run.build_arg_parser().parse_args(runner.call_args.args[0])
        self.assertEqual(parsed.title, "--dry-run")
        self.assertFalse(parsed.dry_run)

    def test_run_next_rejects_forwarded_separators_and_plan_options(self):
        for options in (["--", "--"], ["--plan-id=other"], ["--task-id", "other"],
                        ["--task-file=other.md"], ["--brief-author=human"], ["--plan-dir=other"],
                        ["--title=other"], ["--plan-i=other"]):
            with self.subTest(options=options), patch("runner.devpack_run.main") as runner:
                self.assertEqual(devpack_plan.main([
                    "run-next", str(self.plan), "--", "--worktree", str(self.root), *options,
                ]), 2)
                runner.assert_not_called()

    def test_plan_paths_allow_symlink_above_root_but_reject_links_inside(self):
        real = self.root / "real"
        real.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(real, target_is_directory=True)
        plan = copytree(self.plan, real / "example")
        runs = alias / "runs"
        new_run_dir(runs, task="T1", outcome="pass", run_id="first",
                    plan_link={"plan_id": "example", "task_id": "T1"})
        path = runs / ".." / "example"
        with patch("runner.devpack_run.main", return_value=0) as runner:
            self.assertEqual(devpack_plan.main([
                "run-next", str(path), "--worktree", str(real), "--runs-root", str(runs),
            ]), 0)
        args = devpack_run.build_arg_parser().parse_args(runner.call_args.args[0])
        self.assertEqual(Path(args.task_file), alias / "example" / "briefs" / "T2.md")
        self.assertEqual(len(list(devpack_plan.read_runs(runs))), 1)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(devpack_run.main([
                "--worktree", str(alias), "--config", str(self.engines()), "--dry-run",
                "--plan-id", "example", "--plan-dir", "example", "--task-id", "T2",
                "--task-file", str(path / "briefs/T2.md"),
            ]), 0)
        self.assertIn("template: plan brief", output.getvalue())
        (alias / "linked-plan").symlink_to(plan, target_is_directory=True)
        self.assertEqual(devpack_plan.main(["validate", str(alias / "linked-plan")]), 2)
        brief = plan / "briefs/T1.md"
        brief.unlink()
        brief.symlink_to(self.plan / "briefs/T1.md")
        self.assertEqual(devpack_plan.main(["validate", str(path)]), 2)

    def test_plan_lints_future_inputs_and_inspects_completed_tasks(self):
        docs = self.root / "docs"
        docs.mkdir()
        source = docs / "source.md"
        source.write_text("First line\nSecond line\n")
        first, second, third = (self.plan / "briefs" / f"T{i}.md" for i in range(1, 4))
        first.write_text(first.read_text().replace("Use the workflow description", "Use docs/source.md:2")
                         .replace("A guide under the documentation directory.", "Create `docs/guide.md`."))
        third.write_text(third.read_text().replace("Use the workflow description", "Read `docs/guide.md`"))
        workdir = ["--workdir", str(self.root)]
        self.assertEqual(devpack_plan.main(["validate", str(self.plan), *workdir]), 0)
        run_next = ["run-next", str(self.plan), "--worktree", str(self.root)]
        with patch("runner.devpack_run.main", return_value=0) as runner:
            self.assertEqual(devpack_plan.main(run_next), 0)
        args = devpack_run.build_arg_parser().parse_args(runner.call_args.args[0])
        self.assertEqual(args.task_id, "T1")
        second.write_text(second.read_text().replace("Use the workflow description", "Read `docs/typo.md`"))
        self.assertEqual(devpack_plan.main(["validate", str(self.plan), *workdir]), 2)
        second.write_text(second.read_text().replace("Read `docs/typo.md`", "Read `docs/guide.md`"))
        source.write_text("First line\n")
        new_run_dir(self.root / "runs", task="T1", outcome="pass", run_id="first",
                    plan_link={"plan_id": "example", "task_id": "T1"})
        for command in ("status", "render-md"):
            self.assertEqual(devpack_plan.main([command, str(self.plan), *workdir]), 0)
        with patch("runner.devpack_run.main", return_value=0) as runner:
            self.assertEqual(devpack_plan.main(run_next), 2)
            runner.assert_not_called()
            (docs / "guide.md").touch()
            self.assertEqual(devpack_plan.main(run_next), 0)
        args = devpack_run.build_arg_parser().parse_args(runner.call_args.args[0])
        self.assertEqual(args.task_id, "T2")

    def test_read_runs_skips_unreadable_records(self):
        runs = self.root / "runs"
        good = new_run_dir(runs, task="T1", outcome="pass", run_id="good",
                           plan_link={"plan_id": "example", "task_id": "T1"})
        for name in ("malformed", "oversized", "linked"):
            (runs / name).mkdir()
        (runs / "malformed/run.json").write_text("{")
        (runs / "oversized/run.json").write_bytes(b" " * (1024 * 1024))
        (runs / "linked/run.json").symlink_to(good / "run.json")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(devpack_plan.main([
                "status", str(self.plan), "--workdir", str(self.root), "--json",
            ]), 0)
        self.assertEqual(json.loads(output.getvalue())["tasks"][0]["attempts"], 1)

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
        alias = worktree / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        plan_path = alias / "plans" / ".." / "plans" / "example"
        with redirect_stdout(io.StringIO()), patch("runner.devpack_run.run_agent") as worker:
            result = devpack_plan.main(["run-next", str(plan_path), "--", *args])
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

    def test_linked_resume_failure_releases_plan_task(self):
        subprocess.run(["git", "init", "-q", "--template="], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "Fixture", "--allow-empty"], cwd=self.root, check=True)
        with TemporaryDirectory() as state:
            config = self.engines()
            args = ["--worktree", str(self.root), "--state-dir", state, "--config", str(config),
                    "--isolation", "local", "--allow-unsandboxed-local"]
            self.assertEqual(devpack_plan.main(["run-next", str(self.plan), *args]),
                             checkpoints.AWAITING_APPROVAL_EXIT_CODE)
            (record_path,) = (self.root / "runs").glob("*/run.json")
            approval = json.loads(record_path.read_text())["approval_id"]
            checkpoints.decide_approval(state, approval, decision_status=checkpoints.APPROVED)
            config.write_text("invalid TOML")
            self.assertEqual(devpack_run.main([*args, "--resume", approval]), 2)
        record = json.loads(record_path.read_text())
        self.assertEqual(record["outcome"], "fail")
        self.assertTrue(record["finished"])
        plan, _ = devpack_plan.load_plan(self.plan)
        status = devpack_plan.progress(plan, devpack_plan.read_runs(self.root / "runs"))
        self.assertEqual(status["tasks"][0]["state"], "ready")

    def test_linked_resume_is_running_before_worker_and_finalizes_os_error(self):
        subprocess.run(["git", "init", "-q", "--template="], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "Fixture", "--allow-empty"], cwd=self.root, check=True)
        with TemporaryDirectory() as state:
            args = ["--worktree", str(self.root), "--state-dir", state, "--config", str(self.engines()),
                    "--isolation", "local", "--allow-unsandboxed-local"]
            self.assertEqual(devpack_plan.main(["run-next", str(self.plan), *args]),
                             checkpoints.AWAITING_APPROVAL_EXIT_CODE)
            (path,) = (self.root / "runs").glob("*/run.json")
            approval = json.loads(path.read_text())["approval_id"]
            checkpoints.decide_approval(state, approval, decision_status=checkpoints.APPROVED)

            def offline_worker(request):
                self.assertEqual(request.run_dir, path.parent)
                record = json.loads(path.read_text())
                self.assertEqual((record["outcome"], record["finished"]), ("running", None))
                raise OSError("worker could not start")

            with patch("runner.devpack_run.run_agent", side_effect=offline_worker) as worker:
                self.assertEqual(devpack_run.main([*args, "--resume", approval]), 2)
                worker.assert_called_once()
        record = json.loads(path.read_text())
        self.assertEqual(record["outcome"], "fail")
        self.assertTrue(record["finished"])

    def test_dispatch_links_the_paused_attempt(self):
        subprocess.run(["git", "init", "-q", "--template="], cwd=self.root, check=True)
        config = self.engines()
        subprocess.run(["git", "add", "plans", "engines.toml"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "Fixture"], cwd=self.root, check=True)
        runs = TemporaryDirectory()
        self.addCleanup(runs.cleanup)
        runs_root = Path(runs.name)
        with (TemporaryDirectory() as state,
              patch("runner.devpack_dispatch.run_agent") as worker):
            args = ["--workdir", str(self.root), "--state-dir", state, "--runs-root", str(runs_root)]
            result = devpack_dispatch.main([
                *args, "--engine", "worker", "--engines", str(config),
                "--brief", str(self.plan / "briefs/T1.md"), "--gate-cmd", "true",
                "--plan-id", "example", "--task-id", "T1", "--allow", "docs/**",
                "--isolation", "local", "--allow-unsandboxed-local",
            ])
            worker.assert_not_called()
            self.assertEqual(result, checkpoints.AWAITING_APPROVAL_EXIT_CODE)
            records = list(runs_root.glob("*/run.json"))
            self.assertEqual(len(records), 1)
            record = json.loads(records[0].read_text())
            self.assertEqual((record["plan_id"], record["task_id"], record["plan_dir"]),
                             ("example", "T1", "plans/example"))
            approval = record["approval_id"]
            checkpoints.decide_approval(state, approval, decision_status=checkpoints.APPROVED)

            def offline_worker(request):
                self.assertEqual(request.run_dir, records[0].parent)
                active = json.loads(records[0].read_text())
                self.assertEqual((active["outcome"], active["finished"]), ("running", None))
                return AgentResult()

            worker.side_effect = offline_worker
            self.assertEqual(devpack_dispatch.main([*args, "--resume", approval]), 0)
            self.assertEqual(devpack_dispatch.main([*args, "--resume", approval]), 1)
        self.assertEqual(list(runs_root.glob("*/run.json")), records)
        completed = json.loads(records[0].read_text())
        self.assertEqual((completed["outcome"], completed["approval_id"]), ("pass", approval))
        plan, _ = devpack_plan.load_plan(self.plan)
        status = devpack_plan.progress(plan, devpack_plan.read_runs(runs_root))
        self.assertEqual((status["tasks"][0]["state"], status["tasks"][0]["attempts"]), ("done", 1))

    def test_dispatch_resume_reuses_attempt_and_finalizes_early_failure(self):
        subprocess.run(["git", "init", "-q", "--template="], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "Fixture", "--allow-empty"], cwd=self.root, check=True)
        config = self.engines()
        with TemporaryDirectory() as state, patch("runner.devpack_dispatch.run_agent") as worker:
            args = ["--workdir", str(self.root), "--state-dir", state]
            self.assertEqual(devpack_dispatch.main([
                *args, "--engine", "worker", "--engines", str(config),
                "--brief", str(self.plan / "briefs/T1.md"), "--gate-cmd", "true",
                "--plan-id", "example", "--task-id", "custom-id", "--allow", "docs/**",
                "--isolation", "local", "--allow-unsandboxed-local",
            ]), checkpoints.AWAITING_APPROVAL_EXIT_CODE)
            (path,) = (self.root / "runs").glob("*/run.json")
            paused = json.loads(path.read_text())
            self.assertEqual(paused["task"], "custom-id")
            approval = paused["approval_id"]
            checkpoints.decide_approval(state, approval, decision_status=checkpoints.APPROVED)
            config.write_text("invalid TOML")
            self.assertEqual(devpack_dispatch.main([*args, "--resume", approval]), 1)
            worker.assert_not_called()
        self.assertEqual(list((self.root / "runs").glob("*/run.json")), [path])
        record = json.loads(path.read_text())
        self.assertEqual((record["task"], record["outcome"]), ("custom-id", "fail"))
        self.assertTrue(record["finished"])
