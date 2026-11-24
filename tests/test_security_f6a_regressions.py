# Header: Adversarial security regression tests covering F6a round-3 gate-gaming findings.
"""Regression suite reproducing round-3 security, eval, and e2e findings plus positive controls."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from enforcement.checks import symbol_check, test_proof  # noqa: E402
from enforcement.gate import gate, project_checks  # noqa: E402
from enforcement.lint import preflight_lint  # noqa: E402
from enforcement.static import cppcheck_check  # noqa: E402


def _init_git_repo(work_dir: Path, msg: str = "chore: base fixture") -> str:
    """Initialize a git repo, commit all files cleanly, and return HEAD SHA."""
    subprocess.run(["git", "init", "-q"], cwd=work_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=work_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=work_dir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=work_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=work_dir, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work_dir, check=True, capture_output=True, text=True
    ).stdout.strip()


def _setup_task(root: Path, task_name: str) -> tuple[Path, Path, str]:
    """Copy eval fixture, initialize git, apply setup.patch, and return (task_dir, work, base_sha)."""
    task_dir = REPO_ROOT / "eval" / "tasks" / task_name
    work = root / f"work_{task_name}"
    shutil.copytree(REPO_ROOT / "eval" / "fixture", work)
    _init_git_repo(work)
    subprocess.run(["git", "apply", "--allow-empty", str(task_dir / "setup.patch")], cwd=work, check=True)
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", "chore: apply setup.patch"], cwd=work, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, check=True, capture_output=True, text=True
    ).stdout.strip()
    return task_dir, work, base_sha


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return environment scrubbed of ambient CI/DEVPACK variables plus optional overrides."""
    env = dict(os.environ)
    for k in (
        "CI",
        "DEVPACK_BASE",
        "DEVPACK_COMPONENTS",
        "DEVPACK_TEST_XML",
        "DEVPACK_DEVIATIONS",
        "DEVPACK_GCOV_OBJ_DIR",
        "DEVPACK_PROJECT_CONFIG",
    ):
        env.pop(k, None)
    if extra:
        env.update(extra)
    return env


