"""Offline checks for run ownership and heartbeat classification."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from runner.agent_exec import new_run_dir, parse_stream, update_run_json
from runner import heartbeat


class HeartbeatTests(unittest.TestCase):
    def test_quiet_wait_and_pause_or_completion(self):
        for outcome in ("awaiting_approval", "pass"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as root:
                now = 100.0
                waiting = threading.Event()
                tick = threading.Event()
                refreshed = threading.Event()
                original_write = heartbeat.Heartbeat.write

                def fake_wait(stop, interval):
                    self.assertEqual(interval, 5)
                    waiting.set()
                    while not tick.is_set():
                        if stop.wait(0.01):
                            return True
                    tick.clear()
                    return stop.is_set()

                def write(owner):
                    original_write(owner)
                    refreshed.set()

                @heartbeat.owns_runs
                def run_child():
                    nonlocal now
                    run = new_run_dir(Path(root))
                    path = run / "heartbeat.json"
                    self.assertTrue(waiting.wait(timeout=2))
                    self.assertEqual(json.loads(path.read_text())["heartbeat_at"], 100.0)
                    refreshed.clear()
                    now = 116.0
                    tick.set()
                    self.assertTrue(refreshed.wait(timeout=2))
                    current = json.loads(path.read_text())
                    self.assertEqual(current["heartbeat_at"], now)
                    self.assertIsNone(current["last_event_at"])
                    self.assertEqual(heartbeat.process_health(current, run.name, now), "alive")
                    owner = heartbeat.current(run)
                    update_run_json(run, outcome=outcome)
                    self.assertFalse(owner.thread.is_alive())

                with (
                    patch.object(heartbeat.time, "time", side_effect=lambda: now),
                    patch.object(heartbeat.Heartbeat, "wait", side_effect=fake_wait),
                    patch.object(heartbeat.Heartbeat, "write", write),
                    patch.object(heartbeat, "host_id", return_value="host"),
                    patch.object(heartbeat, "boot_id", return_value="boot"),
                    patch.object(heartbeat, "pid_identity", return_value=("start", "namespace")),
                ):
                    run_child()

    def test_owner_scope_stops_worker_after_exception(self):
        with tempfile.TemporaryDirectory() as root:
            owners = []

            @heartbeat.owns_runs
            def fail():
                run = new_run_dir(root)
                owner = heartbeat.current(run)
                self.assertTrue(owner.thread.is_alive())
                owners.append(owner)
                raise RuntimeError("child wait failed")

            with self.assertRaisesRegex(RuntimeError, "child wait failed"):
                fail()
            self.assertFalse(owners[0].thread.is_alive())

    def test_process_identity_and_killed_owner(self):
        record = {
            "schema_version": 1, "run_id": "run", "pid": 10, "host_id": "host",
            "boot_id": "boot", "pid_start_identity": "start", "pid_namespace": "namespace",
            "heartbeat_at": 100.0,
        }
        with (
            patch.object(heartbeat, "host_id", return_value="host"),
            patch.object(heartbeat, "boot_id", return_value="boot") as boot,
            patch.object(heartbeat, "pid_identity", return_value=("start", "namespace")) as pid,
        ):
            self.assertEqual(heartbeat.process_health(record, "run", 114), "alive")
            self.assertEqual(heartbeat.process_health(record, "run", 115), "stale")
            pid.side_effect = ProcessLookupError
            self.assertEqual(heartbeat.process_health(record, "run", 115), "stale")
            pid.side_effect = PermissionError
            self.assertEqual(heartbeat.process_health(record, "run", 115), "unknown")
            pid.side_effect = None
            pid.return_value = ("reused", "namespace")
            self.assertNotEqual(heartbeat.process_health(record, "run", 101), "alive")
            boot.return_value = "new boot"
            self.assertNotEqual(heartbeat.process_health(record, "run", 101), "alive")
            boot.return_value = None
            pid.return_value = ("start", None)
            record.update(boot_id=None, pid_namespace=None)
            self.assertEqual(heartbeat.process_health(record, "run", 101), "alive")
            record["host_id"] = "remote"
            self.assertEqual(heartbeat.process_health(record, "run", 101), "unknown")
            record["host_id"] = "host"
            record["pid_start_identity"] = None
            self.assertEqual(heartbeat.process_health(record, "run", 101), "unknown")


class NullableUsageTests(unittest.TestCase):
    def test_stream_measurements(self):
        explicit = parse_stream('{"type":"result","cost_usd":0,"usage":{"input_tokens":0}}')
        self.assertEqual((explicit.cost_usd, explicit.tokens_in, explicit.input_tokens), (0, 0, 0))
        self.assertIsNone(explicit.tokens_out)
        for stream in ('{"type":"result"}', '{"type":"result","cost_usd":0'):
            with self.subTest(stream=stream):
                result = parse_stream(stream)
                self.assertEqual((result.cost_usd, result.tokens_in, result.tokens_out), (None, None, None))
