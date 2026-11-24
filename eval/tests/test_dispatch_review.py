"""Offline regression coverage for host orchestration and mixed engine billing."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from eval import run_eval
from eval.tests.test_run_eval import _create_mini_fixture_and_task
from runner import agent_exec, devpack_run
from runner.tests.brief_fixtures import valid_brief


class DispatchReviewTests(unittest.TestCase):
    """Exercise the reviewed boundaries with synthetic agents only."""

    def test_mixed_runner_cost_excludes_local_steps(self):
        """Local telemetry stays separate even when the reviewer is cloud."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {"roles": {
                "implementer": devpack_run.EngineConfig(name="local", model="local", engine_class="local"),
                "reviewer": devpack_run.EngineConfig(name="sonnet", model="sonnet", engine_class="cloud"),
                "fixer": devpack_run.EngineConfig(name="local", model="local", engine_class="local"),
            }, "gate_cmd": ["true"]}
            context = devpack_run.load_task_data(None, "mixed", "Mixed", "Test", "result.txt")
            results = [
                agent_exec.AgentResult(status="ok", exit_code=0, cost_usd=99, tokens_in=10, tokens_out=5, duration_s=3),
                agent_exec.AgentResult(status="ok", exit_code=0, final_text="VERDICT: APPROVE", cost_usd=0.25, tokens_in=20, tokens_out=7, duration_s=2),
            ]
            with mock.patch.object(devpack_run, "run_agent", side_effect=results):
                rc, summary = devpack_run.run_task_workflow(
                    config, context, root, root / "logs", root,
                    isolation="local", allow_unsandboxed_local=True,
                    mode="autonomous",
                )
            self.assertEqual(0, rc)
            self.assertEqual(0.25, summary["cost_usd"])
            self.assertEqual(15, summary["local_tokens"])
            self.assertEqual(3, summary["local_wall_s"])
            self.assertEqual(99, results[0].cost_usd)

    def test_mixed_eval_preserves_cloud_cost_and_local_usage(self):
        """Runner eval summaries report the two engine classes independently."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture, tasks_dir = _create_mini_fixture_and_task(root)
            task = run_eval.load_and_validate_tasks(tasks_dir, "all")[0]
            local = agent_exec.resolve_engine("local")
            cloud = agent_exec.resolve_engine("sonnet")
            summary = {"cost_usd": 0.25, "local_tokens": 15, "local_wall_s": 3,
                       "duration_s": 5, "verdict": "APPROVE"}
            with mock.patch.object(run_eval.devpack_run, "run_task_workflow", return_value=(0, summary)):
                result = run_eval.run_runner_trial(
                    task, "local:sonnet", local, cloud, local, 1, root / "trials",
                    fixture, None, "local", "unused", 30, allow_unsandboxed_local=True,
                )
            self.assertFalse(result.harness_error, result.error)
            self.assertEqual(0.25, result.cost_usd)
            self.assertEqual(15, result.local_tokens)
            self.assertEqual(3, result.local_wall_s)
            tasks, categories = run_eval.aggregate_results([task], ["local:sonnet"], 1, [result])
            self.assertEqual(0.25, tasks[0].mean_cost_usd)
            self.assertEqual(15, tasks[0].mean_local_tokens)
            self.assertEqual(3, categories[0]["mean_local_wall_s"])

    def test_orchestrator_requires_explicit_host_opt_in(self):
        """Project defaults cannot authorize the host orchestrator session."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            goal = run_eval.OrchestratorGoalSpec("mini", "feature", "smoke", root, root / "goal", root / "check")
            for isolation, opt_in in (("docker", True), ("local", False)):
                with self.subTest(isolation=isolation):
                    with self.assertRaisesRegex(ValueError, "--isolation local --allow-unsandboxed-local"):
                        run_eval.run_orchestrator_trial(
                            goal, "sonnet->haiku", agent_exec.resolve_engine("sonnet"), "haiku",
                            1, root / "trials", root, None, isolation, "unused", 30,
                            allow_unsandboxed_local=opt_in,
                        )

    def test_orchestrator_dispatch_end_to_end(self):
        """A fake host orchestrator dispatches a Docker worker through the allowlist."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = root / "fixture"
            fixture.mkdir()
            (fixture / "result.txt").write_text("before")
            goal_md = root / "goal.md"
            goal_md.write_text("Update result.txt using dispatch. The brief is already provided.")
            check = root / "check.sh"
            check.write_text('#!/bin/bash\nset -euo pipefail\ntest "$(cat "$2/result.txt")" = after\n')
            goal = run_eval.OrchestratorGoalSpec("mini", "feature", "smoke", root, goal_md, check)
            worker = root / "worker"
            worker.write_text("#!/usr/bin/env python3\nimport json,os,pathlib\nassert not pathlib.Path('.devpack').exists()\nassert 'broker-auth-token' in (pathlib.Path(os.environ['CLAUDE_CONFIG_DIR']) / '.credentials.json').read_text()\npathlib.Path('result.txt').write_text('after')\nprint(json.dumps({'type':'result','result':'done'}))\n")
            worker.chmod(0o755)
            docker = root / "docker"
            docker.write_text(
                "#!/usr/bin/env python3\nimport os,sys\na=sys.argv\nassert a[1]=='run'\n"
                "mount=a[a.index('-v')+1].split(':')[0]\nos.chdir(mount)\n"
                "mounts=[a[i+1] for i,x in enumerate(a) if x=='-v']\n"
                "cfg=next(x.split(':')[0] for x in mounts if ':/workspace/cfg:' in x)\n"
                "assert any(':/workspace/task/.git:ro' in x for x in mounts)\n"
                "os.environ['CLAUDE_CONFIG_DIR']=cfg\n"
                f"os.execv({str(worker)!r}, [{str(worker)!r}])\n"
            )
            docker.chmod(0o755)
            orchestrator = root / "claude"
            orchestrator.write_text(
                "#!/usr/bin/env python3\nimport fnmatch,json,os,pathlib,subprocess,sys\n"
                "assert not any(k=='DEVPACK_CLAUDE_CREDENTIALS' or k.startswith('DEVPACK_LOCAL_') for k in os.environ)\n"
                "p=pathlib.Path\ns=json.loads(p('.claude/settings.json').read_text())\n"
                "cmd='python3 .devpack/pack/runner/devpack_dispatch.py --submit --engine haiku --brief briefs/job.md --workdir . --allow result.txt --gate-cmd true'\n"
                "assert any((cmd == x[5:-3] or cmd.startswith(x[5:-3]+' ')) if x.endswith(':*)') else fnmatch.fnmatchcase(cmd,x[5:-1]) for x in s['permissions']['allow'] if x.startswith('Bash('))\n"
                "allowed=sys.argv[sys.argv.index('--allowedTools')+1:sys.argv.index('--disallowedTools')]\n"
                "assert 'Bash' not in allowed\nassert s['permissions']['allow']==allowed\n"
                "prompt=sys.argv[-1]\nassert prompt.startswith('## Your tools\\n')\nassert 'Write/Edit only under `briefs/`' in prompt\nassert 'You never build, test or lint yourself' in prompt\nassert 'python3 .devpack/pack/runner/devpack_dispatch.py' in prompt\n"
                "p('briefs/job.md').write_text(" + repr(valid_brief('Update result.txt', ['result.txt'])) + ")\n"
                "r=subprocess.run(cmd.split(),capture_output=True,text=True)\nassert r.returncode==0,(r.stdout,r.stderr)\n"
                "payload=json.loads(r.stdout)\nassert payload['status']=='ok'\n"
                "print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Bash','input':{'command':cmd}}]}}))\n"
                "print(json.dumps({'type':'result','result':'done'}))\n"
            )
            orchestrator.chmod(0o755)
            credentials = root / "credentials.json"
            credentials.write_text('{"accessToken":"broker-auth-token"}')
            with mock.patch.dict(os.environ, {"PATH": str(root) + os.pathsep + os.environ["PATH"],
                                             "DEVPACK_CLAUDE_CREDENTIALS": str(credentials),
                                             "DEVPACK_LOCAL_AUTH_TOKEN": "broker-local-token"}):
                result = run_eval.run_orchestrator_trial(
                    goal=goal, orch_engine=agent_exec.resolve_engine("sonnet"), orch_label="sonnet->haiku",
                    worker_engines_str="haiku", run_index=1, run_root=root / "trials",
                    fixture_dir=fixture, credentials_path=credentials, isolation="local",
                    agent_bin=str(orchestrator), timeout_s=30, allow_unsandboxed_local=True,
                )
            self.assertTrue(result.passed, (result.error, result.stderr_tail, result.check_output))
            self.assertEqual({"count": 0, "commands": []}, result.denied_tool_calls)
            self.assertEqual(1, result.dispatch_calls)
            self.assertEqual(0, result.orchestrator_product_edits)
            transcript = next((root / 'trials').rglob('01-orchestrator-*.jsonl')).read_text()
            self.assertNotIn('"name": "Edit"', transcript)
            self.assertNotIn('"name": "Write"', transcript)
            work = root / "trials/mini__sonnet->haiku__run1/work"
            self.assertEqual("after", (work / "result.txt").read_text())
            self.assertEqual(0, (work / '.devpack/pack/runner/devpack_dispatch.py').stat().st_mode & 0o222)
            self.assertEqual(["runner/devpack_dispatch.py"], sorted(
                p.relative_to(work / '.devpack/pack').as_posix()
                for p in (work / '.devpack/pack').rglob('*') if p.is_file()))
            self.assertFalse((work / '.devpack/broker.json').exists())
            self.assertTrue(list((work / '.devpack/queue').glob('*.response.json')))
            runs = [json.loads(p.read_text()) for p in (root / 'trials').rglob('run.json')]
            self.assertTrue(any(r.get('sandbox_warning') for r in runs))
            self.assertTrue(any(r.get('isolation') == 'docker' for r in runs))