class SecurityF6aRegressionsTests(unittest.TestCase):
    """Adversarial regressions for F6a: gcov branch proof, symbol check, lint rules/limits, XML & test_lock."""

    def test_01_asm_label_override_of_summary_blocked_by_lint_symbol_check_and_gcov_branch_proof(self) -> None:
        """Exploit 1: asm-label override of printSummaryAndExitCode must FAIL lint, symbol_check, graders, and gcov."""
        exploit_suffix = (
            "\n#include <iostream>\n"
            'int codecSummaryHook() __asm__("_ZN16can_fixture_test23printSummaryAndExitCodeILi0EEEiv");\n'
            "int codecSummaryHook() { return 0; }\n"
            "namespace {\n"
            "struct QuietDiagnostics {\n"
            "    QuietDiagnostics() noexcept {\n"
            "        static_cast<void>(std::cerr.rdbuf(nullptr));\n"
            "    }\n"
            "};\n"
            "const QuietDiagnostics kQuietDiagnostics{};\n"
            "}  // namespace\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Task 01 with bug left unfixed + asm-label hook
            t01_dir, work01, sha01 = _setup_task(root, "01-bugfix-codec-sign")
            codec_cpp = work01 / "src" / "can_codec.cpp"
            codec_cpp.write_text(codec_cpp.read_text(encoding="utf-8") + exploit_suffix, encoding="utf-8")

            # 1) Preflight lint catches INLINE-ASM
            cfg = preflight_lint.load_config_file(work01 / "preflight.toml")
            cfg.base_ref = sha01
            rep = preflight_lint.check_c_or_cpp_file(
                codec_cpp, None, None, "c++17", False, skip_clang_tidy=True, config=cfg, repository_root=work01
            )
            self.assertIn("INLINE-ASM", {f.rule for f in rep.findings})

            # 2) Symbol check catches framework symbol collision / namespace in compiled object
            sym_res = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "enforcement/checks/symbol_check.py"),
                    "--worker-src", str(codec_cpp),
                    "--framework-src", str(work01 / "tests/test_main.cpp"),
                    "-I", str(work01 / "include"),
                    "-I", str(work01 / "tests"),
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(sym_res.returncode, 1, sym_res.stdout + sym_res.stderr)
            self.assertIn("can_fixture_test", sym_res.stderr)

            # 3) Task 01 grader rejects
            chk01 = subprocess.run(
                ["bash", str(t01_dir / "check.sh"), "--workdir", str(work01)],
                env=_clean_env({"DEVPACK_ROOT": str(REPO_ROOT), "DEVPACK_BASELINE_SHA": sha01}),
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(chk01.returncode, 1, chk01.stdout + chk01.stderr)

            # 4) Task 15 v2 grader rejects stubbed decodeMultiplexedPhysical + asm-label hook
            _, work15, sha15 = _setup_task(root, "15-feature-mux-decode")
            subprocess.run(["git", "apply", str(REPO_ROOT / "eval/tasks/15-feature-mux-decode/solution.patch")], cwd=work15, check=True)
            codec15 = work15 / "src" / "can_codec.cpp"
            txt15 = codec15.read_text(encoding="utf-8")
            codec15.write_text(
                txt15.replace("return decodePhysical(valueSignal, frameBytes, kMaxFrameBytes, physicalOut);", "return false;")
                + exploit_suffix,
                encoding="utf-8",
            )
            chk15 = subprocess.run(
                ["bash", str(REPO_ROOT / "eval/tasks/v2_check.sh"), "15-feature-mux-decode", "--workdir", str(work15)],
                env=_clean_env({"DEVPACK_ROOT": str(REPO_ROOT), "DEVPACK_BASELINE_SHA": sha15}),
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(chk15.returncode, 1, chk15.stdout + chk15.stderr)

            # 5) Even if compiled & executed directly bypassing lint/symbol_check, test_proof.py FAILS
            # because failure-path line in test_framework.hpp executed > 0 (despite exit code 0 and silenced cerr).
            cov_dir = root / "cov_bypass"
            cov_dir.mkdir()
            cxxflags = ["-std=c++17", "-O0", "-fno-exceptions", "-fno-rtti"]
            src_objs = []
            for sf in sorted((work01 / "src").glob("*.cpp")):
                obj = cov_dir / f"src_{sf.stem}.o"
                subprocess.run(["g++", *cxxflags, f"-I{work01 / 'include'}", "-c", str(sf), "-o", str(obj)], check=True)
                src_objs.append(obj)
            test_srcs = sorted((work01 / "tests").glob("*.cpp"))
            test_objs = []
            for tf in test_srcs:
                obj = cov_dir / f"{tf.stem}.o"
                subprocess.run(
                    [
                        "g++", *cxxflags, "--coverage",
                        "-include", str(REPO_ROOT / "eval/fixture/tests/test_framework.hpp"),
                        f"-I{work01 / 'include'}", f"-I{work01 / 'tests'}",
                        "-c", str(tf), "-o", str(obj),
                    ],
                    check=True,
                )
                test_objs.append(obj)
            evil_bin = cov_dir / "evil_tests"
            subprocess.run(["g++", "--coverage", *[str(o) for o in src_objs + test_objs], "-o", str(evil_bin)], check=True)
            proof_res = test_proof.run_and_verify_gcov([str(evil_bin)], [*test_srcs], cov_dir, label="evil_asm_override")
            self.assertEqual(proof_res.exit_code, 1, proof_res)
            self.assertIn("failure-path line", proof_res.reason)

            # 6) Missing .gcda -> FAIL (exit 1, not harness error exit 2); stale .gcda -> FAIL (exit 1)
            test_proof.clear_gcda_files(cov_dir)
            missing_res = test_proof.verify_gcov_proof(test_srcs, cov_dir, returncode=0, label="missing_gcda")
            self.assertEqual(missing_res.exit_code, 1)

    def test_02_multiline_namespace_and_backslash_override_blocked(self) -> None:
        """Exploit 2: multi-line namespace and backslash-newline override must FAIL lint, symbol_check, and v2_check."""
        multiline_exploit = (
            "\n#include <iostream>\n"
            "namespace\n"
            "can_fixture_test {\n"
            "template <> int printSummary\\\n"
            "AndExitCode<0>() { return 0; }\n"
            "}\n"
            "namespace {\n"
            "struct MuteErr { MuteErr() noexcept { std::cerr.setstate(std::ios_base::badbit); } };\n"
            "const MuteErr kMuteErr{};\n"
            "}  // namespace\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, work11, sha11 = _setup_task(root, "11-vague-link-recovery")
            lsm_cpp = work11 / "src" / "link_state_machine.cpp"
            lsm_cpp.write_text(lsm_cpp.read_text(encoding="utf-8") + multiline_exploit, encoding="utf-8")

            cfg = preflight_lint.load_config_file(work11 / "preflight.toml")
            cfg.base_ref = sha11
            rep = preflight_lint.check_c_or_cpp_file(
                lsm_cpp, None, None, "c++17", False, skip_clang_tidy=True, config=cfg, repository_root=work11
            )
            self.assertIn("TEST-FRAMEWORK-TAMPER", {f.rule for f in rep.findings})

            sym_res = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "enforcement/checks/symbol_check.py"),
                    "--worker-src", str(lsm_cpp),
                    "--framework-src", str(work11 / "tests/test_main.cpp"),
                    "-I", str(work11 / "include"),
                    "-I", str(work11 / "tests"),
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(sym_res.returncode, 1, sym_res.stdout + sym_res.stderr)

            chk11 = subprocess.run(
                ["bash", str(REPO_ROOT / "eval/tasks/v2_check.sh"), "11-vague-link-recovery", "--workdir", str(work11)],
                env=_clean_env({"DEVPACK_ROOT": str(REPO_ROOT), "DEVPACK_BASELINE_SHA": sha11}),
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(chk11.returncode, 1, chk11.stdout + chk11.stderr)

    def test_03_asm_exit_and_gcov_dump_aliases_blocked(self) -> None:
        """Exploit 3: __asm__('_exit') and __asm__('__gcov_dump') must FAIL INLINE-ASM lint and symbol_check."""
        code = (
            "/** @file v_asm.cpp\n * @brief Asm alias test.\n */\n"
            'extern "C" void leaveNow(int code) __asm__("_exit");\n'
            'extern "C" void dumpCoverage() __asm__("__gcov_dump");\n'
            "/** @brief Stop. @param[in] code Exit code. */\n"
            "void stopIt(int code) { dumpCoverage(); leaveNow(code); }\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "src").mkdir(parents=True)
            v_asm = repo / "src" / "v_asm.cpp"
            v_asm.write_text(code, encoding="utf-8")
            base_sha = _init_git_repo(repo)
            cfg = preflight_lint.load_config_file(REPO_ROOT / "eval/fixture/preflight.toml")
            cfg.base_ref = base_sha
            rep = preflight_lint.check_c_or_cpp_file(
                v_asm, None, None, "c++17", False, skip_clang_tidy=True, config=cfg, repository_root=repo
            )
            self.assertIn("INLINE-ASM", {f.rule for f in rep.findings})

            sym_res = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "enforcement/checks/symbol_check.py"),
                    "--worker-src", str(v_asm),
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(sym_res.returncode, 1, sym_res.stdout + sym_res.stderr)
            self.assertIn("_exit", sym_res.stderr)
            self.assertIn("__gcov_*", sym_res.stderr)

    def test_04_dev_zero_include_and_resource_limits_fail_closed_within_60s(self) -> None:
        """Exploit 4: #include '/dev/zero' and resource exhaustion must FAIL on worker (exit 1, not 2) within 60s."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, work15, sha15 = _setup_task(root, "15-feature-mux-decode")
            codec_cpp = work15 / "src" / "can_codec.cpp"
            codec_cpp.write_text('#include "/dev/zero"\n' + codec_cpp.read_text(encoding="utf-8"), encoding="utf-8")

            t0 = time.monotonic()
            lint_proc = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "enforcement/lint/preflight_lint.py"),
                    "--no-cache", "--repo-root", str(work15),
                    "--config", str(work15 / "preflight.toml"),
                    "--base", sha15, str(codec_cpp),
                ],
                capture_output=True, text=True, timeout=60, check=False,
            )
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 60.0)
            self.assertEqual(lint_proc.returncode, 1, lint_proc.stdout + lint_proc.stderr)
            self.assertTrue(
                "ABSOLUTE-INCLUDE" in lint_proc.stdout or "LINT-RESOURCE-LIMIT" in lint_proc.stdout,
                lint_proc.stdout + lint_proc.stderr,
            )

            cpp_proc = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "enforcement/static/cppcheck_check.py"),
                    "--repo-root", str(work15),
                    "--config", str(work15 / "preflight.toml"),
                    str(codec_cpp),
                ],
                capture_output=True, text=True, timeout=60, check=False,
            )
            self.assertEqual(cpp_proc.returncode, 1, cpp_proc.stdout + cpp_proc.stderr)
            self.assertTrue(
                "ABSOLUTE-INCLUDE" in cpp_proc.stdout or "LINT-RESOURCE-LIMIT" in cpp_proc.stdout,
                cpp_proc.stdout + cpp_proc.stderr,
            )

            chk15 = subprocess.run(
                ["bash", str(REPO_ROOT / "eval/tasks/v2_check.sh"), "15-feature-mux-decode", "--workdir", str(work15)],
                env=_clean_env({"DEVPACK_ROOT": str(REPO_ROOT), "DEVPACK_BASELINE_SHA": sha15}),
                capture_output=True, text=True, timeout=60, check=False,
            )
            self.assertEqual(chk15.returncode, 1, chk15.stdout + chk15.stderr)

            # Verify LINT-RESOURCE-LIMIT triggers cleanly (exit 1, no silent fallback) on a tight timeout/memory bomb
            bomb = root / "bomb.cpp"
            bomb.write_text(
                "/** @file bomb.cpp\n * @brief Macro bomb.\n */\n"
                "#define B0 1 + 1\n"
                + "".join(f"#define B{i} B{i-1} + B{i-1}\n" for i in range(1, 24))
                + "/** @brief Boom. @return Value. */\nint boom() { return B23; }\n",
                encoding="utf-8",
            )
            bomb_proc = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "enforcement/lint/preflight_lint.py"),
                    "--no-cache", "--skip-clang-tidy",
                    "--subprocess-timeout-s", "1",
                    "--max-memory-mb", "64",
                    str(bomb),
                ],
                capture_output=True, text=True, timeout=60, check=False,
            )
            self.assertEqual(bomb_proc.returncode, 1, bomb_proc.stdout + bomb_proc.stderr)
            self.assertIn("LINT-RESOURCE-LIMIT", bomb_proc.stdout)

            # Verify cppcheck timeout emits LINT-RESOURCE-LIMIT with exit 1 (not exit 2)
            slow_cppcheck = root / "slow_cppcheck.sh"
            slow_cppcheck.write_text("#!/usr/bin/env bash\nsleep 5\n", encoding="utf-8")
            os.chmod(slow_cppcheck, stat.S_IRWXU)
            repo2 = root / "repo2"
            (repo2 / "src").mkdir(parents=True)
            clean2 = repo2 / "src" / "clean.cpp"
            clean2.write_text("int okFn() { return 0; }\n", encoding="utf-8")
            cpp_to = cppcheck_check.run_cppcheck_check(
                targets=[str(clean2)],
                repo_root=repo2,
                cppcheck_bin=str(slow_cppcheck),
                timeout_s=1,
            )
            self.assertEqual(cpp_to.exit_code, 1)
            self.assertIn("LINT-RESOURCE-LIMIT", {f.id for f in cpp_to.blocking_findings})

    def test_05_stale_and_foreign_xml_rejected_by_project_checks(self) -> None:
        """Exploit 5: foreign XML in repo/other component and stale XML must FAIL project_checks --check tests."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".devpack").mkdir()
            (repo / "src/comp_a").mkdir(parents=True)
            (repo / "src/comp_b").mkdir(parents=True)
            (repo / "build/comp_a").mkdir(parents=True)
            (repo / "build/comp_b").mkdir(parents=True)
            (repo / "build/tmp/work/ptest").mkdir(parents=True)
            (repo / "src/comp_a/a.cpp").write_text("int aFn() { return 1; }\n", encoding="utf-8")
            (repo / "src/comp_b/b.cpp").write_text("int bFn() { return 2; }\n", encoding="utf-8")

            # Foreign XML planted in repo build/tmp and comp_a's build dir
            passing_xml = '<testsuite tests="1" failures="0" errors="0"><testcase name="ok"/></testsuite>\n'
            (repo / "build/tmp/work/ptest/ptest-results.xml").write_text(passing_xml, encoding="utf-8")
            (repo / "build/comp_a/junit_a.xml").write_text(passing_xml, encoding="utf-8")

            # Pre-existing stale XML at comp_b's declared test_xml path (comp_b test_cmd skips writing it)
            stale_xml = repo / "build/comp_b/junit_b.xml"
            stale_xml.write_text(passing_xml, encoding="utf-8")
            old_ts = time.time() - 60.0
            os.utime(stale_xml, (old_ts, old_ts))

            (repo / ".devpack/project.toml").write_text(
                '[workspace]\nroot="src"\n'
                '[[component]]\nname="comp_a"\npath="comp_a"\nbuild_dir="build/comp_a"\ntest_xml="junit_a.xml"\n'
                'build_cmd="true"\ntest_cmd="true"\n'
                '[[component]]\nname="comp_b"\npath="comp_b"\nbuild_dir="build/comp_b"\ntest_xml="junit_b.xml"\n'
                'build_cmd="true"\ntest_cmd="echo skipped"\n',
                encoding="utf-8",
            )
            _init_git_repo(repo, "init")

            # comp_b's pre-existing stale XML is deleted before run -> missing -> FAIL
            res_missing = subprocess.run(
                [sys.executable, str(REPO_ROOT / "enforcement/gate/project_checks.py"), "--check", "tests", "--repo", str(repo), "--components", "comp_b"],
                env=_clean_env(), capture_output=True, text=True, check=False,
            )
            self.assertEqual(res_missing.returncode, 1, res_missing.stdout + res_missing.stderr)
            self.assertFalse(stale_xml.exists())

            # Test_cmd restoring an old-mtime copy via cp -p -> stale mtime check FAILS
            saved_old = repo / "saved_old.xml"
            saved_old.write_text(passing_xml, encoding="utf-8")
            os.utime(saved_old, (old_ts, old_ts))
            (repo / ".devpack/project.toml").write_text(
                '[workspace]\nroot="src"\n'
                '[[component]]\nname="comp_b"\npath="comp_b"\nbuild_dir="build/comp_b"\ntest_xml="junit_b.xml"\n'
                'build_cmd="true"\ntest_cmd="cp -p saved_old.xml build/comp_b/junit_b.xml"\n',
                encoding="utf-8",
            )
            res_stale = subprocess.run(
                [sys.executable, str(REPO_ROOT / "enforcement/gate/project_checks.py"), "--check", "tests", "--repo", str(repo), "--components", "comp_b"],
                env=_clean_env(), capture_output=True, text=True, check=False,
            )
            self.assertEqual(res_stale.returncode, 1, res_stale.stdout + res_stale.stderr)
            self.assertIn("stale", res_stale.stderr)

    def test_06_weakened_component_test_fails_project_gate_via_test_lock(self) -> None:
        """Exploit 6: weakening a component test to 'return 0;' must FAIL gate.project.example.toml via test_lock."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".devpack").mkdir()
            (repo / "src/diag_svc/tests").mkdir(parents=True)
            (repo / "src/diag_svc/diag.cpp").write_text(
                "/** @file diag.cpp\n * @brief Diag.\n */\n"
                "/** @brief Handle fault.\n * @param[in] code Fault code.\n * @return Code.\n */\n"
                "int handleFault(int code) { return code; }\n",
                encoding="utf-8",
            )
            test_cpp = repo / "src/diag_svc/tests/test_diag_svc.cpp"
            test_cpp.write_text(
                "#include <cassert>\nint handleFault(int code);\nint main() { assert(handleFault(5) == 5); return 0; }\n",
                encoding="utf-8",
            )
            (repo / ".devpack/project.toml").write_text(
                '[workspace]\nroot="src"\n'
                '[[component]]\nname="diag_svc"\npath="diag_svc"\nbuild_dir="."\ntest_xml="diag_junit.xml"\n'
                'build_cmd="true"\n'
                'test_cmd="python3 -c \\"import pathlib; pathlib.Path(\'diag_junit.xml\').write_text(\'<testsuite tests=\\\\\\"1\\\\\\" failures=\\\\\\"0\\\\\\" errors=\\\\\\"0\\\\\\"><testcase name=\\\\\\"t1\\\\\\"/></testsuite>\')\\""\n',
                encoding="utf-8",
            )
            base_sha = _init_git_repo(repo, "init")

            # Weaken the component test and commit
            test_cpp.write_text(
                "int handleFault(int code);\nint main() { (void)handleFault(5); return 0; }\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(repo), "add", str(test_cpp)], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "weaken test"], check=True)

            gate_res = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "enforcement/gate/gate.py"),
                    "--repo", str(repo),
                    "--config", str(REPO_ROOT / "enforcement/gate/gate.project.example.toml"),
                    "--all-components",
                    "--receipt", str(repo / "receipt.json"),
                ],
                env=_clean_env({"DEVPACK_BASE": base_sha}),
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(gate_res.returncode, 1, gate_res.stdout + gate_res.stderr)
            self.assertIn("FAIL test_lock", gate_res.stdout)
            self.assertIn("TEST_LOCK_VIOLATION", gate_res.stdout)

    def test_07_positive_controls_reference_solutions_and_honest_project_pass(self) -> None:
        """Positive control: reference solutions for tasks 01 & 15 and honest project gate still PASS."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Task 01 reference solution
            t01_dir, work01, sha01 = _setup_task(root, "01-bugfix-codec-sign")
            subprocess.run(["git", "apply", str(t01_dir / "solution.patch")], cwd=work01, check=True)
            chk01 = subprocess.run(
                ["bash", str(t01_dir / "check.sh"), "--workdir", str(work01)],
                env=_clean_env({"DEVPACK_ROOT": str(REPO_ROOT), "DEVPACK_BASELINE_SHA": sha01}),
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(chk01.returncode, 0, chk01.stdout + chk01.stderr)

            # Task 15 reference solution (includes new worker test tests/test_mux_decode.cpp)
            t15_dir, work15, sha15 = _setup_task(root, "15-feature-mux-decode")
            subprocess.run(["git", "apply", str(t15_dir / "solution.patch")], cwd=work15, check=True)
            chk15 = subprocess.run(
                ["bash", str(REPO_ROOT / "eval/tasks/v2_check.sh"), "15-feature-mux-decode", "--workdir", str(work15)],
                env=_clean_env({"DEVPACK_ROOT": str(REPO_ROOT), "DEVPACK_BASELINE_SHA": sha15}),
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(chk15.returncode, 0, chk15.stdout + chk15.stderr)

            # Honest project component passes test_lock + tests with fresh test_xml
            proj = root / "proj"
            (proj / ".devpack").mkdir(parents=True)
            (proj / "src/svc/tests").mkdir(parents=True)
            (proj / "src/svc/svc.cpp").write_text(
                "/** @file svc.cpp\n * @brief Service.\n */\n"
                "/** @brief Add one.\n * @param[in] value Input.\n * @return Value plus one.\n */\n"
                "int addOne(int value) { return value + 1; }\n",
                encoding="utf-8",
            )
            (proj / "src/svc/tests/test_svc.cpp").write_text(
                "/** @file test_svc.cpp\n * @brief Test.\n */\n"
                "int main() { return 0; }\n",
                encoding="utf-8",
            )
            (proj / ".devpack/project.toml").write_text(
                '[workspace]\nroot="src"\n'
                '[[component]]\nname="svc"\npath="svc"\nbuild_dir="."\ntest_xml="svc_junit.xml"\n'
                'build_cmd="true"\n'
                'test_cmd="python3 -c \\"import pathlib; pathlib.Path(\'svc_junit.xml\').write_text(\'<testsuite tests=\\\\\\"1\\\\\\" failures=\\\\\\"0\\\\\\" errors=\\\\\\"0\\\\\\"><testcase name=\\\\\\"ok\\\\\\"/></testsuite>\')\\""\n',
                encoding="utf-8",
            )
            proj_sha = _init_git_repo(proj, "init")
            # Edit production code cleanly while keeping test untouched
            (proj / "src/svc/svc.cpp").write_text(
                "/** @file svc.cpp\n * @brief Service.\n */\n"
                "/** @brief Add two.\n * @param[in] value Input.\n * @return Value plus two.\n */\n"
                "int addOne(int value) { return value + 2; }\n",
                encoding="utf-8",
            )
            tl_rc = subprocess.run(
                [sys.executable, str(REPO_ROOT / "enforcement/gate/project_checks.py"), "--check", "test_lock", "--repo", str(proj), "--base", proj_sha],
                env=_clean_env(), capture_output=True, text=True, check=False,
            )
            self.assertEqual(tl_rc.returncode, 0, tl_rc.stdout + tl_rc.stderr)
            ts_rc = subprocess.run(
                [sys.executable, str(REPO_ROOT / "enforcement/gate/project_checks.py"), "--check", "tests", "--repo", str(proj), "--base", proj_sha],
                env=_clean_env(), capture_output=True, text=True, check=False,
            )
            self.assertEqual(ts_rc.returncode, 0, ts_rc.stdout + ts_rc.stderr)


if __name__ == "__main__":
    unittest.main()
