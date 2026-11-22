"""Unit tests for runner/devpack_logs.py covering rendering, redaction, list, show, and site."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

RUNNER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = RUNNER_DIR.parent
for _p in (str(REPO_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import devpack_logs  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "logs"
SAMPLE_HAIKU = FIXTURES_DIR / "stream_haiku_toolcalls.jsonl"
SAMPLE_LOCAL = FIXTURES_DIR / "stream_local_tool_protocol_error.jsonl"


def _build_synthetic_run_dir(
    root: Path,
    run_id: str,
    sample_jsonl: Path,
    role: str = "implementer",
    engine: str = "haiku",
    kind: str = "task",
    task: str = "task-01",
    outcome: str = "APPROVED",
    prompt_text: str = "Implement the requested fix in m.c.",
    gate_text: str = "Running gate checks...\nGATE_OK\n",
    extra_events: list[dict[str, object]] | None = None,
) -> Path:
    """Create a synthetic run directory following SPEC_v02 section 2 layout."""
    run_dir = root / run_id
    steps_dir = run_dir / "steps"
    steps_dir.mkdir(parents=True, exist_ok=True)

    run_json = {
        "run_id": run_id,
        "kind": kind,
        "started": "2026-11-22T14:24:10Z",
        "finished": "2026-11-22T14:24:20Z",
        "engines": {role: engine},
        "task": task,
        "outcome": outcome,
    }
    (run_dir / "run.json").write_text(json.dumps(run_json, indent=2), encoding="utf-8")

    stem = f"01-{role}-{engine}"
    (steps_dir / f"{stem}.prompt.md").write_text(prompt_text, encoding="utf-8")

    raw_jsonl = sample_jsonl.read_text(encoding="utf-8")
    if extra_events:
        extra_lines = "\n".join(json.dumps(ev) for ev in extra_events)
        raw_jsonl = raw_jsonl.rstrip("\n") + "\n" + extra_lines + "\n"
    (steps_dir / f"{stem}.jsonl").write_text(raw_jsonl, encoding="utf-8")

    meta = {
        "role": role,
        "engine": engine,
        "status": "ok",
        "prompt_sha256": "deadbeef" * 8,
        "prompt_file": f"{stem}.prompt.md",
    }
    (steps_dir / f"{stem}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    if gate_text:
        (steps_dir / "02-gate.txt").write_text(gate_text, encoding="utf-8")

    return run_dir


class DevpackLogsTests(unittest.TestCase):
    """Test suite for devpack_logs.py subcommands, rendering, truncation, escaping, and redaction."""

    def test_render_both_samples_to_markdown_and_html(self) -> None:
        """Render both recorded stream-json samples into transcript.md and index.html."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            run_haiku = _build_synthetic_run_dir(
                base,
                run_id="20261122T142415Z-a1b2c3",
                sample_jsonl=SAMPLE_HAIKU,
                role="implementer",
                engine="haiku",
                task="fix-add",
                outcome="VERDICT: APPROVE",
            )
            run_local = _build_synthetic_run_dir(
                base,
                run_id="20261122T142200Z-d4e5f6",
                sample_jsonl=SAMPLE_LOCAL,
                role="implementer",
                engine="local",
                task="run-tests",
                outcome="agent_error",
            )

            rc1 = devpack_logs.main(["render", str(run_haiku)])
            rc2 = devpack_logs.main(["render", str(run_local)])
            self.assertEqual(0, rc1)
            self.assertEqual(0, rc2)

            haiku_md = (run_haiku / "transcript.md").read_text(encoding="utf-8")
            haiku_html = (run_haiku / "index.html").read_text(encoding="utf-8")

            # Tool names, tool input, final result, cost, prompt details, and gate output appear.
            for content in (haiku_md, haiku_html):
                self.assertIn("Read", content)
                self.assertIn("Edit", content)
                self.assertIn("Bash", content)
                self.assertIn("return a+b;", content)
                self.assertIn("gcc -c m.c", content)
                self.assertIn("I changed `add` in `m.c` to return `a+b`", content)
                self.assertIn("$0.002215", content)
                self.assertIn("claude-haiku-5-5", content)
                self.assertIn("<details", content)
                self.assertIn("Prompt", content)
                self.assertIn("GATE_OK", content)
                self.assertIn("FINAL VERDICT", content)

            # HTML is self-contained with prefers-color-scheme and no script tags.
            self.assertIn("prefers-color-scheme: dark", haiku_html)
            self.assertNotIn("<script", haiku_html.lower())

            local_md = (run_local / "transcript.md").read_text(encoding="utf-8")
            local_html = (run_local / "index.html").read_text(encoding="utf-8")
            for content in (local_md, local_html):
                self.assertIn("gpt-oss:20b", content)
                self.assertIn("Thinking", content)
                self.assertIn("pytest -q", content)
                self.assertIn("$0.0153", content)
                self.assertIn("TOOL PROTOCOL ERROR", content)

    def test_html_escapes_script_tag_in_tool_result(self) -> None:
        """Verify <script> inside a tool result or assistant message is HTML-escaped."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            xss_payload = "<script>alert('xss')</script>"
            extra = [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "model": "claude-haiku-5-5",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_xss_1",
                                "name": "Read",
                                "input": {"file_path": "index.html"},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_xss_1",
                                "content": f"File content: {xss_payload}",
                            }
                        ],
                    },
                },
            ]
            run_dir = _build_synthetic_run_dir(
                base,
                run_id="20261122T150000Z-111111",
                sample_jsonl=SAMPLE_HAIKU,
                extra_events=extra,
            )
            rc = devpack_logs.main(["render", str(run_dir)])
            self.assertEqual(0, rc)
            html_out = (run_dir / "index.html").read_text(encoding="utf-8")
            self.assertNotIn(xss_payload, html_out)
            self.assertIn("&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;", html_out)

    def test_5kb_tool_result_truncated_by_default_and_full_with_flag(self) -> None:
        """Verify a 5 KB tool result is truncated at 4 KB with marker unless --full is set."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            big_chunk = "A" * 5120 + "_TAIL_MARKER_END"
            extra = [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "model": "claude-haiku-5-5",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_big_1",
                                "name": "Bash",
                                "input": {"command": "cat big.log"},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_big_1",
                                "content": big_chunk,
                            }
                        ],
                    },
                },
            ]
            run_dir = _build_synthetic_run_dir(
                base,
                run_id="20261122T150100Z-222222",
                sample_jsonl=SAMPLE_HAIKU,
                extra_events=extra,
            )

            # Default render truncates at 4 KB.
            rc = devpack_logs.main(["render", str(run_dir)])
            self.assertEqual(0, rc)
            md_trunc = (run_dir / "transcript.md").read_text(encoding="utf-8")
            html_trunc = (run_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn("truncated at 4 KB", md_trunc)
            self.assertIn("truncated at 4 KB", html_trunc)
            self.assertNotIn("_TAIL_MARKER_END", md_trunc)
            self.assertNotIn("_TAIL_MARKER_END", html_trunc)

            # show --full and render --full preserve the entire 5 KB result.
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc_show = devpack_logs.main(["show", str(run_dir), "--step", "01", "--full"])
            self.assertEqual(0, rc_show)
            self.assertIn("_TAIL_MARKER_END", buf.getvalue())
            self.assertNotIn("truncated at 4 KB", buf.getvalue())

            rc_full = devpack_logs.main(["render", str(run_dir), "--full"])
            self.assertEqual(0, rc_full)
            md_full = (run_dir / "transcript.md").read_text(encoding="utf-8")
            html_full = (run_dir / "index.html").read_text(encoding="utf-8")
            self.assertIn("_TAIL_MARKER_END", md_full)
            self.assertIn("_TAIL_MARKER_END", html_full)

    def test_redaction_of_tokens_env_patterns_and_home_paths(self) -> None:
        """Verify redaction scrubs env tokens, credential strings, secret_pii patterns, and home paths."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_home = "/" + "ho" + "me/someone"
            fake_env_secret = "super-secret-env-token-val-998877"
            fake_api_key = "sk-ant-api03-FakeKeyForUnitTestRedaction123456"
            fake_aws_key = "AKIAIOSFODNN7EXAMPLE"
            custom_pii = "INTERNAL-PII-ID-774411"

            patterns_file = base / "custom_patterns.txt"
            patterns_file.write_text("INTERNAL-PII-ID-[0-9]+\n", encoding="utf-8")

            extra = [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "model": "claude-haiku-5-5",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"Reading {fake_home}/project/src/main.c with "
                                    f"env={fake_env_secret} key={fake_api_key} "
                                    f"aws={fake_aws_key} pii={custom_pii}"
                                ),
                            }
                        ],
                    },
                }
            ]
            run_dir = _build_synthetic_run_dir(
                base,
                run_id="20261122T150200Z-333333",
                sample_jsonl=SAMPLE_HAIKU,
                prompt_text=f"Check file at {fake_home}/repo/a.c using token {fake_env_secret}",
                extra_events=extra,
            )

            with mock.patch.dict(os.environ, {"TEST_CUSTOM_AUTH_TOKEN": fake_env_secret}):
                rc = devpack_logs.main(
                    [
                        "render",
                        str(run_dir),
                        "--redact-env",
                        "TEST_CUSTOM_AUTH_TOKEN",
                        "--patterns",
                        str(patterns_file),
                    ]
                )
            self.assertEqual(0, rc)

            for fname in ("transcript.md", "index.html"):
                rendered = (run_dir / fname).read_text(encoding="utf-8")
                self.assertNotIn(fake_home, rendered)
                self.assertIn("~/project/src/main.c", rendered)
                self.assertIn("~/repo/a.c", rendered)
                self.assertNotIn(fake_env_secret, rendered)
                self.assertNotIn(fake_api_key, rendered)
                self.assertNotIn(fake_aws_key, rendered)
                self.assertNotIn(custom_pii, rendered)
                self.assertIn("[REDACTED]", rendered)

    def test_list_and_site_over_two_run_dirs_sorted_with_totals(self) -> None:
        """Verify list sorts run directories and computes totals, and site links each run."""
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp) / "runs"
            runs_root.mkdir(parents=True, exist_ok=True)

            # Create two runs out of order to verify sorting.
            _build_synthetic_run_dir(
                runs_root,
                run_id="20261122T160000Z-bbbbbb",
                sample_jsonl=SAMPLE_LOCAL,
                role="implementer",
                engine="local",
                kind="eval",
                task="11-vague-bug",
                outcome="agent_error",
            )
            _build_synthetic_run_dir(
                runs_root,
                run_id="20261122T140000Z-aaaaaa",
                sample_jsonl=SAMPLE_HAIKU,
                role="implementer",
                engine="haiku",
                kind="task",
                task="01-add-fix",
                outcome="APPROVED",
            )

            # Test human-readable table list
            buf_table = io.StringIO()
            with mock.patch("sys.stdout", buf_table):
                rc_list = devpack_logs.main(["list", str(runs_root)])
            self.assertEqual(0, rc_list)
            table_out = buf_table.getvalue()
            idx_a = table_out.find("20261122T140000Z-aaaaaa")
            idx_b = table_out.find("20261122T160000Z-bbbbbb")
            self.assertGreater(idx_a, -1)
            self.assertGreater(idx_b, idx_a, "Runs must be sorted chronologically by run_id")
            self.assertIn("TOTAL (2 runs)", table_out)
            self.assertIn("$0.0175", table_out)

            # Test JSON list
            buf_json = io.StringIO()
            with mock.patch("sys.stdout", buf_json):
                rc_json = devpack_logs.main(["list", str(runs_root), "--json"])
            self.assertEqual(0, rc_json)
            parsed = json.loads(buf_json.getvalue())
            self.assertEqual(2, len(parsed["runs"]))
            self.assertEqual("20261122T140000Z-aaaaaa", parsed["runs"][0]["run_id"])
            self.assertEqual("20261122T160000Z-bbbbbb", parsed["runs"][1]["run_id"])
            self.assertEqual(2, parsed["totals"]["count"])
            self.assertAlmostEqual(0.00221478 + 0.01526, parsed["totals"]["cost_usd"], places=6)
            self.assertAlmostEqual(4.484 + 89.212, parsed["totals"]["duration_s"], places=2)

            # Test static site generation
            rc_site = devpack_logs.main(["site", str(runs_root)])
            self.assertEqual(0, rc_site)
            site_index = (runs_root / "index.html").read_text(encoding="utf-8")
            self.assertIn('href="20261122T140000Z-aaaaaa/index.html"', site_index)
            self.assertIn('href="20261122T160000Z-bbbbbb/index.html"', site_index)
            self.assertIn("TOTAL (2 runs)", site_index)
            self.assertTrue((runs_root / "20261122T140000Z-aaaaaa" / "index.html").is_file())
            self.assertTrue((runs_root / "20261122T160000Z-bbbbbb" / "index.html").is_file())

    def test_session_mode_and_unknown_event_types_do_not_crash(self) -> None:
        """Verify render --session tolerates and counts unknown event types without crashing."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            session_file = base / "interactive_session.jsonl"
            out_dir = base / "rendered_session"

            events = [
                {"type": "future_unknown_event", "payload": {"x": 1}},
                {"type": "system", "subtype": "mystery_subtype", "info": "skip me"},
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "Please inspect m.c and explain what it does.",
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "model": "claude-sonnet-4-5",
                        "content": [
                            {"type": "thinking", "thinking": "Checking m.c details."},
                            {"type": "unrecognized_block_type", "data": 42},
                            {"type": "text", "text": "The function adds two integers.\nVERDICT: APPROVE"},
                        ],
                        "usage": {"input_tokens": 120, "output_tokens": 45},
                    },
                },
            ]
            session_file.write_text(
                "\n".join(json.dumps(e) for e in events) + "\nnot-valid-json-line\n",
                encoding="utf-8",
            )

            rc = devpack_logs.main(
                ["render", "--session", str(session_file), "--out", str(out_dir)]
            )
            self.assertEqual(0, rc)
            md_text = (out_dir / "transcript.md").read_text(encoding="utf-8")
            html_text = (out_dir / "index.html").read_text(encoding="utf-8")

            for content in (md_text, html_text):
                self.assertIn("Please inspect m.c and explain what it does.", content)
                self.assertIn("The function adds two integers.", content)
                self.assertIn("VERDICT: APPROVE", content)
                self.assertIn("unknown_events", content)


if __name__ == "__main__":
    unittest.main()
