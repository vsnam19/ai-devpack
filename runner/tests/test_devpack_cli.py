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
