#!/usr/bin/env python3
"""Tests for M1 operating modes (hitl / autonomous) and recorded checkpoints."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNER_DIR = REPO_ROOT / "runner"
for _p in (str(REPO_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from runner import (  # noqa: E402
    agent_exec,
    brief_lint,
    checkpoints,
    devpack_approve,
    devpack_broker,
    devpack_dispatch,
    devpack_memory,
    devpack_run,
    memory,
)


def _write_exec(path: Path, body: str) -> Path:
    """Create an executable script at ``path``."""
    path.write_text(body, encoding="utf-8")
    os.chmod(path, stat.S_IRWXU)
    return path


def _init_git_repo(workdir: Path, *, branch: str = "dev/test") -> None:
    """Initialize a clean git repo on ``branch`` with an initial commit."""
    for cmd in (
        ["git", "init", "-q", "-b", branch],
        ["git", "config", "user.email", "tester@example.invalid"],
        ["git", "config", "user.name", "Tester"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(cmd, cwd=workdir, check=True, capture_output=True)


def _valid_brief_text(rel_file: str = "src/mod.cpp") -> str:
    """Return a structured orchestrator brief that passes brief_lint cleanly."""
    return (
        "# Brief: Update module\n\n"
        "## Goal\n"
        f"Update `compute()` in `{rel_file}`.\n\n"
        "## Input\n"
        f"- `{rel_file}:1`\n\n"
        "## Expected output\n"
        "- Update return value cleanly.\n\n"
        "## Files allowed\n"
        f"- `{rel_file}`\n\n"
        "## Constraints\n"
        "- Follow preflight lint and doxygen conventions.\n\n"
        "## Gate\n"
        "- `true`\n"
    )


def _make_sample_repo(wt: Path, *, project_toml: str = "", branch: str = "dev/test") -> Path:
    """Create a minimal C++ git repo ready for dispatch/workflow runs."""
    (wt / "src").mkdir(parents=True, exist_ok=True)
    (wt / "tests").mkdir(parents=True, exist_ok=True)
    (wt / "briefs").mkdir(parents=True, exist_ok=True)
    (wt / "src" / "mod.cpp").write_text(
        "/**\n * @file mod.cpp\n * @brief Module.\n */\n"
        "/**\n * @brief Compute value.\n * @return Value.\n */\n"
        "int compute() { return 1; }\n",
        encoding="utf-8",
    )
    (wt / "tests" / "test_mod.cpp").write_text(
        "void testOne() {}\n",
        encoding="utf-8",
    )
    (wt / "preflight.toml").write_text(
        '[general]\nstd = "c++17"\n[doxygen]\nrequired_for = ["public_functions"]\n',
        encoding="utf-8",
    )
    (wt / ".gitignore").write_text(".devpack/\nbriefs/\nruns/\ngate_receipt.json\n", encoding="utf-8")
    if project_toml:
        (wt / ".devpack").mkdir(parents=True, exist_ok=True)
        (wt / ".devpack" / "project.toml").write_text(project_toml, encoding="utf-8")
    brief = wt / "briefs" / "job.md"
    brief.write_text(_valid_brief_text(), encoding="utf-8")
    _init_git_repo(wt, branch=branch)
    return brief


def _write_engines_v2(path: Path, *, gate_cmd: str = '["true"]', max_fix_rounds: int = 1) -> Path:
    """Write a minimal v2 engines.toml for testing devpack_run."""
    path.write_text(
        f'version = 2\ngate_cmd = {gate_cmd}\n'
        '[limits]\n'
        f'max_fix_rounds = {max_fix_rounds}\n'
        '[engines.haiku]\nmodel = "haiku"\nclass = "cloud"\n'
        '[roles]\nimplementer = "haiku"\nreviewer = "haiku"\nfixer = "haiku"\n',
        encoding="utf-8",
    )
    return path


class PipelineModesTests(unittest.TestCase):
    """Test suite for operating modes, checkpoints, approvals, resume, and tamper boundaries."""

    def test_default_mode_is_hitl_and_blocking_vs_terminal_semantics(self) -> None:
        """Missing [pipeline] defaults to hitl; brief stops (exit 20) and terminal checkpoints keep exit code."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _make_sample_repo(wt)

            cfg = checkpoints.load_pipeline_config(wt)
            self.assertEqual("hitl", cfg.mode)

            # Without pipeline config, dispatch_job defaults to hitl -> blocks at brief (exit 20)
            marker = base / "worker_called.txt"
            worker = _write_exec(
                base / "worker.py",
                "#!/usr/bin/env python3\nimport json, pathlib\n"
                f"pathlib.Path({str(marker)!r}).write_text('called')\n"
                "print(json.dumps({'type':'result','is_error':False,'result':'ok'}))\n",
            )
            state_dir = base / "state"
            with contextlib.redirect_stdout(io.StringIO()):
                rc, payload = devpack_dispatch.dispatch_job(
                    engine_name="haiku",
                    brief_path=wt / "briefs" / "job.md",
                    workdir=wt,
                    allow_globs=["src/**"],
                    gate_cmd="true",
                    runs_root=base / "runs",
                    isolation="local",
                    allow_unsandboxed_local=True,
                    agent_bin=str(worker),
                    state_dir=state_dir,
                )
            self.assertEqual(20, rc, payload)
            self.assertEqual(checkpoints.AWAITING_APPROVAL, payload["status"])
            self.assertFalse(marker.exists(), "Agent must not run while blocking checkpoint is PENDING")

            # Terminal checkpoint (pre-merge) in hitl writes pending record without altering exit 0
            engines_cfg_path = _write_engines_v2(base / "engines.toml")
            engines_cfg = devpack_run.load_engine_config(engines_cfg_path)
            ctx = devpack_run.load_task_data(None, "term-ok", "Terminal OK", "Goal", "src/mod.cpp")
            results = [
                agent_exec.AgentResult(status="ok", exit_code=0, final_text="impl ok"),
                agent_exec.AgentResult(status="ok", exit_code=0, final_text="VERDICT: APPROVE"),
            ]
            term_state = base / "term_state"
            with mock.patch.object(devpack_run, "run_agent", side_effect=results), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc_ok, summary_ok = devpack_run.run_task_workflow(
                    engines_cfg, ctx, wt, base / "logs_ok", REPO_ROOT,
                    isolation="local", allow_unsandboxed_local=True,
                    brief_approved=True, state_dir=term_state,
                    engines_config_path=engines_cfg_path,
                )
            self.assertEqual(0, rc_ok, summary_ok)
            self.assertEqual("hitl", summary_ok["pipeline_mode"])
            pending_pm = checkpoints.list_approvals(term_state, status="pending", checkpoint_id="pre-merge")
            self.assertEqual(1, len(pending_pm))
            self.assertEqual("terminal", pending_pm[0]["kind"])

            # Terminal checkpoint (escalation) preserves exit 1 on ESCALATE_TO_HUMAN
            fail_cfg_path = _write_engines_v2(base / "engines_fail.toml", gate_cmd='["false"]', max_fix_rounds=0)
            fail_cfg = devpack_run.load_engine_config(fail_cfg_path)
            esc_state = base / "esc_state"
            with mock.patch.object(devpack_run, "run_agent", return_value=agent_exec.AgentResult(status="ok", exit_code=0, final_text="impl")), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc_esc, summary_esc = devpack_run.run_task_workflow(
                    fail_cfg, ctx, wt, base / "logs_esc", REPO_ROOT,
                    isolation="local", allow_unsandboxed_local=True,
                    brief_approved=True, state_dir=esc_state,
                    engines_config_path=fail_cfg_path,
                )
            self.assertEqual(1, rc_esc, summary_esc)
            self.assertEqual(devpack_run.ESCALATE_TO_HUMAN, summary_esc["status"])
            pending_esc = checkpoints.list_approvals(esc_state, status="pending", checkpoint_id="escalation")
            self.assertEqual(1, len(pending_esc))
            self.assertTrue(pending_esc[0]["hard_stop"])

    def test_autonomous_brief_auto_approves_clean_and_holds_on_lint_error(self) -> None:
        """In autonomous mode, brief auto-approves on clean brief_lint and holds (exit 20) on error-severity lint findings."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _make_sample_repo(wt, project_toml='[pipeline]\nmode = "autonomous"\n')
            worker = _write_exec(
                base / "worker.py",
                "#!/usr/bin/env python3\nimport json\n"
                "print(json.dumps({'type':'result','is_error':False,'result':'done'}))\n",
            )
            state_dir = base / "state"

            # Clean orchestrator brief -> auto-approved with 00-brief-lint.json receipt
            clean_findings = brief_lint.lint_brief_text(
                (wt / "briefs" / "job.md").read_text(encoding="utf-8"),
                repo_root=wt,
                engine="haiku",
                author="orchestrator",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                rc_ok, payload_ok = devpack_dispatch.dispatch_job(
                    engine_name="haiku",
                    brief_path=wt / "briefs" / "job.md",
                    workdir=wt,
                    allow_globs=["src/**"],
                    gate_cmd="true",
                    runs_root=base / "runs",
                    isolation="local",
                    allow_unsandboxed_local=True,
                    agent_bin=str(worker),
                    state_dir=state_dir,
                    brief_lint_findings=clean_findings,
                )
            self.assertEqual(0, rc_ok, payload_ok)
            decided_briefs = checkpoints.list_approvals(state_dir, status="decided", checkpoint_id="brief")
            self.assertEqual(1, len(decided_briefs))
            self.assertEqual("autonomous-policy", decided_briefs[0]["decided_by"])
            self.assertEqual("APPROVED", decided_briefs[0]["status"])
            self.assertTrue(decided_briefs[0]["gate_receipts"])
            self.assertTrue((Path(payload_ok["run_dir"]) / "steps" / "00-brief-lint.json").is_file())

            # Bad brief with error-severity lint finding (author="orchestrator") -> PENDING (exit 20)
            bad_brief = wt / "briefs" / "bad.md"
            bad_brief.write_text("Missing required headings and references src/does_not_exist.cpp\n", encoding="utf-8")
            bad_findings = brief_lint.lint_brief_text(
                bad_brief.read_text(encoding="utf-8"),
                repo_root=wt,
                engine="haiku",
                author="orchestrator",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                rc_hold, payload_hold = devpack_dispatch.dispatch_job(
                    engine_name="haiku",
                    brief_path=bad_brief,
                    workdir=wt,
                    allow_globs=["src/**"],
                    gate_cmd="true",
                    runs_root=base / "runs",
                    isolation="local",
                    allow_unsandboxed_local=True,
                    agent_bin=str(worker),
                    state_dir=state_dir,
                    brief_lint_findings=bad_findings,
                )
            self.assertEqual(20, rc_hold, payload_hold)
            self.assertEqual(checkpoints.AWAITING_APPROVAL, payload_hold["status"])
            pending_briefs = checkpoints.list_approvals(state_dir, status="pending", checkpoint_id="brief")
            self.assertEqual(1, len(pending_briefs))

    def test_hard_stops_and_gate_evidence_rules_in_autonomous_mode(self) -> None:
        """Hard-stops stay PENDING even in autonomous mode; non-hard-stops require green gate evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _make_sample_repo(wt, project_toml='[pipeline]\nmode = "autonomous"\n', branch="main")
            state_dir = base / "state"
            green_receipt = base / "gate.json"
            green_receipt.write_text(json.dumps({"gate_status": "GATE_OK"}) + "\n", encoding="utf-8")
            red_receipt = base / "gate_red.json"
            red_receipt.write_text(json.dumps({"gate_status": "GATE_FAIL"}) + "\n", encoding="utf-8")

            # Always hard-stops
            for cid in ("escalation", "memory-decision"):
                d = checkpoints.request(cid, f"subj-{cid}", [green_receipt], "summary", mode="autonomous", state_dir=state_dir)
                self.assertTrue(d.pending, cid)
                self.assertTrue(d.hard_stop, cid)

            # Conditional hard-stops: protected branch pre-merge, ASIL-B deviation, security deviation, release selinux
            d_pm_prot = checkpoints.request("pre-merge", "main-merge", [green_receipt], "merge", mode="autonomous", state_dir=state_dir, metadata={"protected_branch": True, "branch": "main"})
            self.assertTrue(d_pm_prot.pending)
            self.assertTrue(d_pm_prot.hard_stop)

            d_dev_asil = checkpoints.register_deviation("DEV-01", "safety dev", [green_receipt], safety_relevant=True, asil="B", mode="autonomous", state_dir=state_dir)
            self.assertTrue(d_dev_asil.pending)
            self.assertTrue(d_dev_asil.hard_stop)

            d_dev_sec = checkpoints.register_deviation("DEV-02", "sec dev", [green_receipt], security_relevant=True, mode="autonomous", state_dir=state_dir)
            self.assertTrue(d_dev_sec.pending)
            self.assertTrue(d_dev_sec.hard_stop)

            d_sel_rel = checkpoints.request("selinux-manifest", "sepolicy", [green_receipt], "release", mode="autonomous", state_dir=state_dir, metadata={"release_build": True})
            self.assertTrue(d_sel_rel.pending)
            self.assertTrue(d_sel_rel.hard_stop)

            # Non-hard-stop variants with green evidence auto-approve; red/missing hold PENDING
            d_pm_feat = checkpoints.request("pre-merge", "feat-merge", [green_receipt], "feature", mode="autonomous", state_dir=state_dir, metadata={"protected_branch": False, "branch": "feat/x"})
            self.assertTrue(d_pm_feat.approved)
            self.assertEqual("autonomous-policy", d_pm_feat.decided_by)

            d_dev_qm = checkpoints.register_deviation("DEV-03", "qm dev", [green_receipt], safety_relevant=False, security_relevant=False, asil="QM", mode="autonomous", state_dir=state_dir)
            self.assertTrue(d_dev_qm.approved)

            d_sel_dev = checkpoints.request("selinux-manifest", "sepolicy-dev", [green_receipt], "dev", mode="autonomous", state_dir=state_dir, metadata={"release_build": False, "build_type": "eng"})
            self.assertTrue(d_sel_dev.approved)

            d_plan_red = checkpoints.request("plan", "plan-red", [red_receipt], "plan red", mode="autonomous", state_dir=state_dir)
            self.assertTrue(d_plan_red.pending)

            d_plan_no_ev = checkpoints.request("plan", "plan-empty", [], "no evidence", mode="autonomous", state_dir=state_dir)
            self.assertTrue(d_plan_no_ev.pending)

    def test_config_validation_and_tightening_only_rules(self) -> None:
        """Hard-stop auto overrides and auto-in-hitl are rejected; CLI/env can only tighten autonomous->hitl."""
        with self.assertRaisesRegex(ValueError, "hard-stop"):
            checkpoints.validate_pipeline_dict({"mode": "autonomous", "checkpoints": {"escalation": "auto"}})
        with self.assertRaisesRegex(ValueError, "hard-stop"):
            checkpoints.validate_pipeline_dict({"mode": "autonomous", "checkpoints": {"memory-decision": "auto"}})
        with self.assertRaisesRegex(ValueError, "hitl"):
            checkpoints.validate_pipeline_dict({"mode": "hitl", "checkpoints": {"brief": "auto"}})

        # Valid autonomous tightening for brief -> human works
        valid = checkpoints.validate_pipeline_dict({"mode": "autonomous", "checkpoints": {"brief": "human"}})
        self.assertEqual("human", valid["checkpoints"]["brief"])

        # CLI / env tightening-only
        self.assertEqual("hitl", checkpoints.resolve_pipeline_mode("autonomous", cli_mode="hitl", env={}))
        self.assertEqual("hitl", checkpoints.resolve_pipeline_mode("autonomous", cli_mode=None, env={"DEVPACK_MODE": "hitl"}))
        with self.assertRaisesRegex(ValueError, "loosen"):
            checkpoints.resolve_pipeline_mode("hitl", cli_mode="autonomous", env={})
        with self.assertRaisesRegex(ValueError, "loosen"):
            checkpoints.resolve_pipeline_mode("hitl", cli_mode=None, env={"DEVPACK_MODE": "autonomous"})

        # CLI devpack_run refuses --mode autonomous against hitl repo; Python API accepts mode='autonomous'
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _make_sample_repo(wt)  # default hitl
            cfg_path = _write_engines_v2(base / "engines.toml")
            with contextlib.redirect_stderr(io.StringIO()):
                rc_cli = devpack_run.main([
                    "--config", str(cfg_path),
                    "--worktree", str(wt),
                    "--runs-root", str(base / "runs"),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                    "--mode", "autonomous",
                ])
            self.assertNotEqual(0, rc_cli)

            cfg = devpack_run.load_engine_config(cfg_path)
            ctx = devpack_run.load_task_data(None, "api-auto", "API", "Goal", "src/mod.cpp")
            results = [
                agent_exec.AgentResult(status="ok", exit_code=0, final_text="ok"),
                agent_exec.AgentResult(status="ok", exit_code=0, final_text="VERDICT: APPROVE"),
            ]
            with mock.patch.object(devpack_run, "run_agent", side_effect=results), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc_api, summary_api = devpack_run.run_task_workflow(
                    cfg, ctx, wt, base / "logs_api", REPO_ROOT,
                    isolation="local", allow_unsandboxed_local=True,
                    mode="autonomous", state_dir=base / "api_state",
                    engines_config_path=cfg_path,
                )
            self.assertEqual(0, rc_api, summary_api)

    def test_approve_reject_resume_round_trip_and_identity_redaction(self) -> None:
        """Test CLI approve/reject, devpack_run --resume round trip, OS username hashing, and git notes provenance."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _make_sample_repo(wt)  # hitl
            state_dir = base / "trusted_state"
            cfg_path = _write_engines_v2(base / "engines.toml")
            fake_claude = _write_exec(
                base / "fake_claude.py",
                "#!/usr/bin/env python3\nimport json, sys\n"
                "prompt = ' '.join(sys.argv[1:])\n"
                "if 'Reviewer' in prompt or 'VERDICT' in prompt:\n"
                "    print(json.dumps({'type':'result','is_error':False,'result':'VERDICT: APPROVE'}))\n"
                "else:\n"
                "    print(json.dumps({'type':'result','is_error':False,'result':'implemented'}))\n",
            )

            # 1. Start workflow in hitl -> stops at brief checkpoint (exit 20)
            out1 = io.StringIO()
            with contextlib.redirect_stdout(out1):
                rc1 = devpack_run.main([
                    "--config", str(cfg_path),
                    "--worktree", str(wt),
                    "--runs-root", str(base / "runs"),
                    "--agent-bin", str(fake_claude),
                    "--task-id", "rt-1",
                    "--allowed-files", "src/**",
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                    "--state-dir", str(state_dir),
                ])
            self.assertEqual(20, rc1, out1.getvalue())
            pending = checkpoints.list_approvals(state_dir, status="pending", checkpoint_id="brief")
            self.assertEqual(1, len(pending))
            aid1 = pending[0]["id"]

            # 2. Resuming while STILL pending returns exit 20
            with contextlib.redirect_stdout(io.StringIO()):
                rc_still = devpack_run.main([
                    "--worktree", str(wt),
                    "--state-dir", str(state_dir),
                    "--resume", aid1,
                ])
            self.assertEqual(20, rc_still)

            # 3. devpack_approve list & show; reject without --note fails; reject --note works -> resume returns 1
            list_buf = io.StringIO()
            with contextlib.redirect_stdout(list_buf):
                self.assertEqual(0, devpack_approve.main(["--state-dir", str(state_dir), "list", "--status", "pending", "--json"]))
            self.assertEqual(aid1, json.loads(list_buf.getvalue())[0]["id"])

            with self.assertRaises(SystemExit) as rej_exc, contextlib.redirect_stderr(io.StringIO()):
                devpack_approve.main(["--workdir", str(wt), "--state-dir", str(state_dir), "reject", aid1])
            self.assertEqual(2, rej_exc.exception.code)
            with self.assertRaisesRegex(ValueError, "note"):
                checkpoints.decide_approval(state_dir, aid1, decision_status=checkpoints.REJECTED, note="")

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, devpack_approve.main([
                    "--workdir", str(wt), "--state-dir", str(state_dir),
                    "reject", aid1, "--note", "needs tighter scope",
                ]))
            with contextlib.redirect_stdout(io.StringIO()):
                rc_rej_resume = devpack_run.main([
                    "--worktree", str(wt), "--state-dir", str(state_dir), "--resume", aid1,
                ])
            self.assertEqual(1, rc_rej_resume)

            # 4. Second run: approve via devpack_approve -> resume completes with exit 0 & records provenance + hashed ID
            with contextlib.redirect_stdout(io.StringIO()):
                rc2 = devpack_run.main([
                    "--config", str(cfg_path),
                    "--worktree", str(wt),
                    "--runs-root", str(base / "runs2"),
                    "--agent-bin", str(fake_claude),
                    "--task-id", "rt-2",
                    "--allowed-files", "src/**",
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                    "--state-dir", str(state_dir),
                ])
            self.assertEqual(20, rc2)
            aid2 = checkpoints.list_approvals(state_dir, status="pending", checkpoint_id="brief")[0]["id"]

            fake_user = "alice_secret_operator_name"
            with mock.patch.object(checkpoints, "_current_os_username", return_value=fake_user), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, devpack_approve.main([
                    "--workdir", str(wt), "--state-dir", str(state_dir),
                    "approve", aid2, "--note", "reviewed and approved",
                ]))

            decided_rec, decided_path = checkpoints.show_approval(state_dir, aid2)
            self.assertEqual("APPROVED", decided_rec["status"])
            self.assertTrue(str(decided_rec["decided_by"]).startswith("human:"))
            self.assertNotIn(fake_user, decided_path.read_text(encoding="utf-8"))

            with contextlib.redirect_stdout(io.StringIO()):
                rc_resumed = devpack_run.main([
                    "--worktree", str(wt), "--state-dir", str(state_dir), "--resume", aid2,
                ])
            self.assertEqual(0, rc_resumed)

            prov_text = (state_dir / "approvals" / "provenance.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(fake_user, prov_text)
            self.assertIn(aid2, prov_text)

            notes_proc = subprocess.run(
                ["git", "notes", "--ref=refs/notes/ai-provenance", "show", "HEAD"],
                cwd=wt,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, notes_proc.returncode, notes_proc.stderr)
            self.assertIn(aid2, notes_proc.stdout)
            self.assertNotIn(fake_user, notes_proc.stdout)

    def test_memory_decision_sync_with_approvals_cli(self) -> None:
        """Pending decision/fact memory entries sync bidirectionally with memory-decision checkpoints."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _make_sample_repo(wt)
            state_dir = base / "state"
            mem_root = wt / ".devpack" / "memory"

            out_add = io.StringIO()
            with contextlib.redirect_stdout(out_add):
                rc_add = devpack_memory.main([
                    "--workdir", str(wt),
                    "--state-dir", str(state_dir),
                    "add",
                    "--type", "decision",
                    "--title", "Use fixed buffer",
                    "--body", "Avoid dynamic allocation in IRQ path.",
                    "--scope", "module:src",
                    "--status", "pending",
                ])
            self.assertEqual(0, rc_add)

            pending_mem = checkpoints.list_approvals(state_dir, status="pending", checkpoint_id="memory-decision")
            self.assertEqual(1, len(pending_mem))
            mem_id = pending_mem[0]["subject"]
            aid = pending_mem[0]["id"]

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, devpack_approve.main([
                    "--workdir", str(wt), "--state-dir", str(state_dir),
                    "approve", aid, "--note", "ADR accepted",
                ]))

            entries = memory.load_memory_entries(mem_root)
            matched = [e for e in entries if e.id == mem_id]
            self.assertEqual(1, len(matched))
            self.assertEqual("active", matched[0].status)

            # Reject path syncs back to expired status in memory store
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, devpack_memory.main([
                    "--workdir", str(wt),
                    "--state-dir", str(state_dir),
                    "add",
                    "--type", "fact",
                    "--title", "Temp workaround",
                    "--body", "Do not persist this fact.",
                    "--status", "pending",
                ]))
            pending_mem2 = checkpoints.list_approvals(state_dir, status="pending", checkpoint_id="memory-decision")
            self.assertEqual(1, len(pending_mem2))
            aid2 = pending_mem2[0]["id"]
            mem_id2 = pending_mem2[0]["subject"]

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, devpack_approve.main([
                    "--workdir", str(wt), "--state-dir", str(state_dir),
                    "reject", aid2, "--note", "superseded",
                ]))
            matched2 = [e for e in memory.load_memory_entries(mem_root) if e.id == mem_id2]
            self.assertEqual(1, len(matched2))
            self.assertEqual("expired", matched2[0].status)

    def test_tamper_resistance_sandbox_and_broker_boundaries(self) -> None:
        """state_dir in worktree refused, mid-run project.toml ignored, broker blocks devpack_approve, sandbox sets env."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _make_sample_repo(wt, project_toml='[pipeline]\nmode = "hitl"\n')

            # 1. Explicit state_dir inside worktree / agent_worktree is refused
            with self.assertRaisesRegex(ValueError, "outside"):
                checkpoints.resolve_state_dir(wt / "inside_state", project_root=wt, explicit=True)
            with self.assertRaisesRegex(ValueError, "outside agent worktree"):
                checkpoints.resolve_state_dir(wt / ".devpack" / "state", project_root=wt, agent_worktree=wt, explicit=False)

            # 2. devpack_approve refuses to run inside DEVPACK_AGENT_SANDBOX=1
            with mock.patch.dict(os.environ, {"DEVPACK_AGENT_SANDBOX": "1"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, devpack_approve.main(["--workdir", str(wt), "list"]))

            # 3. build_invocation sets DEVPACK_AGENT_SANDBOX=1 for both local and docker
            eng_local = agent_exec.EngineConfig(name="haiku", model="haiku", engine_class="cloud")
            for iso, allow_local in (("local", True), ("docker", False)):
                req = agent_exec.AgentRequest(
                    engine=eng_local,
                    prompt="test",
                    workdir=wt,
                    run_dir=base / f"run_{iso}",
                    step_name="01-test",
                    isolation=iso,
                    allow_unsandboxed_local=allow_local,
                )
                argv, env = agent_exec.build_invocation(req)
                if iso == "local":
                    self.assertEqual("1", env.get("DEVPACK_AGENT_SANDBOX"))
                else:
                    self.assertIn("DEVPACK_AGENT_SANDBOX=1", argv)

            # 4. Broker refuses devpack_approve requests and rejects mode loosening
            broker = devpack_broker.Broker(
                wt, base / "broker_runs", isolation="local", allow_unsandboxed_local=True, gate_cmd="true"
            )
            try:
                code_blk, payload_blk = broker.execute({
                    "engine": "haiku",
                    "brief": "briefs/job.md",
                    "allow": ["src/**"],
                    "command": "python3 runner/devpack_approve.py approve x",
                })
                self.assertEqual(1, code_blk)
                self.assertEqual("refused", payload_blk["status"])
                self.assertIn("devpack_approve", payload_blk["error"])

                code_loosen, payload_loosen = broker.execute({
                    "engine": "haiku",
                    "brief": "briefs/job.md",
                    "allow": ["src/**"],
                    "mode": "autonomous",
                })
                self.assertEqual(1, code_loosen)
                self.assertEqual("refused", payload_loosen["status"])
            finally:
                broker.close()

            # 5. Agent tampering with .devpack/project.toml mid-run cannot switch state_dir/mode and fails scope check
            tamper_wt = base / "tamper_wt"
            _make_sample_repo(tamper_wt, project_toml='[pipeline]\nmode = "autonomous"\n')
            tamper_state = base / "tamper_state"
            tamper_worker = _write_exec(
                base / "tamper_worker.py",
                "#!/usr/bin/env python3\nimport json, pathlib\n"
                "pathlib.Path('.devpack/project.toml').write_text('[pipeline]\\nmode = \"hitl\"\\nstate_dir = \"/tmp/evil\"\\n', encoding='utf-8')\n"
                "print(json.dumps({'type':'result','is_error':False,'result':'tampered'}))\n",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                rc_t, payload_t = devpack_dispatch.dispatch_job(
                    engine_name="haiku",
                    brief_path=tamper_wt / "briefs" / "job.md",
                    workdir=tamper_wt,
                    allow_globs=["src/**"],
                    gate_cmd="true",
                    runs_root=base / "tamper_runs",
                    isolation="local",
                    allow_unsandboxed_local=True,
                    agent_bin=str(tamper_worker),
                    state_dir=tamper_state,
                )
            self.assertEqual(1, rc_t)
            self.assertEqual("violation", payload_t["scope"])

    def test_default_state_dir_external_and_docker_mounts_exclude_state_dir(self) -> None:
        """Default state_dir resolves to XDG_STATE_HOME outside worktree; inside-worktree state_dir refused; Docker mounts never include state_dir."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _make_sample_repo(wt, project_toml='[pipeline]\nmode = "autonomous"\n')
            xdg_home = base / "xdg_state"

            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(xdg_home)}):
                cfg = checkpoints.load_pipeline_config(wt, agent_worktree=wt)
                resolved_state = Path(cfg.state_dir)
                self.assertTrue(resolved_state.is_relative_to(xdg_home / "devpack"))
                self.assertFalse(resolved_state.is_relative_to(wt))

                # Even when runs_root/log_dir is inside worktree, devpack_run uses external XDG state_dir
                engines_cfg_path = _write_engines_v2(base / "engines.toml")
                engines_cfg = devpack_run.load_engine_config(engines_cfg_path)
                ctx = devpack_run.load_task_data(None, "ext-1", "Ext", "Goal", "src/mod.cpp")
                with mock.patch.object(
                    devpack_run,
                    "run_agent",
                    side_effect=[
                        agent_exec.AgentResult(status="ok", exit_code=0, final_text="ok"),
                        agent_exec.AgentResult(status="ok", exit_code=0, final_text="VERDICT: APPROVE"),
                    ],
                ), contextlib.redirect_stdout(io.StringIO()):
                    rc_run, summary_run = devpack_run.run_task_workflow(
                        engines_cfg, ctx, wt, wt / "runs" / "in_wt_logs", REPO_ROOT,
                        isolation="local", allow_unsandboxed_local=True,
                        engines_config_path=engines_cfg_path,
                    )
                self.assertEqual(0, rc_run, summary_run)
                self.assertFalse(Path(summary_run["state_dir"]).is_relative_to(wt))
                self.assertFalse((wt / ".devpack" / "state").exists())

            # state_dir configured inside worktree or inside in-worktree runs/logs is refused
            for bad_state in (
                wt / ".devpack" / "state",
                wt / "runs" / "state",
                wt / "custom_state",
            ):
                with self.assertRaisesRegex(ValueError, "outside"):
                    checkpoints.resolve_state_dir(bad_state, project_root=wt, agent_worktree=wt, writable_mounts=(wt,))

            wt_bad = base / "wt_bad"
            _make_sample_repo(wt_bad, project_toml='[pipeline]\nmode = "hitl"\nstate_dir = ".devpack/state"\n')
            with self.assertRaisesRegex(ValueError, "outside"):
                checkpoints.load_pipeline_config(wt_bad, agent_worktree=wt_bad)
            with contextlib.redirect_stderr(io.StringIO()):
                rc_bad = devpack_dispatch.main([
                    "--engine", "haiku",
                    "--brief", str(wt_bad / "briefs" / "job.md"),
                    "--workdir", str(wt_bad),
                    "--allow", "src/**",
                    "--gate-cmd", "true",
                    "--runs-root", str(base / "bad_runs"),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                ])
            self.assertEqual(2, rc_bad)

            # Docker mount list never includes external state_dir, and refuses state_dir inside workdir
            eng = agent_exec.EngineConfig(name="haiku", model="haiku", engine_class="cloud")
            ext_state = base / "ext_state"
            req_ok = agent_exec.AgentRequest(
                engine=eng,
                prompt="test",
                workdir=wt,
                run_dir=base / "docker_run",
                isolation="docker",
                state_dir=ext_state,
            )
            argv_ok, _ = agent_exec.build_invocation(req_ok)
            mounts = [argv_ok[i + 1] for i, tok in enumerate(argv_ok) if tok == "-v"]
            mount_srcs = [Path(m.split(":", 1)[0]).resolve() for m in mounts]
            for src in mount_srcs:
                self.assertNotEqual(ext_state.resolve(), src)
                self.assertFalse(ext_state.resolve().is_relative_to(src))

            req_bad = agent_exec.AgentRequest(
                engine=eng,
                prompt="test",
                workdir=wt,
                run_dir=base / "docker_run_bad",
                isolation="docker",
                state_dir=wt / "inside_state",
            )
            with self.assertRaisesRegex(ValueError, "outside"):
                agent_exec.build_invocation(req_bad)

    def test_dispatch_pause_approve_resume_single_use_and_brief_hash(self) -> None:
        """devpack_dispatch --resume continues once after approval, refuses second resume and edited brief."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            brief_path = _make_sample_repo(wt)  # default hitl
            state_dir = base / "dispatch_state"
            runs_root = base / "dispatch_runs"
            calls_log = base / "worker_calls.txt"
            worker = _write_exec(
                base / "worker.py",
                "#!/usr/bin/env python3\nimport json, pathlib\n"
                f"with open({str(calls_log)!r}, 'a', encoding='utf-8') as f: f.write('1\\n')\n"
                "print(json.dumps({'type':'result','is_error':False,'result':'ok'}))\n",
            )

            # 1. Pause -> approve -> resume runs once -> second resume refused
            with contextlib.redirect_stdout(io.StringIO()):
                rc_pause = devpack_dispatch.main([
                    "--engine", "haiku",
                    "--brief", str(brief_path),
                    "--workdir", str(wt),
                    "--allow", "src/**",
                    "--gate-cmd", "true",
                    "--runs-root", str(runs_root),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                    "--agent-bin", str(worker),
                    "--state-dir", str(state_dir),
                ])
            self.assertEqual(20, rc_pause)
            self.assertFalse(calls_log.exists())
            pending = checkpoints.list_approvals(state_dir, status="pending", checkpoint_id="brief")
            self.assertEqual(1, len(pending))
            aid = pending[0]["id"]

            # Still pending -> resume returns 20 and does not consume
            with contextlib.redirect_stdout(io.StringIO()):
                rc_still = devpack_dispatch.main([
                    "--workdir", str(wt), "--state-dir", str(state_dir), "--resume", aid,
                ])
            self.assertEqual(20, rc_still)

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, devpack_approve.main([
                    "--workdir", str(wt), "--state-dir", str(state_dir), "approve", aid,
                ]))

            with contextlib.redirect_stdout(io.StringIO()):
                rc_res = devpack_dispatch.main([
                    "--workdir", str(wt), "--state-dir", str(state_dir), "--resume", aid,
                ])
            self.assertEqual(0, rc_res)
            self.assertEqual(["1"], calls_log.read_text(encoding="utf-8").splitlines())

            # Second resume refused
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                rc_res2 = devpack_dispatch.main([
                    "--workdir", str(wt), "--state-dir", str(state_dir), "--resume", aid,
                ])
            self.assertEqual(1, rc_res2)
            self.assertIn("consumed", err_buf.getvalue())
            self.assertEqual(["1"], calls_log.read_text(encoding="utf-8").splitlines())

            # 2. Edited brief after approval is refused
            with contextlib.redirect_stdout(io.StringIO()):
                rc_pause2 = devpack_dispatch.main([
                    "--engine", "haiku",
                    "--brief", str(brief_path),
                    "--workdir", str(wt),
                    "--allow", "src/**",
                    "--gate-cmd", "true",
                    "--runs-root", str(runs_root),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                    "--agent-bin", str(worker),
                    "--state-dir", str(state_dir),
                ])
            self.assertEqual(20, rc_pause2)
            aid2 = checkpoints.list_approvals(state_dir, status="pending", checkpoint_id="brief")[0]["id"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, devpack_approve.main([
                    "--workdir", str(wt), "--state-dir", str(state_dir), "approve", aid2,
                ]))
            brief_path.write_text(brief_path.read_text(encoding="utf-8") + "\n<!-- edited -->\n", encoding="utf-8")
            err_edit = io.StringIO()
            with contextlib.redirect_stderr(err_edit):
                rc_edit = devpack_dispatch.main([
                    "--workdir", str(wt), "--state-dir", str(state_dir), "--resume", aid2,
                ])
            self.assertEqual(1, rc_edit)
            self.assertIn("changed", err_edit.getvalue())
            self.assertEqual(["1"], calls_log.read_text(encoding="utf-8").splitlines())

    def test_broker_pause_approve_resume_single_use_and_brief_hash(self) -> None:
        """Broker jobs pause in hitl, resume once after approval via queue/submit, and refuse replay or edited brief."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            brief_path = _make_sample_repo(wt)  # default hitl
            state_dir = base / "broker_state"
            calls_log = base / "broker_calls.txt"
            worker = _write_exec(
                base / "broker_worker.py",
                "#!/usr/bin/env python3\nimport json, pathlib\n"
                f"with open({str(calls_log)!r}, 'a', encoding='utf-8') as f: f.write('run\\n')\n"
                "pathlib.Path('src/mod.cpp').write_text("
                "'/**\\n * @file mod.cpp\\n * @brief Module.\\n */\\n"
                "/**\\n * @brief Compute value.\\n * @return Value.\\n */\\n"
                "int compute() { return 42; }\\n', encoding='utf-8')\n"
                "print(json.dumps({'type':'result','is_error':False,'result':'done'}))\n",
            )
            broker = devpack_broker.Broker(
                wt,
                base / "broker_runs",
                isolation="local",
                allow_unsandboxed_local=True,
                agent_bin=str(worker),
                gate_cmd="true",
                state_dir=state_dir,
            )
            try:
                code1, p1 = broker.execute({"engine": "haiku", "brief": "briefs/job.md", "allow": ["src/**"]})
                self.assertEqual(20, code1, p1)
                self.assertEqual(checkpoints.AWAITING_APPROVAL, p1["status"])
                aid1 = p1["approval_id"]
                self.assertFalse(calls_log.exists())

                # Pending resume keeps status 20 and does not consume
                code_still, p_still = broker.execute({"resume": aid1})
                self.assertEqual(20, code_still, p_still)

                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(0, devpack_approve.main([
                        "--workdir", str(wt), "--state-dir", str(state_dir), "approve", aid1,
                    ]))

                # Queue client (--submit --resume) resumes once
                import threading
                from runner import devpack_submit
                stop_poll = threading.Event()

                def _poll_loop() -> None:
                    while not stop_poll.is_set():
                        broker.poll()
                        stop_poll.wait(0.02)

                t = threading.Thread(target=_poll_loop, daemon=True)
                t.start()
                try:
                    sub_out = io.StringIO()
                    with contextlib.redirect_stdout(sub_out):
                        rc_sub = devpack_submit.main(["--submit", "--workdir", str(wt), "--resume", aid1])
                    resp = json.loads(sub_out.getvalue().strip())
                finally:
                    stop_poll.set()
                    t.join(timeout=2)
                self.assertEqual(0, rc_sub, resp)
                self.assertEqual("ok", resp["status"], resp)
                self.assertEqual("ok", resp["gate"], resp)
                self.assertIn("return 42;", (wt / "src" / "mod.cpp").read_text(encoding="utf-8"))
                self.assertEqual(["run"], calls_log.read_text(encoding="utf-8").splitlines())

                # Second resume refused
                code_replay, p_replay = broker.execute({"resume": aid1})
                self.assertEqual(1, code_replay, p_replay)
                self.assertEqual("refused", p_replay["status"])
                self.assertIn("consumed", p_replay["error"])
                self.assertEqual(["run"], calls_log.read_text(encoding="utf-8").splitlines())

                # Pause -> approve -> edit brief -> resume refused
                code2, p2 = broker.execute({"engine": "haiku", "brief": "briefs/job.md", "allow": ["src/**"]})
                self.assertEqual(20, code2, p2)
                aid2 = p2["approval_id"]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(0, devpack_approve.main([
                        "--workdir", str(wt), "--state-dir", str(state_dir), "approve", aid2,
                    ]))
                brief_path.write_text(brief_path.read_text(encoding="utf-8") + "\n<!-- tampered -->\n", encoding="utf-8")
                code_edit, p_edit = broker.execute({"resume": aid2})
                self.assertEqual(1, code_edit, p_edit)
                self.assertEqual("refused", p_edit["status"])
                self.assertIn("changed", p_edit["error"])
                self.assertEqual(["run"], calls_log.read_text(encoding="utf-8").splitlines())
            finally:
                broker.close()

    def test_pre_merge_checks_target_branch_and_fails_closed_when_unknown(self) -> None:
        """Running on feature/x targeting main keeps pre-merge PENDING; unknown target fails closed; unprotected target auto-approves."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _make_sample_repo(wt, project_toml='[pipeline]\nmode = "autonomous"\n', branch="feature/x")
            engines_cfg_path = _write_engines_v2(base / "engines.toml")
            engines_cfg = devpack_run.load_engine_config(engines_cfg_path)
            ctx = devpack_run.load_task_data(None, "pm-target", "Target Check", "Goal", "src/mod.cpp")

            def _run_with_target(state_subdir: str, target_branch: str | None) -> tuple[int, dict]:
                st = base / state_subdir
                with mock.patch.object(
                    devpack_run,
                    "run_agent",
                    side_effect=[
                        agent_exec.AgentResult(status="ok", exit_code=0, final_text="impl ok"),
                        agent_exec.AgentResult(status="ok", exit_code=0, final_text="VERDICT: APPROVE"),
                    ],
                ), contextlib.redirect_stdout(io.StringIO()):
                    return devpack_run.run_task_workflow(
                        engines_cfg, ctx, wt, base / f"logs_{state_subdir}", REPO_ROOT,
                        isolation="local", allow_unsandboxed_local=True,
                        state_dir=st, engines_config_path=engines_cfg_path,
                        target_branch=target_branch,
                    )

            # 1. feature/x targeting main -> pre-merge stays PENDING (hard-stop)
            rc_main, sum_main = _run_with_target("state_main", "main")
            self.assertEqual(0, rc_main, sum_main)
            pm_main = checkpoints.list_approvals(base / "state_main", checkpoint_id="pre-merge")
            self.assertEqual(1, len(pm_main))
            self.assertEqual("PENDING", pm_main[0]["status"])
            self.assertTrue(pm_main[0]["hard_stop"])
            self.assertEqual("main", pm_main[0]["metadata"]["target_branch"])

            # 2. Default target_branch (omitted) defaults to main -> PENDING
            rc_def, _ = _run_with_target("state_default", None)
            self.assertEqual(0, rc_def)
            pm_def = checkpoints.list_approvals(base / "state_default", checkpoint_id="pre-merge")
            self.assertEqual("PENDING", pm_def[0]["status"])
            self.assertTrue(pm_def[0]["hard_stop"])

            # 3. Unknown/empty target_branch fails closed -> PENDING
            rc_unk, _ = _run_with_target("state_unknown", "")
            self.assertEqual(0, rc_unk)
            pm_unk = checkpoints.list_approvals(base / "state_unknown", checkpoint_id="pre-merge")
            self.assertEqual("PENDING", pm_unk[0]["status"])
            self.assertTrue(pm_unk[0]["hard_stop"])
            self.assertTrue(checkpoints.is_hard_stop("pre-merge", {}))
            self.assertTrue(checkpoints.is_hard_stop("pre-merge", {"target_branch": "HEAD"}))

            # 4. Non-protected target branch (e.g. integration/dev) -> auto-approved in autonomous
            rc_int, _ = _run_with_target("state_int", "integration/dev")
            self.assertEqual(0, rc_int)
            pm_int = checkpoints.list_approvals(base / "state_int", checkpoint_id="pre-merge")
            self.assertEqual("APPROVED", pm_int[0]["status"])
            self.assertFalse(pm_int[0]["hard_stop"])

    def test_dispatch_resume_restores_docker_bin_and_image_tag(self) -> None:
        """devpack_dispatch --resume restores saved docker_bin and image_tag from resume_spec."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            brief_path = _make_sample_repo(wt)  # default hitl
            state_dir = base / "dispatch_state"
            runs_root = base / "dispatch_runs"

            with contextlib.redirect_stdout(io.StringIO()):
                rc_pause, pause_payload = devpack_dispatch.dispatch_job(
                    engine_name="haiku",
                    brief_path=brief_path,
                    workdir=wt,
                    allow_globs=["src/**"],
                    gate_cmd="true",
                    runs_root=runs_root,
                    isolation="docker",
                    docker_bin="/custom/bin/docker",
                    image_tag="custom-img:v42",
                    state_dir=state_dir,
                )
            self.assertEqual(20, rc_pause, pause_payload)
            aid = str(pause_payload["approval_id"])

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, devpack_approve.main([
                    "--workdir", str(wt), "--state-dir", str(state_dir), "approve", aid,
                ]))

            captured: list[agent_exec.AgentRequest] = []

            def _fake_run_agent(req: agent_exec.AgentRequest) -> agent_exec.AgentResult:
                captured.append(req)
                return agent_exec.AgentResult(status="ok", exit_code=0, final_text="ok")

            with mock.patch.object(devpack_dispatch, "run_agent", side_effect=_fake_run_agent), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc_res = devpack_dispatch.main([
                    "--workdir", str(wt), "--state-dir", str(state_dir), "--resume", aid,
                ])
            self.assertEqual(0, rc_res)
            self.assertEqual(1, len(captured))
            self.assertEqual("/custom/bin/docker", captured[0].docker_bin)
            self.assertEqual("custom-img:v42", captured[0].image_tag)

    def test_dispatch_resume_failing_start_does_not_consume_approval(self) -> None:
        """If dispatch fails before starting the agent, the approval stays usable and error is reported."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            brief_path = _make_sample_repo(wt)  # default hitl
            state_dir = base / "dispatch_state"
            runs_root = base / "dispatch_runs"

            with contextlib.redirect_stdout(io.StringIO()):
                rc_pause, pause_payload = devpack_dispatch.dispatch_job(
                    engine_name="haiku",
                    brief_path=brief_path,
                    workdir=wt,
                    allow_globs=["src/**"],
                    gate_cmd="true",
                    runs_root=runs_root,
                    isolation="docker",
                    state_dir=state_dir,
                )
            self.assertEqual(20, rc_pause, pause_payload)
            aid = str(pause_payload["approval_id"])

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, devpack_approve.main([
                    "--workdir", str(wt), "--state-dir", str(state_dir), "approve", aid,
                ]))

            # 1. Pre-start policy refusal (--isolation local without --allow-unsandboxed-local)
            err_iso = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err_iso):
                rc_fail_iso = devpack_dispatch.main([
                    "--workdir", str(wt),
                    "--state-dir", str(state_dir),
                    "--isolation", "local",
                    "--resume", aid,
                ])
            self.assertEqual(1, rc_fail_iso)
            self.assertTrue(err_iso.getvalue().strip())
            rec_after_iso, _ = checkpoints.show_approval(state_dir, aid)
            self.assertEqual(checkpoints.APPROVED, rec_after_iso["status"])
            self.assertFalse(bool(rec_after_iso.get("consumed", False)))
            self.assertFalse((state_dir / "runs" / f"{aid}.consumed").exists())

            # 2. Pre-start exception inside dispatch_job (e.g. engine resolution fails)
            err_exc = io.StringIO()
            with mock.patch.object(
                devpack_dispatch,
                "resolve_engine",
                side_effect=ValueError("engine boom"),
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err_exc):
                rc_fail_exc = devpack_dispatch.main([
                    "--workdir", str(wt),
                    "--state-dir", str(state_dir),
                    "--resume", aid,
                ])
            self.assertEqual(1, rc_fail_exc)
            self.assertIn("engine boom", err_exc.getvalue())
            rec_after_exc, _ = checkpoints.show_approval(state_dir, aid)
            self.assertEqual(checkpoints.APPROVED, rec_after_exc["status"])
            self.assertFalse(bool(rec_after_exc.get("consumed", False)))
            self.assertFalse((state_dir / "runs" / f"{aid}.consumed").exists())

            # 3. Subsequent valid resume succeeds and consumes the approval
            with mock.patch.object(
                devpack_dispatch,
                "run_agent",
                return_value=agent_exec.AgentResult(status="ok", exit_code=0, final_text="ok"),
            ), contextlib.redirect_stdout(io.StringIO()):
                rc_ok = devpack_dispatch.main([
                    "--workdir", str(wt), "--state-dir", str(state_dir), "--resume", aid,
                ])
            self.assertEqual(0, rc_ok)
            rec_final, _ = checkpoints.show_approval(state_dir, aid)
            self.assertTrue(bool(rec_final.get("consumed", False)))
            self.assertTrue((state_dir / "runs" / f"{aid}.consumed").exists())

    def test_override_mode_cannot_loosen_configured_mode(self) -> None:
        """load_pipeline_config and checkpoints.request never let override_mode/mode loosen config."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt_hitl = base / "wt_hitl"
            _make_sample_repo(wt_hitl, project_toml='[pipeline]\nmode = "hitl"\n')
            state_dir = base / "state"

            cfg = checkpoints.load_pipeline_config(
                wt_hitl, override_mode="autonomous", override_state_dir=state_dir, agent_worktree=wt_hitl
            )
            self.assertEqual("hitl", cfg.mode)

            # Blocking checkpoint still pauses when project config is hitl even if mode="autonomous" is passed
            receipt = base / "receipt.json"
            receipt.write_text('{"ok": true}\n', encoding="utf-8")
            decision = checkpoints.request(
                "plan",
                "plan-subj",
                [receipt],
                "summary",
                mode="autonomous",
                project_root=wt_hitl,
                state_dir=state_dir,
            )
            self.assertTrue(decision.pending)
            self.assertEqual("hitl", decision.mode)

            wt_auto = base / "wt_auto"
            _make_sample_repo(wt_auto, project_toml='[pipeline]\nmode = "autonomous"\n')
            cfg_tight = checkpoints.load_pipeline_config(
                wt_auto, override_mode="hitl", override_state_dir=state_dir, agent_worktree=wt_auto
            )
            self.assertEqual("hitl", cfg_tight.mode)
            cfg_env = checkpoints.load_pipeline_config(
                wt_auto,
                override_mode="autonomous",
                env={"DEVPACK_MODE": "hitl"},
                override_state_dir=state_dir,
                agent_worktree=wt_auto,
            )
            self.assertEqual("hitl", cfg_env.mode)

    def test_resume_uses_stricter_of_saved_and_current_mode(self) -> None:
        """Resume runs in the stricter of saved mode, current project config, and CLI/env tightening."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            state_dir = base / "state"
            runs_root = base / "runs"
            worker = _write_exec(
                base / "worker.py",
                "#!/usr/bin/env python3\nimport json\n"
                "print(json.dumps({'type':'result','is_error':False,'result':'ok'}))\n",
            )

            # 1. Paused in autonomous (via brief = "human"), config changed to hitl -> resume runs as hitl
            wt_a2h = base / "wt_a2h"
            brief_a2h = _make_sample_repo(
                wt_a2h,
                project_toml='[pipeline]\nmode = "autonomous"\n\n[pipeline.checkpoints]\nbrief = "human"\n',
            )
            with contextlib.redirect_stdout(io.StringIO()):
                rc_pause1 = devpack_dispatch.main([
                    "--engine", "haiku",
                    "--brief", str(brief_a2h),
                    "--workdir", str(wt_a2h),
                    "--allow", "src/**",
                    "--gate-cmd", "true",
                    "--runs-root", str(runs_root),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                    "--agent-bin", str(worker),
                    "--state-dir", str(state_dir),
                ])
            self.assertEqual(20, rc_pause1)
            pending1 = checkpoints.list_approvals(state_dir, status="pending", checkpoint_id="brief")
            self.assertEqual(1, len(pending1))
            aid1 = pending1[0]["id"]
            self.assertEqual("autonomous", checkpoints.load_resume_state(state_dir, aid1)["pipeline_mode"])

            # Change project config to hitl and approve
            (wt_a2h / ".devpack" / "project.toml").write_text('[pipeline]\nmode = "hitl"\n', encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, devpack_approve.main([
                    "--workdir", str(wt_a2h), "--state-dir", str(state_dir), "approve", aid1,
                ]))

            out1 = io.StringIO()
            with contextlib.redirect_stdout(out1):
                rc_res1 = devpack_dispatch.main([
                    "--workdir", str(wt_a2h), "--state-dir", str(state_dir), "--resume", aid1,
                ])
            self.assertEqual(0, rc_res1)
            payload1 = json.loads(out1.getvalue().strip().splitlines()[-1])
            run1_json = json.loads((Path(payload1["run_dir"]) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual("hitl", run1_json["pipeline_mode"])

            # Verify blocking checkpoints pause under the updated hitl config even if mode="autonomous"
            receipt = base / "receipt_a2h.json"
            receipt.write_text('{"ok": true}\n', encoding="utf-8")
            blk = checkpoints.request(
                "plan",
                "plan-after-resume",
                [receipt],
                "summary",
                mode="autonomous",
                project_root=wt_a2h,
                state_dir=state_dir,
            )
            self.assertTrue(blk.pending)
            self.assertEqual("hitl", blk.mode)

            # 2. Paused in hitl, config changed to autonomous -> still hitl on resume
            wt_h2a = base / "wt_h2a"
            brief_h2a = _make_sample_repo(wt_h2a, project_toml='[pipeline]\nmode = "hitl"\n')
            with contextlib.redirect_stdout(io.StringIO()):
                rc_pause2 = devpack_dispatch.main([
                    "--engine", "haiku",
                    "--brief", str(brief_h2a),
                    "--workdir", str(wt_h2a),
                    "--allow", "src/**",
                    "--gate-cmd", "true",
                    "--runs-root", str(runs_root),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                    "--agent-bin", str(worker),
                    "--state-dir", str(state_dir),
                ])
            self.assertEqual(20, rc_pause2)
            pending2 = checkpoints.list_approvals(state_dir, status="pending", checkpoint_id="brief")
            self.assertEqual(1, len(pending2))
            aid2 = pending2[0]["id"]

            (wt_h2a / ".devpack" / "project.toml").write_text('[pipeline]\nmode = "autonomous"\n', encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, devpack_approve.main([
                    "--workdir", str(wt_h2a), "--state-dir", str(state_dir), "approve", aid2,
                ]))

            out2 = io.StringIO()
            with contextlib.redirect_stdout(out2):
                rc_res2 = devpack_dispatch.main([
                    "--workdir", str(wt_h2a), "--state-dir", str(state_dir), "--resume", aid2,
                ])
            self.assertEqual(0, rc_res2)
            payload2 = json.loads(out2.getvalue().strip().splitlines()[-1])
            run2_json = json.loads((Path(payload2["run_dir"]) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual("hitl", run2_json["pipeline_mode"])

    def test_resume_from_unrelated_directory_reads_saved_project_mode(self) -> None:
        """Resuming with --state-dir + --resume and no --workdir reads mode from the saved project."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            state_dir = base / "state"
            runs_root = base / "runs"
            unrelated_dir = base / "unrelated"
            unrelated_dir.mkdir(parents=True, exist_ok=True)
            worker = _write_exec(
                base / "worker.py",
                "#!/usr/bin/env python3\nimport json\n"
                "print(json.dumps({'type':'result','is_error':False,'result':'ok'}))\n",
            )

            proj_a = base / "project_a"
            brief_a = _make_sample_repo(
                proj_a,
                project_toml='[pipeline]\nmode = "autonomous"\n\n[pipeline.checkpoints]\nbrief = "human"\n',
            )

            # 1. Pause autonomous job in project A, resume from unrelated dir -> runs autonomous
            with contextlib.redirect_stdout(io.StringIO()):
                rc_pause1 = devpack_dispatch.main([
                    "--engine", "haiku",
                    "--brief", str(brief_a),
                    "--workdir", str(proj_a),
                    "--allow", "src/**",
                    "--gate-cmd", "true",
                    "--runs-root", str(runs_root),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                    "--agent-bin", str(worker),
                    "--state-dir", str(state_dir),
                ])
            self.assertEqual(20, rc_pause1)
            pending1 = checkpoints.list_approvals(state_dir, status="pending", checkpoint_id="brief")
            self.assertEqual(1, len(pending1))
            aid1 = pending1[0]["id"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, devpack_approve.main([
                    "--workdir", str(proj_a), "--state-dir", str(state_dir), "approve", aid1,
                ]))

            prev_cwd = os.getcwd()
            try:
                os.chdir(unrelated_dir)
                out1 = io.StringIO()
                with contextlib.redirect_stdout(out1):
                    rc_res1 = devpack_dispatch.main([
                        "--state-dir", str(state_dir), "--resume", aid1,
                    ])
            finally:
                os.chdir(prev_cwd)
            self.assertEqual(0, rc_res1)
            payload1 = json.loads(out1.getvalue().strip().splitlines()[-1])
            run1_json = json.loads((Path(payload1["run_dir"]) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual("autonomous", run1_json["pipeline_mode"])

            # 2. Pause autonomous job in project A, change project A config to hitl, resume from unrelated dir -> hitl
            with contextlib.redirect_stdout(io.StringIO()):
                rc_pause2 = devpack_dispatch.main([
                    "--engine", "haiku",
                    "--brief", str(brief_a),
                    "--workdir", str(proj_a),
                    "--allow", "src/**",
                    "--gate-cmd", "true",
                    "--runs-root", str(runs_root),
                    "--isolation", "local",
                    "--allow-unsandboxed-local",
                    "--agent-bin", str(worker),
                    "--state-dir", str(state_dir),
                ])
            self.assertEqual(20, rc_pause2)
            pending2 = checkpoints.list_approvals(state_dir, status="pending", checkpoint_id="brief")
            self.assertEqual(1, len(pending2))
            aid2 = pending2[0]["id"]

            (proj_a / ".devpack" / "project.toml").write_text('[pipeline]\nmode = "hitl"\n', encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, devpack_approve.main([
                    "--workdir", str(proj_a), "--state-dir", str(state_dir), "approve", aid2,
                ]))

            try:
                os.chdir(unrelated_dir)
                out2 = io.StringIO()
                with contextlib.redirect_stdout(out2):
                    rc_res2 = devpack_dispatch.main([
                        "--state-dir", str(state_dir), "--resume", aid2,
                    ])
            finally:
                os.chdir(prev_cwd)
            self.assertEqual(0, rc_res2)
            payload2 = json.loads(out2.getvalue().strip().splitlines()[-1])
            run2_json = json.loads((Path(payload2["run_dir"]) / "run.json").read_text(encoding="utf-8"))
            self.assertEqual("hitl", run2_json["pipeline_mode"])


if __name__ == "__main__":
    unittest.main()
