"""External engine contract tests."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from runner import devpack_dispatch, devpack_logs, devpack_run
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
                    self.assertFalse(metadata["events_streamed"])
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

    def test_docker_launch_and_timeout_cleanup(self):
        docker = self.root / "devpack-test-docker"
        receipt = self.root / "docker.jsonl"
        docker.write_text(f"#!{sys.executable}\n" + """
import json
import os
from pathlib import Path
import stat
import sys
import time

args = sys.argv[1:]
record = {"args": args, "env": dict(os.environ)}
if args[0] == "run":
    mounts = [args[i + 1].split(":") for i, arg in enumerate(args) if arg == "-v"]
    record["modes"] = {
        dest: stat.S_IMODE(Path(src).stat().st_mode) for src, dest, _ in mounts
    }
    brief = next(Path(src) for src, dest, _ in mounts if dest.endswith("/brief.md"))
    record["staging_mode"] = stat.S_IMODE(brief.parent.stat().st_mode)
""" + f"""
with Path({str(receipt)!r}).open("a") as handle:
    print(json.dumps(record), file=handle)
if args[0] == "run":
    time.sleep(10)
""")
        docker.chmod(0o755)
        expected_env = {
            "PATH": str(self.root), "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root), "DOCKER_HOST": "unix:///fake.sock",
            "DOCKER_CONFIG": str(self.root / "config"), "XDG_RUNTIME_DIR": str(self.root),
            "LANG": "C", "LC_ALL": "C", "FIXTURE_TOKEN": "fixture-secret",
        }
        engine = load_engines(self.configuration()).engines["external"]
        previous_umask = os.umask(0o077)
        try:
            result = run_agent(AgentRequest(
                engine=engine, prompt="Check Docker invocation.", workdir=self.root,
                run_dir=self.root / "run", isolation="docker", role="implementer",
                docker_bin=docker.name, env_overrides={**expected_env, "HIDDEN": "hidden"},
            ))
        finally:
            os.umask(previous_umask)
        self.assertEqual(result.status, "timeout", result.error)
        launch, cleanup = map(json.loads, receipt.read_text().splitlines())
        for call in (launch, cleanup):
            self.assertEqual({key: call["env"].get(key) for key in expected_env}, expected_env)
            self.assertNotIn("HIDDEN", call["env"])
        args = launch["args"]
        self.assertEqual([args[i + 1] for i, arg in enumerate(args) if arg == "-e"],
                         ["FIXTURE_TOKEN"])
        self.assertEqual(cleanup["args"], ["rm", "-f", args[args.index("--name") + 1]])
        self.assertEqual(launch["modes"]["/workspace/external/brief.md"], 0o644)
        self.assertEqual(launch["modes"]["/workspace/external/output"], 0o777)
        self.assertEqual(launch["staging_mode"], 0o700)

    def test_docker_reports_protected_staging_without_changing_result(self):
        docker = self.root / "fake-docker"
        docker.write_text(f"#!{sys.executable}\n" + """
import json
from pathlib import Path
import sys

