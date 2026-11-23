# Header: Regression tests for F6a_fix2: exact object mapping, joined include rebasing, framework origin.
"""Regression suite for F6a_fix2 robustness findings in the gate symbol and lint code.

Covers three findings:
  1. Object fallback must map object to source exactly (no prefix/mtime guessing).
  2. Joined relative ``-I``/``-isystem``/``-iquote`` flags are rebased on the compile DB ``directory``.
  3. TEST-FRAMEWORK-TAMPER decides framework origin by preprocessor line markers and macro origin.
All work happens in temporary directories; nothing is written into the repository.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "runner")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from enforcement.checks import symbol_check  # noqa: E402
from enforcement.gate import project_checks  # noqa: E402
from enforcement.lint import preflight_lint  # noqa: E402
from runner.workspace_scope import ComponentSpec  # noqa: E402


def _init_git_repo(work_dir: Path, msg: str = "chore: base fixture") -> str:
    """Initialize a git repo in ``work_dir``, commit everything, and return the HEAD SHA."""
    subprocess.run(["git", "init", "-q"], cwd=work_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=work_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=work_dir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=work_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=work_dir, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work_dir, check=True, capture_output=True, text=True
    ).stdout.strip()


def _lint_rules(path: Path, root: Path, base_sha: str) -> set[str]:
    """Run the C++ preflight lint on ``path`` and return the set of finding rule IDs."""
    cfg = preflight_lint.load_config_file(REPO_ROOT / "eval/fixture/preflight.toml")
    cfg.base_ref = base_sha
    rep = preflight_lint.check_c_or_cpp_file(
        path,
        None,
        None,
        "c++17",
        False,
        skip_clang_tidy=True,
        config=cfg,
        repository_root=root,
    )
    return {f.rule for f in rep.findings}


class F6aFix2FrameworkOriginTests(unittest.TestCase):
    """Item 3: framework origin decided by line markers and macro origin."""

    def _fixture_repo(self, root: Path) -> tuple[Path, str]:
        """Copy the pack fixture into ``root`` as a committed base and return (work, base_sha)."""
        work = root / "work"
        shutil.copytree(REPO_ROOT / "eval" / "fixture", work)
        base_sha = _init_git_repo(work)
        return work, base_sha

    def test_01_indirect_include_check_macro_passes_and_direct_reference_fails(self) -> None:
        """A new test using CHECK via a helper header that includes test_framework.hpp must pass lint.

        A worker spelling ``can_fixture_test::recordCheck`` itself must still fail.
        """
        with tempfile.TemporaryDirectory() as tmp:
            work, base_sha = self._fixture_repo(Path(tmp))
            (work / "tests" / "helpers.hpp").write_text(
                "#pragma once\n#include \"test_framework.hpp\"\ninline int helperValue() { return 1; }\n",
                encoding="utf-8",
            )
            ok_test = work / "tests" / "test_indirect_check.cpp"
            ok_test.write_text(
                "#include \"helpers.hpp\"\n"
                "void testIndirectCheck() {\n"
                "  CHECK(true);\n"
                "  CHECK_EQ(helperValue(), 1);\n"
                "}\n",
                encoding="utf-8",
            )
            self.assertNotIn("TEST-FRAMEWORK-TAMPER", _lint_rules(ok_test, work, base_sha))

            bad_test = work / "tests" / "test_direct_ref.cpp"
            bad_test.write_text(
                "#include \"helpers.hpp\"\n"
                "void testDirectRef() {\n"
                "  can_fixture_test::recordCheck<0>(true, \"true\", __FILE__, __LINE__);\n"
                "}\n",
                encoding="utf-8",
            )
            self.assertIn("TEST-FRAMEWORK-TAMPER", _lint_rules(bad_test, work, base_sha))

    def test_02_worker_redefinition_in_helper_header_still_fails(self) -> None:
        """A worker helper header that spells framework symbols, or defines macros over them, must fail."""
        with tempfile.TemporaryDirectory() as tmp:
            work, base_sha = self._fixture_repo(Path(tmp))

            spoof = work / "tests" / "spoof_helpers.hpp"
            spoof.write_text(
                "#pragma once\n"
                "namespace can_fixture_test {\n"
                "inline int spoofCounter() { return 0; }\n"
                "}\n",
                encoding="utf-8",
            )
            self.assertIn("TEST-FRAMEWORK-TAMPER", _lint_rules(spoof, work, base_sha))

            macro_helper = work / "tests" / "macro_helpers.hpp"
            macro_helper.write_text(
                "#pragma once\n"
                "#include \"test_framework.hpp\"\n"
                "#define WORKER_RECORD can_fixture_test::recordCheck<0>\n",
                encoding="utf-8",
            )
            self.assertIn("TEST-FRAMEWORK-TAMPER", _lint_rules(macro_helper, work, base_sha))

            paste_helper = work / "tests" / "paste_helpers.hpp"
            paste_helper.write_text(
                "#pragma once\n"
                "#define WORKER_JOIN(a, b) a##b\n",
                encoding="utf-8",
            )
            self.assertIn("TEST-FRAMEWORK-TAMPER", _lint_rules(paste_helper, work, base_sha))


class _NoExclusions:
    """Scope stub with no excluded paths, enough for the component symbol check."""

    def is_excluded(self, _path: Path) -> bool:
        return False


class F6aFix2ObjectMappingTests(unittest.TestCase):
    """Item 1: object↔source mapping in the symbol-check fallback is exact, never prefix or mtime."""

    def _duplicate_stem_layout(self, root: Path) -> tuple[Path, Path, Path, Path, Path]:
        """Two components sources share the stem ``driver``; return (comp, build, a_src, b_src, cc_path)."""
        comp = root / "comp"
        a_dir = comp / "src" / "a"
        b_dir = comp / "src" / "b"
        build = comp / "build"
        for d in (a_dir, b_dir, build / "a", build / "b"):
            d.mkdir(parents=True, exist_ok=True)
        a_src = a_dir / "driver.cpp"
        b_src = b_dir / "driver.cpp"
        # The a/ source is the one that does not compile; the b/ source is clean and was built.
        a_src.write_text("int broken( {\n", encoding="utf-8")
        b_src.write_text("int cleanValue() { return 1; }\n", encoding="utf-8")
        subprocess.run(
            ["g++", "-std=c++17", "-c", str(b_src), "-o", str(build / "b" / "driver.cpp.o")],
            check=True, capture_output=True, text=True,
        )
        cc_entries = [
            {
                "directory": str(build),
                "file": str(a_src),
                "output": "a/driver.cpp.o",
                "arguments": ["g++", "-std=c++17", "-c", str(a_src), "-o", "a/driver.cpp.o"],
            },
            {
                "directory": str(build),
                "file": str(b_src),
                "output": "b/driver.cpp.o",
                "arguments": ["g++", "-std=c++17", "-c", str(b_src), "-o", "b/driver.cpp.o"],
            },
        ]
        cc_path = build / "compile_commands.json"
        cc_path.write_text(json.dumps(cc_entries), encoding="utf-8")
        return comp, build, a_src, b_src, cc_path

    def test_01_duplicate_stem_broken_source_fails_closed_instead_of_clean_sibling_object(self) -> None:
        """Broken a/driver.cpp with no exact object must FAIL SYMBOL-OBJECT-UNRESOLVED, not reuse b/driver.cpp.o."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            comp, build, _a_src, _b_src, cc_path = self._duplicate_stem_layout(root)
            component = ComponentSpec(
                name="comp",
                path="comp",
                repo_rel_path="comp",
                abs_path=comp,
                compile_commands=str(cc_path.relative_to(root)),
            )
            code, msg = project_checks._run_component_symbol_check(
                root,
                component,
                build,
                _NoExclusions(),
                base_ref="",
                uses_pack_fw=False,
            )
            self.assertEqual(code, 1, msg)
            self.assertIn("SYMBOL-OBJECT-UNRESOLVED", msg)
            self.assertNotIn("PASS", msg)

    def test_02_exact_mapping_returns_only_the_object_recorded_for_that_source(self) -> None:
        """The exact lookup returns a/'s object for a/driver.cpp and b/'s object for b/driver.cpp."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _comp, build, a_src, b_src, cc_path = self._duplicate_stem_layout(root)
            # Build a's object from a clean copy so it exists, then restore the broken source.
            clean_a = root / "clean_a.cpp"
            clean_a.write_text("int cleanAValue() { return 2; }\n", encoding="utf-8")
            subprocess.run(
                ["g++", "-std=c++17", "-c", str(clean_a), "-o", str(build / "a" / "driver.cpp.o")],
                check=True, capture_output=True, text=True,
            )
            # Make a's source older than its object so the object is fresh for it.
            a_mtime = a_src.stat().st_mtime
            os.utime(a_src, (a_mtime - 100, a_mtime - 100))

            got_a = project_checks._exact_compile_db_object(cc_path, a_src, build)
            got_b = project_checks._exact_compile_db_object(cc_path, b_src, build)
            self.assertEqual(got_a, (build / "a" / "driver.cpp.o").resolve())
            self.assertEqual(got_b, (build / "b" / "driver.cpp.o").resolve())
            self.assertNotEqual(got_a, got_b)

    def test_03_source_without_an_output_entry_or_with_stale_object_has_no_exact_match(self) -> None:
        """No output entry, or an object older than its source, must not be used."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _comp, build, a_src, b_src, cc_path = self._duplicate_stem_layout(root)
            # a/ has an output entry but no object on disk: no exact match.
            self.assertIsNone(project_checks._exact_compile_db_object(cc_path, a_src, build))

            # A source with no compile DB entry at all: no exact match even though a driver*.o exists.
            orphan = root / "comp" / "src" / "orphan" / "driver.cpp"
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_text("int orphan() { return 3; }\n", encoding="utf-8")
            self.assertIsNone(project_checks._exact_compile_db_object(cc_path, orphan, build))

            # b/ object exists but the source was edited after the object was built: stale, not used.
            b_mtime = b_src.stat().st_mtime
            os.utime(b_src, (b_mtime + 100, b_mtime + 100))
            self.assertIsNone(project_checks._exact_compile_db_object(cc_path, b_src, build))


