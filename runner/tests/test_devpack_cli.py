"""Test the public launcher without invoking agents or external services."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from runner import devpack_cli


class DevpackCliTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.project = self.base / "project"
        self.project.mkdir()

    def test_dispatch_preserves_arguments_and_exit_status(self):
        targets = {name: [sys.executable, str(devpack_cli.ROOT / f"runner/devpack_{name}.py")]
                   for name in ("run", "plan", "dispatch", "broker", "approve", "logs", "dash")}
        targets.update(
            tui=[sys.executable, "-m", "runner.dash.tui"],
            gate=[sys.executable, str(devpack_cli.ROOT / "enforcement/gate/gate.py")],
            skills=[sys.executable, str(devpack_cli.ROOT / "runner/agent_exec.py")],
            hooks=["bash", str(devpack_cli.ROOT / "enforcement/hooks/install.sh")],
            selftest=["bash", str(devpack_cli.ROOT / "tests/run_all.sh")],
        )
        self.assertEqual(set(targets), set(devpack_cli.COMMANDS))
        args = ["--", "a b", "; literal", ""]
        for name, target in targets.items():
            with self.subTest(name=name), patch.object(devpack_cli.subprocess, "run") as run:
                run.return_value.returncode = 23
                self.assertEqual(devpack_cli.main([name, *args]), 23)
                self.assertEqual(run.call_args.args[0], target + args)
                self.assertFalse(run.call_args.kwargs.get("shell"))
                if name == "tui":
                    self.assertEqual(run.call_args.kwargs["env"]["PYTHONPATH"].split(os.pathsep)[0],
                                     str(devpack_cli.ROOT))

    def test_init_is_idempotent_and_keeps_existing_content(self):
        ignore = self.project / ".gitignore"
        ignore.write_text("local-entry")
        args = ["init", "--workdir", str(self.project),
                "--mode", "autonomous", "--data-class", "confidential"]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(devpack_cli.main(args), 0)
            config = self.project / ".devpack"
            project = config / "project.toml"
            self.assertIn('mode = "autonomous"', project.read_text())
            self.assertIn('data_class = "confidential"', project.read_text())
            self.assertIn('# class = "external"', (config / "engines.toml").read_text())
            self.assertTrue((config / "gate.toml").is_file())
            brief = self.project / "plans/example/briefs/T1.md"
            self.assertTrue(brief.is_file())
            brief.write_text("custom brief")
            self.assertEqual(devpack_cli.main(args[:-2]), 0)
        self.assertIn("Exists", output.getvalue())
        self.assertEqual(brief.read_text(), "custom brief")
        self.assertIn('data_class = "confidential"', project.read_text())
        self.assertEqual(ignore.read_text(), "local-entry\n/runs/\n")

    def test_doctor_statuses(self):
        args = ["--workdir", str(self.project)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(devpack_cli.main(["init", *args]), 0)
        summary = {"engines": {"example": {"external": {"command": ["example-tool"]}}}}
        completed = subprocess.CompletedProcess([], 0, json.dumps(summary), "")
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.base / "state")}), \
             patch.object(devpack_cli.shutil, "which", side_effect=lambda name: (
                 name if name in ("git", "shellcheck") else None)), \
             patch.object(devpack_cli.importlib.util, "find_spec", return_value=None), \
             patch.object(devpack_cli.subprocess, "run", return_value=completed) as run, \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(devpack_cli.main(["doctor", *args]), 1)
        report = output.getvalue()
        for status, label in (("PASS", "Git"), ("WARN", "Docker"), ("WARN", "Textual"),
                              ("PASS", "State directory"), ("FAIL", "Engine example")):
            self.assertRegex(report, rf"{status}\s+{label}")
        self.assertEqual(run.call_args.args[0][-2:], [
            "--check-config", str(self.project / ".devpack/engines.toml")])

    def _which_with(self, *names):
        return lambda name: f"/usr/bin/{name}" if name in names else None

    def test_doctor_rejects_unrunnable_project_engine_and_state_configs(self):
        args = ["--workdir", str(self.project)]
        base_env = {
            "XDG_STATE_HOME": str(self.base / "state"),
            "DEVPACK_LOCAL_BASE_URL": "http://127.0.0.1:8080",
            "DEVPACK_LOCAL_AUTH_TOKEN": "token",
        }

        # Valid confidential project with matching local engines passes.
        with redirect_stdout(io.StringIO()):
            self.assertEqual(devpack_cli.main(["init", *args, "--data-class", "confidential"]), 0)
        engines_path = self.project / ".devpack/engines.toml"
        engines_path.write_text(
            'version = 2\n'
            '[engines.local_a]\n'
            'model = "coder-a"\n'
            'class = "local"\n'
            'max_data_class = "confidential"\n'
            'base_url_env = "DEVPACK_LOCAL_BASE_URL"\n'
            'auth_token_env = "DEVPACK_LOCAL_AUTH_TOKEN"\n'
            '[engines.local_b]\n'
            'model = "coder-b"\n'
            'class = "local"\n'
            'max_data_class = "confidential"\n'
            'base_url_env = "DEVPACK_LOCAL_BASE_URL"\n'
            'auth_token_env = "DEVPACK_LOCAL_AUTH_TOKEN"\n'
            '[roles]\n'
            'implementer = "local_a"\n'
            'reviewer = "local_b"\n'
        )
        with patch.dict(os.environ, base_env, clear=False), \
             patch.object(devpack_cli.shutil, "which", side_effect=self._which_with("git", "shellcheck", "claude")), \
             patch.object(devpack_cli.importlib.util, "find_spec", return_value=None), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(devpack_cli.main(["doctor", *args]), 0, out.getvalue())

        # Missing DEVPACK_LOCAL_BASE_URL fails local engine check.
        missing_env = {k: v for k, v in os.environ.items()
                       if k not in ("DEVPACK_LOCAL_BASE_URL", "DEVPACK_LOCAL_AUTH_TOKEN")}
        missing_env["XDG_STATE_HOME"] = str(self.base / "state")
        with patch.dict(os.environ, missing_env, clear=True), \
             patch.object(devpack_cli.shutil, "which", side_effect=self._which_with("git", "shellcheck", "claude")), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(devpack_cli.main(["doctor", *args]), 1)
            self.assertRegex(out.getvalue(), r"FAIL\s+Engine local_a")

        # Secret data_class refuses every engine.
        proj_path = self.project / ".devpack/project.toml"
        proj_text = proj_path.read_text()
        proj_path.write_text(proj_text.replace('data_class = "confidential"', 'data_class = "secret"'))
        with patch.dict(os.environ, base_env, clear=False), \
             patch.object(devpack_cli.shutil, "which", side_effect=self._which_with("git", "shellcheck", "claude")), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(devpack_cli.main(["doctor", *args]), 1)
            self.assertRegex(out.getvalue(), r"FAIL\s+Engine local_a.*secret")

        # Confidential project with internal-only cloud engine fails data_class check.
        proj_path.write_text(proj_text)
        engines_path.write_text(
            'version = 2\n'
            '[engines.haiku]\n'
            'model = "haiku"\n'
            'class = "cloud"\n'
            'max_data_class = "internal"\n'
            '[roles]\n'
            'implementer = "haiku"\n'
            'reviewer = "haiku"\n'
        )
        with patch.dict(os.environ, base_env, clear=False), \
             patch.object(devpack_cli.shutil, "which", side_effect=self._which_with("git", "shellcheck", "claude")), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(devpack_cli.main(["doctor", *args]), 1)
            self.assertRegex(out.getvalue(), r"FAIL\s+Engine haiku")

        # Missing required roles fails Engine roles check.
        proj_path.write_text(proj_text.replace('data_class = "confidential"', 'data_class = "internal"'))
        engines_path.write_text(
            'version = 2\n'
            '[engines.haiku]\n'
            'model = "haiku"\n'
            'class = "cloud"\n'
            'max_data_class = "internal"\n'
            '[roles]\n'
            'implementer = "haiku"\n'
        )
        with patch.dict(os.environ, base_env, clear=False), \
             patch.object(devpack_cli.shutil, "which", side_effect=self._which_with("git", "shellcheck", "claude")), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(devpack_cli.main(["doctor", *args]), 1)
            self.assertRegex(out.getvalue(), r"FAIL\s+Engine roles.*reviewer")

        # [default_engines] referencing undefined engine fails.
        proj_path.write_text(
            proj_text.replace('data_class = "confidential"', 'data_class = "internal"')
            + '\n[default_engines]\nimplementer = "missing_engine"\n'
        )
        with patch.dict(os.environ, base_env, clear=False), \
             patch.object(devpack_cli.shutil, "which", side_effect=self._which_with("git", "shellcheck", "claude")), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(devpack_cli.main(["doctor", *args]), 1)
            self.assertRegex(out.getvalue(), r"FAIL\s+Default engine implementer")

        # Missing gate.toml fails Gate config.
        proj_path.write_text(proj_text.replace('data_class = "confidential"', 'data_class = "internal"'))
        (self.project / ".devpack/gate.toml").unlink()
        with patch.dict(os.environ, base_env, clear=False), \
             patch.object(devpack_cli.shutil, "which", side_effect=self._which_with("git", "shellcheck", "claude")), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(devpack_cli.main(["doctor", *args]), 1)
            self.assertRegex(out.getvalue(), r"FAIL\s+Gate config")

        # DEVPACK_STATE_DIR inside worktree fails State directory.
        inside_state = self.project / ".state"
        with patch.dict(os.environ, {**base_env, "DEVPACK_STATE_DIR": str(inside_state)}, clear=False), \
             patch.object(devpack_cli.shutil, "which", side_effect=self._which_with("git", "shellcheck", "claude")), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(devpack_cli.main(["doctor", *args]), 1)
            self.assertRegex(out.getvalue(), r"FAIL\s+State directory")
            self.assertFalse(inside_state.exists())

        # Symlinked project.toml fails Project config.
        target_toml = self.base / "real-project.toml"
        target_toml.write_text(proj_path.read_text())
        proj_path.unlink()
        proj_path.symlink_to(target_toml)
        with patch.dict(os.environ, base_env, clear=False), \
             patch.object(devpack_cli.shutil, "which", side_effect=self._which_with("git", "shellcheck", "claude")), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(devpack_cli.main(["doctor", *args]), 1)
            self.assertRegex(out.getvalue(), r"FAIL\s+Project config.*symlink")

    def test_doctor_skips_state_dir_when_workdir_missing(self):
        missing = self.base / "no-such-project"
        state_home = self.base / "unused-state"
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}), \
             patch.object(devpack_cli.shutil, "which", side_effect=self._which_with("git", "shellcheck")), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(devpack_cli.main(["doctor", "--workdir", str(missing)]), 1)
        self.assertRegex(out.getvalue(), r"FAIL\s+Project config")
        self.assertRegex(out.getvalue(), r"FAIL\s+State directory")
        self.assertFalse(state_home.exists())

    def test_init_install_hooks_requires_git_repo_root(self):
        # Non-git directory is rejected before writing any scaffold files.
        plain = self.base / "plain"
        plain.mkdir()
        err = io.StringIO()
        with patch.object(sys, "stderr", err):
            rc = devpack_cli.main(["init", "--workdir", str(plain), "--install-hooks"])
        self.assertEqual(rc, 1)
        self.assertIn("git repository root", err.getvalue())
        self.assertFalse((plain / ".devpack").exists())
        self.assertFalse((plain / ".gitignore").exists())

        # Subdirectory nested inside an ancestor git repository is rejected without touching ancestor hooks.
        mono = self.base / "monorepo"
        mono.mkdir()
        subprocess.run(["git", "init", str(mono)], check=True, capture_output=True)
        sub = mono / "subproject"
        sub.mkdir()
        err = io.StringIO()
        with patch.object(sys, "stderr", err):
            rc = devpack_cli.main(["init", "--workdir", str(sub), "--install-hooks"])
        self.assertEqual(rc, 1)
        self.assertIn("git repository root", err.getvalue())
        self.assertFalse((sub / ".devpack").exists())
        self.assertFalse((mono / ".git/hooks/pre-commit").exists())

        # Actual git repository root succeeds and is idempotent.
        with redirect_stdout(io.StringIO()) as out1:
            self.assertEqual(devpack_cli.main(["init", "--workdir", str(mono), "--install-hooks"]), 0)
        self.assertTrue((mono / ".git/hooks/pre-commit").is_symlink())
        with redirect_stdout(io.StringIO()) as out2:
            self.assertEqual(devpack_cli.main(["init", "--workdir", str(mono), "--install-hooks"]), 0)
        self.assertIn("Exists  pre-commit hook", out2.getvalue())

    def test_selftest_sets_pythondontwritebytecode(self):
        with patch.object(devpack_cli.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(devpack_cli.main(["selftest"]), 0)
            self.assertEqual(run.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")
