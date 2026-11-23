"""Behavior tests for the project gate component adapter."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest


class TestProjectChecks(unittest.TestCase):
    """Run project gate checks in temporary repositories with real component commands."""

    def test_template_uses_pack_paths_and_four_project_checks(self) -> None:
        """The project template cannot accidentally invoke pack self-tests."""
        import tomllib
        root = Path(__file__).resolve().parents[3]
        template = root / "enforcement/gate/gate.project.example.toml"
        self.assertTrue(template.is_file())
        checks = tomllib.loads(template.read_text())["check"]
        self.assertEqual([entry["name"] for entry in checks], ["preflight", "cppcheck", "build", "tests"])
        for entry in checks:
            self.assertIn("{pack_root}/enforcement/gate/project_checks.py", entry["cmd"])

    def test_empty_changes_skip_and_component_commands_run(self) -> None:
        """No C/C++ changes skip lint, while build and test execute configured commands."""
        root = Path(__file__).resolve().parents[3]
        helper = root / "enforcement/gate/project_checks.py"
        self.assertTrue(helper.is_file())
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".devpack").mkdir()
            (repo / "src/part").mkdir(parents=True)
            (repo / "src/part/main.cpp").write_text("int value;\n")
            test_script = (
                "import pathlib; "
                "pathlib.Path('junit.xml').write_text("
                "'<testsuite tests=\"1\" failures=\"0\" errors=\"0\"><testcase name=\"smoke\"/></testsuite>\\n'); "
                "print(456)"
            )
            test_cmd = f"python3 -c {json.dumps(test_script)}"
            (repo / ".devpack/project.toml").write_text(
                '[workspace]\nroot="src"\n[[component]]\nname="part"\npath="part"\n'
                'build_cmd="python3 -c \'print(123)\'"\n'
                f"test_cmd={json.dumps(test_cmd)}\n"
            )
            for args in (["init"], ["-c", "user.name=test", "-c", "user.email=test@local.invalid", "add", "."], ["-c", "user.name=test", "-c", "user.email=test@local.invalid", "commit", "-m", "init"]):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            result = subprocess.run(["python3", str(helper), "--check", "lint", "--repo", tmp], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("SKIP: no C/C++ changes", result.stdout)
            for check, expected in (("build", "123"), ("tests", "456")):
                result = subprocess.run(["python3", str(helper), "--check", check, "--repo", tmp], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(expected, result.stdout)

    def test_tests_check_rejects_exit0_without_proof_and_empty_or_failing_xml(self) -> None:
        """Plain exit 0 without XML/gcov, tests='0' XML, or failures='1' XML must fail --check tests."""
        root = Path(__file__).resolve().parents[3]
        helper = root / "enforcement/gate/project_checks.py"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".devpack").mkdir()
            (repo / "src/part").mkdir(parents=True)
            (repo / "src/part/main.cpp").write_text("int value;\n")
            for idx, (cmd_script, expected_err) in enumerate(
                [
                    ("print('exit zero only')", "without JUnit/CTest XML"),
                    (
                        "import pathlib; pathlib.Path('junit.xml').write_text('<testsuite tests=\"0\" failures=\"0\" errors=\"0\"/>\\n')",
                        "executed=0",
                    ),
                    (
                        "import pathlib; pathlib.Path('junit.xml').write_text('<testsuite tests=\"2\" failures=\"1\" errors=\"0\"/>\\n')",
                        "failures=1",
                    ),
                ]
            ):
                with self.subTest(case=idx):
                    (repo / ".devpack/project.toml").write_text(
                        '[workspace]\nroot="src"\n[[component]]\nname="part"\npath="part"\n'
                        f'build_cmd="true"\ntest_cmd={json.dumps("python3 -c " + json.dumps(cmd_script))}\n'
                    )
                    for args in (
                        ["init"],
                        ["-c", "user.name=test", "-c", "user.email=test@local.invalid", "add", "."],
                        ["-c", "user.name=test", "-c", "user.email=test@local.invalid", "commit", "-m", f"case {idx}", "--allow-empty"],
                    ):
                        subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
                    for xml in repo.rglob("*.xml"):
                        xml.unlink()
                    res = subprocess.run(["python3", str(helper), "--check", "tests", "--repo", tmp], capture_output=True, text=True)
                    self.assertNotEqual(res.returncode, 0, res.stdout + res.stderr)
                    self.assertIn(expected_err, res.stdout + res.stderr)

    def test_tests_check_accepts_ctest_xml_and_real_gcov_proof(self) -> None:
        """CTest Site/Test.xml passes and pack-framework projects require real gcov assertion execution."""
        root = Path(__file__).resolve().parents[3]
        helper = root / "enforcement/gate/project_checks.py"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".devpack").mkdir()
            (repo / "src/part").mkdir(parents=True)
            (repo / "src/part/main.cpp").write_text("int value;\n")
            ctest_script = (
                "import pathlib; pathlib.Path('Test.xml').write_text("
                "'<Site><Testing><Test Status=\"passed\"><Name>t1</Name></Test></Testing></Site>\\n')"
            )
            (repo / ".devpack/project.toml").write_text(
                '[workspace]\nroot="src"\n[[component]]\nname="part"\npath="part"\n'
                f'build_cmd="true"\ntest_cmd={json.dumps("python3 -c " + json.dumps(ctest_script))}\n'
            )
            for args in (
                ["init"],
                ["-c", "user.name=test", "-c", "user.email=test@local.invalid", "add", "."],
                ["-c", "user.name=test", "-c", "user.email=test@local.invalid", "commit", "-m", "ctest"],
            ):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            res = subprocess.run(["python3", str(helper), "--check", "tests", "--repo", tmp], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("CTest XML", res.stdout)

        # Pack-framework project using fixture must pass gcov proof
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            import shutil
            shutil.copytree(root / "eval/fixture", repo, dirs_exist_ok=True)
            (repo / ".devpack").mkdir()
            (repo / ".devpack/project.toml").write_text(
                '[workspace]\nroot="."\n[[component]]\nname="fix"\npath="."\n'
                'build_cmd="true"\ntest_cmd="bash build_and_test.sh --no-cmake"\n'
            )
            for args in (
                ["init"],
                ["-c", "user.name=test", "-c", "user.email=test@local.invalid", "add", "."],
                ["-c", "user.name=test", "-c", "user.email=test@local.invalid", "commit", "-m", "fixture"],
            ):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            res = subprocess.run(["python3", str(helper), "--check", "tests", "--repo", tmp], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("verified via gcov", res.stdout)
