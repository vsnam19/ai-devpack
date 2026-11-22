"""Unit tests for devpack_logs analyze and attribute subcommands."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

RUNNER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = RUNNER_DIR.parent
for _p in (str(REPO_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import devpack_logs  # noqa: E402

ATTRIBUTION_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "attribution_runs"
SAMPLE_RUN = REPO_ROOT / "tests" / "fixtures" / "sample_run"
EVAL_FIXTURE = REPO_ROOT / "eval" / "fixture"
ENGINES_EXAMPLE = REPO_ROOT / "runner" / "engines.example.toml"


class FailureAttributionTests(unittest.TestCase):
    """Verify heuristic attribution across BRIEF/CONTEXT/TOOL/ENV and confirmation validation."""

    def _copy_fixture(self, src_dir: Path, tmp_root: Path, name: str) -> Path:
        """Copy an immutable run fixture into ``tmp_root`` before running analyze/attribute."""
        dst = tmp_root / name
        shutil.copytree(src_dir, dst)
        return dst

    def test_analyze_brief_missing_path(self) -> None:
        """Crafted BRIEF fixture run yields suspected_cause == 'BRIEF' with evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._copy_fixture(ATTRIBUTION_FIXTURES / "brief_missing_path", Path(tmp), "run")
            with contextlib.redirect_stdout(io.StringIO()):
                rc = devpack_logs.main(
                    ["analyze", str(run_dir), "--repo", str(EVAL_FIXTURE), "--engines", str(ENGINES_EXAMPLE)]
                )
            self.assertEqual(0, rc)
            attr = json.loads((run_dir / "attribution.json").read_text(encoding="utf-8"))
            self.assertEqual("BRIEF", attr["suspected_cause"])
            self.assertIn(attr["confidence"], ("medium", "high"))
            self.assertTrue(attr["evidence"])
            self.assertTrue(attr["signals"]["missing_path_reads"])

    def test_analyze_context_many_searches_and_missing_symbol(self) -> None:
        """Crafted CONTEXT fixture run yields suspected_cause == 'CONTEXT' with search & symbol signals."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._copy_fixture(ATTRIBUTION_FIXTURES / "context_search_symbol", Path(tmp), "run")
            with contextlib.redirect_stdout(io.StringIO()):
                rc = devpack_logs.main(
                    ["analyze", str(run_dir), "--repo", str(EVAL_FIXTURE), "--engines", str(ENGINES_EXAMPLE)]
                )
            self.assertEqual(0, rc)
            attr = json.loads((run_dir / "attribution.json").read_text(encoding="utf-8"))
            self.assertEqual("CONTEXT", attr["suspected_cause"])
            self.assertEqual("high", attr["confidence"])
            self.assertGreaterEqual(attr["signals"]["search_calls_before_first_edit"], 5)
            missing_syms = [m["symbol"] for m in attr["signals"]["missing_symbols"]]
            self.assertIn("phantomRecoverLink", missing_syms)
            self.assertTrue(attr["evidence"])

    def test_analyze_tool_denied_calls_and_over_context(self) -> None:
        """Crafted TOOL fixtures (denied calls and prompt over context) yield suspected_cause == 'TOOL'."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            denied_run = self._copy_fixture(ATTRIBUTION_FIXTURES / "tool_denied", base, "denied")
            over_run = self._copy_fixture(ATTRIBUTION_FIXTURES / "tool_over_context", base, "over")

            with contextlib.redirect_stdout(io.StringIO()):
                rc1 = devpack_logs.main(["analyze", str(denied_run), "--engines", str(ENGINES_EXAMPLE)])
                rc2 = devpack_logs.main(["analyze", str(over_run), "--engines", str(ENGINES_EXAMPLE)])
            self.assertEqual(0, rc1)
            self.assertEqual(0, rc2)

            attr_denied = json.loads((denied_run / "attribution.json").read_text(encoding="utf-8"))
            self.assertEqual("TOOL", attr_denied["suspected_cause"])
            self.assertTrue(attr_denied["signals"]["denied_calls"])

            attr_over = json.loads((over_run / "attribution.json").read_text(encoding="utf-8"))
            self.assertEqual("TOOL", attr_over["suspected_cause"])
            self.assertTrue(attr_over["signals"]["prompt_over_context"])
            self.assertEqual(32768, attr_over["signals"]["context_window"])

    def _write_local_run(
        self,
        base: Path,
        request_inputs: list[int],
        result_input: int,
        *,
        message_usage: bool = True,
        result_cache_read: int = 0,
    ) -> Path:
        """Create a minimal run dir whose local step has one assistant turn per ``request_inputs`` entry.

        ``message_usage=False`` omits per-assistant-message usage so only the result event carries tokens.
        """
        run_dir = base / "ctx-run"
        steps_dir = run_dir / "steps"
        steps_dir.mkdir(parents=True)
        run_meta = {
            "run_id": "20261123T010300Z-t00009",
            "kind": "dispatch",
            "started": "2026-11-23T01:03:00Z",
            "finished": "2026-11-23T01:03:20Z",
            "engines": {"implementer": "local"},
            "task": "context-window-signal",
            "outcome": "ok",
        }
        (run_dir / "run.json").write_text(json.dumps(run_meta), encoding="utf-8")
        events: list[dict[str, object]] = [{"type": "system", "subtype": "init", "cwd": "/tmp/work", "model": "local-coder"}]
        for n in request_inputs:
            message: dict[str, object] = {
                "role": "assistant",
                "model": "local-coder",
                "content": [{"type": "text", "text": "Working on it."}],
            }
            if message_usage:
                message["usage"] = {"input_tokens": n, "output_tokens": 10}
            events.append({"type": "assistant", "message": message})
        events.append(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": len(request_inputs),
                "duration_ms": 1000,
                "total_cost_usd": 0.0,
                "usage": {
                    "input_tokens": result_input,
                    "cache_read_input_tokens": result_cache_read,
                    "output_tokens": 10 * len(request_inputs),
                },
                "result": "Done.",
            }
        )
        (steps_dir / "01-implementer-local.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
        )
        return run_dir

    def test_context_signal_uses_per_request_max_not_cumulative(self) -> None:
        """Three 12k turns on a 32k engine are not a prompt-over-context TOOL failure; cumulative is reported apart."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._write_local_run(Path(tmp), [12000, 12000, 12000], result_input=36000)
            with contextlib.redirect_stdout(io.StringIO()):
                rc = devpack_logs.main(["analyze", str(run_dir), "--engines", str(ENGINES_EXAMPLE)])
            self.assertEqual(0, rc)
            attr = json.loads((run_dir / "attribution.json").read_text(encoding="utf-8"))
            self.assertEqual(12000, attr["signals"]["max_request_tokens_in"])
            self.assertEqual(36000, attr["signals"]["cumulative_tokens_in"])
            self.assertEqual(32768, attr["signals"]["context_window"])
            self.assertFalse(attr["signals"]["prompt_over_context"])
            self.assertNotEqual("TOOL", attr["suspected_cause"])

    def test_context_signal_fires_on_single_oversized_request(self) -> None:
        """One request of 33k input tokens on a 32k engine yields the prompt-over-context TOOL signal."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._write_local_run(Path(tmp), [33000], result_input=33000)
            with contextlib.redirect_stdout(io.StringIO()):
                rc = devpack_logs.main(["analyze", str(run_dir), "--engines", str(ENGINES_EXAMPLE)])
            self.assertEqual(0, rc)
            attr = json.loads((run_dir / "attribution.json").read_text(encoding="utf-8"))
            self.assertEqual(33000, attr["signals"]["max_request_tokens_in"])
            self.assertTrue(attr["signals"]["prompt_over_context"])
            self.assertEqual("TOOL", attr["suspected_cause"])

    def test_context_signal_falls_back_to_result_usage_single_turn(self) -> None:
        """One turn with only result usage of 33k input tokens on a 32k engine is flagged over context."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._write_local_run(Path(tmp), [0], result_input=33000, message_usage=False)
            with contextlib.redirect_stdout(io.StringIO()):
                rc = devpack_logs.main(["analyze", str(run_dir), "--engines", str(ENGINES_EXAMPLE)])
            self.assertEqual(0, rc)
            attr = json.loads((run_dir / "attribution.json").read_text(encoding="utf-8"))
            self.assertEqual(33000, attr["signals"]["max_request_tokens_in"])
            self.assertTrue(attr["signals"]["prompt_over_context"])
            self.assertEqual("medium", attr["signals"]["context_signal_confidence"])
            self.assertEqual("TOOL", attr["suspected_cause"])

    def test_context_signal_fallback_counts_cache_tokens(self) -> None:
        """Fallback total is input + cache read; cache-only context pressure still triggers the signal."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._write_local_run(
                Path(tmp), [0], result_input=1000, message_usage=False, result_cache_read=32000
            )
            with contextlib.redirect_stdout(io.StringIO()):
                rc = devpack_logs.main(["analyze", str(run_dir), "--engines", str(ENGINES_EXAMPLE)])
            self.assertEqual(0, rc)
            attr = json.loads((run_dir / "attribution.json").read_text(encoding="utf-8"))
            self.assertEqual(33000, attr["signals"]["max_request_tokens_in"])
            self.assertTrue(attr["signals"]["prompt_over_context"])

    def test_context_signal_fallback_multi_turn_is_low_confidence(self) -> None:
        """Three turns with 36k result total and no per-message usage: estimate 12k, never a high-confidence flag."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._write_local_run(
                Path(tmp), [0, 0, 0], result_input=36000, message_usage=False
            )
            with contextlib.redirect_stdout(io.StringIO()):
                rc = devpack_logs.main(["analyze", str(run_dir), "--engines", str(ENGINES_EXAMPLE)])
            self.assertEqual(0, rc)
            attr = json.loads((run_dir / "attribution.json").read_text(encoding="utf-8"))
            self.assertEqual(12000, attr["signals"]["max_request_tokens_in"])
            self.assertEqual("low", attr["signals"]["context_signal_confidence"])
            self.assertFalse(attr["signals"]["prompt_over_context"])
            self.assertNotEqual("high", attr["confidence"])

    def test_context_signal_fallback_multi_turn_over_window_stays_low(self) -> None:
        """A low-confidence estimate over the window is flagged, but the cause confidence is capped below high."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._write_local_run(
                Path(tmp), [0, 0, 0], result_input=120000, message_usage=False
            )
            with contextlib.redirect_stdout(io.StringIO()):
                rc = devpack_logs.main(["analyze", str(run_dir), "--engines", str(ENGINES_EXAMPLE)])
            self.assertEqual(0, rc)
            attr = json.loads((run_dir / "attribution.json").read_text(encoding="utf-8"))
            self.assertEqual(40000, attr["signals"]["max_request_tokens_in"])
            self.assertEqual("low", attr["signals"]["context_signal_confidence"])
            self.assertTrue(attr["signals"]["prompt_over_context"])
            self.assertNotEqual("high", attr["confidence"])

    def test_analyze_env_timeout_and_quota(self) -> None:
        """Crafted ENV fixtures (timeout and quota) yield suspected_cause == 'ENV'."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            to_run = self._copy_fixture(ATTRIBUTION_FIXTURES / "env_timeout", base, "timeout")
            q_run = self._copy_fixture(ATTRIBUTION_FIXTURES / "env_quota", base, "quota")

            with contextlib.redirect_stdout(io.StringIO()):
                rc1 = devpack_logs.main(["analyze", str(to_run), "--engines", str(ENGINES_EXAMPLE)])
                rc2 = devpack_logs.main(["analyze", str(q_run), "--engines", str(ENGINES_EXAMPLE)])
            self.assertEqual(0, rc1)
            self.assertEqual(0, rc2)

            attr_to = json.loads((to_run / "attribution.json").read_text(encoding="utf-8"))
            self.assertEqual("ENV", attr_to["suspected_cause"])
            self.assertTrue(attr_to["signals"]["timeout"])

            attr_q = json.loads((q_run / "attribution.json").read_text(encoding="utf-8"))
            self.assertEqual("ENV", attr_q["suspected_cause"])
            self.assertTrue(attr_q["signals"]["quota_or_network_error"])

    def test_analyze_clean_sample_run(self) -> None:
        """Clean passing sample_run analyzes cleanly with suspected_cause == None."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._copy_fixture(SAMPLE_RUN, Path(tmp), "clean")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = devpack_logs.main(["analyze", str(run_dir), "--json", "--engines", str(ENGINES_EXAMPLE)])
            self.assertEqual(0, rc)
            attr = json.loads(buf.getvalue())
            self.assertIsNone(attr["suspected_cause"])
            self.assertEqual({"Read": 1, "Edit": 1, "Bash": 1}, attr["signals"]["tool_calls_by_kind"])

    def test_attribute_confirmation_and_validation(self) -> None:
        """Human attribution appends confirmed entry with timestamp and validates taxonomy."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._copy_fixture(ATTRIBUTION_FIXTURES / "brief_missing_path", Path(tmp), "run")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, devpack_logs.main(["analyze", str(run_dir)]))

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = devpack_logs.main(
                    [
                        "attribute",
                        str(run_dir),
                        "--cause",
                        "brief",
                        "--secondary",
                        "CONTEXT",
                        "--note",
                        "Missing file path in task brief",
                        "--by",
                        "tech-lead",
                    ]
                )
            self.assertEqual(0, rc)
            attr = json.loads((run_dir / "attribution.json").read_text(encoding="utf-8"))
            self.assertEqual(1, len(attr["confirmed"]))
            entry = attr["confirmed"][0]
            self.assertEqual("BRIEF", entry["cause"])
            self.assertEqual("CONTEXT", entry["secondary"])
            self.assertEqual("Missing file path in task brief", entry["note"])
            self.assertEqual("tech-lead", entry["by"])
            self.assertTrue(entry["timestamp"])

            # Invalid cause -> exit 2
            with contextlib.redirect_stderr(io.StringIO()):
                rc_bad_cause = devpack_logs.main(
                    ["attribute", str(run_dir), "--cause", "BOGUS", "--note", "x", "--by", "tl"]
                )
            self.assertEqual(2, rc_bad_cause)

            # Secondary equal to primary -> exit 2
            with contextlib.redirect_stderr(io.StringIO()):
                rc_same_sec = devpack_logs.main(
                    [
                        "attribute",
                        str(run_dir),
                        "--cause",
                        "BRIEF",
                        "--secondary",
                        "BRIEF",
                        "--note",
                        "x",
                        "--by",
                        "tl",
                    ]
                )
            self.assertEqual(2, rc_same_sec)


def _agent_stream(request_inputs: list[int], text: str) -> str:
    """Return a stream-json transcript with one assistant turn per request input and a closing text block."""
    events: list[dict[str, object]] = [{"type": "system", "subtype": "init", "cwd": "/tmp/work"}]
    for n in request_inputs:
        events.append(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": n, "output_tokens": 10},
                },
            }
        )
    events.append(
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": len(request_inputs), "result": text}
    )
    return "\n".join(json.dumps(e) for e in events) + "\n"


class StepwiseContextAndReworkTests(unittest.TestCase):
    """Per-step context window comparison and rework-based clean-pass rules in analyze."""

    def _write_run(self, base: Path, outcome: str, steps: list[tuple[str, str]]) -> Path:
        """Create run dir ``base`` (new) with run.json and steps; ``body`` is a stream or gate text."""
        run_dir = base
        steps_dir = run_dir / "steps"
        steps_dir.mkdir(parents=True)
        run_meta = {
            "run_id": "20261123T020000Z-t00019",
            "kind": "dispatch",
            "started": "2026-11-23T02:00:00Z",
            "finished": "2026-11-23T02:05:00Z",
            "engines": {"implementer": "haiku"},
            "task": "attribution-steps",
            "outcome": outcome,
        }
        (run_dir / "run.json").write_text(json.dumps(run_meta), encoding="utf-8")
        for stem, body in steps:
            target = steps_dir / (f"{stem}.txt" if stem.endswith("-gate") else f"{stem}.jsonl")
            target.write_text(body, encoding="utf-8")
        return run_dir

    def _analyze(self, run_dir: Path) -> dict[str, object]:
        """Run analyze with the example engines config and return the parsed attribution.json."""
        with contextlib.redirect_stdout(io.StringIO()):
            rc = devpack_logs.main(["analyze", str(run_dir), "--engines", str(ENGINES_EXAMPLE)])
        self.assertEqual(0, rc)
        return json.loads((run_dir / "attribution.json").read_text(encoding="utf-8"))

    def test_context_window_is_checked_per_step(self) -> None:
        """A 50k request on a 200k sonnet step plus 10k on a 32k local step is clean; 33k on the local step is flagged there."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            clean_run = self._write_run(
                base / "clean",
                "passed",
                [
                    ("01-implementer-sonnet", _agent_stream([50000], "Done A.")),
                    ("02-implementer-local", _agent_stream([10000], "Done B.")),
                ],
            )
            attr = self._analyze(clean_run)
            self.assertFalse(attr["signals"]["prompt_over_context"], attr["evidence"])
            self.assertEqual(50000, attr["signals"]["max_request_tokens_in"])

            over_run = self._write_run(
                base / "over",
                "passed",
                [
                    ("01-implementer-sonnet", _agent_stream([50000], "Done A.")),
                    ("02-implementer-local", _agent_stream([33000], "Done B.")),
                ],
            )
            attr_over = self._analyze(over_run)
            self.assertTrue(attr_over["signals"]["prompt_over_context"])
            self.assertEqual(32768, attr_over["signals"]["context_window"])
            self.assertEqual("TOOL", attr_over["suspected_cause"])
            steps_flagged = [e["step"] for e in attr_over["evidence"] if "exceeded engine context window" in e["text"]]
            self.assertEqual(["02-implementer-local"], steps_flagged)

    def test_passed_run_with_implementer_review_and_gate_has_no_cause(self) -> None:
        """A passing three-record run (implementer, review, gate) with no failure signals gets suspected_cause None."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._write_run(
                Path(tmp),
                "passed",
                [
                    ("01-implementer-haiku", _agent_stream([1000], "Implemented.")),
                    ("02-reviewer-sonnet", _agent_stream([1000], "VERDICT: APPROVE")),
                    ("03-gate", "GATE_OK\n"),
                ],
            )
            attr = self._analyze(run_dir)
            self.assertIsNone(attr["suspected_cause"])
            self.assertEqual(0, attr["signals"]["rework_rounds"])

    def test_request_changes_review_then_fixer_counts_as_rework(self) -> None:
        """A REQUEST_CHANGES review followed by a fixer round is one rework round, so a passing run is not 'none'."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._write_run(
                Path(tmp),
                "passed",
                [
                    ("01-implementer-haiku", _agent_stream([1000], "Implemented.")),
                    ("02-reviewer-sonnet", _agent_stream([1000], "VERDICT: REQUEST_CHANGES")),
                    ("03-fixer-haiku", _agent_stream([1000], "Fixed.")),
                    ("04-gate", "GATE_OK\n"),
                ],
            )
            attr = self._analyze(run_dir)
            self.assertEqual(1, attr["signals"]["rework_rounds"])
            self.assertIsNotNone(attr["suspected_cause"])

    def test_two_fixer_rounds_after_failed_gates_are_rework(self) -> None:
        """Two fixer rounds after failed gates with a final failure count exactly two rework rounds and are not 'none'."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._write_run(
                Path(tmp),
                "fail",
                [
                    ("01-implementer-haiku", _agent_stream([1000], "Implemented.")),
                    ("02-gate", "GATE_FAIL\n"),
                    ("03-fixer-haiku", _agent_stream([1000], "Fix one.")),
                    ("04-gate", "GATE_FAIL\n"),
                    ("05-fixer-haiku", _agent_stream([1000], "Fix two.")),
                    ("06-gate", "GATE_FAIL\n"),
                ],
            )
            attr = self._analyze(run_dir)
            self.assertEqual(2, attr["signals"]["rework_rounds"])
            self.assertIsNotNone(attr["suspected_cause"])


if __name__ == "__main__":
    unittest.main()