args = sys.argv[1:]
mounts = [args[i + 1].split(":") for i, arg in enumerate(args) if arg == "-v"]
output = next(Path(src) for src, dest, _ in mounts if dest.endswith("/output"))
(output / "reply.md").write_text("Completed.")
(output / "events").write_text('{"type": "engine_event"}\\n')
attempt = output / "attempt"
attempt.mkdir()
(attempt / "container-owned").write_text("attempt output")
attempt.chmod(0o555)
print(json.dumps({"status": "ok", "exit_code": 0, "reply_path": "reply.md"}))
""")
        docker.chmod(0o755)
        engine = load_engines(self.configuration()).engines["external"]
        unlink = os.unlink

        def unlink_as_host(path, *args, **kwargs):
            # Simulate a different owner even when the test runs as the container UID.
            if Path(path).name == "container-owned":
                raise PermissionError("container-owned file")
            return unlink(path, *args, **kwargs)

        with self.assertLogs("runner.agent_exec", level="WARNING") as logs:
            with patch("runner.agent_exec.tempfile.tempdir", str(self.root)):
                with patch("runner.agent_exec.os.unlink", side_effect=unlink_as_host):
                    result = run_agent(AgentRequest(
                        engine=engine, prompt="Check staging cleanup.", workdir=self.root,
                        run_dir=self.root / "run", isolation="docker", role="implementer",
                        docker_bin=str(docker),
                    ))

        self.assertEqual((result.status, result.exit_code, result.final_text), ("ok", 0, "Completed."))
        staging, = self.root.glob("devpack-external-*")
        self.assertIn(str(staging), logs.output[0])
        self.assertTrue((staging / "output" / "attempt" / "container-owned").is_file())
        events = self.root / "run" / "steps" / "01-implementer-external.jsonl"
        self.assertEqual(json.loads(events.read_text()), {"type": "engine_event"})

    def run_artifact_job(self, script, events_from="{run_dir}/events"):
        config = self.configuration()
        original = config.read_text()
        job = self.root / "artifact_job.py"
        job.write_text(script, encoding="utf-8")
        command = [sys.executable, str(job), "{run_dir}"]
        config.write_text(original.replace('events_from = "{run_dir}/events"',
                                           f"events_from = {json.dumps(events_from)}").replace(
            next(line for line in original.splitlines() if line.startswith("command = ")),
            f"command = {json.dumps(command)}",
        ))
        return run_agent(AgentRequest(
            engine=load_engines(config).engines["external"], prompt="Check artifacts.",
            workdir=self.root, run_dir=self.root / "run", isolation="local",
            allow_unsandboxed_local=True, role="implementer",
        ))

    def test_events_stream_before_exit_and_match_batch(self):
        def run(mode):
            engine = load_engines(self.configuration(mode)).engines["external"]
            return run_agent(AgentRequest(
                engine=engine, prompt="Check live events.", workdir=self.root,
                run_dir=self.root / mode, isolation="local", allow_unsandboxed_local=True,
                role="implementer", allow_globs=["literal;touch marker"], timeout=10,
                env_overrides={"FIXTURE_TOKEN": "external-test-secret"},
            ))

        def wait_for(predicate):
            deadline = time.monotonic() + 3
            while not predicate():
                if time.monotonic() >= deadline:
                    self.fail("Timed out waiting for the fixture or its transcript")
                time.sleep(0.01)

        step = Path("steps/01-implementer-external.jsonl")
        live = self.root / "live" / step
        with ThreadPoolExecutor(max_workers=1) as executor:
            job = executor.submit(run, "live")
            try:
                wait_for((self.root / "ready").exists)
                self.assertEqual(live.read_text() if live.exists() else None, "")
                (self.root / "start-events").touch()
                wait_for(lambda: live.exists() and live.stat().st_size > 0)
                self.assertFalse(job.done())
                streamed = live.read_text()
                self.assertNotIn("external-test-secret", streamed)
                self.assertNotIn("\\u001b", streamed)
                self.assertEqual(json.loads(streamed)["type"], "progress")
            finally:
                (self.root / "start-events").touch()
                (self.root / "finish-events").touch()
            result = job.result()
        self.assertEqual(result.status, "ok", result.error)
        self.assertTrue(json.loads(live.with_suffix(".meta.json").read_text())["events_streamed"])
        with patch("runner.agent_exec._ExternalEvents.follow", return_value=nullcontext()):
            batch = run("batch")
        self.assertEqual(batch.status, "ok", batch.error)
        self.assertFalse(json.loads(batch.transcript_path.with_suffix(".meta.json").read_text())
                         ["events_streamed"])
        self.assertEqual(live.read_bytes(), (self.root / "batch" / step).read_bytes())
        first, last = map(json.loads, live.read_text().splitlines())
        self.assertEqual(first, json.loads(streamed))
        self.assertEqual(last, {"type": "done", "text": "finished ☃"})

    def test_unsafe_event_paths_are_refused(self):
        for path, write in (
            ("../events", '(output.parent / "events").write_text(event)'),
            ("events", '(output / "events").symlink_to(reply)'),
            ("events", '(output / "events").symlink_to(output / "../brief.md")'),
            ("nested/events", '(output / "nested").symlink_to(output, target_is_directory=True)\n'
                              '(output / "events").write_text(event)'),
        ):
            with self.subTest(path=path, write=write):
                result = self.run_artifact_job(f"""
import json
from pathlib import Path
import sys

