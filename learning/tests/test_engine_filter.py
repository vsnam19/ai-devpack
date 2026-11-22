"""Unit tests: learning/validate.py refuses to score another engine's trials as this engine."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from learning import validate  # noqa: E402


class EngineFilterTests(unittest.TestCase):
    """Verify engine filtering in ``_extract_trials_from_payload`` and the CLI refusal path."""

    def test_extract_returns_empty_when_no_trial_matches_engine(self) -> None:
        """A payload containing only haiku trials yields no trials for engine 'local'."""
        payload = {
            "trials": [
                {"model": "haiku", "passed": True, "harness_error": False},
                {"model": "haiku", "passed": False, "harness_error": False},
            ]
        }
        self.assertEqual([], validate._extract_trials_from_payload(payload, "local"))

    def test_extract_keeps_matching_and_untagged_trials_only(self) -> None:
        """Matching trials and trials without any engine tag are kept; other engines are dropped."""
        payload = [
            {"model": "local", "passed": True},
            {"model": "haiku", "passed": True},
            {"passed": False},
        ]
        kept = validate._extract_trials_from_payload(payload, "local")
        self.assertEqual([{"model": "local", "passed": True}, {"passed": False}], kept)

    def test_cli_exits_nonzero_and_names_engine_when_no_trials_match(self) -> None:
        """The validate CLI fails with non-zero exit and names the engine; no report is written."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            candidate = base / "candidate.md"
            candidate.write_text("# Engine Profile: local\nVersion: 1\n", encoding="utf-8")
            active = base / "active.md"
            active.write_text("# Engine Profile: local\nVersion: 0\n", encoding="utf-8")
            active_json = base / "active.json"
            cand_json = base / "cand.json"
            haiku_only = [
                {"model": "haiku", "passed": True, "harness_error": False, "lint_findings_first_attempt": 0},
            ]
            active_json.write_text(json.dumps(haiku_only), encoding="utf-8")
            cand_json.write_text(json.dumps(haiku_only), encoding="utf-8")
            out_dir = base / "reports"

            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = validate.main(
                    [
                        "--engine",
                        "local",
                        "--candidate",
                        str(candidate),
                        "--active",
                        str(active),
                        "--results-json-pair",
                        str(active_json),
                        str(cand_json),
                        "--out-dir",
                        str(out_dir),
                    ]
                )
            self.assertNotEqual(0, rc)
            self.assertIn("local", err.getvalue())
            self.assertIn("no trials for engine", err.getvalue())
            self.assertEqual([], list(out_dir.glob("validate-*")) if out_dir.exists() else [])


if __name__ == "__main__":
    unittest.main()
