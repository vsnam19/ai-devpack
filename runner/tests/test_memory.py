"""Unit tests for bounded agent memory, CLI, secret rejection, agent_exec injection, and post-gate proposals."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

RUNNER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = RUNNER_DIR.parent
for _p in (str(REPO_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent_exec  # noqa: E402
import devpack_broker  # noqa: E402
import devpack_dispatch  # noqa: E402
import devpack_memory  # noqa: E402
import devpack_run  # noqa: E402
from runner import memory  # noqa: E402
from runner.tests.brief_fixtures import valid_brief  # noqa: E402


def _write_exec(path: Path, content: str) -> Path:
    """Write an executable Python script to ``path``."""
    path.write_text(content, encoding="utf-8")
    os.chmod(path, stat.S_IRWXU)
    return path


def _init_git_repo(workdir: Path) -> None:
    """Initialize a minimal clean C++ git worktree."""
    (workdir / "src").mkdir(parents=True, exist_ok=True)
    (workdir / "tests").mkdir(parents=True, exist_ok=True)
    (workdir / "preflight.toml").write_text('[general]\nstd = "c++17"\n', encoding="utf-8")
    (workdir / "src" / "mod.cpp").write_text(
        "/**\n * @file mod.cpp\n * @brief Module.\n */\n"
        "/**\n * @brief Value.\n * @return One.\n */\n"
        "int val() { return 1; }\n",
        encoding="utf-8",
    )
    (workdir / "tests" / "test_mod.cpp").write_text(
        "#include <cassert>\nvoid t() { assert(1 == 1); }\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=workdir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=workdir, check=True)


class MemoryStoreAndRetrievalTests(unittest.TestCase):
    """Tests for memory store CRUD, CLI, ranking, budget, supersede, expiry, scope, and secrets."""

    def test_store_cli_lifecycle_and_secret_rejection(self) -> None:
        """Verify CLI add/list/show/approve/supersede/expire/query and secret/PII write rejection."""
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp)
            mem_root = memory.memory_root_for(wt)

            # Secret and PII payloads must be rejected before writing anything to disk.
            fake_aws = "AKIA" + "IOSFODNN7EXAMPLE"
            fake_key = "sk-ant-" + "api03-secretvalue123456"
            fake_email = "alice.engineer@example-corp.com"
            fake_host = "/" + "ho" + "me/alice/project"
            for bad_body in (
                f"Use key {fake_aws}",
                f"Token: {fake_key}",
                f"Contact {fake_email}",
                f"Look in {fake_host}",
            ):
                with self.assertRaises(memory.SecretPiiRejectedError) as ctx:
                    memory.add_memory_entry(
                        mem_root,
                        entry_type="fact",
                        scope="project",
                        body=bad_body,
                        tags=["REQ-1"],
                    )
                self.assertNotIn(fake_aws, str(ctx.exception))
                self.assertNotIn(fake_key, str(ctx.exception))
            self.assertEqual([], memory.load_memory_entries(mem_root))

            # CLI add -> list -> show -> approve -> query --explain -> supersede -> expire
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = devpack_memory.main(
                    [
                        "--workdir",
                        str(wt),
                        "add",
                        "--id",
                        "mem-001",
                        "--type",
                        "decision",
                        "--scope",
                        "project",
                        "--tags",
                        "REQ-LINK-005,link_state_machine",
                        "--body",
                        "Initial backoff is 3 ticks.",
                        "--status",
                        "pending",
                    ]
                )
            self.assertEqual(0, rc)
            self.assertFalse((Path.cwd() / ".devpack" / "memory" / "project" / "mem-001.md").exists())
            # Pending entries must never be retrieved before approve
            q_pending = memory.retrieve_memory(mem_root, "Fix REQ-LINK-005 in link_state_machine")
            self.assertEqual([], q_pending.ids)

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, devpack_memory.main(["--workdir", str(wt), "approve", "mem-001"]))
            q_active = memory.retrieve_memory(mem_root, "Fix REQ-LINK-005 in link_state_machine")
            self.assertEqual(["mem-001"], q_active.ids)

            q_out = io.StringIO()
            with contextlib.redirect_stdout(q_out):
                rc_q = devpack_memory.main(
                    [
                        "--workdir",
                        str(wt),
                        "query",
                        "--task",
                        "Fix REQ-LINK-005 in link_state_machine",
                        "--explain",
                    ]
                )
            self.assertEqual(0, rc_q)
            self.assertIn("INCLUDED", q_out.getvalue())
            self.assertIn("mem-001", q_out.getvalue())

            # Supersede mem-001 with mem-002
            with contextlib.redirect_stdout(io.StringIO()):
                rc_s = devpack_memory.main(
                    [
                        "--workdir",
                        str(wt),
                        "add",
                        "--id",
                        "mem-002",
                        "--type",
                        "decision",
                        "--scope",
                        "module:src/link_state_machine.cpp",
                        "--tags",
                        "REQ-LINK-005,link_state_machine",
                        "--supersedes",
                        "mem-001",
                        "--body",
                        "Updated backoff is 7 ticks.",
                    ]
                )
            self.assertEqual(0, rc_s)
            self.assertEqual("superseded", memory.find_memory_entry(mem_root, "mem-001").status)
            q_sup = memory.retrieve_memory(mem_root, "Fix REQ-LINK-005 in link_state_machine")
            self.assertEqual(["mem-002"], q_sup.ids)

            # Expiry handling
            memory.add_memory_entry(
                mem_root,
                entry_id="mem-exp",
                entry_type="handoff",
                scope="project",
                tags=["REQ-LINK-005"],
                expires="2020-01-01",
                body="Expired handoff.",
            )
            q_exp = memory.retrieve_memory(mem_root, "Fix REQ-LINK-005")
            self.assertNotIn("mem-exp", q_exp.ids)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, devpack_memory.main(["--workdir", str(wt), "expire", "--expired"]))
            self.assertEqual("expired", memory.find_memory_entry(mem_root, "mem-exp").status)

            # List & Show
            l_out = io.StringIO()
            with contextlib.redirect_stdout(l_out):
                self.assertEqual(0, devpack_memory.main(["--workdir", str(wt), "list"]))
            self.assertIn("mem-002", l_out.getvalue())
            s_out = io.StringIO()
            with contextlib.redirect_stdout(s_out):
                self.assertEqual(0, devpack_memory.main(["--workdir", str(wt), "show", "mem-002"]))
            self.assertIn("Updated backoff is 7 ticks.", s_out.getvalue())

    def test_ranking_budget_top_k_and_scope_isolation(self) -> None:
        """Verify specificity+recency ranking, hard budget/top-k bounds, and role/engine scope isolation."""
        with tempfile.TemporaryDirectory() as tmp:
            mem_root = memory.memory_root_for(Path(tmp))

            # Unapproved pending entry must NOT supersede active mem-base
            memory.add_memory_entry(
                mem_root,
                entry_id="mem-base",
                entry_type="decision",
                scope="project",
                tags=["REQ-DTC-002"],
                created="2026-11-20T00:00:00Z",
                body="Base decision.",
            )
            memory.add_memory_entry(
                mem_root,
                entry_id="mem-pending-sup",
                entry_type="decision",
                scope="project",
                tags=["REQ-DTC-002"],
                supersedes=["mem-base"],
                status="pending",
                body="Unapproved override.",
            )
            memory.add_memory_entry(
                mem_root,
                entry_id="mem-mod",
                entry_type="fact",
                scope="module:src/dtc_debouncer.cpp",
                tags=["REQ-DTC-002", "dtc_debouncer"],
                created="2026-11-21T00:00:00Z",
                body="Module specific fact.",
            )
            memory.add_memory_entry(
                mem_root,
                entry_id="mem-handoff",
                entry_type="handoff",
                scope="project",
                tags=["REQ-DTC-002"],
                created="2026-11-22T00:00:00Z",
                body="Implementer handoff summary.",
            )
            memory.add_memory_entry(
                mem_root,
                entry_id="mem-rev-only",
                entry_type="fact",
                scope="role:reviewer",
                tags=["REQ-DTC-002"],
                created="2026-11-22T01:00:00Z",
                body="Reviewer checklist note.",
            )
            memory.add_memory_entry(
                mem_root,
                entry_id="mem-local-only",
                entry_type="fact",
                scope="engine:local",
                tags=["REQ-DTC-002"],
                created="2026-11-22T02:00:00Z",
                body="Local model hint.",
            )
            memory.add_memory_entry(
                mem_root,
                entry_id="mem-distractor",
                entry_type="fact",
                scope="project",
                tags=["REQ-CODEC-999", "unrelated_mod"],
                body="Unrelated distractor.",
            )

            # Implementer on haiku gets module > project, includes handoff, excludes role:reviewer & engine:local & distractor
            res_impl = memory.retrieve_memory(
                mem_root,
                "Update src/dtc_debouncer.cpp for REQ-DTC-002",
                role="implementer",
                engine="haiku",
                top_k=2,
                budget_tokens=800,
            )
            self.assertEqual(["mem-mod", "mem-handoff"], res_impl.ids)
            self.assertIn(memory.INJECTED_MEMORY_HEADING, res_impl.formatted_prompt)
            self.assertIn("source:", res_impl.formatted_prompt)
            self.assertIn("take precedence on conflict", res_impl.formatted_prompt)
            self.assertLessEqual(res_impl.total_tokens, 800)

            # Reviewer on sonnet does NOT get implementer handoff unless reviewer_sees_handoffs=True
            res_rev = memory.retrieve_memory(
                mem_root,
                "Review src/dtc_debouncer.cpp for REQ-DTC-002",
                role="reviewer",
                engine="sonnet",
            )
            self.assertIn("mem-rev-only", res_rev.ids)
            self.assertNotIn("mem-handoff", res_rev.ids)
            self.assertNotIn("mem-local-only", res_rev.ids)

            res_rev_handoff = memory.retrieve_memory(
                mem_root,
                "Review src/dtc_debouncer.cpp for REQ-DTC-002",
                role="reviewer",
                engine="sonnet",
                reviewer_sees_handoffs=True,
            )
            self.assertIn("mem-handoff", res_rev_handoff.ids)

            # Engine scope visibility
            res_local = memory.retrieve_memory(
                mem_root,
                "Update src/dtc_debouncer.cpp for REQ-DTC-002",
                role="implementer",
                engine="local",
            )
            self.assertIn("mem-local-only", res_local.ids)

            # Tight token budget drops entries that don't fit
            res_tight = memory.retrieve_memory(
                mem_root,
                "Update src/dtc_debouncer.cpp for REQ-DTC-002",
                role="implementer",
                engine="haiku",
                budget_tokens=120,
            )
            self.assertLessEqual(res_tight.total_tokens, 120)
            self.assertLessEqual(len(res_tight.ids), 1)

    def test_proposal_parsing_and_post_gate_processing(self) -> None:
        """Verify markdown & JSON proposal parsing, same-task handoff/known-gap auto-accept, and decision pending."""
        text = (
            "Done implementing.\n\n"
            "## Memory proposals\n"
            "- type: handoff\n"
            "  scope: task:task-001\n"
            "  tags: REQ-1, mod\n"
            "  summary: Left invariant intact.\n"
            "- type: handoff\n"
            "  scope: module:src/mod.cpp\n"
            "  tags: REQ-1, mod\n"
            "  summary: Non-task scope must remain pending.\n"
            "- type: decision\n"
            "  scope: project\n"
            "  tags: REQ-1\n"
            "  summary: Always clamp inputs.\n"
        )
        stop_text = (
            "# STOP\n\n"
            "## Memory proposals\n"
            "```json\n"
            '[{"type": "known-gap", "scope": "task:task-001", "tags": ["REQ-2"], "summary": "Missing hardware timer mock."}]\n'
            "```\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            mem_root = memory.memory_root_for(Path(tmp))
            res = memory.process_memory_proposals(
                mem_root,
                text,
                source="run:test-run-1",
                task_id="task-001",
                stop_report_text=stop_text,
            )
            self.assertEqual(2, len(res.accepted))
            self.assertEqual({"handoff", "known-gap"}, {e.entry_type for e in res.accepted})
            self.assertTrue(all(e.status == "active" for e in res.accepted))
            self.assertEqual(2, len(res.pending))
            self.assertTrue(all(e.status == "pending" for e in res.pending))
            decision_entries = [e for e in res.pending if e.entry_type == "decision"]
            self.assertEqual(1, len(decision_entries))
            self.assertEqual("decision", decision_entries[0].entry_type)
            self.assertEqual("pending", decision_entries[0].status)

            # Empty/None proposal section yields 0 proposals
            self.assertEqual([], memory.parse_memory_proposals("## Memory proposals\nNone.\n"))


class MemorySupersedeGuardTests(unittest.TestCase):
    """Tests that agent proposals never retire human-approved decision/fact entries before approval."""

    def _seed(self, mem_root: Path, entry_id: str, entry_type: str, body: str) -> None:
        """Persist an active entry as if a human had approved it."""
        memory.add_memory_entry(
            mem_root,
            entry_type=entry_type,
            body=body,
            entry_id=entry_id,
            status="active",
            source="human",
        )

    def test_handoff_superseding_decision_or_fact_waits_for_approval(self) -> None:
        """A handoff superseding an active decision/fact is stored pending; the target changes only on approve."""
        for target_type in ("decision", "fact"):
            with self.subTest(target_type=target_type), tempfile.TemporaryDirectory() as tmp:
                mem_root = memory.memory_root_for(Path(tmp))
                target_id = f"{target_type}-clamp"
                self._seed(mem_root, target_id, target_type, "Clamp inputs to 0..255.")
                text = (
                    "## Memory proposals\n"
                    "- type: handoff\n"
                    "  id: mem-handoff-clamp\n"
                    "  scope: project\n"
                    "  tags: REQ-1\n"
                    f"  supersedes: {target_id}\n"
                    "  summary: Clamp is now 0..100.\n"
                )
                res = memory.process_memory_proposals(mem_root, text, source="run:test-run-2")
                self.assertEqual([], res.rejected)
                self.assertEqual([], res.accepted)
                self.assertEqual(["mem-handoff-clamp"], [e.id for e in res.pending])
                self.assertEqual("pending", memory.find_memory_entry(mem_root, "mem-handoff-clamp").status)
                self.assertEqual("active", memory.find_memory_entry(mem_root, target_id).status)

                memory.approve_memory_entry(mem_root, "mem-handoff-clamp")
                self.assertEqual("active", memory.find_memory_entry(mem_root, "mem-handoff-clamp").status)
                self.assertEqual("superseded", memory.find_memory_entry(mem_root, target_id).status)

    def test_handoff_superseding_handoff_is_auto_accepted(self) -> None:
        """A same-task handoff superseding only a handoff keeps auto-accept and retires the old handoff."""
        with tempfile.TemporaryDirectory() as tmp:
            mem_root = memory.memory_root_for(Path(tmp))
            self._seed(mem_root, "mem-handoff-old", "handoff", "Old progress note.")
            text = (
                "## Memory proposals\n"
                "- type: handoff\n"
                "  id: mem-handoff-new\n"
                "  scope: task:task-sup-3\n"
                "  tags: REQ-1\n"
                "  supersedes: mem-handoff-old\n"
                "  summary: Newer progress note.\n"
            )
            res = memory.process_memory_proposals(
                mem_root, text, source="run:test-run-3", task_id="task-sup-3"
            )
            self.assertEqual([], res.rejected)
            self.assertEqual(["mem-handoff-new"], [e.id for e in res.accepted])
            self.assertEqual([], res.pending)
            self.assertEqual("active", memory.find_memory_entry(mem_root, "mem-handoff-new").status)
            self.assertEqual("superseded", memory.find_memory_entry(mem_root, "mem-handoff-old").status)

    def test_supersedes_unknown_id_rejects_proposal(self) -> None:
        """A supersedes id that is not in the store rejects the proposal with a clear reason and writes nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            mem_root = memory.memory_root_for(Path(tmp))
            text = (
                "## Memory proposals\n"
                "- type: handoff\n"
                "  id: mem-handoff-orphan\n"
                "  scope: project\n"
                "  tags: REQ-1\n"
                "  supersedes: mem-does-not-exist\n"
                "  summary: Refers to a missing entry.\n"
            )
            res = memory.process_memory_proposals(mem_root, text, source="run:test-run-4")
            self.assertEqual([], res.accepted)
            self.assertEqual([], res.pending)
            self.assertEqual(1, len(res.rejected))
            reason = res.rejected[0]["reason"]
            self.assertIn("unknown memory id", reason)
            self.assertIn("mem-does-not-exist", reason)
            self.assertEqual([], memory.load_memory_entries(mem_root))