class F6aFix2JoinedIncludeTests(unittest.TestCase):
    """Item 2: joined relative -I/-isystem/-iquote flags are rebased on the compile DB directory."""

    def test_01_joined_and_separated_relative_include_flags_are_rebased_on_directory(self) -> None:
        """Every relative include form resolves against the entry directory; -D and -std survive."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build = root / "comp" / "build"
            build.mkdir(parents=True)
            src = root / "comp" / "src" / "unit.cpp"
            src.parent.mkdir(parents=True)
            src.write_text("int unit() { return 0; }\n", encoding="utf-8")
            cc_path = build / "compile_commands.json"
            cc_path.write_text(
                json.dumps(
                    [
                        {
                            "directory": str(build),
                            "file": str(src),
                            "arguments": [
                                "g++",
                                "-std=c++17",
                                "-I../sdk/include",
                                "-isystem../sys/inc",
                                "-iquote../quote/inc",
                                "-I",
                                "../sep/include",
                                "-isystem",
                                "../sys2/inc",
                                "-iquote",
                                "../quote2/inc",
                                "-DUNIT_FLAG=1",
                                "-c",
                                str(src),
                            ],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            flags = symbol_check._load_compile_db_flags(cc_path, src)
            expected = [
                "-std=c++17",
                "-I" + str(build / "../sdk/include"),
                "-isystem" + str(build / "../sys/inc"),
                "-iquote" + str(build / "../quote/inc"),
                "-I",
                str(build / "../sep/include"),
                "-isystem",
                str(build / "../sys2/inc"),
                "-iquote",
                str(build / "../quote2/inc"),
                "-DUNIT_FLAG=1",
            ]
            self.assertEqual([os.path.normpath(f) for f in flags], [os.path.normpath(e) for e in expected])
            for flag in flags:
                for prefix in ("-isystem", "-iquote", "-I"):
                    if flag.startswith(prefix) and len(flag) > len(prefix):
                        self.assertTrue(
                            os.path.normpath(flag[len(prefix):]).startswith(str(root)),
                            f"include flag not rebased under the entry directory: {flag}",
                        )
                        break

    def test_02_joined_relative_include_compiles_against_entry_directory(self) -> None:
        """A source that needs ``-I../sdk/include`` compiles when run from an unrelated working directory."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sdk_inc = root / "comp" / "sdk" / "include"
            build = root / "comp" / "build"
            src_dir = root / "comp" / "src"
            for d in (sdk_inc, build, src_dir):
                d.mkdir(parents=True)
            (sdk_inc / "sdk_types.hpp").write_text(
                "#pragma once\ninline constexpr int kSdkValue = 5;\n", encoding="utf-8"
            )
            src = src_dir / "uses_sdk.cpp"
            src.write_text(
                '#include "sdk_types.hpp"\nint useSdk() { return kSdkValue; }\n', encoding="utf-8"
            )
            cc_path = build / "compile_commands.json"
            cc_path.write_text(
                json.dumps(
                    [
                        {
                            "directory": str(build),
                            "file": str(src),
                            "arguments": ["g++", "-std=c++17", "-I../sdk/include", "-c", str(src)],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            obj_out = root / "uses_sdk.o"
            ok, err = symbol_check.compile_source_to_object(src, obj_out, compile_commands=cc_path)
            self.assertTrue(ok, err)
            self.assertTrue(obj_out.is_file())


if __name__ == "__main__":
    unittest.main()
