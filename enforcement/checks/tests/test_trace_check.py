"""Unit tests for enforcement.checks.trace_check."""

from __future__ import annotations

import contextlib
import io
import pathlib
import sys
import tempfile
import unittest

_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from enforcement.checks import trace_check  # noqa: E402


class TestTraceCheck(unittest.TestCase):
    """Verify requirement-to-test bidirectional traceability checking."""

    def setUp(self) -> None:
        """Create a temporary workspace with requirements and test files."""
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = pathlib.Path(self._tmp.name)
        self.req_file = self.tmp_path / "requirements.txt"
        self.tests_dir = self.tmp_path / "tests"
        self.tests_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        """Clean up temporary workspace."""
        self._tmp.cleanup()

    def _run_trace(self, args: list[str]) -> tuple[int, str, str]:
        """Run trace_check.main and return (exit_code, stdout, stderr)."""
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            code = trace_check.main(args)
        return code, out_buf.getvalue(), err_buf.getvalue()

    def test_happy_path_all_reqs_covered_and_no_unknown_tags(self) -> None:
        """When every requirement is tagged in tests and all tags are valid, exit 0."""
        self.req_file.write_text(
            "# Automotive codec requirements\n"
            "REQ-001: CAN frame decoder shall validate DLC bounds\n"
            "REQ-002: Ring buffer shall drop oldest element on overflow\n",
            encoding="utf-8",
        )
        (self.tests_dir / "test_can.cpp").write_text(
            "// @req REQ-001\nvoid test_dlc() {}\n"
            "// @req REQ-002\nvoid test_ring() {}\n",
            encoding="utf-8",
        )

        code, out, _ = self._run_trace(
            [
                "--repo",
                str(self.tmp_path),
                "--requirements",
                str(self.req_file),
                "--tests",
                "tests/**/*.cpp",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("TRACE_OK", out)

    def test_untested_requirement_fails_with_report(self) -> None:
        """A requirement in the file with no corresponding @req tag causes exit 1."""
        self.req_file.write_text(
            "REQ-001: First requirement\n"
            "REQ-002: Second requirement without test\n",
            encoding="utf-8",
        )
        (self.tests_dir / "test_can.cpp").write_text(
            "// @req REQ-001\nvoid test_first() {}\n",
            encoding="utf-8",
        )

        code, out, _ = self._run_trace(
            [
                "--repo",
                str(self.tmp_path),
                "--requirements",
                str(self.req_file),
                "--tests",
                "tests/**/*.cpp",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("UNTESTED_REQ: REQ-002", out)

    def test_unknown_test_tag_fails_with_report(self) -> None:
        """A @req tag referencing a non-existent requirement ID causes exit 1."""
        self.req_file.write_text(
            "REQ-001: First requirement\n",
            encoding="utf-8",
        )
        (self.tests_dir / "test_can.cpp").write_text(
            "// @req REQ-001\n"
            "// @req REQ-999\n"
            "void test_first() {}\n",
            encoding="utf-8",
        )

        code, out, _ = self._run_trace(
            [
                "--repo",
                str(self.tmp_path),
                "--requirements",
                str(self.req_file),
                "--tests",
                "tests/**/*.cpp",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("UNKNOWN_TAG: REQ-999", out)

    def test_both_untested_and_unknown_reported_together(self) -> None:
        """When both untested requirements and unknown tags exist, both are reported and exit is 1."""
        self.req_file.write_text(
            "REQ-001: Tested requirement\n"
            "REQ-002: Untested requirement\n",
            encoding="utf-8",
        )
        (self.tests_dir / "test_can.cpp").write_text(
            "// @req REQ-001\n"
            "// @req REQ-404\n",
            encoding="utf-8",
        )

        code, out, _ = self._run_trace(
            [
                "--repo",
                str(self.tmp_path),
                "--requirements",
                str(self.req_file),
                "--tests",
                "tests/*.cpp",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("UNTESTED_REQ: REQ-002", out)
        self.assertIn("UNKNOWN_TAG: REQ-404", out)


if __name__ == "__main__":
    unittest.main()
