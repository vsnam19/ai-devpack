"""External engine contract tests."""
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from runner import devpack_dispatch, devpack_run
from runner.tests.test_devpack_dispatch import _init_git_repo
from runner.agent_exec import (
    AgentRequest, check_data_class_allowed, compute_engine_config_hash, load_engines, run_agent,
)


class ExternalEngineTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def configuration(self, mode="success"):
        fixture = Path(__file__).parent / "fixtures" / "external_job.py"
        command = [sys.executable, str(fixture), mode, "{brief}", "{run_dir}",
                   "{role}", "{model}", "{allow_args}"]
        config = self.root / "engines.toml"
        config.write_text(f"""
[engines.external]
class = "external"
model = "worker:test"
command = {json.dumps(command)}
result = "json-stdout"
reply_from = "json:reply_path"
events_from = "{{run_dir}}/events"
max_data_class = "internal"
timeout_s = 1
env_allow = ["FIXTURE_TOKEN"]
[engines.external.role_map]
implementer = "implement"
""", encoding="utf-8")
        return config

    def test_results_and_evidence(self):
        for mode, status in (("success", "ok"), ("quiet", "ok"),
                             ("failed", "agent_error"), ("quota", "engine_unavailable"),
                             ("timeout", "timeout")):
            with self.subTest(mode=mode):
                engine = load_engines(self.configuration(mode)).engines["external"]
                self.assertFalse(check_data_class_allowed(engine, "confidential")[0])
                result = run_agent(AgentRequest(
                    engine=engine, prompt="Deliver the scoped change.", workdir=self.root,
                    run_dir=self.root / mode, isolation="local", allow_unsandboxed_local=True,
                    role="implementer", allow_globs=["literal;touch marker"],
                    env_overrides={"FIXTURE_TOKEN": "external-test-secret", "HIDDEN": "hidden"},
                ))
                step = self.root / mode / "steps" / "01-implementer-external"
                self.assertEqual(result.status, status, result.error)
                self.assertIn("Deliver the scoped change.", step.with_suffix(".prompt.md").read_text())
                metadata = json.loads(step.with_suffix(".meta.json").read_text())
                self.assertEqual(metadata["status"], status)
                self.assertIsNone(metadata["tokens_in"])
                self.assertIsNone(metadata["tokens_out"])
                self.assertIsNone(metadata["cost_usd"])
                self.assertEqual(metadata["model"], "worker:test")
                self.assertGreater(metadata["duration_s"], 0)
                self.assertEqual(metadata["exit_code"], {"failed": 1, "timeout": 124}.get(mode, 0))
                if mode == "quota":
                    self.assertEqual(metadata["failure_attribution"], "ENV/TOOL")
                events = step.with_suffix(".jsonl").read_text()
                self.assertNotIn("external-test-secret", events)
                event, = [json.loads(line) for line in events.splitlines()]
                if mode in ("quiet", "timeout"):
                    self.assertEqual(event["type"], "result")
                else:
                    self.assertEqual(event["item"]["type"], "agent_message")
                if mode != "timeout":
                    self.assertEqual(result.final_text, "Deliver the scoped change.")
                self.assertFalse((self.root / "marker").exists())

    def test_strict_config_and_python_310_fallback(self):
        config = self.configuration()
        original = config.read_text()
        with patch("runner.agent_exec.tomllib", None):
            self.assertEqual(load_engines(config).engines["external"].external.role_map,
                             {"implementer": "implement"})
        initial = compute_engine_config_hash(load_engines(config).engines["external"])
        for old, new, error in (
            ("{brief}", "{unknown}", "placeholder"),
            ('env_allow = ["FIXTURE_TOKEN"]', "", "missing fields"),
            ('env_allow = ["FIXTURE_TOKEN"]', "env_allow = [1]", "env_allow"),
            ('timeout_s = 1', 'timeout_s = true', "positive integer"),
            ('reply_from = "json:reply_path"', 'reply_from = "file:../reply"', "relative"),
        ):
            with self.subTest(old=old, new=new):
                config.write_text(original.replace(old, new))
                with self.assertRaisesRegex(ValueError, error):
                    load_engines(config)
        config.write_text(original.replace("FIXTURE_TOKEN", "OTHER_TOKEN"))
        self.assertNotEqual(initial, compute_engine_config_hash(load_engines(config).engines["external"]))

    def test_review_requires_distinct_names_and_external_identities(self):
        config = self.configuration()
        engine = config.read_text()
        roles = '\n[roles]\nimplementer = "external"\nreviewer = "{}"\n'
        config.write_text(engine + roles.format("external"))
        with self.assertRaisesRegex(ValueError, "different engine"):
            load_engines(config)
        reviewer = engine.replace("engines.external", "engines.review")
        config.write_text(engine + reviewer + roles.format("review"))
        with self.assertRaisesRegex(ValueError, "different engine"):
            load_engines(config)
        config.write_text(engine + reviewer.replace("worker:test", "reviewer:test") + roles.format("review"))
        self.assertEqual(load_engines(config).engine_for_role("reviewer").model, "reviewer:test")

    def test_plain_exit_code_and_file_reply(self):
        config = self.configuration("quiet")
        config.write_text(config.read_text().replace('result = "json-stdout"', 'result = "exit-code"')
                          .replace('reply_from = "json:reply_path"', 'reply_from = "file:reply.md"'))
        engine = load_engines(config).engines["external"]
        result = run_agent(AgentRequest(
            engine=engine, prompt="Brief passed by file.", workdir=self.root,
            run_dir=self.root / "plain", isolation="local", allow_unsandboxed_local=True,
            role="implementer", allow_globs=["literal;touch marker"],
        ))
        self.assertEqual((result.status, result.final_text), ("ok", "Brief passed by file."))

    def test_local_policy_refuses_before_launch(self):
        engine = load_engines(self.configuration()).engines["external"]
        result = run_agent(AgentRequest(
            engine=engine, prompt="Refused.", workdir=self.root, run_dir=self.root / "refused",
            isolation="local",
        ))
        self.assertEqual((result.status, result.exit_code), ("harness_error", 126))
        self.assertIn("allow-unsandboxed-local", result.error)

    def test_task_and_dispatch_use_pack_gate_and_data_class_policy(self):
        worktree = self.root / "worktree"
        _init_git_repo(worktree)
        config = self.configuration("workflow")
        implementer = config.read_text()
        reviewer = (implementer.replace("engines.external", "engines.review")
                    .replace("worker:test", "reviewer:test").replace('"workflow"', '"workflow-review"'))
        gate = self.root / "gate.py"
        gate_receipt = self.root / "gate-receipt"
        gate.write_text(
            f"from pathlib import Path\nPath({str(gate_receipt)!r}).touch()\nprint('GATE_OK')\n",
            encoding="utf-8",
        )
        config.write_text(
            f"gate_cmd = {json.dumps([sys.executable, str(gate)])}\n"
            + implementer + reviewer + '\n[roles]\nimplementer = "external"\nreviewer = "review"\n'
        )
        context = devpack_run.load_task_data(
            None, task_id="external-test", title="External task", goal="Check the scoped task.",
            allowed_files="src/**",
        )
        task_args = dict(
            config=devpack_run.load_engine_config(config), context=context, worktree=worktree,
            log_dir=self.root / "task-run", repo_root=devpack_run.REPO_ROOT,
            isolation="local", allow_unsandboxed_local=True, engines_config_path=config,
        )
        brief = self.root / "brief.md"
        brief.write_text("# Brief\nCheck the scoped task.\n")
        dispatch_args = dict(
            engine_name="external", workdir=worktree, brief_path=brief, engines_config_path=config,
            runs_root=self.root / "dispatch-runs", isolation="local", allow_unsandboxed_local=True,
            role="implementer", allow_globs=["src/**"], gate_cmd=f"{sys.executable} {gate}",
        )
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}):
            rc, summary = devpack_run.run_task_workflow(**task_args)
            self.assertEqual(rc, 0, summary)
            self.assertTrue(gate_receipt.is_file())
            self.assertEqual(summary["verdict"], "APPROVE")
            gate_receipt.unlink()
            rc, payload = devpack_dispatch.dispatch_job(**dispatch_args)
            self.assertEqual((rc, payload["status"], payload["gate"]), (0, "ok", "ok"), payload)
            self.assertTrue(gate_receipt.is_file())
            gate_receipt.unlink()
            project = worktree / ".devpack" / "project.toml"
            project.write_text('data_class = "confidential"\n' + project.read_text())
            with self.assertRaisesRegex(ValueError, "confidential"):
                devpack_run.run_task_workflow(**task_args)
            rc, payload = devpack_dispatch.dispatch_job(**dispatch_args)
            self.assertEqual((rc, payload["status"]), (1, "refused"), payload)
            self.assertFalse(gate_receipt.exists())
