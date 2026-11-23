"""Unit tests for enforcement.checks.test_lock."""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

_REPO_ROOT = str(pathlib.Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from enforcement.checks import test_lock  # noqa: E402


def _init_repo_with_tests(repo_dir: pathlib.Path) -> None:
    """Initialize a temporary git repo with a protected test file."""
    subprocess.run(["git", "init", str(repo_dir)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.name", "test-user"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "config", "user.email", "test@local.invalid"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    tests_dir = repo_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_codec.cpp").write_text(
        "void test_decode() {\n"
        "    ASSERT_EQ(1, 1);\n"
        "    EXPECT_TRUE(true);\n"
        "    REQUIRE(2 == 2);\n"
        "}\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "add", "."],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", "initial tests"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class TestTestLock(unittest.TestCase):
    """Verify test_lock behavior on assertion decrease, file deletion/rename, and deviation override."""

    def setUp(self) -> None:
        """Create an isolated temporary git repository."""
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_dir = pathlib.Path(self._tmp.name) / "repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        _init_repo_with_tests(self.repo_dir)

    def tearDown(self) -> None:
        """Clean up temporary directory."""
        self._tmp.cleanup()

    def _run_lock(self, args: list[str]) -> tuple[int, str, str]:
        """Run test_lock.main and capture (exit_code, stdout, stderr)."""
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            code = test_lock.main(args)
        return code, out_buf.getvalue(), err_buf.getvalue()

    def test_happy_path_unchanged_or_increased_assertions_passes(self) -> None:
        """Adding an assertion to a protected test file passes with exit code 0."""
        (self.repo_dir / "tests" / "test_codec.cpp").write_text(
            "void test_decode() {\n"
            "    ASSERT_EQ(1, 1);\n"
            "    EXPECT_TRUE(true);\n"
            "    REQUIRE(2 == 2);\n"
            "    CHECK_EQ(3, 3);\n"
            "}\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "commit", "-am", "add assertion"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        code, out, _ = self._run_lock(
            [
                "--repo",
                str(self.repo_dir),
                "--base",
                "HEAD~1",
                "--protected",
                "tests/**",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("TEST_LOCK_OK", out)

    def test_comment_aware_line_preservation(self) -> None:
        """Protect code while allowing comments and inserted tests anywhere."""
        path = self.repo_dir / "tests/test_codec.cpp"
        original = path.read_text()
        baseline = '/**\n * old documentation ASSERT_FAKE(x)\n */\n' + original
        path.write_text(baseline)
        subprocess.run(["git", "-C", str(self.repo_dir), "commit", "-am", "document tests"],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        cases = [
            (baseline.replace("old documentation ASSERT_FAKE(x)", "new documentation"), 0),
            (baseline.replace("ASSERT_EQ(1, 1)", "ASSERT_EQ(1, 2)"), 1),
            (baseline.replace("void test_decode() {", "void changed_name() {"), 1),
            (baseline.replace("    EXPECT_TRUE(true);\n", ""), 1),
            (baseline.replace(original, "void added() { CHECK(true); }\n" + original), 0),
            (baseline.replace("ASSERT_EQ(1, 1);", "ASSERT_EQ(1, 2); // explanation"), 1),
            (baseline.replace("ASSERT_EQ(1, 1);", "ASSERT_EQ(1, 1); // explanation"), 0),
            (baseline.replace("    ASSERT_EQ(1, 1);", "/*    ASSERT_EQ(1, 1); */"), 1),
        ]
        for content, expected in cases:
            with self.subTest(content=content):
                path.write_text(content)
                code, output, _ = self._run_lock([
                    "--repo", str(self.repo_dir), "--base", "HEAD", "--protected", "tests/**"])
                self.assertEqual(code, expected, output)

    def test_comment_markers_in_literals_are_code(self) -> None:
        """Quoted strings, escaped quotes, and raw strings retain comment markers."""
        samples = [
            'CHECK_EQ("https://example.invalid", "/*literal*/");',
            "CHECK_EQ('/', '\\'');",
            'CHECK_EQ(R"tag(first /*literal*/\n//literal)tag", "value");',
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertEqual(test_lock.strip_comments(text), text)
                self.assertFalse(test_lock.preserves_lines(text, text.replace("CHECK_EQ", "CHECK_NE")))
        self.assertEqual(test_lock.strip_comments("a; /* one\ntwo */ b; // end\n"),
                         "a;  \n b;  \n")

    def test_decreased_assertion_count_fails(self) -> None:
        """Removing an assertion from a protected test file without deviation fails with exit 1."""
        (self.repo_dir / "tests" / "test_codec.cpp").write_text(
            "void test_decode() {\n"
            "    ASSERT_EQ(1, 1);\n"
            "}\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "commit", "-am", "weaken test"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        code, out, _ = self._run_lock(
            [
                "--repo",
                str(self.repo_dir),
                "--base",
                "HEAD~1",
                "--protected",
                "tests/**",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("assertion count decreased", out)

    def test_deleted_protected_file_fails(self) -> None:
        """Deleting a protected test file without deviation fails with exit 1."""
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "rm", "tests/test_codec.cpp"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "commit", "-m", "delete test"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        code, out, _ = self._run_lock(
            [
                "--repo",
                str(self.repo_dir),
                "--base",
                "HEAD~1",
                "--protected",
                "tests/**",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("protected test file deleted", out)

    def test_renamed_protected_file_fails(self) -> None:
        """Renaming a protected test file without deviation fails with exit 1."""
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "mv", "tests/test_codec.cpp", "tests/renamed_codec.cpp"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "commit", "-m", "rename test"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        code, out, _ = self._run_lock(
            [
                "--repo",
                str(self.repo_dir),
                "--base",
                "HEAD~1",
                "--protected",
                "tests/**",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("protected test file renamed", out)

    def test_deviation_in_head_commit_message_allows_change(self) -> None:
        """A decreased assertion count is allowed only when matching an external human registry."""
        (self.repo_dir / "tests" / "test_codec.cpp").write_text(
            "void test_decode() {\n"
            "    ASSERT_EQ(1, 1);\n"
            "}\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo_dir),
                "commit",
                "-am",
                "refactor obsolete check\n\nDeviation: DEV-404",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Without external DEVPACK_DEVIATIONS registry, commit-message Deviation is rejected.
        code_no_reg, _, err_no_reg = self._run_lock(
            ["--repo", str(self.repo_dir), "--base", "HEAD~1", "--protected", "tests/**"]
        )
        self.assertEqual(code_no_reg, 1)
        self.assertIn("TEST_LOCK_DEVIATION_REJECTED", err_no_reg)

        with tempfile.TemporaryDirectory() as reg_dir:
            reg = pathlib.Path(reg_dir) / "deviations.json"
            reg.write_text(
                '{"deviations":[{"id":"DEV-404","files":["tests/test_codec.cpp"],'
                '"approved_by":"tech-lead","expires":"2099-12-31"}]}',
                encoding="utf-8",
            )
            old_env = os.environ.get("DEVPACK_DEVIATIONS")
            os.environ["DEVPACK_DEVIATIONS"] = str(reg)
            try:
                code, out, _ = self._run_lock(
                    [
                        "--repo",
                        str(self.repo_dir),
                        "--base",
                        "HEAD~1",
                        "--protected",
                        "tests/**",
                    ]
                )
            finally:
                if old_env is None:
                    os.environ.pop("DEVPACK_DEVIATIONS", None)
                else:
                    os.environ["DEVPACK_DEVIATIONS"] = old_env
        self.assertEqual(code, 0)
        self.assertIn("Deviation: DEV-404", out)

    def test_no_deviation_flag_ignores_head_deviation_trailer(self) -> None:
        """--no-deviation ignores HEAD 'Deviation: <ID>' trailers even when registered."""
        (self.repo_dir / "tests" / "test_codec.cpp").write_text(
            "void test_decode() {\n"
            "    ASSERT_EQ(1, 1);\n"
            "    EXPECT_TRUE(true);\n"
            "    REQUIRE(2 == 2);\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo_dir),
                "commit",
                "-am",
                "drop closing brace\n\nDeviation: DEV-777",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        protected_args = [
            "--repo",
            str(self.repo_dir),
            "--base",
            "HEAD~1",
            "--protected",
            "tests/**",
        ]

        with tempfile.TemporaryDirectory() as reg_dir:
            reg = pathlib.Path(reg_dir) / "deviations.json"
            reg.write_text(
                '{"deviations":[{"id":"DEV-777","files":["tests/test_codec.cpp"],'
                '"approved_by":"tech-lead","expires":"2099-12-31"}]}',
                encoding="utf-8",
            )
            old_env = os.environ.get("DEVPACK_DEVIATIONS")
            os.environ["DEVPACK_DEVIATIONS"] = str(reg)
            try:
                code, out, _ = self._run_lock(protected_args)
                self.assertEqual(code, 0)
                self.assertIn("Deviation: DEV-777", out)

                code, out, _ = self._run_lock(protected_args + ["--no-deviation"])
                self.assertEqual(code, 1)
                self.assertIn("existing non-comment test lines modified or deleted", out)
                self.assertNotIn("Deviation", out)
            finally:
                if old_env is None:
                    os.environ.pop("DEVPACK_DEVIATIONS", None)
                else:
                    os.environ["DEVPACK_DEVIATIONS"] = old_env

    def test_custom_assert_regex_is_respected(self) -> None:
        """Custom --assert-regex detects decrease of custom assertion macro tokens."""
        custom_test = self.repo_dir / "tests" / "test_custom.cpp"
        custom_test.write_text("VERIFY_OK(a);\nVERIFY_OK(b);\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "add", "."],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "commit", "-m", "add custom test"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        custom_test.write_text("VERIFY_OK(a);\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.repo_dir), "commit", "-am", "reduce custom verify"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        code, out, _ = self._run_lock(
            [
                "--repo",
                str(self.repo_dir),
                "--base",
                "HEAD~1",
                "--protected",
                "tests/test_custom.cpp",
                "--assert-regex",
                r"\bVERIFY_OK\(",
            ]
        )
        self.assertEqual(code, 1)
        self.assertIn("assertion count decreased", out)


if __name__ == "__main__":
    unittest.main()
