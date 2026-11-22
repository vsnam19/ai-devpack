"""Unit tests for runner/brief_lint.py and broker/dispatch brief-lint integration."""

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

RUNNER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = RUNNER_DIR.parent
for _p in (str(REPO_ROOT), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import brief_lint  # noqa: E402
from runner import devpack_broker, devpack_dispatch  # noqa: E402


def _create_sample_repo(root: Path) -> None:
    """Create a small C++ git repository for brief_lint and broker integration tests."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "include").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "briefs").mkdir(parents=True, exist_ok=True)
    (root / "preflight.toml").write_text('[general]\nstd = "c++17"\n', encoding="utf-8")
    (root / ".gitignore").write_text(".devpack/\nbriefs/\nruns/\n", encoding="utf-8")
    (root / "include" / "widget.hpp").write_text(
        "/**\n * @file widget.hpp\n * @brief Widget header.\n */\n"
        "class Widget {\npublic:\n    int computeValue() const;\n};\n",
        encoding="utf-8",
    )
    (root / "src" / "widget.cpp").write_text(
        "/**\n * @file widget.cpp\n * @brief Widget impl.\n */\n"
        "#include \"widget.hpp\"\n"
        "/**\n * @brief Return 1.\n * @return One.\n */\n"
        "int Widget::computeValue() const { return 1; }\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_widget.cpp").write_text(
        "#include <cassert>\nvoid testWidget() { assert(1 == 1); }\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True)


def _valid_brief_text() -> str:
    """Return a clean brief referencing existing files and symbols in _create_sample_repo."""
    return (
        "# Brief: Update Widget\n\n"
        "## Goal\n"
        "Update `computeValue()` in `src/widget.cpp` to return 2.\n\n"
        "## Input\n"
        "- `include/widget.hpp` (around line 5)\n"
        "- `src/widget.cpp:7`\n\n"
        "## Expected output\n"
        "- Modify `src/widget.cpp` so `computeValue()` returns 2.\n\n"
        "## Files allowed\n"
        "- `src/widget.cpp`\n\n"
        "## Constraints\n"
        "- Never modify `tests/test_widget.cpp`.\n\n"
        "## Gate\n"
        "- `python3 -m unittest`\n"
    )


class BriefLintTests(unittest.TestCase):
    """Positive and negative coverage for every brief_lint rule and broker/dispatch wiring."""

    def test_clean_brief_and_example_implement_pass(self) -> None:
        """A complete valid brief and orchestrator/briefs/example_implement.md exit 0."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _create_sample_repo(repo)
            brief = repo / "briefs" / "clean.md"
            brief.write_text(_valid_brief_text(), encoding="utf-8")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = brief_lint.main(
                    [
                        str(brief),
                        "--repo",
                        str(repo),
                        "--engine",
                        "haiku",
                        "--engines-config",
                        str(REPO_ROOT / "runner" / "engines.example.toml"),
                    ]
                )
            self.assertEqual(0, rc, buf.getvalue())
            self.assertEqual([], json.loads(buf.getvalue()))

        ex_brief = REPO_ROOT / "orchestrator" / "briefs" / "example_implement.md"
        ex_repo = REPO_ROOT / "eval" / "fixture"
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            rc2 = brief_lint.main(
                [
                    str(ex_brief),
                    "--repo",
                    str(ex_repo),
                    "--engine",
                    "haiku",
                    "--engines-config",
                    str(REPO_ROOT / "runner" / "engines.example.toml"),
                ]
            )
        self.assertEqual(0, rc2, buf2.getvalue())

    def test_rule_brief_headings_positive_and_negative(self) -> None:
        """Missing required headings trigger BRIEF-HEADINGS error; template aliases pass."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _create_sample_repo(repo)

            bad = "## Goal\nDo work.\n\n## Files allowed\n- `src/widget.cpp`\n"
            findings = brief_lint.lint_brief_text(bad, repo_root=repo)
            missing_msgs = [f["message"] for f in findings if f["rule"] == "BRIEF-HEADINGS"]
            self.assertEqual(4, len(missing_msgs))
            self.assertTrue(all(f["severity"] == "error" for f in findings if f["rule"] == "BRIEF-HEADINGS"))
            self.assertTrue(any("Gate" in m for m in missing_msgs))

            aliased = (
                "## Goal\nFix.\n## Input\n- `src/widget.cpp`\n"
                "## Expected output\n- Updated.\n## Files allowed\n- `src/widget.cpp`\n"
                "## STOP conditions\n- Never weaken tests.\n## Commands\n- `true`\n"
            )
            ok_findings = brief_lint.lint_brief_text(aliased, repo_root=repo)
            self.assertEqual([], [f for f in ok_findings if f["rule"] == "BRIEF-HEADINGS"])

    def test_rule_brief_path_missing_positive_and_negative(self) -> None:
        """Non-existent repo paths trigger BRIEF-PATH-MISSING; existing paths and STOP_REPORT.md pass."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _create_sample_repo(repo)
            text = _valid_brief_text().replace("src/widget.cpp:7", "src/missing_mod.cpp")
            text += "\nWrite `STOP_REPORT.md` on conflict.\n"
            findings = brief_lint.lint_brief_text(text, repo_root=repo)
            path_findings = [f for f in findings if f["rule"] == "BRIEF-PATH-MISSING"]
            self.assertEqual(1, len(path_findings), findings)
            self.assertIn("src/missing_mod.cpp", path_findings[0]["message"])
            self.assertEqual("error", path_findings[0]["severity"])

    def test_rule_brief_path_missing_in_files_allowed_or_glob_is_warning_only(self) -> None:
        """A missing path listed under Files allowed (or matched by a glob there) warns and never blocks."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _create_sample_repo(repo)
            text = _valid_brief_text().replace(
                "## Files allowed\n- `src/widget.cpp`\n",
                "## Files allowed\n- `src/widget.cpp`\n- `src/new.cpp`\n- `src/gen_*.cpp`\n",
            )
            findings = brief_lint.lint_brief_text(text, repo_root=repo)
            missing = [f for f in findings if f["rule"] == "BRIEF-PATH-MISSING"]
            self.assertEqual(1, len(missing), findings)
            self.assertIn("src/new.cpp", missing[0]["message"])
            self.assertEqual("warning", missing[0]["severity"])
            self.assertFalse(brief_lint.has_errors(findings), findings)

    def test_rule_brief_path_missing_expected_output_only_warns(self) -> None:
        """Two missing paths named by create wording in Expected output yield warnings only, never errors."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _create_sample_repo(repo)
            text = _valid_brief_text().replace(
                "- Modify `src/widget.cpp` so `computeValue()` returns 2.\n",
                "- Modify `src/widget.cpp` so `computeValue()` returns 2.\n"
                "- Create `src/a.cpp` and `src/b.cpp`.\n",
            )
            findings = brief_lint.lint_brief_text(text, repo_root=repo)
            missing = [f for f in findings if f["rule"] == "BRIEF-PATH-MISSING"]
            self.assertEqual(2, len(missing), findings)
            self.assertTrue(any("src/a.cpp" in f["message"] for f in missing), findings)
            self.assertTrue(any("src/b.cpp" in f["message"] for f in missing), findings)
            self.assertTrue(all(f["severity"] == "warning" for f in missing), findings)
            self.assertFalse(brief_lint.has_errors(findings), findings)

    def test_rule_brief_path_missing_negation_no_longer_changes_severity(self) -> None:
        """Negation wording is not special: a missing path in Expected output is still only a warning."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _create_sample_repo(repo)
            text = _valid_brief_text().replace(
                "- Modify `src/widget.cpp` so `computeValue()` returns 2.\n",
                "- Modify `src/widget.cpp` so `computeValue()` returns 2.\n"
                "- Do not create `src/new.cpp`.\n",
            )
            findings = brief_lint.lint_brief_text(text, repo_root=repo)
            missing = [f for f in findings if f["rule"] == "BRIEF-PATH-MISSING"]
            self.assertEqual(1, len(missing), findings)
            self.assertEqual("warning", missing[0]["severity"])
            self.assertFalse(brief_lint.has_errors(findings), findings)

    def test_rule_brief_path_missing_same_path_in_input_and_expected_output_warns(self) -> None:
        """A missing path named in Input is a warning when Expected output also names it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _create_sample_repo(repo)
            text = _valid_brief_text().replace(
                "- `src/widget.cpp:7`\n",
                "- `src/widget.cpp:7`\n- `src/ghost.cpp`\n",
            ).replace(
                "- Modify `src/widget.cpp` so `computeValue()` returns 2.\n",
                "- Modify `src/widget.cpp` so `computeValue()` returns 2.\n"
                "- Create `src/ghost.cpp` with a stub.\n",
            )
            findings = brief_lint.lint_brief_text(text, repo_root=repo)
            missing = [f for f in findings if f["rule"] == "BRIEF-PATH-MISSING"]
            self.assertEqual(2, len(missing), findings)
            self.assertTrue(all("src/ghost.cpp" in f["message"] for f in missing), findings)
            self.assertTrue(all(f["severity"] == "warning" for f in missing), findings)
            self.assertFalse(brief_lint.has_errors(findings), findings)

    def test_rule_brief_path_missing_fix_in_input_errors_even_with_files_allowed(self) -> None:
        """A missing path that Input asks to fix is an error; its Files allowed mention is only a warning."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _create_sample_repo(repo)
            text = _valid_brief_text().replace(
                "- `src/widget.cpp:7`\n",
                "- `src/widget.cpp:7`\n- Fix `src/driver.cpp` as well.\n",
            ).replace(
                "## Files allowed\n- `src/widget.cpp`\n",
                "## Files allowed\n- `src/widget.cpp`\n- `src/driver.cpp`\n",
            )
            findings = brief_lint.lint_brief_text(text, repo_root=repo)
            missing = [f for f in findings if f["rule"] == "BRIEF-PATH-MISSING"]
            self.assertEqual(2, len(missing), findings)
            self.assertTrue(all("src/driver.cpp" in f["message"] for f in missing))
            by_severity = {f["severity"]: f for f in missing}
            self.assertEqual({"error", "warning"}, set(by_severity), findings)
            self.assertEqual(text.splitlines().index("- Fix `src/driver.cpp` as well.") + 1,
                             by_severity["error"]["line"])
            self.assertTrue(brief_lint.has_errors(findings))

    def test_rule_brief_path_missing_in_goal_is_error(self) -> None:
        """A missing path named in Goal is a blocking error."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _create_sample_repo(repo)
            text = _valid_brief_text().replace(
                "Update `computeValue()` in `src/widget.cpp` to return 2.",
                "Update `computeValue()` in `src/ghost.cpp` to return 2.",
            )
            findings = brief_lint.lint_brief_text(text, repo_root=repo)
            missing = [f for f in findings if f["rule"] == "BRIEF-PATH-MISSING"]
            self.assertEqual(1, len(missing), findings)
            self.assertEqual("error", missing[0]["severity"])
            self.assertIn("src/ghost.cpp", missing[0]["message"])

    def test_rule_brief_path_missing_input_only_still_errors(self) -> None:
        """A missing path mentioned only under Input stays a blocking BRIEF-PATH-MISSING error."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _create_sample_repo(repo)
            text = _valid_brief_text().replace(
                "- `src/widget.cpp:7`\n",
                "- `src/widget.cpp:7`\n- `src/ghost.cpp`\n",
            )
            findings = brief_lint.lint_brief_text(text, repo_root=repo)
            missing = [f for f in findings if f["rule"] == "BRIEF-PATH-MISSING"]
            self.assertEqual(1, len(missing), findings)
            self.assertIn("src/ghost.cpp", missing[0]["message"])
            self.assertEqual("error", missing[0]["severity"])
            self.assertTrue(brief_lint.has_errors(findings))

    def test_rule_line_out_of_range_positive_and_negative(self) -> None:
        """file:line and 'around line N' beyond EOF trigger BRIEF-LINE-OUT-OF-RANGE."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _create_sample_repo(repo)
            text = _valid_brief_text().replace("around line 5", "around line 999").replace("src/widget.cpp:7", "src/widget.cpp:500")
            findings = brief_lint.lint_brief_text(text, repo_root=repo)
            oob = [f for f in findings if f["rule"] == "BRIEF-LINE-OUT-OF-RANGE"]
            self.assertEqual(2, len(oob), findings)
            self.assertTrue(all(f["severity"] == "error" for f in oob))

    def test_rule_symbol_missing_warning_only(self) -> None:
        """Missing backticked call or named class emits warning only and exits 0."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _create_sample_repo(repo)
            text = _valid_brief_text().replace("`computeValue()`", "`nonExistentHelper()`")
            text += "\nInspect class `GhostClass`.\n"
            brief = repo / "briefs" / "sym.md"
            brief.write_text(text, encoding="utf-8")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = brief_lint.main([str(brief), "--repo", str(repo), "--author", "orchestrator"])
            self.assertEqual(0, rc)
            findings = json.loads(buf.getvalue())
            sym_findings = [f for f in findings if f["rule"] == "BRIEF-SYMBOL-MISSING"]
            self.assertEqual(3, len(sym_findings), findings)
            self.assertTrue(all(f["severity"] == "warning" for f in sym_findings))
            msgs = "\n".join(f["message"] for f in sym_findings)
            self.assertIn("nonExistentHelper", msgs)
            self.assertIn("GhostClass", msgs)

    def test_rule_brief_too_long_per_engine(self) -> None:
        """Brief exceeding engine token budget triggers BRIEF-TOO-LONG while larger engine passes."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _create_sample_repo(repo)
            engines_toml = repo / "engines.toml"
            engines_toml.write_text(
                'version = 2\n'
                '[engines.small]\nmodel = "small"\nclass = "local"\ncontext_tokens = 800\n'
                '[engines.big]\nmodel = "big"\nclass = "cloud"\ncontext_tokens = 200000\n',
                encoding="utf-8",
            )
            long_brief = _valid_brief_text() + ("\nDetail line for padding.\n" * 40)
            f_small = brief_lint.lint_brief_text(
                long_brief, repo_root=repo, engine="small", engines_config=engines_toml
            )
            self.assertTrue(any(f["rule"] == "BRIEF-TOO-LONG" and f["severity"] == "error" for f in f_small))
            f_big = brief_lint.lint_brief_text(
                long_brief, repo_root=repo, engine="big", engines_config=engines_toml
            )
            self.assertFalse(any(f["rule"] == "BRIEF-TOO-LONG" for f in f_big))

    def test_rule_brief_no_scope_and_contradiction_and_human_mode(self) -> None:
        """Missing scope and Constraints-vs-Expected-output contradiction are errors for orchestrator and warnings for human."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _create_sample_repo(repo)
            bad = (
                "## Goal\nEdit test.\n## Input\n- `tests/test_widget.cpp`\n"
                "## Expected output\n- Modify `tests/test_widget.cpp`.\n"
                "## Constraints\n- Never modify `tests/test_widget.cpp`.\n"
                "## Gate\n- `true`\n"
            )
            orch_findings = brief_lint.lint_brief_text(bad, repo_root=repo, author="orchestrator")
            rules = {f["rule"]: f["severity"] for f in orch_findings}
            self.assertEqual("error", rules.get("BRIEF-NO-SCOPE"))
            self.assertEqual("error", rules.get("BRIEF-CONTRADICTION"))

            human_findings = brief_lint.lint_brief_text(bad, repo_root=repo, author="human")
            self.assertTrue(all(f["severity"] == "warning" for f in human_findings))
            self.assertFalse(brief_lint.has_errors(human_findings))

    def test_cli_exit_codes_error_and_usage(self) -> None:
        """CLI returns 1 on error findings and 2 on invalid arguments or missing brief."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _create_sample_repo(repo)
            bad_brief = repo / "briefs" / "bad.md"
            bad_brief.write_text("Incomplete brief.\n", encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()):
                rc_err = brief_lint.main([str(bad_brief), "--repo", str(repo)])
            self.assertEqual(1, rc_err)

            with contextlib.redirect_stderr(io.StringIO()):
                rc_usage = brief_lint.main([str(repo / "briefs" / "missing.md"), "--repo", str(repo)])
            self.assertEqual(2, rc_usage)

    def test_broker_refuses_invalid_brief_and_records_warnings_in_run_json(self) -> None:
        """Broker refuses briefs with brief_lint errors and records warnings in run.json when valid."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _create_sample_repo(wt)

            worker = base / "worker.py"
            worker.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib\n"
                "pathlib.Path('src/widget.cpp').write_text("
                "'/**\\n * @file widget.cpp\\n * @brief Widget impl.\\n */\\n"
                "#include \"widget.hpp\"\\n"
                "/**\\n * @brief Return 2.\\n * @return Two.\\n */\\n"
                "int Widget::computeValue() const { return 2; }\\n', encoding='utf-8')\n"
                "print(json.dumps({'type':'result','is_error':False,'result':'done'}))\n",
                encoding="utf-8",
            )
            os.chmod(worker, stat.S_IRWXU)

            (wt / "briefs" / "bad.md").write_text("Update src/missing.cpp\n", encoding="utf-8")
            warn_brief = _valid_brief_text().replace("`computeValue()`", "`futureHelper()`")
            (wt / "briefs" / "warn.md").write_text(warn_brief, encoding="utf-8")

            broker = devpack_broker.Broker(
                wt,
                base / "runs",
                isolation="local",
                allow_unsandboxed_local=True,
                agent_bin=str(worker),
                gate_cmd="true",
            )
            try:
                code_bad, payload_bad = broker.execute(
                    {"engine": "haiku", "brief": "briefs/bad.md", "allow": ["src/**"]}
                )
                self.assertEqual(1, code_bad)
                self.assertEqual("refused", payload_bad["status"])
                self.assertIn("brief_lint", payload_bad)
                self.assertTrue(any(f["rule"] == "BRIEF-HEADINGS" for f in payload_bad["brief_lint"]))

                code_ok, payload_ok = broker.execute(
                    {"engine": "haiku", "brief": "briefs/warn.md", "allow": ["src/**"]}
                )
                self.assertEqual(0, code_ok, payload_ok)
                dispatch_run = Path(payload_ok["run_dir"])
                run_json = json.loads((dispatch_run / "run.json").read_text(encoding="utf-8"))
                self.assertIn("brief_lint", run_json)
                self.assertTrue(
                    any(
                        f["rule"] == "BRIEF-SYMBOL-MISSING" and f["severity"] == "warning"
                        for f in run_json["brief_lint"]
                    ),
                    run_json,
                )
            finally:
                broker.close()

    def test_human_dispatch_does_not_block_on_warnings_and_records_brief_lint(self) -> None:
        """Direct dispatch treats brief_lint findings as warnings and records them in run.json."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            wt = base / "wt"
            _create_sample_repo(wt)
            brief = base / "human_brief.md"
            brief.write_text("Minimal human brief missing headings.\n", encoding="utf-8")

            worker = base / "worker.py"
            worker.write_text(
                "#!/usr/bin/env python3\nimport json\n"
                "print(json.dumps({'type':'result','is_error':False,'result':'ok'}))\n",
                encoding="utf-8",
            )
            os.chmod(worker, stat.S_IRWXU)

            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc, payload = devpack_dispatch.dispatch_job(
                    engine_name="haiku",
                    brief_path=brief,
                    workdir=wt,
                    allow_globs=["src/**"],
                    gate_cmd="true",
                    runs_root=base / "runs",
                    isolation="local",
                    allow_unsandboxed_local=True,
                    agent_bin=str(worker),
                )
            self.assertEqual(0, rc, payload)
            run_json = json.loads((Path(payload["run_dir"]) / "run.json").read_text(encoding="utf-8"))
            self.assertIn("brief_lint", run_json)
            self.assertTrue(all(f["severity"] == "warning" for f in run_json["brief_lint"]))


if __name__ == "__main__":
    unittest.main()
