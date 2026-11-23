# Header: Regression tests for F6a_fix3: stdio-only test framework and preflight fallback tuple shape.
"""Regression suite for F6a_fix3.

Covers two findings:
  1. The forced framework header must not pull static-init iostream objects into test TUs. On GCC 12
     (older libstdc++) ``<iostream>`` emitted ``std::ios_base::Init`` and ``__cxa_atexit`` imports into every
     test TU. The header now uses ``<cstdio>``; the test-object symbol allowlist admits only the exact stdio
     names, and ``__cxa_atexit`` / ``atexit`` / ``ios_base`` stay forbidden. The nm test below guards the GCC 12
     regression on whatever GCC is installed (it does not need GCC 12 itself).
  2. ``_preprocess_repo_origin_lines`` returned 2-tuples on its no-preprocessor fallback while callers unpack
     3-tuples, raising ``ValueError`` when ``allow_missing_tools`` is set and no gcc/clang is present.
All compilation happens in temporary directories; nothing is written into the repository.
"""

from __future__ import annotations

import io
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "runner")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from enforcement.checks import symbol_check  # noqa: E402
from enforcement.lint import preflight_lint  # noqa: E402

FIXTURE_TESTS_DIR = REPO_ROOT / "eval" / "fixture" / "tests"
FRAMEWORK_HDR = FIXTURE_TESTS_DIR / "test_framework.hpp"
_CXX = shutil.which("g++")
_NM = shutil.which("nm")
_FORBIDDEN_IMPORT_SUBSTRINGS = ("ios_base", "__cxa_atexit")

_COMPILER_NAMES = frozenset({"g++", "gcc", "clang++", "clang"})


def _undefined_symbols(obj: pathlib.Path) -> list[str]:
    """Return demangled undefined symbol names of ``obj`` as reported by ``nm -u -C``."""
    proc = subprocess.run(
        [_NM, "-u", "-C", str(obj)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"nm failed: {proc.stderr.strip()}")
    names: list[str] = []
    for line in proc.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] == "U":
            names.append(parts[1].strip())
    return names


def _write(path: pathlib.Path, text: str) -> pathlib.Path:
    """Write ``text`` to ``path`` (UTF-8) and return it."""
    path.write_text(text, encoding="utf-8")
    return path


