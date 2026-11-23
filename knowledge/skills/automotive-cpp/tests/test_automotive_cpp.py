"""Offline unit and mutant regression tests for the automotive-cpp skill."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

TESTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = TESTS_DIR.parent
REPO_ROOT = SKILL_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skilltest import skilltest  # noqa: E402


class AutomotiveCppSkillTests(unittest.TestCase):
    """Verify SKILL.md rules, fixture/expected behaviour, and grader mutant rejection."""

    def test_skilltest_lint_and_modernize_use_auto_guidance(self) -> None:
        """SKILL.md passes lint, stays <= 200 lines, and documents clang-tidy auto cast init."""
        self.assertEqual(skilltest.lint_skill(SKILL_DIR), [])
        skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        _, body, _ = skilltest.parse_frontmatter(skill_text)
        self.assertLessEqual(len(body), 200)
        self.assertIn("clang-tidy", skill_text)
        self.assertIn("modernize-use-auto", skill_text)
        self.assertIn("const auto", skill_text)

    def test_fixtures_fail_and_expected_pass(self) -> None:
        """Every input fixture fails its grader and every expected output passes."""
        config = skilltest.load_config(SKILL_DIR)
        env = {**os.environ, "DEVPACK_ROOT": str(REPO_ROOT)}
        for case in config["behaviour"]:
            name = case["name"]
            grader = TESTS_DIR / case["grader"]
            input_dir = TESTS_DIR / case["fixture"]
            expected_dir = TESTS_DIR / "expected" / name
            fail_res = subprocess.run(
                ["bash", "check.sh", str(input_dir)],
                cwd=grader,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(fail_res.returncode, 0, f"{name} input unexpectedly passed")
            pass_res = subprocess.run(
                ["bash", "check.sh", str(expected_dir)],
                cwd=grader,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                pass_res.returncode,
                0,
                f"{name} expected failed:\n{pass_res.stdout}\n{pass_res.stderr}",
            )

    def test_mutants_ndebug_pragma_undef_and_zero_crc_rejected(self) -> None:
        """Graders reject NDEBUG, diagnostic pragmas, grader macro undef, and zero-CRC mutants."""
        env = {**os.environ, "DEVPACK_ROOT": str(REPO_ROOT)}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            # 1. Zero-CRC without NDEBUG must fail GRADER_CHECK.
            zero_crc = root / "zero_crc"
            shutil.copytree(TESTS_DIR / "expected" / "doxygen-directions", zero_crc)
            crc_cpp = zero_crc / "frame_crc.cpp"
            crc_cpp.write_text(
                crc_cpp.read_text(encoding="utf-8").replace(
                    "crcAccumulator = workingCrc;",
                    "crcAccumulator = 0U;",
                ),
                encoding="utf-8",
            )
            res_zero = subprocess.run(
                ["bash", "check.sh", str(zero_crc)],
                cwd=TESTS_DIR / "graders" / "doxygen-directions",
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(res_zero.returncode, 0)
            self.assertIn("GRADER_CHECK", res_zero.stderr)

            # 2. Reviewer mutant: NDEBUG + #pragma GCC diagnostic + zero-CRC must fail.
            ndebug_crc = root / "ndebug_crc"
            shutil.copytree(zero_crc, ndebug_crc)
            crc_hpp = ndebug_crc / "frame_crc.hpp"
            crc_hpp.write_text(
                crc_hpp.read_text(encoding="utf-8").replace(
                    "#include <cstddef>",
                    "#define NDEBUG 1\n#pragma GCC diagnostic ignored \"-Wunused-variable\"\n#include <cstddef>",
                ),
                encoding="utf-8",
            )
            res_ndebug = subprocess.run(
                ["bash", "check.sh", str(ndebug_crc)],
                cwd=TESTS_DIR / "graders" / "doxygen-directions",
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(res_ndebug.returncode, 0)

            # 3. All three graders reject NDEBUG, clang diagnostic pragma, and #undef GRADER_CHECK.
            cases = (
                ("error-status-optional", "voltage_decoder.hpp"),
                ("heap-exceptions-casts", "sample_filter.hpp"),
                ("doxygen-directions", "frame_crc.hpp"),
            )
            injections = (
                "#ifndef NDEBUG\n#define NDEBUG 1\n#endif\n",
                "#pragma clang diagnostic ignored \"-Wunused-variable\"\n",
                "#undef GRADER_CHECK\n#define GRADER_CHECK(x) ((void)0)\n",
            )
            for idx, (case_name, hdr_name) in enumerate(cases):
                for inj_idx, inj in enumerate(injections):
                    cand = root / f"{case_name}_{idx}_{inj_idx}"
                    shutil.copytree(TESTS_DIR / "expected" / case_name, cand)
                    hdr = cand / hdr_name
                    hdr.write_text(
                        hdr.read_text(encoding="utf-8").replace(
                            "#include <cstdint>",
                            inj + "#include <cstdint>",
                        ),
                        encoding="utf-8",
                    )
                    res = subprocess.run(
                        ["bash", "check.sh", str(cand)],
                        cwd=TESTS_DIR / "graders" / case_name,
                        env=env,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(res.returncode, 0, f"{case_name} accepted injection {inj_idx}")


if __name__ == "__main__":
    unittest.main()
