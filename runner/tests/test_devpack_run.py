"""Unit tests for runner/devpack_run.py using mocked agent and gate scripts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

RUNNER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = RUNNER_DIR.parent
for _p in (str(REPO_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import devpack_run  # noqa: E402


def _write_fake_driver_script(script_path: Path, state_path: Path) -> None:
    """Write a fake agent/gate script driven by a JSON scenario state file."""
    script_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "role = sys.argv[1]\n"
        "prompt = sys.argv[2] if len(sys.argv) > 2 else ''\n"
        f"state_file = pathlib.Path({str(state_path)!r})\n"
        "state = json.loads(state_file.read_text(encoding='utf-8'))\n"
        "calls = state.setdefault('calls', [])\n"
        "calls.append({'role': role, 'prompt': prompt})\n"
        "queue = state.get(role, [])\n"
        "if queue:\n"
        "    action = queue.pop(0)\n"
        "else:\n"
        "    action = {'rc': 0, 'out': 'VERDICT: APPROVE'}\n"
        "state_file.write_text(json.dumps(state), encoding='utf-8')\n"
        "if action.get('out'):\n"
        "    print(action['out'])\n"
        "if action.get('err'):\n"
        "    sys.stderr.write(action['err'] + '\\n')\n"
        "sys.exit(int(action.get('rc', 0)))\n",
        encoding="utf-8",
    )
    os.chmod(script_path, stat.S_IRWXU)


def _write_test_toml(config_path: Path, script_path: Path, max_fix_rounds: int = 2) -> None:
    """Write a test engines.toml pointing implementer, reviewer, fixer, and gate at script_path."""
    config_path.write_text(
        f"max_fix_rounds = {max_fix_rounds}\n"
        f'gate_cmd = ["python3", "{script_path}", "gate"]\n'
        "gate_timeout_s = 30\n\n"
        "[roles.implementer]\n"
        'model = "sonnet"\n'
        f'cmd = ["python3", "{script_path}", "implementer", "{{prompt}}"]\n'
        "timeout_s = 30\n\n"
        "[roles.reviewer]\n"
        'model = "sonnet"\n'
        f'cmd = ["python3", "{script_path}", "reviewer", "{{prompt}}"]\n'
        "timeout_s = 30\n\n"
        "[roles.fixer]\n"
        'model = "sonnet"\n'
        f'cmd = ["python3", "{script_path}", "fixer", "{{prompt}}"]\n'
        "timeout_s = 30\n",
        encoding="utf-8",
    )


class DevpackRunUnitTests(unittest.TestCase):
    """Test suite for verdict parsing, template rendering, fix round cap -> ESCALATE_TO_HUMAN, and dry-run."""

    def test_verdict_parsing_strict_last_line(self) -> None:
        """Verify verdict parser only accepts exact VERDICT lines on the last non-empty line."""
        self.assertEqual(
            devpack_run.VERDICT_APPROVE,
            devpack_run.parse_review_verdict("No findings.\n\nVERDICT: APPROVE\n"),
        )
        self.assertEqual(
            devpack_run.VERDICT_REQUEST_CHANGES,
            devpack_run.parse_review_verdict(
                "| F-1 | COR | major | src/a.cpp:10 | bug | fix |\nVERDICT: REQUEST_CHANGES\n"
            ),
        )
        # JSON envelope from claude --output-format json
        self.assertEqual(
            devpack_run.VERDICT_APPROVE,
            devpack_run.parse_review_verdict(
                json.dumps({"type": "result", "result": "All good.\nVERDICT: APPROVE"})
            ),
        )
        # Trailing text after verdict line is rejected (fail-closed)
        self.assertEqual(
            "INVALID_VERDICT",
            devpack_run.parse_review_verdict("VERDICT: APPROVE\nWait, one more thing"),
        )
        self.assertEqual("INVALID_VERDICT", devpack_run.parse_review_verdict(""))

    def test_template_rendering_prefers_workflow_template_and_falls_back(self) -> None:
        """Verify workflow/templates/brief_implement.md is rendered when present, else built-in template."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = devpack_run.load_task_data(
                task_file=None,
                task_id="T-42",
                title="CAN Codec Bounds",
                goal="Check bounds on pack.",
                allowed_files="src/can_codec.cpp",
            )
            # Built-in fallback when workflow/templates/brief_implement.md is absent
            rendered_fallback, src_fallback = devpack_run.render_implement_brief(root, ctx)
            self.assertEqual("built-in:brief_implement", src_fallback)
            self.assertIn("CAN Codec Bounds", rendered_fallback)
            self.assertIn("T-42", rendered_fallback)

            # Custom workflow/templates/brief_implement.md when present
            tpl_dir = root / "workflow" / "templates"
            tpl_dir.mkdir(parents=True, exist_ok=True)
            (tpl_dir / "brief_implement.md").write_text(
                "# Custom Brief: {{TASK_TITLE}} [{{TASK_ID}}]\nGoal: {{ONE_PARAGRAPH_GOAL}}\n",
                encoding="utf-8",
            )
            rendered_custom, src_custom = devpack_run.render_implement_brief(root, ctx)
            self.assertEqual("workflow/templates/brief_implement.md", src_custom)
            self.assertIn("# Custom Brief: CAN Codec Bounds [T-42]", rendered_custom)
            self.assertIn("Goal: Check bounds on pack.", rendered_custom)

    def test_dry_run_prints_plan_without_executing_commands(self) -> None:
        """Verify --dry-run exits 0 and does not invoke agent or gate scripts."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            state_path = base / "state.json"
            state_path.write_text(json.dumps({}), encoding="utf-8")
            script_path = base / "fake_driver.py"
            _write_fake_driver_script(script_path, state_path)
            config_path = base / "engines.toml"
            _write_test_toml(config_path, script_path)

            rc = devpack_run.main(
                [
                    "--config",
                    str(config_path),
                    "--worktree",
                    str(base),
                    "--log-dir",
                    str(base / "logs"),
                    "--task-id",
                    "dry-run-task",
                    "--dry-run",
                ]
            )
            self.assertEqual(0, rc)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual([], state.get("calls", []))

    def test_gate_fail_then_review_request_changes_then_approve(self) -> None:
        """Verify 1 gate fail (round 1) + 1 review REQUEST_CHANGES (round 2) -> APPROVE succeeds within 2 rounds."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            state_path = base / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "implementer": [{"rc": 0, "out": "Implemented initial version"}],
                        "gate": [
                            {"rc": 1, "out": "GATE_FAIL unit-tests"},
                            {"rc": 0, "out": "GATE_OK"},
                            {"rc": 0, "out": "GATE_OK"},
                        ],
                        "reviewer": [
                            {
                                "rc": 0,
                                "out": "Finding 1: missing check\nVERDICT: REQUEST_CHANGES",
                            },
                            {"rc": 0, "out": "Looks good\nVERDICT: APPROVE"},
                        ],
                        "fixer": [
                            {"rc": 0, "out": "Fixed gate failure"},
                            {"rc": 0, "out": "Fixed review finding"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            script_path = base / "fake_driver.py"
            _write_fake_driver_script(script_path, state_path)
            config_path = base / "engines.toml"
            _write_test_toml(config_path, script_path, max_fix_rounds=2)
            log_dir = base / "logs"

            rc = devpack_run.main(
                [
                    "--config",
                    str(config_path),
                    "--worktree",
                    str(base),
                    "--log-dir",
                    str(log_dir),
                    "--task-id",
                    "task-converge",
                ]
            )
            self.assertEqual(0, rc)
            summary = json.loads((log_dir / "run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual("APPROVED", summary["status"])
            self.assertEqual("APPROVE", summary["verdict"])
            self.assertEqual(2, summary["fix_rounds_used"])
            self.assertEqual(8, len(summary["step_logs"]))

    def test_round_cap_stops_with_escalate_to_human(self) -> None:
        """Verify that exceeding max_fix_rounds=2 stops with ESCALATE_TO_HUMAN and writes escalation log."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            state_path = base / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "implementer": [{"rc": 0, "out": "Initial implementation"}],
                        "gate": [
                            {"rc": 0, "out": "GATE_OK"},
                            {"rc": 0, "out": "GATE_OK"},
                            {"rc": 0, "out": "GATE_OK"},
                        ],
                        "reviewer": [
                            {"rc": 0, "out": "Still broken 1\nVERDICT: REQUEST_CHANGES"},
                            {"rc": 0, "out": "Still broken 2\nVERDICT: REQUEST_CHANGES"},
                            {"rc": 0, "out": "Still broken 3\nVERDICT: REQUEST_CHANGES"},
                        ],
                        "fixer": [
                            {"rc": 0, "out": "Fix attempt 1"},
                            {"rc": 0, "out": "Fix attempt 2"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            script_path = base / "fake_driver.py"
            _write_fake_driver_script(script_path, state_path)
            config_path = base / "engines.toml"
            _write_test_toml(config_path, script_path, max_fix_rounds=2)
            log_dir = base / "logs"

            rc = devpack_run.main(
                [
                    "--config",
                    str(config_path),
                    "--worktree",
                    str(base),
                    "--log-dir",
                    str(log_dir),
                    "--task-id",
                    "task-escalate",
                ]
            )
            self.assertEqual(1, rc)
            summary = json.loads((log_dir / "run_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(devpack_run.ESCALATE_TO_HUMAN, summary["status"])
            self.assertEqual(2, summary["fix_rounds_used"])
            self.assertTrue(any("escalate" in name for name in summary["step_logs"]))


if __name__ == "__main__":
    unittest.main()