class FrameworkStdioOnlyTests(unittest.TestCase):
    """The framework header and test-object symbol policy must not reintroduce static-init iostream objects."""

    @unittest.skipUnless(_CXX and _NM, "requires g++ and nm on PATH")
    def test_01_test_tu_including_framework_imports_no_ios_base_or_cxa_atexit(self) -> None:
        """A test TU that includes the framework has no ``ios_base`` or ``__cxa_atexit`` undefined symbols.

        Guards the GCC 12 regression: there, ``<iostream>`` in the forced framework header emitted
        ``std::ios_base::Init`` and ``__cxa_atexit`` into every test object. This runs on the installed GCC.
        """
        with tempfile.TemporaryDirectory(prefix="f6a-fix3-nm-") as tmp_dir:
            root = pathlib.Path(tmp_dir)
            src = _write(
                root / "framework_user.cpp",
                '#include "test_framework.hpp"\n'
                "namespace can_fixture_test {\n"
                "void runFrameworkUserTests() {\n"
                "    CHECK(1 == 1);\n"
                "    CHECK_EQ(2, 2);\n"
                "}\n"
                "}  // namespace can_fixture_test\n"
                "int main() {\n"
                "    can_fixture_test::runFrameworkUserTests();\n"
                "    return can_fixture_test::printSummaryAndExitCode();\n"
                "}\n",
            )
            obj = root / "framework_user.o"
            proc = subprocess.run(
                [_CXX, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-fno-exceptions", "-fno-rtti",
                 "-I", str(FIXTURE_TESTS_DIR), "-c", str(src), "-o", str(obj)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(0, proc.returncode, proc.stdout)
            undefined = _undefined_symbols(obj)
            self.assertTrue(undefined, "expected at least the stdio imports from the framework")
            for name in undefined:
                for needle in _FORBIDDEN_IMPORT_SUBSTRINGS:
                    self.assertNotIn(needle, name, f"forbidden static-init import {name!r} in test TU")

    @unittest.skipUnless(_CXX and _NM, "requires g++ and nm on PATH")
    def test_02_framework_test_object_passes_eval_profile_and_static_destructor_hook_is_rejected(self) -> None:
        """Framework-only test TUs pass the eval test-object policy; a static destructor (``__cxa_atexit``) fails it."""
        with tempfile.TemporaryDirectory(prefix="f6a-fix3-sym-") as tmp_dir:
            root = pathlib.Path(tmp_dir)
            clean_src = _write(
                root / "clean_test.cpp",
                '#include "test_framework.hpp"\n'
                "namespace {\n"
                "void runCleanTests() { CHECK(2 + 2 == 4); }\n"
                "}  // namespace\n",
            )
            hook_src = _write(
                root / "hook_test.cpp",
                '#include "test_framework.hpp"\n'
                "namespace {\n"
                "struct Hook {\n"
                "    ~Hook() {}\n"
                "};\n"
                "Hook gHook;\n"
                "}  // namespace\n"
                "namespace {\n"
                "void runHookTests() { CHECK(true); }\n"
                "}  // namespace\n",
            )
            clean_obj = root / "clean_test.o"
            hook_obj = root / "hook_test.o"
            for src, obj in ((clean_src, clean_obj), (hook_src, hook_obj)):
                ok, err = symbol_check.compile_source_to_object(
                    src,
                    obj,
                    include_dirs=[FIXTURE_TESTS_DIR],
                    extra_flags=["-fno-exceptions", "-fno-rtti"],
                )
                self.assertTrue(ok, err)

            clean_result = symbol_check.check_worker_objects(
                (),
                worker_test_objs=(clean_obj,),
                framework_objs=(),
                nm_bin="nm",
                import_profile="eval",
            )
            self.assertEqual(0, clean_result.exit_code, clean_result.violations)

            hook_result = symbol_check.check_worker_objects(
                (),
                worker_test_objs=(hook_obj,),
                framework_objs=(),
                nm_bin="nm",
                import_profile="eval",
            )
            self.assertEqual(1, hook_result.exit_code)
            hook_symbols = [v.symbol for v in hook_result.violations if v.kind == "symbol_not_allowed"]
            self.assertTrue(
                any("__cxa_atexit" in sym for sym in hook_symbols),
                f"expected __cxa_atexit to be rejected, got {hook_symbols}",
            )

    def test_03_test_object_allowlist_admits_only_exact_stdio_names(self) -> None:
        """Framework-added test-object imports are exactly fprintf/fputs/fflush/stderr/stdout."""
        allowed = {"fprintf", "fputs", "fflush", "stderr", "stdout"}
        for name in sorted(allowed):
            self.assertTrue(
                symbol_check.is_allowed_undefined_symbol(
                    f"{name}@GLIBC_2.2.5",
                    (),
                    is_test_obj=True,
                    include_eval_test_framework=True,
                ),
                name,
            )
        for name in ("__cxa_atexit", "atexit", "std::ios_base::Init::Init()", "std::cerr", "fprintf_chk", "xfprintf"):
            self.assertFalse(
                symbol_check.is_allowed_undefined_symbol(
                    name,
                    (),
                    is_test_obj=True,
                    include_eval_test_framework=True,
                ),
                name,
            )
        # Worker src objects never receive the framework extras, even for the same names.
        self.assertFalse(
            symbol_check.is_allowed_undefined_symbol(
                "fprintf",
                (),
                is_test_obj=False,
                include_eval_test_framework=True,
            )
        )


class PreflightFallbackTupleTests(unittest.TestCase):
    """The no-preprocessor fallback must return the same 3-tuple shape the callers unpack."""

    def test_01_fallback_returns_three_tuples(self) -> None:
        """With no gcc/clang on PATH and allow_missing_tools=True, each fallback entry is (line, text, origin)."""
        real_which = shutil.which

        def _no_compilers(name: str, *args: object, **kwargs: object) -> str | None:
            if name in _COMPILER_NAMES:
                return None
            return real_which(name, *args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="f6a-fix3-pp-") as tmp_dir:
            root = pathlib.Path(tmp_dir)
            target = _write(root / "sample.cpp", "int twice(int v) {\n  return v * 2;\n}\n")
            with mock.patch.object(preflight_lint.shutil, "which", side_effect=_no_compilers):
                entries = preflight_lint._preprocess_repo_origin_lines(
                    target,
                    target.read_text(encoding="utf-8"),
                    is_c=False,
                    std="c++17",
                    c_std="c11",
                    repository_root=root,
                    allow_missing_tools=True,
                    effective_flags=[],
                )
            self.assertTrue(entries)
            for entry in entries:
                self.assertEqual(3, len(entry))
                line_no, text, origin = entry
                self.assertIsInstance(line_no, int)
                self.assertIsInstance(text, str)
                self.assertEqual(str(target.resolve()), origin)

            with self.assertRaises(preflight_lint.PreprocessorMissingError):
                with mock.patch.object(preflight_lint.shutil, "which", side_effect=_no_compilers):
                    preflight_lint._preprocess_repo_origin_lines(
                        target,
                        target.read_text(encoding="utf-8"),
                        is_c=False,
                        std="c++17",
                        c_std="c11",
                        repository_root=root,
                        allow_missing_tools=False,
                        effective_flags=[],
                    )

    def test_02_lint_run_without_preprocessor_and_allow_missing_tools_completes(self) -> None:
        """Full lint with allow_missing_tools=True and no compiler runs the fallback checks without raising."""
        real_which = shutil.which

        def _no_compilers(name: str, *args: object, **kwargs: object) -> str | None:
            if name in _COMPILER_NAMES:
                return None
            return real_which(name, *args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="f6a-fix3-lint-") as tmp_dir:
            root = pathlib.Path(tmp_dir)
            clean = _write(
                root / "clean.cpp",
                "/** @file clean.cpp\n * @brief Clean file.\n */\n"
                "int addValues(int firstValue, int secondValue) {\n"
                "  return firstValue + secondValue;\n"
                "}\n",
            )
            bad = _write(
                root / "bad.cpp",
                "/** @file bad.cpp\n * @brief Lexical cast violation.\n */\n"
                "int castValue(double rawInput) {\n"
                "  return (int)rawInput;\n"
                "}\n",
            )
            with mock.patch.object(preflight_lint.shutil, "which", side_effect=_no_compilers):
                out_clean, err_clean = io.StringIO(), io.StringIO()
                with redirect_stdout(out_clean), redirect_stderr(err_clean):
                    rc_clean = preflight_lint.main(["--allow-missing-tools", "--json", str(clean)])
                self.assertEqual(0, rc_clean, err_clean.getvalue())
                clean_rules = [f["rule"] for f in json.loads(out_clean.getvalue())["reports"][0]["findings"]]
                self.assertIn("TOOL-MISSING", clean_rules)

                out_bad, err_bad = io.StringIO(), io.StringIO()
                with redirect_stdout(out_bad), redirect_stderr(err_bad):
                    rc_bad = preflight_lint.main(["--allow-missing-tools", "--json", str(bad)])
                self.assertEqual(1, rc_bad, err_bad.getvalue())
                bad_rules = [f["rule"] for f in json.loads(out_bad.getvalue())["reports"][0]["findings"]]
                self.assertIn("MISRA-8.2.2", bad_rules)
                self.assertNotIn("ValueError", err_bad.getvalue())


if __name__ == "__main__":
    unittest.main()
