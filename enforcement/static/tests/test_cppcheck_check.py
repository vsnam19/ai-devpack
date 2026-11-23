"""Unit and integration tests for enforcement/static/cppcheck_check.py."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

STATIC_TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = STATIC_TESTS_DIR.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from enforcement.static import cppcheck_check  # noqa: E402

SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<results version="2">
    <cppcheck version="2.19.0"/>
    <errors>
        <error id="checkersReport" severity="information" msg="Active checkers: 10/20"/>
        <error id="nullPointer" severity="error" msg="Null pointer dereference: p" verbose="Null pointer dereference: p" cwe="476" file0="src/bad.cpp">
            <location file="{REPO}/src/bad.cpp" line="4" column="24" info="Null pointer dereference"/>
            <location file="{REPO}/src/bad.cpp" line="3" column="14" info="Assignment"/>
        </error>
        <error id="uninitvar" severity="warning" msg="Uninitialized variable: y" file0="src/bad.cpp">
            <location file="src/bad.cpp" line="5" column="12"/>
        </error>
        <error id="unassignedVariable" severity="style" msg="Variable &apos;y&apos; is not assigned a value." file0="src/bad.cpp">
            <location file="src/bad.cpp" line="2" column="9"/>
        </error>
    </errors>
</results>
"""

SAMPLE_TEMPLATE = """Checking src/bad.cpp ...
{REPO}/src/bad.cpp:4:error:nullPointer:Null pointer dereference: p
src/bad.cpp:5:warning:uninitvar:Uninitialized variable: y
src/bad.cpp:2:style:unassignedVariable:Variable 'y' is not assigned a value.
"""


