"""Behavior tests for the project gate component adapter."""
from __future__ import annotations

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
        """Explicitly allowed empty C/C++ changes skip lint, while build and test execute configured commands."""
        root = Path(__file__).resolve().parents[3]
        helper = root / "enforcement/gate/project_checks.py"
        self.assertTrue(helper.is_file())
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".devpack").mkdir()
            (repo / "src/part").mkdir(parents=True)
            (repo / "src/part/main.cpp").write_text("int value;\n")
            (repo / ".devpack/project.toml").write_text('[workspace]\nroot="src"\n[[component]]\nname="part"\npath="part"\nbuild_cmd="python3 -c \'print(123)\'"\ntest_cmd="python3 -c \'print(456)\'"\n')
            for args in (["init"], ["-c", "user.name=test", "-c", "user.email=test@local.invalid", "add", "."], ["-c", "user.name=test", "-c", "user.email=test@local.invalid", "commit", "-m", "init"]):
                subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True)
            result = subprocess.run(["python3", str(helper), "--check", "lint", "--repo", tmp, "--allow-empty"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("SKIP: no C/C++ changes", result.stdout)
            for check, expected in (("build", "123"), ("tests", "456")):
                result = subprocess.run(["python3", str(helper), "--check", check, "--repo", tmp], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(expected, result.stdout)
