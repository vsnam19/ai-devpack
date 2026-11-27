"""Offline fixtures for the dashboard's Pack reader."""

import json
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from runner import heartbeat
from runner.dash.ui import job_lines

from runner.dash.reader import Reader, Settings


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class ReaderTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(self.root, self.root / "runs", self.root / "state", history_limit=1)
        self.reader = Reader(self.settings, clock=lambda: 1000)

    def run_record(self, name, **fields):
        path = self.settings.runs / name
        write(path / "run.json", {"kind": "task", **fields})
        return path

    def test_plan_snapshot_joins_attempts_and_redacts_briefs(self):
        example = Path(__file__).resolve().parents[2] / "workflow/templates/plan.example"
        root = self.root / "custom-plans"
        plan = copytree(example, root / "example")
        brief = plan / "briefs/T1.md"
        brief.write_text(brief.read_text() + "\nTOKEN=fixture-secret-value\n")
        self.run_record("linked", plan_id="example", task_id="T1", outcome="pass")
        self.run_record("legacy", task="T1", outcome="fail")
        self.run_record("other", plan_id="another-plan", task_id="T1", outcome="fail")
        outside = copytree(example, self.root / "outside")
        outside_manifest = outside / "plan.toml"
        outside_manifest.write_text(outside_manifest.read_text().replace('id = "example"', 'id = "linked-plan"'))
        (root / "linked-plan").symlink_to(outside, target_is_directory=True)
        bad = copytree(example, root / "bad")
        bad_manifest = bad / "plan.toml"
        bad_manifest.write_text(bad_manifest.read_text().replace('id = "example"', 'id = "bad"'))
        (bad / "briefs/T1.md").unlink()
        (bad / "briefs/T1.md").symlink_to(outside / "briefs/T1.md")
        settings = Settings(self.root, self.settings.runs, self.settings.state, plans=root)
        snapshot = Reader(settings).snapshot()
        self.assertEqual(snapshot["sources"]["plans"], "partial")
        self.assertEqual([entry["id"] for entry in snapshot["plans"]], ["example"])
        entry = snapshot["plans"][0]
        self.assertEqual((entry["done"], entry["total"]), (1, 3))
        first, second, third = entry["tasks"]
        self.assertEqual([record["run_id"] for record in first["runs"]], ["linked"])
        self.assertIn("Draft the guide", first["brief_excerpt"])
        self.assertNotIn("fixture-secret-value", first["brief_excerpt"])
        self.assertEqual(second["state"], "ready")
        self.assertEqual(third["blocked_by"], ["T2"])

    def test_detail_outside_window_and_legacy_cost(self):
        run = self.run_record("old", outcome="fail")
        self.run_record("recent", outcome="pass")
        write(run / "run_summary.json", {"status": "pass", "cost_usd": 0})
        snapshot = self.reader.snapshot(query="state=failed")
        self.assertEqual(snapshot["jobs"][0]["id"], "old")
        detail = self.reader.snapshot(job_id="old")["jobs"][0]
        self.assertEqual(detail["state"], "failed")
        self.assertIsNone(detail["cost_usd"])
        self.assertIsNone(detail["process_health"])

    def test_heartbeat_is_independent_of_event_age_and_overridden_by_state(self):
        run = self.run_record("active", outcome="running")
        record = {
            "schema_version": 1, "run_id": "active", "pid": 123, "host_id": "host",
            "boot_id": "boot", "pid_namespace": "ns", "pid_start_identity": "start",
            "heartbeat_at": 996, "last_event_at": 600,
        }
        write(run / "heartbeat.json", record)
        with (
            patch.object(heartbeat, "host_id", return_value="host"),
            patch.object(heartbeat, "boot_id", return_value="boot"),
            patch.object(heartbeat, "pid_identity", return_value=("start", "ns")) as probe,
        ):
            job = self.reader.snapshot(job_id="active")["jobs"][0]
            self.assertEqual((job["process_health"], job["activity"], job["last_event_time"]),
                             ("alive", "quiet", 600))
            self.assertIn("Process health alive", list(job_lines(job, "Details")))
            probe.assert_called_once_with(123)
            for outcome in ("awaiting_approval", "pass"):
                self.run_record("active", outcome=outcome)
                job = self.reader.snapshot(job_id="active")["jobs"][0]
                self.assertIsNone(job["process_health"])
            probe.assert_called_once_with(123)

    def test_heartbeat_written_during_read_is_current(self):
        run = self.run_record("active", outcome="running")
        path = run / "heartbeat.json"
        now = 1000
        reader = Reader(self.settings, clock=lambda: now)
        read_optional = reader.optional

        def optional_with_heartbeat_update(candidate):
            nonlocal now
            if candidate == path:
                now = 1001
                write(path, {
                    "schema_version": 1, "run_id": "active", "pid": 123, "host_id": "host",
                    "boot_id": "boot", "pid_namespace": "ns", "pid_start_identity": "start",
                    "heartbeat_at": now, "last_event_at": now,
                })
            return read_optional(candidate)

        with (
            patch.object(reader, "optional", side_effect=optional_with_heartbeat_update),
            patch.object(heartbeat, "host_id", return_value="host"),
            patch.object(heartbeat, "boot_id", return_value="boot"),
            patch.object(heartbeat, "pid_identity", return_value=("start", "ns")),
        ):
            job = reader.snapshot(job_id="active")["jobs"][0]
        self.assertEqual(job["process_health"], "alive")
        self.assertEqual(job["last_event_time"], 1001)

    def test_mixed_usage_and_versioned_zero(self):
        run = self.run_record("mixed", outcome="pass")
        write(run / "steps" / "01-build-engine.meta.json",
              {"schema_version": 1, "usage_provenance": "measured", "cost_usd": 0, "tokens_in": 0})
        write(run / "steps" / "02-review-engine.meta.json",
              {"schema_version": 1, "usage_provenance": "unknown", "cost_usd": None, "tokens_out": 0})
        job = self.reader.snapshot(job_id="mixed")["jobs"][0]
        self.assertEqual((job["cost_usd"], job["tokens_in"], job["tokens_out"]), (0, 0, 0))
        self.assertEqual(job["cost_usd_coverage"], {"source": "steps", "known": 1, "eligible": 2})
        write(run / "run_summary.json", {
            "status": "pass", "schema_version": 1, "usage_provenance": "measured", "cost_usd": 0,
            "usage_coverage": {"cost_usd": {"known": 1, "eligible": 2}},
        })
        job = self.reader.snapshot(job_id="mixed")["jobs"][0]
        self.assertEqual(job["cost_usd"], 0)
        self.assertEqual(job["cost_usd_coverage"], {"source": "summary", "known": 1, "eligible": 2})
        self.assertTrue(any(line.startswith("Cost ") and " + ?" in line for line in job_lines(job, "Details")))

    def test_structured_gate_and_active_role(self):
        run = self.run_record("direct", outcome="running", engines=["worker-engine"])
        write(run / "steps" / "01-broker-worker-worker-engine.meta.json", {"stderr": "x" * 280000})
        write(run / "step_01_gate.json", {"gate_passed": False, "returncode": 0, "stdout": "x" * 280000})
        job = self.reader.snapshot(job_id="direct")["jobs"][0]
        self.assertEqual(job["role"], "broker-worker")
        self.assertEqual(job["engine"], "worker-engine")
        self.assertEqual(job["gates"][0]["status"], "fail")
        self.assertEqual(job["gates"][0]["returncode"], 0)

    def test_broker_worker_reconciliation_and_conflicts(self):
        wrapper = self.run_record("wrapper", kind="dispatch", task="broker-request", outcome="fail")
        worker = self.run_record("worker", kind="dispatch", outcome="pass")
        (worker / "steps").mkdir()
        (worker / "steps" / "01-broker-worker-engine.jsonl").write_text("unused\n")
        self.run_record("other", kind="dispatch", outcome="fail")
        write(wrapper / "req.request.json", {})
        pending = self.reader.snapshot(job_id="req")["jobs"][0]
        self.assertIn("Worker link unknown", pending["notices"])
        self.assertEqual(self.reader.snapshot(job_id="worker")["jobs"][0]["id"], "worker")
        write(wrapper / "req.response.json", {"run_dir": "~/elsewhere/worker", "status": "ok"})
        linked = self.reader.snapshot(job_id="worker")["jobs"][0]
        self.assertEqual(linked["id"], "req")
        self.assertEqual(linked["key"], pending["key"])
        self.assertEqual(linked["state"], "succeeded")
        self.assertEqual(linked["broker_disposition"], "fail")
        self.assertIsNone(self.reader.stream)
        write(self.root / ".devpack" / "queue" / "req.response.json", {"run_dir": "/elsewhere/other"})
        conflict = self.reader.snapshot(job_id="req")["jobs"][0]
        self.assertIn("Conflicting broker links", conflict["notices"])
        self.assertIsNone(conflict["worker_id"])

    def test_refusal_before_step_and_unverified_process(self):
        wrapper = self.run_record("wrapper", kind="dispatch", task="broker-request", outcome="fail")
        write(wrapper / "req.request.json", {})
        write(wrapper / "req.response.json", {"run_dir": "~/wrapper", "status": "refused", "error": "rule failure"})
        refusal = self.reader.snapshot(job_id="req")["jobs"][0]
        self.assertEqual(refusal["state"], "refused")
        self.assertEqual(refusal["steps"], [])
        self.assertEqual(refusal["diagnostics"]["broker_error"], "rule failure")
        self.run_record("active", kind="dispatch", outcome="running", pid=123, arbitrary="private")
        active = self.reader.snapshot(job_id="active")["jobs"][0]
        self.assertIsNone(active["process_health"])
        self.assertIsNone(active["last_event_time"])
        self.assertNotIn("private", json.dumps(active))
        self.run_record("missing-result", kind="dispatch")
        self.assertIsNone(self.reader.snapshot(job_id="missing-result")["jobs"][0]["state"])

    def test_approval_joins_and_bounded_evidence(self):
        run = self.run_record("paused", outcome="awaiting_approval", approval_id="brief-id", checkpoint_id="brief")
        write(run / "steps" / "00-brief-lint.json", {"brief_lint": [{"message": "bad heading"}], "error_count": 1})
        write(run / "run_summary.json", {"memory_proposals": {"pending": ["memory-id"]}})
        state = self.settings.state
        brief = {"id": "brief-id", "checkpoint": "brief", "status": "pending", "run_dir": "~/removed-name"}
        pending = write(state / "approvals" / "pending" / "brief-id.json", brief)
        with self.assertRaises(KeyError):
            self.reader.snapshot()["approvals"][0]["evidence"]
        before = self.reader.snapshot(job_id="paused", include_evidence=True)
        self.assertEqual(before["jobs"][0]["state"], "waiting for approval")
        self.assertEqual(before["approvals"][0]["evidence"]["brief_status"], "unavailable")
        decided = write(state / "approvals" / "decided" / "brief-id.json", {**brief, "status": "approved"})
        write(state / "runs" / "brief-id.json", {"kind": "run", "checkpoint_id": "brief", "approval_id": "brief-id",
                                                 "credentials_path": "private-payload"})
        snapshot = self.reader.snapshot(job_id="paused", include_evidence=True)
        self.assertEqual(snapshot["jobs"][0]["state"], "ready to resume")
        self.assertEqual(snapshot["approvals"][0]["status"], "approved")
        self.assertNotIn("private-payload", json.dumps(snapshot))
        (state / "runs" / "brief-id.consumed").touch()
        consumed = self.reader.snapshot(job_id="paused")["jobs"][0]
        self.assertEqual(consumed["state"], "waiting for approval")
        self.assertIn("Resume status unknown", consumed["notices"])
        decided.unlink()
        pending.unlink()
        write(state / "approvals" / "pending" / "memory-vote.json",
              {"id": "memory-vote", "status": "pending", "checkpoint": "memory-decision",
               "requested_by": "run:paused", "metadata": {"memory_id": "memory-id", "scope": "project"}})
        evidence = self.root / ".devpack" / "memory" / "project" / "memory-id.md"
        evidence.parent.mkdir(parents=True)
        secret = "fictional-token-for-testing"
        evidence.write_text(f"note\nAuthorization: Bearer {secret}\n", encoding="utf-8")
        approval = self.reader.snapshot(include_evidence=True)["approvals"][0]
        self.assertEqual(approval["run_id"], "paused")
        self.assertIn("[REDACTED]", approval["evidence"]["excerpts"][0]["text"])
        self.assertNotIn(secret, json.dumps(approval))
        write(state / "approvals" / "pending" / "by-path.json",
              {"id": "by-path", "status": "pending", "checkpoint": "pre-merge", "run_dir": "~/paused"})
        approvals = {item["id"]: item for item in self.reader.snapshot()["approvals"]}
        self.assertEqual(approvals["by-path"]["run_id"], "paused")

    def test_alternate_gate_and_symlink_boundaries(self):
        from dataclasses import replace

        run = self.run_record("task", outcome="fail")
        transcript = run / "steps" / "01-gate.txt"
        transcript.parent.mkdir()
        transcript.write_text("custom failure\n", encoding="utf-8")
        self.assertIsNone(self.reader.snapshot(job_id="task")["jobs"][0]["gates"][0]["status"])
        logs = self.root / "logs"
        write(logs / "run_summary.json", {"run_dir": "/redacted/task"})
        write(logs / "step_01_gate.json", {"gate_passed": False, "returncode": 42})
        alternate = Reader(replace(self.settings, log_roots=(logs,)))
        gate = alternate.snapshot(job_id="task")["jobs"][0]["gates"][0]
        self.assertEqual((gate["status"], gate["returncode"], gate["transcript"]), ("fail", 42, "custom failure\n"))
        secret = write(self.root / "outside.json", {"kind": "task", "task": "outside-boundary"})
        unwanted = self.settings.runs / "unwanted"
        unwanted.mkdir()
        (unwanted / "run.json").symlink_to(secret)
        result = alternate.snapshot(job_id="unwanted")
        self.assertEqual(result["jobs"], [])
        self.assertEqual(result["scan"]["unreadable"], 1)

    def test_resumed_history_and_stale_snapshot(self):
        import os
        from unittest.mock import patch

        older = self.run_record("older", outcome="pass")
        newer = self.run_record("newer", outcome="pass")
        for path in (older, older / "run.json", newer, newer / "run.json"):
            rank = 10 if path in (older, older / "run.json") else 20
            os.utime(path, (rank, rank))
        monotonic = [0]
        reader = Reader(self.settings, clock=lambda: 1000, monotonic=lambda: monotonic[0])
        self.assertEqual(reader.snapshot(interactive=True)["jobs"][0]["id"], "newer")
        with patch.object(reader, "read_run", wraps=reader.read_run) as read:
            monotonic[0] = 5
            self.assertEqual(reader.snapshot(interactive=True)["jobs"][0]["id"], "newer")
            read.assert_not_called()
        write(older / "run.json", {"kind": "task", "outcome": "running"})
        monotonic[0] = 10
        resumed = reader.snapshot(interactive=True)["jobs"][0]
        self.assertEqual(resumed["id"], "older")
        (older / "run.json").write_text("{")
        monotonic[0] = 13
        stale = reader.snapshot(interactive=True)["jobs"][0]
        self.assertEqual(stale["state"], "running")
        self.assertEqual(stale["health"], "stale")
        self.assertEqual(stale["last_good_at"], 1000)

    def test_incremental_utf8_controls_and_tool_filter(self):
        run = self.run_record("active", outcome="running")
        stream = run / "steps" / "01-implement-engine.jsonl"
        stream.parent.mkdir()
        stream.touch()
        initial = self.reader.snapshot(job_id="active", event_kind="all")
        self.assertEqual(initial["sources"]["stream"], "empty")
        self.assertIn("buffered", initial["jobs"][0]["notices"][-1])
        message = b'\x1b[32m{"type":"message","content":"caf\xc3\xa9"}\x1b[0m\n'
        stream.write_bytes(message[:2])
        self.assertEqual(self.reader.snapshot(job_id="active", event_kind="all")["events"], [])
        with stream.open("ab") as output:
            output.write(message[2:message.index(b"\xa9")])
        self.assertEqual(self.reader.snapshot(job_id="active", event_kind="all")["events"], [])
        with stream.open("ab") as output:
            output.write(message[message.index(b"\xa9"):])
            output.write(b'{"type":"tool_use","name":"shell","id":"call","input":{"command":"ls"}}\n')
            output.write(b'{"type":"tool_result","tool_use_id":"call","content":"failed","is_error":true}\n')
        all_events = self.reader.snapshot(job_id="active", event_kind="all")["events"]
        self.assertEqual(all_events[0]["text"], "café")
        tools = self.reader.snapshot(job_id="active", event_kind="tool")["events"]
        self.assertEqual([event["tool"] for event in tools], ["shell", "shell"])
        self.assertTrue(tools[-1]["is_error"])
        stream.write_bytes(b"plain output\n")
        truncated = self.reader.snapshot(job_id="active", event_kind="all")
        self.assertEqual(truncated["sources"]["stream"], "plain")
        self.assertTrue(any("truncated" in (event["text"] or "") for event in truncated["events"]))
        self.assertEqual(truncated["events"][-1]["text"], "plain output")
        replacement = stream.with_suffix(".new")
        replacement.write_bytes(b"other output\n")
        replacement.replace(stream)
        replaced = self.reader.snapshot(job_id="active", event_kind="all")
        self.assertEqual(replaced["events"][-1]["text"], "other output")
        self.assertIn("replaced", replaced["events"][-2]["text"])

    def test_stream_budget_and_oversized_record(self):
        from runner.dash.events import Stream
        from runner.dash.files import RECORD_LIMIT, TICK_LIMIT

        path = self.root / "selected.jsonl"
        path.write_bytes(b'{"type":"message","content":"first"}\n' + b"x" * (TICK_LIMIT + 1) + b"\n")
        stream = Stream(self.reader.redactor, lambda: 1000)
        status, used = stream.poll(path)
        self.assertEqual(status, "jsonl")
        self.assertEqual(used, TICK_LIMIT)
        self.assertLessEqual(len(stream.partial), RECORD_LIMIT)
        stream.poll(path)
        self.assertEqual(stream.errors, 1)
        self.assertEqual(stream.events[-1]["text"], "Oversized stream record")

    def test_broker_brief_requires_recorded_digest(self):
        import hashlib

        brief = self.root / "brief.md"
        brief.write_text("verified brief", encoding="utf-8")
        wrapper = self.run_record(
            "wrapper", kind="dispatch", task="broker-request", outcome="awaiting_approval",
            approval_id="pause", checkpoint="brief", brief_sha256=hashlib.sha256(brief.read_bytes()).hexdigest(),
        )
        write(wrapper / "req.request.json", {"brief": "brief.md"})
        write(wrapper / "req.response.json", {"run_dir": "~/wrapper", "status": "AWAITING_APPROVAL"})
        write(self.settings.state / "approvals" / "pending" / "pause.json",
              {"id": "pause", "status": "pending", "checkpoint": "brief", "run_dir": "~/wrapper"})
        snapshot = self.reader.snapshot(job_id="req", include_evidence=True)
        self.assertEqual(snapshot["jobs"][0]["state"], "waiting for approval")
        self.assertEqual(snapshot["approvals"][0]["evidence"]["brief"], "verified brief")
        brief.write_text("changed after checkpoint", encoding="utf-8")
        evidence = self.reader.snapshot(include_evidence=True)["approvals"][0]["evidence"]
        self.assertEqual(evidence["brief_status"], "unavailable")
        self.assertIsNone(evidence["brief"])

    def test_model_filter_and_quiet_with_seen_event(self):
        from dataclasses import replace

        run = self.run_record("active", outcome="running", engines=["worker-engine"])
        (run / "steps").mkdir()
        (run / "steps" / "01-broker-worker-worker-engine.jsonl").write_text("activity\n")
        now = [1000]
        reader = Reader(replace(self.settings, models=(("worker-engine", "Sample Model"),)), clock=lambda: now[0])
        query = 'backend=PACK role=broker-worker model="sample model" WORKER'
        job = reader.snapshot(query=query)["jobs"][0]
        self.assertEqual(job["model_source"], "configured")
        self.assertIsNone(job["activity"])
        self.assertIsNone(reader.stream)
        observed = reader.snapshot(job_id="active", event_kind="all")["jobs"][0]
        self.assertEqual(observed["last_event_time"], 1000)
        now[0] += 301
        details = reader.snapshot(job_id="active")["jobs"][0]
        self.assertEqual(details["last_event_age_s"], 301)
        self.assertEqual(details["activity"], "quiet")
        self.assertIsNone(details["process_health"])

    def test_continuous_batch_preserves_events_beyond_ui_history(self):
        from runner.dash.events import Stream

        path = self.root / "stream.jsonl"
        path.write_bytes(b'{"type":"message","content":"same"}\n' * 1100)
        stream = Stream(self.reader.redactor, lambda: 1000)
        stream.poll(path, collect=True)
        self.assertEqual(len(stream.events), 1000)
        self.assertEqual(len(stream.batch), 1100)
        self.assertEqual((stream.batch[0]["sequence"], stream.batch[-1]["sequence"]), (1, 1100))

    def test_checkpoint_writer_statuses_and_escalation(self):
        state = self.settings.state
        for status, expected in (("PENDING", "waiting for approval"),
                                 ("APPROVED", "ready to resume"), ("REJECTED", "rejected")):
            with self.subTest(status=status):
                name = status.lower()
                self.run_record(name, outcome="awaiting_approval", approval_id=name, checkpoint_id="brief")
                folder = "pending" if status == "PENDING" else "decided"
                write(state / "approvals" / folder / f"{name}.json",
                      {"id": name, "status": status, "checkpoint": "brief"})
                if status == "APPROVED":
                    write(state / "runs" / f"{name}.json",
                          {"approval_id": name, "checkpoint_id": "brief", "kind": "run"})
                snapshot = self.reader.snapshot(job_id=name)
                self.assertEqual(snapshot["sources"]["approvals"], "current")
                self.assertEqual(snapshot["pending_approvals"], 1)
                self.assertEqual(snapshot["jobs"][0]["state"], expected)
                approval = next(item for item in snapshot["approvals"] if item["id"] == name)
                self.assertEqual(approval["status"], name)
                if status == "PENDING":
                    self.assertTrue(approval["commands"])

        (state / "runs" / "approved.consumed").touch()
        self.run_record("approved", outcome="ESCALATE_TO_HUMAN", approval_id="approved",
                        checkpoint_id="brief", checkpoints=[{"checkpoint": "escalation"}])
        self.assertEqual(self.reader.snapshot(query="state=failed")["jobs"][0]["id"], "approved")
        self.run_record("later", outcome="ESCALATE_TO_HUMAN", approval_id="later", checkpoint_id="deviation")
        write(state / "approvals" / "pending" / "later.json",
              {"id": "later", "status": "pending", "checkpoint": "deviation"})
        self.assertEqual(self.reader.snapshot(job_id="later")["jobs"][0]["state"], "waiting for approval")
        self.run_record("later", outcome="ESCALATE_TO_HUMAN", approval_id="later",
                        checkpoint_id="pre-merge", checkpoints=[{"checkpoint": "escalation"}])
        self.assertEqual(self.reader.snapshot(job_id="later")["jobs"][0]["state"], "failed")

    def test_large_diagnostics_and_isolated_gate_errors(self):
        run = self.run_record("large", outcome="fail", approval_id="brief-id", checkpoint_id="brief")
        diagnostic = "compiler failure " + "x" * 20000
        write(self.settings.state / "approvals" / "pending" / "brief-id.json",
              {"id": "brief-id", "status": "PENDING", "checkpoint": "brief"})
        write(run / "steps" / "01-worker-engine.meta.json",
              {"stderr": diagnostic, "final_text": "x" * 280000})
        (run / "steps" / "01-worker-engine.prompt.md").write_text(diagnostic)
        write(run / "step_01_gate.json", {"gate_passed": False, "returncode": 11, "stderr": "x" * 1100000})
        write(run / "step_02_gate.json", {"gate_passed": True, "stderr": diagnostic})
        write(run / "step_03_gate.json", {}).write_text("{")
        write(run / "steps" / "02-gate-receipt.json", {}).write_text("{")
        (run / "steps" / "01-gate.txt").write_text(diagnostic)
        (run / "steps" / "03-gate.txt").write_text("fallback diagnostic")
        (run / "steps" / "01-worker-engine.jsonl").write_text(
            json.dumps({"type": "tool_result", "content": diagnostic}) + "\n")
        snapshot = self.reader.snapshot(job_id="large", event_kind="all", include_evidence=True)
        job = snapshot["jobs"][0]
        self.assertFalse(job["read_failed"])
        self.assertEqual(job["steps"][0]["stderr"], diagnostic[:1600])
        self.assertEqual(job["steps"][0]["final_text"], "[Text exceeds display limit]")
        self.assertEqual(snapshot["approvals"][0]["evidence"]["brief"], diagnostic[:1600])
        gates = job["gates"]
        self.assertEqual((gates[0]["status"], gates[0]["returncode"]), ("fail", 11))
        self.assertEqual(gates[0]["transcript"], diagnostic[:1600])
        self.assertEqual((gates[1]["status"], gates[1]["stderr"]), ("pass", diagnostic[:1600]))
        self.assertEqual(gates[2]["transcript"], "fallback diagnostic")
        self.assertEqual(snapshot["events"][0]["text"], diagnostic[:1600])
        self.assertIn("exceeds read limit", gates[0]["artifact_notice"])
        self.assertIn("Gate receipt unreadable for step 2", job["notices"])
        write(run / "steps" / "02-gate-receipt.json",
              {"gate_status": "GATE_FAIL", "checks": [{"stderr": "x" * 1100000}]})
        gates = self.reader.snapshot(job_id="large")["jobs"][0]["gates"]
        self.assertEqual(gates[1]["status"], "GATE_FAIL")
        self.assertIn("exceeds read limit", gates[1]["artifact_notice"])

        unreadable = self.run_record("unreadable", outcome="fail")
        write(unreadable / "step_01_gate.json", {}).write_text("{")
        self.assertTrue(self.reader.snapshot(job_id="unreadable")["jobs"][0]["read_failed"])

    def test_gate_receipts_are_read_for_each_numbered_attempt(self):
        run = self.run_record("retry", outcome="pass")
        write(run / "steps" / "02-gate-receipt.json", {"gate_status": "GATE_FAIL"})
        write(run / "steps" / "05-gate-receipt.json", {"gate_status": "GATE_OK"})
        (run / "steps" / "05-gate.txt").write_text("passed after fix")
        gates = self.reader.snapshot(job_id="retry")["jobs"][0]["gates"]
        self.assertEqual([(item["step"], item["status"]) for item in gates],
                         [(2, "GATE_FAIL"), (5, "GATE_OK")])
        self.assertEqual(gates[1]["transcript"], "passed after fix")

    def test_gate_receipt_preserves_combined_verdict_and_findings(self):
        run = self.run_record("combined", outcome="fail")
        lint = [{"message": "lint failure"}]
        static = [{"message": "static failure"}]
        checks = [{"name": "unit", "status": "PASS"}]
        write(run / "step_02_gate.json", {
            "gate_passed": False, "lint_findings": lint, "static_findings": static,
        })
        write(run / "steps" / "02-gate-receipt.json", {"gate_status": "GATE_OK", "checks": checks})
        gate, = self.reader.snapshot(job_id="combined")["jobs"][0]["gates"]
        self.assertEqual(gate["status"], "fail")
        self.assertEqual(gate["lint_findings"], lint)
        self.assertEqual(gate["static_findings"], static)
        self.assertEqual(gate["checks"], checks)

    def test_interactive_broker_selection_refreshes_native_runs(self):
        from unittest.mock import patch

        request = "a" * 32
        wrapper = self.run_record("wrapper", kind="broker", outcome="pass")
        self.run_record("worker", kind="dispatch", outcome="pass")
        write(wrapper / f"{request}.request.json", {})
        write(wrapper / f"{request}.response.json", {"run_dir": "/target/worker"})
        self.reader.snapshot(interactive=True)
        with patch.object(self.reader, "refresh_run", wraps=self.reader.refresh_run) as refresh:
            snapshot = self.reader.snapshot(job_id=request, interactive=True)
        self.assertEqual([call.args[0] for call in refresh.call_args_list], ["wrapper", "worker"])
        self.assertTrue(snapshot["scan"]["complete"])
        self.assertEqual(snapshot["scan"]["scanned"], 2)
        self.assertEqual(snapshot["jobs"][0]["worker_id"], "worker")

    def test_configured_root_below_linked_parent(self):
        from dataclasses import replace

        parent = self.root / "linked-parent"
        parent.symlink_to(self.root, target_is_directory=True)
        self.run_record("visible", outcome="pass")
        reader = Reader(replace(self.settings, runs=parent / "runs"))
        self.assertEqual(reader.snapshot(job_id="visible")["jobs"][0]["id"], "visible")
        (self.settings.runs / "linked-run").symlink_to(self.settings.runs / "visible", target_is_directory=True)
        self.assertEqual(reader.snapshot(job_id="linked-run")["jobs"], [])
        linked_root = self.root / "linked-root"
        linked_root.symlink_to(self.settings.runs, target_is_directory=True)
        self.assertEqual(Reader(replace(self.settings, runs=linked_root)).snapshot()["sources"]["runs"],
                         "inaccessible")
