"""Offline fixtures for the dashboard's Pack reader."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

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
