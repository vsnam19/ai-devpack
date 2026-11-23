#!/usr/bin/env python3
"""
@file test_preflight_lint.py
@brief Unit and integration tests for enforcement/lint/preflight_lint.py.
@details Covers positive and negative C/C++ snippets for every generic rule,
         configurable naming regexes and parameter limits via preflight.toml and CLI,
         optional clang-tidy notice when absent, fail-closed TOOL-MISSING behavior
         with --allow-missing-tools override, baseline add/suppress ratcheting,
         path-portability of fingerprints across checkouts/machines, and
         fingerprint_policy_version mismatch error with migration hint.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = pathlib.Path(__file__).resolve().parent
LINT_DIR = TESTS_DIR.parent
PREFLIGHT_PATH = LINT_DIR / "preflight_lint.py"

if str(LINT_DIR) not in sys.path:
    sys.path.insert(0, str(LINT_DIR))

import preflight_lint  # noqa: E402


def _write_text(file_path: pathlib.Path, content: str) -> pathlib.Path:
    """Write UTF-8 text to file_path, creating parent directories as needed."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return file_path


def _run_preflight_cli(
    arguments: list[str],
    working_dir: pathlib.Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute preflight_lint.py as a subprocess in working_dir."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    command = [sys.executable, str(PREFLIGHT_PATH), *arguments]
    return subprocess.run(
        command,
        cwd=str(working_dir),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class RuleSnippetTests(unittest.TestCase):
    """Positive and negative C/C++ snippet tests for each preflight lint rule."""

    def _lint_snippet(
        self,
        filename: str,
        content: str,
        config: preflight_lint.LintConfig | None = None,
        allow_new_in_init: bool = False,
        max_params: int = preflight_lint.DEFAULT_MAX_PARAMS,
    ) -> list[preflight_lint.Finding]:
        """Write a snippet to a temporary directory, lint it, and return findings."""
        with tempfile.TemporaryDirectory(prefix="preflight-rule-") as tmp_dir:
            root = pathlib.Path(tmp_dir)
            source = _write_text(root / filename, content)
            cfg = config or preflight_lint.LintConfig(
                max_params=max_params,
                allow_new_in_init=allow_new_in_init,
                skip_clang_tidy=True,
            )
            cfg.skip_clang_tidy = True
            report = preflight_lint.check_c_or_cpp_file(
                source,
                compile_commands=None,
                clang_tidy_cfg=None,
                std=cfg.std,
                allow_new_in_init=cfg.allow_new_in_init,
                max_params=cfg.max_params,
                skip_clang_tidy=True,
                config=cfg,
                repository_root=root,
            )
            return report.findings

    def test_direction_and_file_block_regression(self) -> None:
        """Catch ordinary file comments and output directions on value parameters."""
        code = ("// @file speed.hpp\n// @brief Speed utility.\n"
                "/** @brief Clamp speed.\n * @param[in] speed Current speed.\n"
                " * @param[in,out] limit Maximum speed.\n */\n"
                "unsigned clampVehicleSpeed(unsigned speed, unsigned limit) noexcept;\n")
        findings = self._lint_snippet("speed.hpp", code)
        selected = [f for f in findings if f.rule in {
            "DOXYGEN-FILE-BLOCK", "DOXYGEN-PARAM-DIR-MISMATCH"}]
        self.assertEqual(2, len(selected))
        self.assertEqual({"error"}, {f.severity for f in selected})
        mismatch = next(f for f in selected if f.rule.endswith("MISMATCH"))
        self.assertIn("limit", mismatch.message)
        self.assertIn("unsigned", mismatch.message)

    def test_direction_types_and_configuration(self) -> None:
        """Check const placement, mutable warnings, and rule disabling."""
        for type_name, direction, severity in (
            ("int", "out", "error"), ("const int&", "out", "error"),
            ("int const*", "in,out", "error"),
            ("int* const", "in", "warning"), ("int&", "in", "warning"),
            ("int*", "out", None), ("const int*", "in", None),
            ("int const&", "in", None), ("int", "in", None),
        ):
            with self.subTest(type_name=type_name, direction=direction):
                code = ("/** @file sample.hpp\n * @brief Sample.\n */\n"
                        f"/** @brief Process.\n * @param[{direction}] reading Value.\n */\n"
                        f"void processReading({type_name} reading);\n")
                findings = self._lint_snippet("sample.hpp", code)
                selected = [f for f in findings if f.rule == "DOXYGEN-PARAM-DIR-MISMATCH"]
                self.assertEqual([severity] if severity else [], [f.severity for f in selected])
                cfg = preflight_lint.LintConfig(disabled_rules={"DOXYGEN-PARAM-DIR-MISMATCH"})
                self.assertFalse(any(f.rule == "DOXYGEN-PARAM-DIR-MISMATCH"
                                     for f in self._lint_snippet("sample.hpp", code, config=cfg)))

    def test_direction_nested_type_declarators(self) -> None:
        """Ignore template and function signature indirection inside AST types."""
        for declaration, direction, severity in (
            ("const std::vector<int*>& items", "in", None),
            ("const std::vector<int*>& items", "out", "error"),
            ("Span<int*> items", "in", None),
            ("Span<int*> items", "out", "error"),
            ("void (*items)(int)", "in", None),
            ("void (*items)(int)", "out", "error"),
            ("std::vector<const int*>& items", "in", "warning"),
            ("int& items", "in", "warning"),
        ):
            with self.subTest(declaration=declaration, direction=direction):
                code = ("namespace std { template<class T> class vector {}; }\n"
                        "template<class T> class Span {};\n"
                        f"/** @brief Process.\n * @param[{direction}] items Values.\n */\n"
                        f"void processItems({declaration});\n")
                findings = self._lint_snippet("sample.hpp", code)
                selected = [f.severity for f in findings
                            if f.rule == "DOXYGEN-PARAM-DIR-MISMATCH"]
                self.assertEqual([severity] if severity else [], selected)

    def test_direction_utf8_byte_offsets(self) -> None:
        """Keep documentation attached after multibyte source comments."""
        code = ("// Timing in µs — temperature in °C.\n"
                "/** @brief First.\n * @param[out] value Reading.\n */\n"
                "void firstReading(int value);\n"
                "/** @brief Second.\n * @param[out] value Reading.\n */\n"
                "void secondReading(int value);\n")
        findings = self._lint_snippet("sample.hpp", code)
        selected = [(f.line, f.severity) for f in findings
                    if f.rule == "DOXYGEN-PARAM-DIR-MISMATCH"]
        self.assertEqual([(5, "error"), (9, "error")], selected)

    def test_file_comment_forms(self) -> None:
        """Accept all Doxygen comment forms and ignore tags in string literals."""
        for header in ("/** @file sample.hpp */", "/*! @file sample.hpp */",
                       "/// @file sample.hpp", "//! @file sample.hpp",
                       "/* @file sample.hpp */", "// @file sample.hpp"):
            with self.subTest(header=header):
                findings = self._lint_snippet("sample.hpp", header + "\n/// @brief Sample.\n")
                self.assertEqual(int(header.startswith(("/* @", "// @"))),
                                 sum(f.rule == "DOXYGEN-FILE-BLOCK" for f in findings))
        findings = self._lint_snippet("sample.hpp", 'constexpr auto kText = "// @file fake";\n')
        self.assertFalse(any(f.rule == "DOXYGEN-FILE-BLOCK" for f in findings))

    def test_no_c_style_cast_positive_and_negative(self) -> None:
        """Verify MISRA-8.2.2 flags C-style casts and allows static_cast and (void) casts."""
        bad_code = (
            "/** @file cast_bad.cpp\n * @brief Bad C-style cast.\n */\n"
            "int convertReading(double rawReading) {\n"
            "  return (int)rawReading;\n"
            "}\n"
        )
        bad_findings = self._lint_snippet("cast_bad.cpp", bad_code)
        self.assertIn("MISRA-8.2.2", [f.rule for f in bad_findings])

        good_code = (
            "/** @file cast_good.cpp\n * @brief Good static_cast and void cast.\n */\n"
            "int convertReading(double rawReading) {\n"
            "  (void)rawReading;\n"
            "  return static_cast<int>(rawReading);\n"
            "}\n"
        )
        good_findings = self._lint_snippet("cast_good.cpp", good_code)
        self.assertEqual([], [f.rule for f in good_findings])

    def test_no_heap_allocation_outside_allowed_paths(self) -> None:
        """Verify MISRA-21.6.1 / MISRA-C-21.3 forbids new/delete/malloc/free outside allowed paths."""
        bad_cpp = (
            "/** @file heap_bad.cpp\n * @brief Disallowed heap allocation.\n */\n"
            "#include <cstdlib>\n"
            "void allocateSample() {\n"
            "  int* numberPtr = new int(42);\n"
            "  delete numberPtr;\n"
            "  void* rawBlock = std::malloc(16U);\n"
            "  std::free(rawBlock);\n"
            "}\n"
        )
        bad_findings = self._lint_snippet("heap_bad.cpp", bad_cpp)
        heap_rules = [f.rule for f in bad_findings if f.rule == "MISRA-21.6.1"]
        self.assertGreaterEqual(len(heap_rules), 3)

        bad_c = (
            "/** @file heap_bad.c\n * @brief Disallowed C heap allocation.\n */\n"
            "void* malloc(unsigned long sizeValue);\n"
            "void free(void* memoryBlock);\n"
            "void runAlloc(void) {\n"
            "  void* memoryBlock = malloc(32UL);\n"
            "  free(memoryBlock);\n"
            "}\n"
        )
        c_findings = self._lint_snippet("heap_bad.c", bad_c)
        self.assertIn("MISRA-C-21.3", [f.rule for f in c_findings])

        # Allowed via allowed_heap_paths or placement new / = delete
        allowed_cfg = preflight_lint.LintConfig(
            allowed_heap_paths=("bootstrap/*",),
            skip_clang_tidy=True,
        )
        allowed_findings = self._lint_snippet("bootstrap/heap_ok.cpp", bad_cpp, config=allowed_cfg)
        self.assertNotIn("MISRA-21.6.1", [f.rule for f in allowed_findings])

        placement_and_deleted = (
            "/** @file placement_ok.cpp\n * @brief Placement new and deleted copy.\n */\n"
            "#include <new>\n"
            "class NonCopyable {\n"
            "public:\n"
            "  NonCopyable() = default;\n"
            "  NonCopyable(const NonCopyable&) = delete;\n"
            "};\n"
            "void constructInPlace(void* storageArea) {\n"
            "  (void)new (storageArea) int(7);\n"
            "}\n"
        )
        placement_findings = self._lint_snippet("placement_ok.cpp", placement_and_deleted)
        self.assertEqual([], [f.rule for f in placement_findings])

    def test_no_implicit_lambda_captures_positive_and_negative(self) -> None:
        """Verify MISRA-LAMBDA-CAPTURE flags [=] and [&] captures and allows explicit captures."""
        bad_lambda = (
            "/** @file lambda_bad.cpp\n * @brief Implicit lambda captures.\n */\n"
            "int runLambdas(int baseFactor) {\n"
            "  auto byRef = [&]() { return baseFactor + 1; };\n"
            "  auto byVal = [=]() { return baseFactor + 2; };\n"
            "  return byRef() + byVal();\n"
            "}\n"
        )
        bad_findings = self._lint_snippet("lambda_bad.cpp", bad_lambda)
        lambda_rules = [f for f in bad_findings if f.rule == "MISRA-LAMBDA-CAPTURE"]
        self.assertGreaterEqual(len(lambda_rules), 2)

        good_lambda = (
            "/** @file lambda_good.cpp\n * @brief Explicit lambda captures.\n */\n"
            "int runLambdas(int baseFactor) {\n"
            "  auto explicitVal = [baseFactor]() { return baseFactor + 1; };\n"
            "  auto noCapture = [](int deltaVal) { return deltaVal * 2; };\n"
            "  return explicitVal() + noCapture(baseFactor);\n"
            "}\n"
        )
        good_findings = self._lint_snippet("lambda_good.cpp", good_lambda)
        self.assertEqual([], [f.rule for f in good_findings])

    def test_explicit_single_arg_ctors_positive_and_negative(self) -> None:
        """Verify MISRA-15.1.3 flags non-explicit single-arg constructors and allows explicit/copy/multi-arg."""
        bad_ctor = (
            "/** @file ctor_bad.cpp\n * @brief Non-explicit single-argument constructors.\n */\n"
            "class FrameCodec {\n"
            "public:\n"
            "  FrameCodec(int channelId) : m_channelId(channelId) {}\n"
            "  FrameCodec(double scaleFactor, int offsetValue = 0)\n"
            "      : m_channelId(static_cast<int>(scaleFactor) + offsetValue) {}\n"
            "private:\n"
            "  int m_channelId;\n"
            "};\n"
        )
        bad_findings = self._lint_snippet("ctor_bad.cpp", bad_ctor)
        ctor_findings = [f for f in bad_findings if f.rule == "MISRA-15.1.3"]
        self.assertEqual(2, len(ctor_findings))

        good_ctor = (
            "/** @file ctor_good.cpp\n * @brief Explicit single-arg and copy/multi-arg constructors.\n */\n"
            "class FrameCodec {\n"
            "public:\n"
            "  FrameCodec() : m_channelId(0) {}\n"
            "  explicit FrameCodec(int channelId) : m_channelId(channelId) {}\n"
            "  FrameCodec(int channelId, int busIndex) : m_channelId(channelId + busIndex) {}\n"
            "  FrameCodec(const FrameCodec& otherCodec) = default;\n"
            "  int getChannelId() const { return m_channelId; }\n"
            "private:\n"
            "  int m_channelId;\n"
            "};\n"
        )
        good_findings = self._lint_snippet("ctor_good.cpp", good_ctor)
        self.assertEqual([], [f.rule for f in good_findings])

    def test_explicit_constructor_declarations_do_not_flag_out_of_class_definitions(self) -> None:
        """Verify MISRA-15.1.3 checks only in-class constructors and retains its copy/move exemption."""
        source = (
            "/** @file ctor_declarations.cpp\n * @brief Constructor declaration coverage.\n */\n"
            "class ExplicitCodec {\n"
            "public:\n"
            "  explicit ExplicitCodec(int channelId);\n"
            "};\n"
            "ExplicitCodec::ExplicitCodec(int channelId) {}\n"
            "class ImplicitCodec {\n"
            "public:\n"
            "  ImplicitCodec(int channelId);\n"
            "};\n"
            "ImplicitCodec::ImplicitCodec(int channelId) {}\n"
            "class CopyMoveCodec {\n"
            "public:\n"
            "  CopyMoveCodec(const CopyMoveCodec& otherCodec);\n"
            "  CopyMoveCodec(CopyMoveCodec&& otherCodec);\n"
            "};\n"
            "CopyMoveCodec::CopyMoveCodec(const CopyMoveCodec& otherCodec) {}\n"
            "CopyMoveCodec::CopyMoveCodec(CopyMoveCodec&& otherCodec) {}\n"
        )
        findings = self._lint_snippet("ctor_declarations.cpp", source)
        ctor_findings = [f for f in findings if f.rule == "MISRA-15.1.3"]
        self.assertTrue(ctor_findings)
        self.assertTrue(all(finding.line == 11 for finding in ctor_findings))
        self.assertTrue(all("ImplicitCodec" in finding.message for finding in ctor_findings))

    def test_override_on_virtual_methods_positive_and_negative(self) -> None:
        """Verify MISRA-13.3.1 flags overridden virtual methods missing override/final."""
        bad_override = (
            "/** @file override_bad.cpp\n * @brief Missing override keyword.\n */\n"
            "class BaseFilter {\n"
            "public:\n"
            "  virtual ~BaseFilter() = default;\n"
            "  virtual int applyFilter(int sampleInput) const { return sampleInput; }\n"
            "};\n"
            "class LowPassFilter : public BaseFilter {\n"
            "public:\n"
            "  int applyFilter(int sampleInput) const { return sampleInput / 2; }\n"
            "};\n"
        )
        bad_findings = self._lint_snippet("override_bad.cpp", bad_override)
        self.assertIn("MISRA-13.3.1", [f.rule for f in bad_findings])

        good_override = (
            "/** @file override_good.cpp\n * @brief Explicit override keyword.\n */\n"
            "class BaseFilter {\n"
            "public:\n"
            "  virtual ~BaseFilter() = default;\n"
            "  virtual int applyFilter(int sampleInput) const { return sampleInput; }\n"
            "};\n"
            "class LowPassFilter : public BaseFilter {\n"
            "public:\n"
            "  int applyFilter(int sampleInput) const override { return sampleInput / 2; }\n"
            "};\n"
        )
        good_findings = self._lint_snippet("override_good.cpp", good_override)
        self.assertEqual([], [f.rule for f in good_findings])

    def test_switch_default_positive_and_negative(self) -> None:
        """Verify MISRA-9.4.2 flags switch statements missing default and passes when default exists."""
        bad_switch = (
            "/** @file switch_bad.cpp\n * @brief Switch without default.\n */\n"
            "int mapGear(int gearCode) {\n"
            "  switch (gearCode) {\n"
            "  case 1:\n"
            "    return 10;\n"
            "  case 2:\n"
            "    return 20;\n"
            "  }\n"
            "  return 0;\n"
            "}\n"
        )
        bad_findings = self._lint_snippet("switch_bad.cpp", bad_switch)
        self.assertIn("MISRA-9.4.2", [f.rule for f in bad_findings])

        good_switch = (
            "/** @file switch_good.cpp\n * @brief Switch with default.\n */\n"
            "int mapGear(int gearCode) {\n"
            "  switch (gearCode) {\n"
            "  case 1:\n"
            "    return 10;\n"
            "  default:\n"
            "    return 0;\n"
            "  }\n"
            "}\n"
        )
        good_findings = self._lint_snippet("switch_good.cpp", good_switch)
        self.assertEqual([], [f.rule for f in good_findings])

    def test_compound_braces_and_goto_positive_and_negative(self) -> None:
        """Verify MISRA-9.3.1 (braces) and MISRA-9.6.1 (goto) positive and negative cases."""
        bad_control = (
            "/** @file control_bad.cpp\n * @brief Unbraced if and goto.\n */\n"
            "int checkLimit(int speedReading) {\n"
            "  if (speedReading < 0)\n"
            "    goto errorLabel;\n"
            "  return speedReading;\n"
            "errorLabel:\n"
            "  return 0;\n"
            "}\n"
        )
        bad_findings = self._lint_snippet("control_bad.cpp", bad_control)
        rules = {f.rule for f in bad_findings}
        self.assertIn("MISRA-9.3.1", rules)
        self.assertIn("MISRA-9.6.1", rules)

        good_control = (
            "/** @file control_good.cpp\n * @brief Braced control flow without goto.\n */\n"
            "int checkLimit(int speedReading) {\n"
            "  if (speedReading < 0) {\n"
            "    return 0;\n"
            "  }\n"
            "  return speedReading;\n"
            "}\n"
        )
        good_findings = self._lint_snippet("control_good.cpp", good_control)
        self.assertEqual([], [f.rule for f in good_findings])

    def test_doxygen_presence_and_param_directions_positive_and_negative(self) -> None:
        """Verify DOXYGEN-FILE and DOXYGEN-PARAM-DIR positive and negative cases."""
        missing_doxygen_code = (
            "/// @param scaleFactor Missing direction.\n"
            "int scaleSample(int scaleFactor) {\n"
            "  return scaleFactor * 2;\n"
            "}\n"
        )
        bad_findings = self._lint_snippet("dox_bad.cpp", missing_doxygen_code)
        rules = {f.rule for f in bad_findings}
        self.assertIn("DOXYGEN-FILE", rules)
        self.assertIn("DOXYGEN-PARAM-DIR", rules)

        valid_doxygen_code = (
            "/**\n"
            " * @file dox_good.cpp\n"
            " * @brief Valid Doxygen file header and parameter directions.\n"
            " */\n"
            "/**\n"
            " * @brief Scale input and write result.\n"
            " * @param[in] scaleFactor Multiplier value.\n"
            " * @param[in,out] accumulator Running sum.\n"
            " * @param[out] statusOut Status output.\n"
            " */\n"
            "void scaleSample(int scaleFactor, int* accumulator, int* statusOut) {\n"
            "  *accumulator += scaleFactor;\n"
            "  *statusOut = 0;\n"
            "}\n"
        )
        good_findings = self._lint_snippet("dox_good.cpp", valid_doxygen_code)
        self.assertEqual([], [f.rule for f in good_findings])

    def test_naming_meaningful_and_configurable_regexes(self) -> None:
        """Verify NAMING-MEANINGFUL and configurable NAMING-CONVENTION regexes via config and CLI."""
        bad_naming = (
            "/** @file naming_bad.cpp\n * @brief Naming violations.\n */\n"
            "namespace BadNamespace {\n"
            "class bad_class_name {\n"
            "public:\n"
            "  int Bad_Method_Name(int tmp) {\n"
            "    int x = tmp + m_BadField;\n"
            "    return x;\n"
            "  }\n"
            "private:\n"
            "  int m_BadField = 0;\n"
            "};\n"
            "}\n"
        )
        bad_findings = self._lint_snippet("naming_bad.cpp", bad_naming)
        rules = [f.rule for f in bad_findings]
        self.assertIn("NAMING-MEANINGFUL", rules)
        self.assertIn("NAMING-CONVENTION", rules)

        # Custom snake_case function/variable naming configured via preflight.toml
        with tempfile.TemporaryDirectory(prefix="preflight-naming-toml-") as tmp_dir:
            root = pathlib.Path(tmp_dir)
            toml_path = _write_text(
                root / "preflight.toml",
                "[general]\n"
                "skip_clang_tidy = true\n"
                "[naming]\n"
                'class_regex = "^C_[A-Z][a-z0-9_]*$"\n'
                'function_regex = "^fn_[a-z][a-z0-9_]*$"\n'
                'parameter_regex = "^p_[a-z][a-z0-9_]*$"\n'
                'variable_regex = "^v_[a-z][a-z0-9_]*$"\n',
            )
            matching_cpp = _write_text(
                root / "custom_ok.cpp",
                "/** @file custom_ok.cpp\n * @brief Matches custom TOML naming regexes.\n */\n"
                "class C_Codec {\n"
                "public:\n"
                "  int fn_decode_frame(int p_input_frame) const {\n"
                "    int v_result_code = p_input_frame + 1;\n"
                "    return v_result_code;\n"
                "  }\n"
                "};\n",
            )
            non_matching_cpp = _write_text(
                root / "custom_bad.cpp",
                "/** @file custom_bad.cpp\n * @brief Violates custom TOML naming regexes.\n */\n"
                "class StandardCodec {\n"
                "public:\n"
                "  int decodeFrame(int inputFrame) const {\n"
                "    return inputFrame;\n"
                "  }\n"
                "};\n",
            )

            ok_res = _run_preflight_cli(["--config", str(toml_path), str(matching_cpp)], root)
            self.assertEqual(0, ok_res.returncode, ok_res.stdout + ok_res.stderr)

            bad_res = _run_preflight_cli(["--config", str(toml_path), str(non_matching_cpp)], root)
            self.assertEqual(1, bad_res.returncode, bad_res.stdout + bad_res.stderr)
            self.assertIn("NAMING-CONVENTION", bad_res.stdout)

    def test_parameter_count_limit_default_and_configurable(self) -> None:
        """Verify HIS-PAR enforces default limit 5 and respects custom max_params."""
        six_param_code = (
            "/** @file params.cpp\n * @brief Parameter limit check.\n */\n"
            "int combineSignals(\n"
            "    int firstSig,\n"
            "    int secondSig,\n"
            "    int thirdSig,\n"
            "    int fourthSig,\n"
            "    int fifthSig,\n"
            "    int sixthSig) {\n"
            "  return firstSig + secondSig + thirdSig + fourthSig + fifthSig + sixthSig;\n"
            "}\n"
        )
        default_findings = self._lint_snippet("params.cpp", six_param_code)
        self.assertIn("HIS-PAR", [f.rule for f in default_findings])

        relaxed_findings = self._lint_snippet("params.cpp", six_param_code, max_params=6)
        self.assertEqual([], [f.rule for f in relaxed_findings])

        four_param_code = (
            "/** @file params4.cpp\n * @brief Four parameter function.\n */\n"
            "int combineFour(int firstSig, int secondSig, int thirdSig, int fourthSig) {\n"
            "  return firstSig + secondSig + thirdSig + fourthSig;\n"
            "}\n"
        )
        self.assertEqual([], [f.rule for f in self._lint_snippet("params4.cpp", four_param_code, max_params=5)])
        self.assertIn("HIS-PAR", [f.rule for f in self._lint_snippet("params4.cpp", four_param_code, max_params=3)])

    def test_comment_hygiene_and_lexical_safety_rules_positive_and_negative(self) -> None:
        """Verify comment hygiene, abort/exit, setjmp, unbounded strings, keyword macros, and VLA rules."""
        bad_lexical = (
            "/** @file lexical_bad.cpp\n * @brief Lexical violations.\n */\n"
            "#include <csetjmp>\n"
            "#define true false\n"
            "//\n"
            "// return 42;\n"
            "void unsafeCalls(char* destBuffer, const char* srcBuffer) {\n"
            "  strcpy(destBuffer, srcBuffer);\n"
            "  abort();\n"
            "}\n"
        )
        bad_findings = self._lint_snippet("lexical_bad.cpp", bad_lexical)
        rules = {f.rule for f in bad_findings}
        self.assertIn("MISRA-21.10.2", rules)
        self.assertIn("MISRA-C-20.4", rules)
        self.assertIn("MISRA-COMMENT-REDUNDANT", rules)
        self.assertIn("MISRA-5.7.2", rules)
        self.assertIn("MISRA-STR-BOUND", rules)
        self.assertIn("MISRA-18.5.2", rules)

        bad_c_vla = (
            "/** @file vla_bad.c\n * @brief C VLA violation.\n */\n"
            "int sumDynamicArray(int countArg) {\n"
            "  int dynamicValues[countArg];\n"
            "  dynamicValues[0] = countArg;\n"
            "  return dynamicValues[0];\n"
            "}\n"
        )
        vla_findings = self._lint_snippet("vla_bad.c", bad_c_vla)
        self.assertIn("MISRA-C-18.8", [f.rule for f in vla_findings])

        good_lexical = (
            "/** @file lexical_good.cpp\n * @brief Clean lexical file.\n */\n"
            "// Explain domain invariant rather than commenting out dead code.\n"
            "int computeBounded(int inputSignal) {\n"
            "  return inputSignal + 1;\n"
            "}\n"
        )
        self.assertEqual([], [f.rule for f in self._lint_snippet("lexical_good.cpp", good_lexical)])

    def test_no_exceptions_no_rtti_and_no_impl_in_header_positive_and_negative(self) -> None:
        """Verify NO-EXCEPTIONS, NO-RTTI, and NO-IMPL-IN-HEADER rules."""
        bad_header = (
            "/** @file widget.hpp\n * @brief Header with non-constexpr implementation.\n */\n"
            "#pragma once\n"
            "int computeInHeader(int inputVal) {\n"
            "  return inputVal + 1;\n"
            "}\n"
        )
        hdr_findings = self._lint_snippet("widget.hpp", bad_header)
        self.assertIn("NO-IMPL-IN-HEADER", [f.rule for f in hdr_findings])

        good_header = (
            "/** @file widget_good.hpp\n * @brief Header with declaration and constexpr/template only.\n */\n"
            "#pragma once\n"
            "int computeInSource(int inputVal);\n"
            "constexpr int computeConstexpr(int inputVal) {\n"
            "  return inputVal + 1;\n"
            "}\n"
        )
        self.assertEqual([], [f.rule for f in self._lint_snippet("widget_good.hpp", good_header)])

        bad_exc_rtti = (
            "/** @file exc_rtti.cpp\n * @brief Exceptions and RTTI constructs.\n */\n"
            "class PolyBase {\n"
            "public:\n"
            "  virtual ~PolyBase() = default;\n"
            "};\n"
            "class PolyDerived : public PolyBase {};\n"
            "int testConstructs(PolyBase* basePtr) {\n"
            "  try {\n"
            "    if (dynamic_cast<PolyDerived*>(basePtr) != nullptr) {\n"
            "      throw 1;\n"
            "    }\n"
            "  } catch (...) {\n"
            "    return -1;\n"
            "  }\n"
            "  return 0;\n"
            "}\n"
        )
        no_exc_cfg = preflight_lint.LintConfig(no_exceptions=False, no_rtti=False, skip_clang_tidy=True)
        # First parse AST with compiler flags allowing exceptions/rtti, then run walk_ast_for_misra with default config
        with tempfile.TemporaryDirectory(prefix="preflight-exc-rtti-") as tmp_dir:
            src = _write_text(pathlib.Path(tmp_dir) / "exc_rtti.cpp", bad_exc_rtti)
            # Test via lexical fallback when AST is bypassed or via AST walk
            no_tool_env = preflight_lint.RuntimeToolEnv(
                clang_bin="", clang_version="", clangxx_bin="", clangxx_version="",
                clang_tidy_bin="", clang_tidy_version="", gcc_dir="", linter_sha256="",
                env_vars=(), compile_commands_path="", compile_commands_sha256="",
                compile_commands_entries=(),
            )
            rep = preflight_lint.check_c_or_cpp_file(
                src, None, None, "c++17", False, skip_clang_tidy=True, tool_env=no_tool_env,
                config=preflight_lint.LintConfig(allow_missing_tools=True, skip_clang_tidy=True),
            )
            found_rules = {f.rule for f in rep.findings}
            self.assertIn("NO-EXCEPTIONS", found_rules)
            self.assertIn("NO-RTTI", found_rules)
            self.assertEqual([], [
                f.rule for f in self._lint_snippet("exc_rtti.cpp", bad_exc_rtti, config=no_exc_cfg)
            ])


class MissingToolsAndClangTidyTests(unittest.TestCase):
    """Tests for fail-closed TOOL-MISSING behavior, --allow-missing-tools, and clang-tidy notice."""

    def test_missing_compiler_fails_closed_by_default_and_passes_with_allow_missing_tools(self) -> None:
        """Missing clang++ reports TOOL-MISSING and exits 1 unless --allow-missing-tools is passed."""
        with tempfile.TemporaryDirectory(prefix="preflight-missing-tool-") as tmp_dir:
            root = pathlib.Path(tmp_dir)
            clean_cpp = _write_text(
                root / "clean.cpp",
                "/** @file clean.cpp\n * @brief Clean file.\n */\n"
                "int addValues(int firstValue, int secondValue) {\n"
                "  return firstValue + secondValue;\n"
                "}\n",
            )
            bad_cpp = _write_text(
                root / "bad.cpp",
                "/** @file bad.cpp\n * @brief Lexical cast violation.\n */\n"
                "int castValue(double rawInput) {\n"
                "  return (int)rawInput;\n"
                "}\n",
            )

            no_compiler_env = preflight_lint.RuntimeToolEnv(
                clang_bin="",
                clang_version="",
                clangxx_bin="",
                clangxx_version="",
                clang_tidy_bin="",
                clang_tidy_version="",
                gcc_dir="",
                linter_sha256="0" * 64,
                env_vars=(),
                compile_commands_path="",
                compile_commands_sha256="",
                compile_commands_entries=(),
            )

            with mock.patch("preflight_lint.build_runtime_tool_env", return_value=no_compiler_env):
                # 1. Default fail-closed: TOOL-MISSING causes exit code 1 even on a clean file
                out_buf = io.StringIO()
                err_buf = io.StringIO()
                with mock.patch("sys.stdout", out_buf), mock.patch("sys.stderr", err_buf):
                    rc_default = preflight_lint.main(["--json", str(clean_cpp)])
                self.assertEqual(1, rc_default)
                payload_default = json.loads(out_buf.getvalue())
                rules_default = [f["rule"] for f in payload_default["reports"][0]["findings"]]
                self.assertIn("TOOL-MISSING", rules_default)
                # Also verify notice for absent clang-tidy was printed to stderr
                self.assertIn("clang-tidy not found", err_buf.getvalue())

                # 2. With --allow-missing-tools: TOOL-MISSING is still reported, but clean file exits 0
                out_allow = io.StringIO()
                err_allow = io.StringIO()
                with mock.patch("sys.stdout", out_allow), mock.patch("sys.stderr", err_allow):
                    rc_allow = preflight_lint.main(["--allow-missing-tools", "--json", str(clean_cpp)])
                self.assertEqual(0, rc_allow)
                payload_allow = json.loads(out_allow.getvalue())
                rules_allow = [f["rule"] for f in payload_allow["reports"][0]["findings"]]
                self.assertIn("TOOL-MISSING", rules_allow)

                # 3. With --allow-missing-tools on a file with a lexical violation: still exits 1
                out_bad = io.StringIO()
                err_bad = io.StringIO()
                with mock.patch("sys.stdout", out_bad), mock.patch("sys.stderr", err_bad):
                    rc_bad = preflight_lint.main(["--allow-missing-tools", "--json", str(bad_cpp)])
                self.assertEqual(1, rc_bad)
                payload_bad = json.loads(out_bad.getvalue())
                rules_bad = [f["rule"] for f in payload_bad["reports"][0]["findings"]]
                self.assertIn("TOOL-MISSING", rules_bad)
                self.assertIn("MISRA-8.2.2", rules_bad)

    def test_missing_compile_commands_info_reports_tool_missing(self) -> None:
        """Missing compile_commands.json or missing file entry reports TOOL-MISSING and fails closed."""
        with tempfile.TemporaryDirectory(prefix="preflight-missing-db-") as tmp_dir:
            root = pathlib.Path(tmp_dir)
            target_cpp = _write_text(
                root / "src" / "target.cpp",
                "/** @file target.cpp\n * @brief Target file.\n */\n"
                "int computeTarget(int inputVal) {\n"
                "  return inputVal;\n"
                "}\n",
            )
            other_cpp = _write_text(
                root / "src" / "other.cpp",
                "/** @file other.cpp\n * @brief Other file.\n */\n"
                "int computeOther(int inputVal) {\n"
                "  return inputVal;\n"
                "}\n",
            )
            incomplete_db = _write_text(
                root / "compile_commands.json",
                json.dumps(
                    [
                        {
                            "directory": str(root),
                            "file": str(other_cpp),
                            "arguments": ["clang++", "-c", str(other_cpp)],
                        }
                    ]
                ),
            )

            # Fail-closed when target.cpp has no entry in compile_commands.json
            res_fail = _run_preflight_cli(
                ["--compile-commands", str(incomplete_db), "--skip-clang-tidy", "--json", str(target_cpp)],
                root,
            )
            self.assertEqual(1, res_fail.returncode, res_fail.stdout + res_fail.stderr)
            payload_fail = json.loads(res_fail.stdout)
            self.assertIn(
                "TOOL-MISSING",
                [f["rule"] for f in payload_fail["reports"][0]["findings"]],
            )

            # Passes when --allow-missing-tools is provided
            res_allow = _run_preflight_cli(
                [
                    "--compile-commands",
                    str(incomplete_db),
                    "--allow-missing-tools",
                    "--skip-clang-tidy",
                    "--json",
                    str(target_cpp),
                ],
                root,
            )
            self.assertEqual(0, res_allow.returncode, res_allow.stdout + res_allow.stderr)
            payload_allow = json.loads(res_allow.stdout)
            self.assertIn(
                "TOOL-MISSING",
                [f["rule"] for f in payload_allow["reports"][0]["findings"]],
            )

    def test_clang_tidy_absent_prints_notice_never_silently(self) -> None:
        """When clang-tidy is not installed, preflight_lint prints an explicit notice to stderr."""
        with tempfile.TemporaryDirectory(prefix="preflight-no-tidy-") as tmp_dir:
            root = pathlib.Path(tmp_dir)
            sample_cpp = _write_text(
                root / "sample.cpp",
                "/** @file sample.cpp\n * @brief Clean sample.\n */\n"
                "int readSensor(int channelIndex) {\n"
                "  return channelIndex;\n"
                "}\n",
            )
            real_env = preflight_lint.build_runtime_tool_env(None)
            no_tidy_env = preflight_lint.RuntimeToolEnv(
                clang_bin=real_env.clang_bin,
                clang_version=real_env.clang_version,
                clangxx_bin=real_env.clangxx_bin,
                clangxx_version=real_env.clangxx_version,
                clang_tidy_bin="",
                clang_tidy_version="",
                gcc_dir=real_env.gcc_dir,
                linter_sha256=real_env.linter_sha256,
                env_vars=real_env.env_vars,
                compile_commands_path="",
                compile_commands_sha256="",
                compile_commands_entries=(),
            )
            out_buf = io.StringIO()
            err_buf = io.StringIO()
            with mock.patch("preflight_lint.build_runtime_tool_env", return_value=no_tidy_env), \
                    mock.patch("sys.stdout", out_buf), \
                    mock.patch("sys.stderr", err_buf):
                exit_code = preflight_lint.main([str(sample_cpp)])
            self.assertEqual(0, exit_code)
            self.assertIn("notice: clang-tidy not found", err_buf.getvalue())


class BaselineAndPortabilityTests(unittest.TestCase):
    """Tests for baseline add/suppress, path-relative portable fingerprints, and policy version mismatch."""

    def test_baseline_add_suppress_new_excess_and_fixed(self) -> None:
        """Verify baseline creation, suppression, excess detection, replacement failure, and fixed notice."""
        with tempfile.TemporaryDirectory(prefix="preflight-baseline-") as tmp_dir:
            repo_dir = pathlib.Path(tmp_dir)
            _write_text(repo_dir / "CMakeLists.txt", "project(sample_lib LANGUAGES CXX)\n")
            target_cpp = _write_text(
                repo_dir / "src" / "codec.cpp",
                "/** @file codec.cpp\n * @brief Codec with one legacy finding.\n */\n"
                "int evaluateState(int stateCode) {\n"
                "  if (stateCode > 0) return 1;\n"
                "  return 0;\n"
                "}\n",
            )
            baseline_path = repo_dir / "baseline.json"

            # Without baseline, linting fails with exit code 1
            initial_res = _run_preflight_cli(["--skip-clang-tidy", str(target_cpp)], repo_dir)
            self.assertEqual(1, initial_res.returncode)

            # Record baseline (--baseline-update)
            update_res = _run_preflight_cli(
                ["--skip-clang-tidy", "--baseline", str(baseline_path), "--baseline-update", str(target_cpp)],
                repo_dir,
            )
            self.assertEqual(0, update_res.returncode, update_res.stderr + update_res.stdout)
            self.assertIn("BASELINE_UPDATED", update_res.stderr)
            self.assertTrue(baseline_path.is_file())

            # Compare against baseline: suppressed -> exits 0
            suppress_res = _run_preflight_cli(
                ["--skip-clang-tidy", "--baseline", str(baseline_path), str(target_cpp)],
                repo_dir,
            )
            self.assertEqual(0, suppress_res.returncode, suppress_res.stderr + suppress_res.stdout)
            self.assertNotIn("BASELINE_NEW", suppress_res.stderr)
            self.assertNotIn("BASELINE_EXCESS", suppress_res.stderr)

            # Duplicate the identical violation -> fails with BASELINE_EXCESS
            _write_text(
                target_cpp,
                "/** @file codec.cpp\n * @brief Codec with duplicate finding.\n */\n"
                "int evaluateState(int stateCode) {\n"
                "  if (stateCode > 0) return 1;\n"
                "  if (stateCode > 0) return 1;\n"
                "  return 0;\n"
                "}\n",
            )
            excess_res = _run_preflight_cli(
                ["--skip-clang-tidy", "--baseline", str(baseline_path), str(target_cpp)],
                repo_dir,
            )
            self.assertEqual(1, excess_res.returncode)
            self.assertIn("BASELINE_EXCESS", excess_res.stderr)

            # Replace with a different violation at same total count -> fails with BASELINE_NEW
            _write_text(
                target_cpp,
                "/** @file codec.cpp\n * @brief Codec with different finding.\n */\n"
                "int evaluateState(double stateCode) {\n"
                "  if (stateCode > 0.0) { return (int)stateCode; }\n"
                "  return 0;\n"
                "}\n",
            )
            replace_res = _run_preflight_cli(
                ["--skip-clang-tidy", "--baseline", str(baseline_path), str(target_cpp)],
                repo_dir,
            )
            self.assertEqual(1, replace_res.returncode)
            self.assertIn("BASELINE_NEW", replace_res.stderr)
            self.assertIn("BASELINE_FIXED", replace_res.stderr)

            # Fix the original violation -> exits 0 and logs BASELINE_FIXED
            _write_text(
                target_cpp,
                "/** @file codec.cpp\n * @brief Clean codec.\n */\n"
                "int evaluateState(int stateCode) {\n"
                "  if (stateCode > 0) { return 1; }\n"
                "  return 0;\n"
                "}\n",
            )
            fixed_res = _run_preflight_cli(
                ["--skip-clang-tidy", "--baseline", str(baseline_path), str(target_cpp)],
                repo_dir,
            )
            self.assertEqual(0, fixed_res.returncode)
            self.assertIn("BASELINE_FIXED", fixed_res.stderr)

    def test_baseline_fingerprints_are_path_relative_and_portable_across_machines(self) -> None:
        """Verify baseline manifests contain no absolute paths and transfer cleanly between directory trees."""
        with tempfile.TemporaryDirectory(prefix="preflight-portability-") as tmp_dir:
            base_dir = pathlib.Path(tmp_dir)
            machine_a_repo = base_dir / "machine_a" / "workspace" / "project_root"
            machine_b_repo = base_dir / "machine_b" / "different_mount" / "checkout"
            shared_baseline = base_dir / "portable_baseline.json"

            snippet = (
                "/** @file frame_parser.cpp\n * @brief Parser with baselined cast.\n */\n"
                "int parseHeaderByte(double rawByte) {\n"
                "  return (int)rawByte;\n"
                "}\n"
            )
            _write_text(machine_a_repo / "CMakeLists.txt", "project(portable LANGUAGES CXX)\n")
            file_a = _write_text(machine_a_repo / "src" / "parser" / "frame_parser.cpp", snippet)

            # Generate baseline on Machine A
            gen_res = _run_preflight_cli(
                [
                    "--repo-root",
                    str(machine_a_repo),
                    "--skip-clang-tidy",
                    "--baseline",
                    str(shared_baseline),
                    "--baseline-update",
                    str(file_a),
                ],
                machine_a_repo,
            )
            self.assertEqual(0, gen_res.returncode, gen_res.stderr + gen_res.stdout)

            raw_manifest_text = shared_baseline.read_text(encoding="utf-8")
            self.assertNotIn(str(machine_a_repo), raw_manifest_text)
            self.assertNotIn(str(base_dir), raw_manifest_text)

            manifest_data = json.loads(raw_manifest_text)
            self.assertEqual(
                preflight_lint.FINGERPRINT_POLICY_VERSION,
                manifest_data["fingerprint_policy_version"],
            )
            entries = manifest_data["modules"]["default"]["release"]["entries"]
            self.assertEqual(1, len(entries))
            self.assertEqual("src/parser/frame_parser.cpp", entries[0]["path"])
            self.assertFalse(pathlib.PurePosixPath(entries[0]["path"]).is_absolute())

            # Remove Machine A checkout completely, create Machine B checkout with formatting differences
            shutil.rmtree(machine_a_repo)
            _write_text(machine_b_repo / "CMakeLists.txt", "project(portable LANGUAGES CXX)\n")
            reformatted_snippet = (
                "/** @file frame_parser.cpp\n * @brief Parser with baselined cast.\n */\n"
                "\n\n// Additional whitespace and line shifts on Machine B\n"
                "int parseHeaderByte(\n"
                "    double rawByte) {\n"
                "  return (int)rawByte;\n"
                "}\n"
            )
            file_b = _write_text(
                machine_b_repo / "src" / "parser" / "frame_parser.cpp",
                reformatted_snippet,
            )

            verify_res = _run_preflight_cli(
                [
                    "--repo-root",
                    str(machine_b_repo),
                    "--skip-clang-tidy",
                    "--baseline",
                    str(shared_baseline),
                    str(file_b),
                ],
                machine_b_repo,
            )
            self.assertEqual(0, verify_res.returncode, verify_res.stderr + verify_res.stdout)

    def test_fingerprint_policy_version_mismatch_reports_clear_error_and_migration_hint(self) -> None:
        """Verify mismatched fingerprint_policy_version fails with exit code 2 and migration hint."""
        with tempfile.TemporaryDirectory(prefix="preflight-fp-version-") as tmp_dir:
            repo_dir = pathlib.Path(tmp_dir)
            sample_cpp = _write_text(
                repo_dir / "src" / "sample.cpp",
                "/** @file sample.cpp\n * @brief Clean sample.\n */\n"
                "int computeOffset(int baseOffset) {\n"
                "  return baseOffset;\n"
                "}\n",
            )
            manifest_path = _write_text(
                repo_dir / "baseline.json",
                json.dumps(
                    {
                        "schema_version": preflight_lint.BASELINE_SCHEMA_VERSION,
                        "fingerprint_policy_version": 1,
                        "modules": {
                            "default": {
                                "release": {
                                    "entries": [],
                                }
                            }
                        },
                    }
                ),
            )
            res = _run_preflight_cli(
                ["--skip-clang-tidy", "--baseline", str(manifest_path), str(sample_cpp)],
                repo_dir,
            )
            self.assertEqual(2, res.returncode)
            self.assertIn("fingerprint_policy_version mismatch", res.stderr)
            self.assertIn(f"expected {preflight_lint.FINGERPRINT_POLICY_VERSION}, got 1", res.stderr)
            self.assertIn("Migration hint:", res.stderr)
            self.assertIn("--baseline-update", res.stderr)

    def test_baseline_rejects_manifest_with_absolute_entry_path(self) -> None:
        """Verify load_and_validate_manifest rejects baseline entries containing absolute paths."""
        with tempfile.TemporaryDirectory(prefix="preflight-abs-path-") as tmp_dir:
            manifest_path = _write_text(
                pathlib.Path(tmp_dir) / "bad_baseline.json",
                json.dumps(
                    {
                        "schema_version": preflight_lint.BASELINE_SCHEMA_VERSION,
                        "fingerprint_policy_version": preflight_lint.FINGERPRINT_POLICY_VERSION,
                        "modules": {
                            "default": {
                                "release": {
                                    "entries": [
                                        {
                                            "fingerprint": "a" * 64,
                                            "path": "/abs/path/to/file.cpp",
                                            "rule": "MISRA-8.2.2",
                                            "severity": "error",
                                            "allowance": 1,
                                        }
                                    ]
                                }
                            }
                        },
                    }
                ),
            )
            with self.assertRaises(ValueError) as ctx:
                preflight_lint.load_and_validate_manifest(manifest_path)
            self.assertIn("repository-relative", str(ctx.exception))


class PluginAndAstScopeTests(unittest.TestCase):
    """Tests for the plugin hook interface and Clang AST location cursor ownership."""

    def test_plugin_hook_interface_custom_extension_and_rule(self) -> None:
        """Verify custom plugins can register extensions and report custom findings."""
        with tempfile.TemporaryDirectory(prefix="preflight-plugin-") as tmp_dir:
            root = pathlib.Path(tmp_dir)
            plugin_file = _write_text(
                root / "custom_plugin.py",
                '"""Custom test plugin for preflight_lint."""\n'
                "import preflight_lint\n"
                'SUPPORTED_EXTENSIONS = {".custom"}\n'
                "def check_file(path, raw_text, stripped_text, config):\n"
                "    findings = []\n"
                "    for idx, line in enumerate(stripped_text.splitlines(), start=1):\n"
                '        if "FORBIDDEN_CALL" in line:\n'
                "            findings.append(preflight_lint.Finding(\n"
                '                str(path), idx, "PLUGIN-FORBIDDEN-CALL", "error",\n'
                '                "FORBIDDEN_CALL is prohibited by plugin"\n'
                "            ))\n"
                "    return findings\n",
            )
            custom_target = _write_text(root / "service.custom", "FORBIDDEN_CALL();\n")

            res = _run_preflight_cli(
                ["--plugin", str(plugin_file), "--json", str(root)],
                root,
            )
            self.assertEqual(1, res.returncode, res.stderr + res.stdout)
            payload = json.loads(res.stdout)
            self.assertEqual(1, payload["total_findings"])
            self.assertEqual(
                "PLUGIN-FORBIDDEN-CALL",
                payload["reports"][0]["findings"][0]["rule"],
            )
            self.assertEqual(str(custom_target), payload["reports"][0]["path"])

    def test_included_header_findings_do_not_leak_into_target_source(self) -> None:
        """Verify AST traversal only reports findings in the target file, not included headers."""
        with tempfile.TemporaryDirectory(prefix="preflight-ast-scope-") as tmp_dir:
            root = pathlib.Path(tmp_dir)
            _write_text(
                root / "bad_header.hpp",
                "inline int headerViolation(int x) { if (x > 0) return (int)x; return 0; }\n",
            )
            clean_main = _write_text(
                root / "clean_main.cpp",
                "/** @file clean_main.cpp\n * @brief Includes a header with violations.\n */\n"
                '#include "bad_header.hpp"\n'
                "int runClean(int validInput) {\n"
                "  if (validInput > 0) {\n"
                "    return validInput;\n"
                "  }\n"
                "  return 0;\n"
                "}\n",
            )
            res = _run_preflight_cli(["--skip-clang-tidy", str(clean_main)], root)
            self.assertEqual(0, res.returncode, res.stderr + res.stdout)

    def test_content_hash_cache_hits_and_invalidates_on_change(self) -> None:
        """Verify --cache-dir caches clean/failing reports and invalidates when source or config changes."""
        with tempfile.TemporaryDirectory(prefix="preflight-cache-") as tmp_dir:
            root = pathlib.Path(tmp_dir)
            cache_dir = root / "cache"
            sample_cpp = _write_text(
                root / "cached.cpp",
                "/** @file cached.cpp\n * @brief Cache test.\n */\n"
                "int computeCached(int firstArg, int secondArg) {\n"
                "  return firstArg + secondArg;\n"
                "}\n",
            )
            argv = ["--cache-dir", str(cache_dir), "--skip-clang-tidy", "--json", str(sample_cpp)]
            with mock.patch(
                "preflight_lint.check_c_or_cpp_file",
                wraps=preflight_lint.check_c_or_cpp_file,
            ) as spy_check, mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(0, preflight_lint.main(argv))
                self.assertEqual(1, spy_check.call_count)

                # Second run hits cache without calling check_c_or_cpp_file
                self.assertEqual(0, preflight_lint.main(argv))
                self.assertEqual(1, spy_check.call_count)

                # Changing --max-params invalidates cache
                self.assertEqual(0, preflight_lint.main([*argv, "--max-params", "3"]))
                self.assertEqual(2, spy_check.call_count)

    def test_config_rejects_plugins_paths_and_invalid_std_without_executing(self) -> None:
        """preflight_lint exits 2 without executing config-defined plugins unless --allow-config-plugins is passed."""
        with tempfile.TemporaryDirectory(prefix="preflight-hardening-") as tmp_dir:
            root = pathlib.Path(tmp_dir)
            marker = root / "pwned.txt"
            evil_plugin = _write_text(
                root / "evil_plugin.py",
                f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
            )
            sample_cpp = _write_text(
                root / "clean.cpp",
                "/** @file clean.cpp\n * @brief Clean.\n */\nint okFunc() { return 0; }\n",
            )
            cfg_plugins = _write_text(
                root / "preflight.toml",
                f'[plugins]\npaths = [{json.dumps(str(evil_plugin))}]\n',
            )
            res = _run_preflight_cli(
                ["--config", str(cfg_plugins), "--skip-clang-tidy", str(sample_cpp)],
                root,
            )
            self.assertEqual(2, res.returncode, res.stderr + res.stdout)
            self.assertFalse(marker.exists())
            self.assertIn("disallowed", res.stderr)

            cfg_bad_std = _write_text(
                root / "bad_std.toml",
                '[general]\nstd = "c++17 -fsyntax-only"\n',
            )
            res_std = _run_preflight_cli(
                ["--config", str(cfg_bad_std), "--skip-clang-tidy", str(sample_cpp)],
                root,
            )
            self.assertEqual(2, res_std.returncode)


if __name__ == "__main__":
    unittest.main()