class CppcheckCheckUnitTests(unittest.TestCase):
    """Tests for XML/template parsing, severity filtering, baseline, TOOL_MISSING, and real cppcheck."""

    def test_parse_xml_and_template_normalize_repo_relative_paths(self) -> None:
        """Both XML and --template parsers emit normalized findings with repo-relative paths."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            xml_findings = cppcheck_check.parse_cppcheck_xml(
                SAMPLE_XML.replace("{REPO}", str(repo)),
                repo_root=repo,
            )
            tpl_findings = cppcheck_check.parse_cppcheck_template(
                SAMPLE_TEMPLATE.replace("{REPO}", str(repo)),
                repo_root=repo,
            )
            expected = [
                cppcheck_check.CppcheckFinding(
                    file="src/bad.cpp",
                    line=4,
                    id="nullPointer",
                    severity="error",
                    message="Null pointer dereference: p",
                ),
                cppcheck_check.CppcheckFinding(
                    file="src/bad.cpp",
                    line=5,
                    id="uninitvar",
                    severity="warning",
                    message="Uninitialized variable: y",
                ),
                cppcheck_check.CppcheckFinding(
                    file="src/bad.cpp",
                    line=2,
                    id="unassignedVariable",
                    severity="style",
                    message="Variable 'y' is not assigned a value.",
                ),
            ]
            self.assertEqual(expected, xml_findings)
            self.assertEqual(expected, tpl_findings)
            for item in xml_findings:
                d = item.to_dict()
                self.assertEqual({"file", "line", "id", "severity", "message"}, set(d.keys()))
                self.assertFalse(Path(d["file"]).is_absolute())

    def test_severity_filtering_and_baseline_suppression(self) -> None:
        """Default fail_on blocks error/warning, ignores style, and baseline suppresses only pre-existing counts."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "preflight.toml").write_text(
                '[general]\nstd = "c++17"\n\n'
                '[cppcheck]\n'
                'enable = ["warning", "style", "performance", "portability"]\n'
                'fail_on = ["error", "warning"]\n',
                encoding="utf-8",
            )
            cfg, _ = cppcheck_check.load_cppcheck_config(repo_root=repo)
            self.assertEqual(("error", "warning"), cfg.fail_on)

            findings = cppcheck_check.parse_cppcheck_xml(
                SAMPLE_XML.replace("{REPO}", str(repo)),
                repo_root=repo,
            )
            # Only style finding -> non-blocking
            style_only = [f for f in findings if f.severity == "style"]
            self.assertEqual(1, len(style_only))

            baseline_file = repo / "cppcheck_baseline.json"
            cppcheck_check.save_baseline(baseline_file, [findings[0], findings[1]])
            loaded = cppcheck_check.load_baseline(baseline_file)
            active, baselined = cppcheck_check.filter_findings_with_baseline(findings, loaded)
            self.assertEqual(2, len(baselined))
            self.assertEqual([findings[2]], active)

            # A duplicate new error finding in excess of baseline count=1 is NOT suppressed
            extra = [*findings, findings[0]]
            active2, baselined2 = cppcheck_check.filter_findings_with_baseline(extra, loaded)
            self.assertEqual(2, len(baselined2))
            self.assertEqual([findings[2], findings[0]], active2)

    def test_tool_missing_exits_3_and_never_silently_passes(self) -> None:
        """When cppcheck is absent from PATH, cppcheck_check exits 3 with a clear TOOL_MISSING message."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / "src" / "clean.cpp").write_text("int ok() { return 1; }\n", encoding="utf-8")

            old_path = os.environ.get("PATH", "")
            out_buf = io.StringIO()
            err_buf = io.StringIO()
            try:
                os.environ["PATH"] = str(repo / "empty_bin")
                with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                    rc = cppcheck_check.main(["--json", "--repo-root", str(repo), "src/clean.cpp"])
            finally:
                os.environ["PATH"] = old_path

            self.assertEqual(3, rc)
            self.assertIn("TOOL_MISSING", err_buf.getvalue())
            payload = json.loads(out_buf.getvalue())
            self.assertTrue(payload["tool_missing"])
            self.assertIn("TOOL_MISSING", payload["error"])

    def test_real_cppcheck_detects_null_deref_and_uninitvar_and_respects_baseline(self) -> None:
        """Run real cppcheck on a temp file with null deref + uninitialized variable and verify JSON, exit 1, and baseline."""
        if shutil.which("cppcheck") is None:
            self.skipTest("cppcheck is not installed on PATH; skipping real cppcheck integration test")

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "include").mkdir()
            (repo / "src").mkdir()
            (repo / "preflight.toml").write_text(
                '[general]\nstd = "c++17"\n\n'
                '[cppcheck]\n'
                'enable = ["warning", "style", "performance", "portability"]\n'
                'std = "c++17"\n'
                'include_dirs = ["include"]\n'
                'fail_on = ["error", "warning"]\n',
                encoding="utf-8",
            )
            bad_cpp = repo / "src" / "bad.cpp"
            bad_cpp.write_text(
                "int badFunc(int flag) {\n"
                "    int uninitVal;\n"
                "    int *ptr = nullptr;\n"
                "    if (flag > 0) {\n"
                "        return *ptr;\n"
                "    }\n"
                "    return uninitVal;\n"
                "}\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
            subprocess.run(["git", "add", "preflight.toml"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

            out_buf = io.StringIO()
            err_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                rc = cppcheck_check.main(
                    ["--json", "--repo-root", str(repo), "--base", "HEAD"]
                )
            self.assertEqual(1, rc, err_buf.getvalue())
            payload = json.loads(out_buf.getvalue())
            self.assertGreaterEqual(payload["blocking_findings"], 1)
            ids = {f["id"] for f in payload["findings"]}
            self.assertTrue(
                ids & {"nullPointer", "uninitvar"},
                f"Expected nullPointer or uninitvar in {ids}",
            )
            for item in payload["findings"]:
                self.assertEqual("src/bad.cpp", item["file"])
                self.assertIsInstance(item["line"], int)
                self.assertGreater(item["line"], 0)

            # Record baseline and verify subsequent run exits 0
            baseline = repo / "cppcheck_baseline.json"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc_upd = cppcheck_check.main(
                    ["--repo-root", str(repo), "--baseline", str(baseline), "--baseline-update", "src/bad.cpp"]
                )
                rc_base = cppcheck_check.main(
                    ["--repo-root", str(repo), "--baseline", str(baseline), "src/bad.cpp"]
                )
            self.assertEqual(0, rc_upd)
            self.assertEqual(0, rc_base)


if __name__ == "__main__":
    unittest.main()
