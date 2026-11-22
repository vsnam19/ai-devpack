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


if __name__ == "__main__":
    unittest.main()
