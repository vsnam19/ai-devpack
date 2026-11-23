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
from unittest import mock

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


EMPTY_RESULTS_XML = '<?xml version="1.0" encoding="UTF-8"?>\n<results version="2">\n    <errors/>\n</results>\n'


def _recording_subprocess_run(calls: list[list[str]], cwds: list[str]):
    """Return a fake ``subprocess.run`` that records argv and cwd and reports no findings."""

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        cwds.append(str(kwargs.get("cwd")))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr=EMPTY_RESULTS_XML)

    return fake_run


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


class CppcheckPathResolutionTests(unittest.TestCase):
    """Tests that include and suppression paths resolve against the analysed repo, not the config directory."""

    def test_include_dirs_resolve_against_analysed_repo_not_config_dir(self) -> None:
        """A relative include_dirs entry in a config under dir A must point cppcheck at repo B's include dir."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pack = base / "pack"
            repo = base / "trial"
            (pack / "include").mkdir(parents=True)
            (repo / "include").mkdir(parents=True)
            (repo / "src").mkdir()
            (pack / "include" / "x.hpp").write_text("#define X_VALUE 1\n", encoding="utf-8")
            (repo / "include" / "x.hpp").write_text("#define X_VALUE 2\n", encoding="utf-8")
            (repo / "src" / "bad.cpp").write_text('#include "x.hpp"\nint f() { return X_VALUE; }\n', encoding="utf-8")
            cfg = pack / "preflight.toml"
            cfg.write_text('[cppcheck]\ninclude_dirs = ["include"]\n', encoding="utf-8")

            calls: list[list[str]] = []
            cwds: list[str] = []
            with mock.patch.object(
                cppcheck_check.subprocess,
                "run",
                side_effect=_recording_subprocess_run(calls, cwds),
            ):
                result = cppcheck_check.run_cppcheck_check(
                    targets=["src/bad.cpp"],
                    repo_root=repo,
                    config_path=cfg,
                    cppcheck_bin=sys.executable,
                )

            self.assertEqual(0, result.exit_code)
            self.assertEqual(1, len(calls))
            argv = calls[0]
            self.assertIn("-I", argv)
            inc_value = argv[argv.index("-I") + 1]
            self.assertEqual(str(repo.resolve()), cwds[0])
            self.assertEqual((repo / "include").resolve(), (Path(cwds[0]) / inc_value).resolve())
            self.assertNotIn(str((pack / "include").resolve()), argv)
            self.assertNotIn(str(pack / "include"), argv)

    def test_include_paths_outside_repo_root_are_usage_errors(self) -> None:
        """Absolute or ``..``-escaping include paths outside --repo-root exit 2 and never reach cppcheck."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "trial"
            outside = base / "elsewhere" / "include"
            (repo / "src").mkdir(parents=True)
            outside.mkdir(parents=True)
            (repo / "src" / "ok.cpp").write_text("int ok() { return 1; }\n", encoding="utf-8")

            calls: list[list[str]] = []
            cwds: list[str] = []
            with mock.patch.object(
                cppcheck_check.subprocess,
                "run",
                side_effect=_recording_subprocess_run(calls, cwds),
            ):
                for bad_dir in (str(outside), "../elsewhere/include"):
                    err_buf = io.StringIO()
                    with contextlib.redirect_stderr(err_buf):
                        rc = cppcheck_check.main(
                            [
                                "--repo-root", str(repo),
                                "--cppcheck-bin", sys.executable,
                                "-I", bad_dir,
                                "src/ok.cpp",
                            ]
                        )
                    self.assertEqual(2, rc, bad_dir)
                    self.assertIn("outside the analysed repo root", err_buf.getvalue())
            self.assertEqual([], calls)

    def test_include_path_inside_repo_root_is_accepted_when_absolute(self) -> None:
        """An absolute include directory that lies inside --repo-root is passed to cppcheck."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / "vendor").mkdir()
            (repo / "src" / "ok.cpp").write_text("int ok() { return 1; }\n", encoding="utf-8")

            calls: list[list[str]] = []
            cwds: list[str] = []
            with mock.patch.object(
                cppcheck_check.subprocess,
                "run",
                side_effect=_recording_subprocess_run(calls, cwds),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = cppcheck_check.main(
                        [
                            "--repo-root", str(repo),
                            "--cppcheck-bin", sys.executable,
                            "-I", str((repo / "vendor").resolve()),
                            "src/ok.cpp",
                        ]
                    )
            self.assertEqual(0, rc)
            argv = calls[0]
            inc_value = argv[argv.index("-I") + 1]
            self.assertEqual((repo / "vendor").resolve(), (Path(cwds[0]) / inc_value).resolve())

    def test_suppressions_file_prefers_repo_then_falls_back_to_config_dir(self) -> None:
        """suppressions_file resolves against the repo root first and then against the config directory."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pack = base / "pack"
            repo = base / "trial"
            pack.mkdir()
            (repo / "src").mkdir(parents=True)
            (repo / "src" / "ok.cpp").write_text("int ok() { return 1; }\n", encoding="utf-8")
            (pack / "suppr.txt").write_text("# pack-provided list\n", encoding="utf-8")
            cfg = pack / "preflight.toml"
            cfg.write_text('[cppcheck]\nsuppressions_file = "suppr.txt"\n', encoding="utf-8")

            calls: list[list[str]] = []
            cwds: list[str] = []
            with mock.patch.object(
                cppcheck_check.subprocess,
                "run",
                side_effect=_recording_subprocess_run(calls, cwds),
            ):
                cppcheck_check.run_cppcheck_check(
                    targets=["src/ok.cpp"], repo_root=repo, config_path=cfg, cppcheck_bin=sys.executable
                )
                self.assertIn(f"--suppressions-list={(pack / 'suppr.txt').resolve()}", calls[0])

                (repo / "suppr.txt").write_text("# repo-owned list\n", encoding="utf-8")
                cppcheck_check.run_cppcheck_check(
                    targets=["src/ok.cpp"], repo_root=repo, config_path=cfg, cppcheck_bin=sys.executable
                )
                self.assertIn(f"--suppressions-list={(repo / 'suppr.txt').resolve()}", calls[1])

    def test_real_cppcheck_reads_repo_header_not_config_dir_header(self) -> None:
        """Real cppcheck reports a defect visible only through the analysed repo's header, not the pack copy."""
        if shutil.which("cppcheck") is None:
            self.skipTest("cppcheck is not installed on PATH; skipping real cppcheck path-resolution test")

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pack = base / "pack"
            repo = base / "trial"
            (pack / "include").mkdir(parents=True)
            (repo / "include").mkdir(parents=True)
            (repo / "src").mkdir()
            # Pristine pack header keeps the index in bounds; the edited repo header moves it out of bounds.
            (pack / "include" / "limits_cfg.hpp").write_text("#define SLOT_INDEX 2\n", encoding="utf-8")
            (repo / "include" / "limits_cfg.hpp").write_text("#define SLOT_INDEX 9\n", encoding="utf-8")
            (repo / "src" / "bad.cpp").write_text(
                '#include "limits_cfg.hpp"\n'
                "int readSlot() {\n"
                "    int buf[4] = {0, 1, 2, 3};\n"
                "    return buf[SLOT_INDEX];\n"
                "}\n",
                encoding="utf-8",
            )
            cfg = pack / "preflight.toml"
            cfg.write_text(
                '[general]\nstd = "c++17"\n\n'
                '[cppcheck]\n'
                'enable = ["warning", "style", "performance", "portability"]\n'
                'include_dirs = ["include"]\n'
                'fail_on = ["error", "warning"]\n',
                encoding="utf-8",
            )

            out_buf = io.StringIO()
            err_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                rc = cppcheck_check.main(
                    ["--json", "--repo-root", str(repo), "--config", str(cfg), "src/bad.cpp"]
                )
            self.assertEqual(1, rc, err_buf.getvalue())
            payload = json.loads(out_buf.getvalue())
            ids = {f["id"] for f in payload["findings"]}
            self.assertIn("arrayIndexOutOfBounds", ids)
            for item in payload["findings"]:
                self.assertEqual("src/bad.cpp", item["file"])

    def test_full_scan_filters_compile_db_and_analyses_unindexed_sources_via_include_dirs(self) -> None:
        """Full component scan passes a filtered temp compile DB and analyses missing component sources via include_dirs."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ws = repo / "meta-org" / "src"
            ipc_inc = ws / "ipc_bus" / "include"
            ipc_src = ws / "ipc_bus" / "src"
            ipc_tp = ws / "ipc_bus" / "third_party"
            diag_src = ws / "diag_svc" / "src"
            poky_dir = repo / "poky" / "core"
            build_dir = repo / "build" / "ipc_bus"
            for d in (ipc_inc, ipc_src, ipc_tp, diag_src, poky_dir, build_dir, repo / ".devpack"):
                d.mkdir(parents=True)

            (repo / ".devpack" / "project.toml").write_text(
                '[workspace]\nroot = "meta-org/src"\nexclude = ["**/third_party/**"]\n\n'
                '[[component]]\nname = "ipc_bus"\npath = "ipc_bus"\n'
                'compile_commands = "build/ipc_bus/compile_commands.json"\n'
                'include_dirs = ["ipc_bus/include"]\n\n'
                '[[component]]\nname = "diag_svc"\npath = "diag_svc"\n\n'
                '[scan]\ndefault = "selected"\nselected = ["ipc_bus"]\n',
                encoding="utf-8",
            )
            (repo / "preflight.toml").write_text(
                '[general]\nstd = "c++17"\n\n[cppcheck]\n'
                'enable = ["warning", "style", "performance", "portability"]\n'
                'fail_on = ["error", "warning"]\n',
                encoding="utf-8",
            )
            (ipc_inc / "ipc_defs.hpp").write_text("#define BUF_CAP 4\n", encoding="utf-8")
            (ipc_src / "indexed.cpp").write_text(
                '#include "ipc_defs.hpp"\nint okFunc() { int a[BUF_CAP] = {0}; return a[0]; }\n',
                encoding="utf-8",
            )
            # Source file present in component but missing from compile_commands.json
            (ipc_src / "unindexed.cpp").write_text(
                '#include "ipc_defs.hpp"\n'
                "int badUnindexed(int flag) {\n"
                "    int *p = nullptr;\n"
                "    if (flag > 0) { return *p; }\n"
                "    int a[BUF_CAP] = {0};\n"
                "    return a[0];\n"
                "}\n",
                encoding="utf-8",
            )
            # Out-of-scope / excluded files with bugs that must never be included in the filtered compile DB
            (ipc_tp / "vendor.cpp").write_text("int vBad() { int *p = nullptr; return *p; }\n", encoding="utf-8")
            (diag_src / "diag.cpp").write_text("int dBad() { int *p = nullptr; return *p; }\n", encoding="utf-8")
            (poky_dir / "poky.c").write_text("int pBad() { int *p = 0; return *p; }\n", encoding="utf-8")

            cc_entries = [
                {
                    "directory": ".",
                    "command": "g++ -std=c++17 -Imeta-org/src/ipc_bus/include -c meta-org/src/ipc_bus/src/indexed.cpp",
                    "file": "meta-org/src/ipc_bus/src/indexed.cpp",
                },
                {
                    "directory": ".",
                    "command": "g++ -c meta-org/src/ipc_bus/third_party/vendor.cpp",
                    "file": "meta-org/src/ipc_bus/third_party/vendor.cpp",
                },
                {
                    "directory": ".",
                    "command": "g++ -c meta-org/src/diag_svc/src/diag.cpp",
                    "file": "meta-org/src/diag_svc/src/diag.cpp",
                },
                {
                    "directory": ".",
                    "command": "gcc -c poky/core/poky.c",
                    "file": "poky/core/poky.c",
                },
            ]
            (build_dir / "compile_commands.json").write_text(json.dumps(cc_entries), encoding="utf-8")

            calls: list[list[str]] = []
            captured_dbs: list[list[dict[str, object]]] = []

            def fake_run(cmd, **kwargs):
                calls.append(list(cmd))
                for arg in cmd:
                    if arg.startswith("--project="):
                        db_path = Path(arg.split("=", 1)[1])
                        captured_dbs.append(json.loads(db_path.read_text(encoding="utf-8")))
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr=EMPTY_RESULTS_XML)

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DEVPACK_COMPONENTS", None)
                with mock.patch.object(cppcheck_check.subprocess, "run", side_effect=fake_run):
                    mocked_res = cppcheck_check.run_cppcheck_check(
                        repo_root=repo,
                        cppcheck_bin=sys.executable,
                    )

            self.assertEqual(0, mocked_res.exit_code)
            self.assertEqual(1, mocked_res.fallback_files_count)
            self.assertEqual(2, len(calls))
            self.assertEqual(1, len(captured_dbs))
            self.assertEqual(
                ["meta-org/src/ipc_bus/src/indexed.cpp"],
                [e["file"] for e in captured_dbs[0]],
            )
            self.assertIn("meta-org/src/ipc_bus/src/unindexed.cpp", calls[1])
            self.assertIn("-I", calls[1])
            self.assertIn("meta-org/src/ipc_bus/include", calls[1])
            self.assertEqual(1, json.loads(cppcheck_check.format_result_json(mocked_res))["fallback_files"])
            self.assertIn("1 file(s) missing from compile_commands.json", cppcheck_check.format_result_text(mocked_res))

            if shutil.which("cppcheck") is not None:
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("DEVPACK_COMPONENTS", None)
                    real_res = cppcheck_check.run_cppcheck_check(repo_root=repo)
                self.assertEqual(1, real_res.exit_code)
                self.assertEqual(1, real_res.fallback_files_count)
                self.assertEqual(
                    {"meta-org/src/ipc_bus/src/unindexed.cpp"},
                    {f.file for f in real_res.blocking_findings},
                )

    def test_extra_args_allowlist_and_fail_on_validation(self) -> None:
        """Disallowed extra_args and empty/unknown fail_on exit 2 without executing cppcheck; allowed extra_args pass."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / "src" / "ok.cpp").write_text("int ok() { return 1; }\n", encoding="utf-8")

            calls: list[list[str]] = []
            cwds: list[str] = []

            # 1. Disallowed extra_args in config -> exit 2 and nothing executed
            for bad_arg in (
                "--addon=x.py",
                "--addon",
                "--rule-file=rules.xml",
                "--rule=foo",
                "--output-file=out.txt",
                "--dump",
                "--plist-output=out_plist",
                "--cppcheck-build-dir=build",
                "--project=other.json",
                "--library=/tmp/evil.cfg",
                "--suppressions-list=/etc/passwd",
                "--platform=/tmp/evil.xml",
                "--suppress=*",
            ):
                cfg = repo / "preflight.toml"
                cfg.write_text(f'[cppcheck]\nextra_args = [{json.dumps(bad_arg)}]\n', encoding="utf-8")
                with mock.patch.object(
                    cppcheck_check.subprocess,
                    "run",
                    side_effect=_recording_subprocess_run(calls, cwds),
                ):
                    err_buf = io.StringIO()
                    with contextlib.redirect_stderr(err_buf):
                        rc = cppcheck_check.main(
                            ["--repo-root", str(repo), "--config", str(cfg), "src/ok.cpp"]
                        )
                self.assertEqual(2, rc, bad_arg)
                self.assertEqual([], calls, f"subprocess executed for disallowed arg {bad_arg}")

            # 2. Empty or unknown fail_on -> exit 2 and nothing executed
            for bad_fail_on in ("fail_on = []", 'fail_on = ["bogus"]'):
                cfg = repo / "preflight.toml"
                cfg.write_text(f"[cppcheck]\n{bad_fail_on}\n", encoding="utf-8")
                with mock.patch.object(
                    cppcheck_check.subprocess,
                    "run",
                    side_effect=_recording_subprocess_run(calls, cwds),
                ):
                    with contextlib.redirect_stderr(io.StringIO()):
                        rc = cppcheck_check.main(
                            ["--repo-root", str(repo), "--config", str(cfg), "src/ok.cpp"]
                        )
                self.assertEqual(2, rc, bad_fail_on)
                self.assertEqual([], calls)

            # 3. Escaping suppressions_file -> exit 2 and nothing executed
            (repo / "preflight.toml").write_text(
                '[cppcheck]\nsuppressions_file = "../outside_suppr.txt"\n', encoding="utf-8"
            )
            with mock.patch.object(
                cppcheck_check.subprocess,
                "run",
                side_effect=_recording_subprocess_run(calls, cwds),
            ):
                with contextlib.redirect_stderr(io.StringIO()):
                    rc = cppcheck_check.main(["--repo-root", str(repo), "src/ok.cpp"])
            self.assertEqual(2, rc)
            self.assertEqual([], calls)

            # 4. Allowlisted extra_args are accepted and forwarded
            allowed = [
                "--inline-suppr",
                "--check-level=exhaustive",
                "--suppress=nullPointer",
                "--suppress=uninitvar:src/ok.cpp",
                "--std=c++20",
                "-DENABLE_DIAG",
                "-DMAX_COUNT=16",
                "-ULEGACY_MODE",
                "--platform=unix64",
                "--max-ctu-depth=4",
            ]
            (repo / "preflight.toml").write_text(
                f"[cppcheck]\nextra_args = {json.dumps(allowed)}\nfail_on = [\"error\", \"warning\"]\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                cppcheck_check.subprocess,
                "run",
                side_effect=_recording_subprocess_run(calls, cwds),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc_ok = cppcheck_check.main(
                        ["--repo-root", str(repo), "--cppcheck-bin", sys.executable, "src/ok.cpp"]
                    )
            self.assertEqual(0, rc_ok)
            self.assertEqual(1, len(calls))
            for flag in allowed:
                self.assertIn(flag, calls[0])

    def test_directory_target_restricts_compile_db_and_rejects_escaping_compile_db_symlink(self) -> None:
        """Directory targets filter compile_commands entries to requested directory, and escaping symlinks in compile_commands fail."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            ws = repo / "ws"
            sub_a = ws / "comp" / "sub_a"
            sub_b = ws / "comp" / "sub_b"
            build_dir = repo / "build"
            outside_file = base / "outside.cpp"
            outside_file.write_text("int outside() { return 0; }\n", encoding="utf-8")
            for d in (sub_a, sub_b, build_dir, repo / ".devpack"):
                d.mkdir(parents=True)
            (repo / ".devpack" / "project.toml").write_text(
                '[workspace]\nroot = "ws"\n\n'
                '[[component]]\nname = "comp"\npath = "comp"\n'
                'compile_commands = "build/compile_commands.json"\n',
                encoding="utf-8",
            )
            (sub_a / "a.cpp").write_text("int fnA() { return 1; }\n", encoding="utf-8")
            (sub_b / "sibling.cpp").write_text("int fnB() { int *p = nullptr; return *p; }\n", encoding="utf-8")
            (build_dir / "compile_commands.json").write_text(
                json.dumps(
                    [
                        {"directory": ".", "command": "g++ -c ws/comp/sub_a/a.cpp", "file": "ws/comp/sub_a/a.cpp"},
                        {"directory": ".", "command": "g++ -c ws/comp/sub_b/sibling.cpp", "file": "ws/comp/sub_b/sibling.cpp"},
                    ]
                ),
                encoding="utf-8",
            )

            calls: list[list[str]] = []
            captured_dbs: list[list[dict[str, object]]] = []

            def fake_run(cmd, **kwargs):
                calls.append(list(cmd))
                for arg in cmd:
                    if arg.startswith("--project="):
                        db_path = Path(arg.split("=", 1)[1])
                        captured_dbs.append(json.loads(db_path.read_text(encoding="utf-8")))
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr=EMPTY_RESULTS_XML)

            with mock.patch.object(cppcheck_check.subprocess, "run", side_effect=fake_run):
                res = cppcheck_check.run_cppcheck_check(
                    targets=["ws/comp/sub_a"],
                    repo_root=repo,
                    cppcheck_bin=sys.executable,
                )
            self.assertEqual(0, res.exit_code)
            self.assertEqual(1, len(captured_dbs))
            self.assertEqual(["ws/comp/sub_a/a.cpp"], [e["file"] for e in captured_dbs[0]])

            # Symlink inside workspace escaping outside repo/workspace in compile_commands.json -> error exit 2
            (sub_a / "escape.cpp").symlink_to(outside_file)
            (build_dir / "compile_commands.json").write_text(
                json.dumps(
                    [
                        {"directory": ".", "command": "g++ -c ws/comp/sub_a/escape.cpp", "file": "ws/comp/sub_a/escape.cpp"},
                    ]
                ),
                encoding="utf-8",
            )
            with contextlib.redirect_stderr(io.StringIO()):
                rc_esc = cppcheck_check.main(
                    ["--repo-root", str(repo), "--cppcheck-bin", sys.executable, "ws/comp/sub_a"]
                )
            self.assertEqual(2, rc_esc)


if __name__ == "__main__":
    unittest.main()
