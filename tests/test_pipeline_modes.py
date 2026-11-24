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


if __name__ == "__main__":
    unittest.main()