output = Path(sys.argv[1])
event = '{{"type": "unsafe"}}'
reply = output / "reply.md"
reply.write_text(event)
{write}
print(json.dumps(dict(status="ok", exit_code=0, reply_path=str(reply))))
""", events_from=path)
                self.assertEqual(result.status, "harness_error", result.error)
                self.assertEqual(json.loads(result.transcript_path.read_text())["type"], "result")
                meta = json.loads(result.transcript_path.with_suffix(".meta.json").read_text())
                self.assertFalse(meta["events_streamed"])

    def test_json_stdout_normalizes_failure_status(self):
        for reported, expected, attribution, exit_code in (
            ("unknown_state", "harness_error", "ENV/TOOL", 0),
            ("failed", "agent_error", "", 1),
        ):
            with self.subTest(reported=reported):
                payload = json.dumps({"status": reported, "exit_code": 0})
                result = self.run_artifact_job(f"print({payload!r})\n")
                self.assertEqual(result.status, expected)
                self.assertEqual(result.exit_code, exit_code)
                self.assertEqual(result.failure_attribution, attribution)

    def test_oversized_external_output_is_harness_error(self):
        for name, write in (
            ("stdout", "sys.stdout.write(large)"),
            ("stderr", "sys.stderr.write(large)"),
            ("reply", 'reply.write_text(large)'),
            ("events", '(output / "events").write_text(\'{"type":"message","content":"Progress"}\\n\' + large)'),
        ):
            with self.subTest(name=name):
                result = self.run_artifact_job(f"""
import json
from pathlib import Path
import sys

output = Path(sys.argv[1])
reply = output / "reply.md"
reply.write_text("ok")
large = ("!" * 1023 + chr(10)) * 4096 + "!"
{write}
print(json.dumps(dict(status="ok", exit_code=0, reply_path=str(reply))))
""")
                self.assertEqual(result.status, "harness_error", result.error)
                self.assertIn(f"external {name} exceeds", result.error)
                if name == "events":
                    progress, notice = map(json.loads, result.transcript_path.read_text().splitlines())
                    self.assertEqual(progress, {"type": "message", "content": "Progress"})
                    self.assertEqual(notice["type"], "notice")
                    self.assertIn("truncated", notice["message"])
                    from runner.dash.events import Stream

                    stream = Stream(devpack_logs.Redactor(), lambda: 0)
                    stream.poll(result.transcript_path)
                    self.assertEqual([event["kind"] for event in stream.events], ["message", "notice"])
                    self.assertEqual(stream.events[-1]["text"], notice["message"])

    def test_external_events_preserve_unicode_line_separator(self):
        message = "first\u2028second"
        event = json.dumps({
            "type": "item.completed", "item": {"type": "agent_message", "text": message},
        }, ensure_ascii=False) + "\n"
        result = self.run_artifact_job(f"""
import json
from pathlib import Path
import sys

output = Path(sys.argv[1])
reply = output / "reply.md"
reply.write_text("ok")
(output / "events").write_text({event!r}, encoding="utf-8")
print(json.dumps(dict(status="ok", exit_code=0, reply_path=str(reply))))
""")
        self.assertEqual(result.status, "ok", result.error)
        transcript = self.root / "run/steps/01-implementer-external.jsonl"
        self.assertEqual(json.loads(transcript.read_text())["item"]["text"], message)

    def test_external_token_limits_must_be_positive_integers(self):
        config = self.configuration()
        original = config.read_text()
        config.write_text(original.replace("timeout_s = 1", """
timeout_s = 1
context_tokens = 128000
brief_tokens_max = 8000
"""))
        self.assertIn("external", load_engines(config).engines)
        for key in ("context_tokens", "brief_tokens_max"):
            for value in ("0", "-1", "true", '"8000"', "1.5"):
                with self.subTest(key=key, value=value):
                    config.write_text(original.replace("timeout_s = 1", f"timeout_s = 1\n{key} = {value}"))
                    with self.assertRaisesRegex(ValueError, f"{key} must be a positive integer"):
                        load_engines(config)

    def test_fixer_and_reviewer_require_distinct_engines(self):
        config = self.configuration()
        implementer = config.read_text()
        reviewer = (implementer.replace("engines.external", "engines.review")
                    .replace("worker:test", "reviewer:test"))
        config.write_text(implementer + reviewer + """
[roles]
implementer = "external"
fixer = "review"
reviewer = "review"
""")
        with self.assertRaisesRegex(ValueError, "different engine"):
            load_engines(config)

    def test_log_parser_ignores_invalid_external_message_text(self):
        transcript = self.root / "step.jsonl"
        transcript.write_text("\n".join(json.dumps({
            "type": "item.completed", "item": {"type": "agent_message", "text": value},
        }) for value in (None, "", 42, "valid")))
        step = devpack_logs.StepRecord("01", "01-implementer-external", "implementer", "external")
        devpack_logs.parse_stream_jsonl(transcript, devpack_logs.Redactor(), step)
        self.assertEqual([item.text for item in step.items], ["valid"])
        self.assertEqual(step.num_turns, 1)

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