class MemoryRunnerIntegrationTests(unittest.TestCase):
    """Tests for agent_exec memory injection meta, dispatch, devpack_run, and broker."""

    def test_agent_exec_injection_meta_and_engines_config(self) -> None:
        """Verify engines.toml [memory] parsing, prompt injection, and ids+hashes in step meta."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            wt.mkdir()
            engines_toml = base / "engines.toml"
            engines_toml.write_text(
                "version = 2\n"
                "[engines.haiku]\n"
                'model = "haiku"\n'
                'class = "cloud"\n'
                "[memory]\n"
                "enabled = true\n"
                "top_k = 2\n"
                "budget_tokens = 500\n",
                encoding="utf-8",
            )
            cfg = agent_exec.load_engines(engines_toml)
            self.assertTrue(cfg.memory.enabled)
            self.assertEqual(2, cfg.memory.top_k)
            self.assertEqual(500, cfg.memory.budget_tokens)

            mem_root = memory.memory_root_for(wt)
            entry = memory.add_memory_entry(
                mem_root,
                entry_id="mem-exec-1",
                entry_type="fact",
                scope="project",
                tags=["REQ-EXEC-1", "mod.cpp"],
                body="Return code must be 42.",
            )

            cred = base / "creds.json"
            cred.write_text('{"accessToken":"fake-cred-tok"}', encoding="utf-8")
            fake_claude = _write_exec(
                base / "fake_claude.py",
                "#!/usr/bin/env python3\n"
                "import json\n"
                "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
                "'total_cost_usd':0.01,'num_turns':1,'duration_ms':10,"
                "'usage':{'input_tokens':10,'output_tokens':5},'result':'OK'}))\n",
            )
            run_dir = agent_exec.new_run_dir(base / "runs", kind="task", task="mem-test")
            req = agent_exec.AgentRequest(
                engine=cfg.engines["haiku"],
                prompt="Update mod.cpp for REQ-EXEC-1",
                workdir=wt,
                run_dir=run_dir,
                isolation="local",
                allow_unsandboxed_local=True,
                credentials_path=cred,
                agent_bin=str(fake_claude),
                role="implementer",
                memory_config=cfg.memory,
            )
            res = agent_exec.run_agent(req)
            self.assertEqual("ok", res.status)
            self.assertEqual(["mem-exec-1"], res.memory_ids)
            expected_sha = hashlib.sha256(entry.path.read_bytes()).hexdigest()
            self.assertEqual({"mem-exec-1": expected_sha}, res.memory_hashes)
            self.assertGreater(res.memory_tokens, 0)

            prompt_saved = (run_dir / "steps" / "01-implementer-haiku.prompt.md").read_text(encoding="utf-8")
            self.assertIn(memory.INJECTED_MEMORY_HEADING, prompt_saved)
            self.assertIn("mem-exec-1", prompt_saved)

            meta_saved = json.loads((run_dir / "steps" / "01-implementer-haiku.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(["mem-exec-1"], meta_saved["memory_ids"])
            self.assertEqual({"mem-exec-1": expected_sha}, meta_saved["memory_hashes"])
            self.assertEqual(res.memory_tokens, meta_saved["memory_tokens"])

            # Disabled memory injects nothing
            run_dir2 = agent_exec.new_run_dir(base / "runs", kind="task", task="mem-off")
            req_off = agent_exec.AgentRequest(
                engine=cfg.engines["haiku"],
                prompt="Update mod.cpp for REQ-EXEC-1",
                workdir=wt,
                run_dir=run_dir2,
                isolation="local",
                allow_unsandboxed_local=True,
                credentials_path=cred,
                agent_bin=str(fake_claude),
                role="implementer",
                memory_enabled=False,
            )
            res_off = agent_exec.run_agent(req_off)
            self.assertEqual([], res_off.memory_ids)
            self.assertEqual(0, res_off.memory_tokens)

    def test_dispatch_gate_gates_proposals_and_broker_isolates_dotdevpack(self) -> None:
        """Verify dispatch only accepts proposals when gate passes, and broker keeps work/.devpack absent."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _init_git_repo(wt)
            (wt / ".gitignore").write_text(".devpack/\nbriefs/\nruns/\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore"], cwd=wt, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "gitignore"], cwd=wt, check=True)
            (wt / "briefs").mkdir()
            brief = wt / "briefs" / "job.md"
            brief.write_text(valid_brief("Update src/mod.cpp for REQ-MOD-1.", ["src/mod.cpp"]), encoding="utf-8")

            cred = base / "creds.json"
            cred.write_text('{"accessToken":"fake-cred-tok"}', encoding="utf-8")

            fake_claude = _write_exec(
                base / "fake_claude.py",
                "#!/usr/bin/env python3\n"
                "import json, pathlib\n"
                "assert not pathlib.Path('.devpack').exists()\n"
                "pathlib.Path('src/mod.cpp').write_text("
                "'/**\\n * @file mod.cpp\\n * @brief Module.\\n */\\n"
                "/**\\n * @brief Value.\\n * @return Two.\\n */\\n"
                "int val() { return 2; }\\n', encoding='utf-8')\n"
                "final_txt = ('Done.\\n\\n## Memory proposals\\n'\n"
                "             '- type: handoff\\n  scope: task:job\\n  tags: REQ-MOD-1\\n'\n"
                "             '  summary: Updated val() to 2.\\n'\n"
                "             '- type: decision\\n  scope: project\\n  tags: REQ-MOD-1\\n'\n"
                "             '  summary: Keep val() positive.\\n')\n"
                "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
                "'total_cost_usd':0.01,'num_turns':1,'duration_ms':10,"
                "'usage':{'input_tokens':10,'output_tokens':5},'result':final_txt}))\n",
            )
            fail_gate = _write_exec(base / "fail_gate.py", "#!/usr/bin/env python3\nimport sys; sys.exit(1)\n")
            ok_gate = _write_exec(base / "ok_gate.py", "#!/usr/bin/env python3\nprint('GATE_OK')\n")

            # Direct dispatch with failing gate must NOT store proposals
            with contextlib.redirect_stdout(io.StringIO()):
                rc_fail, _ = devpack_dispatch.dispatch_job(
                    engine_name="haiku",
                    brief_path=brief,
                    workdir=wt,
                    allow_globs=["src/mod.cpp"],
                    gate_cmd=f"python3 {fail_gate}",
                    runs_root=base / "runs",
                    isolation="local",
                    allow_unsandboxed_local=True,
                    credentials_path=cred,
                    agent_bin=str(fake_claude),
                    no_checklist=True,
                    mode="autonomous",
                )
            self.assertEqual(1, rc_fail)
            self.assertEqual([], memory.load_memory_entries(memory.memory_root_for(wt)))

            # Reset worktree and test via Broker with passing gate
            subprocess.run(["git", "checkout", "--", "src/mod.cpp"], cwd=wt, check=True)
            (wt / ".devpack").mkdir(exist_ok=True)
            (wt / ".devpack" / "project.toml").write_text('[pipeline]\nmode = "autonomous"\n', encoding="utf-8")
            broker = devpack_broker.Broker(
                wt,
                base / "broker_runs",
                credentials_path=cred,
                isolation="local",
                allow_unsandboxed_local=True,
                agent_bin=str(fake_claude),
                gate_cmd=f"python3 {ok_gate}",
            )
            try:
                code, payload = broker.execute(
                    {"engine": "haiku", "brief": "briefs/job.md", "allow": ["src/mod.cpp"]}
                )
            finally:
                broker.close()
            self.assertEqual(0, code, payload)
            entries = memory.load_memory_entries(memory.memory_root_for(wt))
            self.assertEqual(2, len(entries))
            by_type = {e.entry_type: e for e in entries}
            self.assertEqual("active", by_type["handoff"].status)
            self.assertEqual("pending", by_type["decision"].status)


if __name__ == "__main__":
    unittest.main()
