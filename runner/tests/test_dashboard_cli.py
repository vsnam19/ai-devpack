"""Exercise the dashboard CLI without a process or a real terminal."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from runner.devpack_dash import main


class CLITest(unittest.TestCase):
    def test_snapshot_and_exit_codes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "target"
            root.mkdir()
            args = ["--workdir", str(root), "--state-dir", str(root.parent / "state"), "--json"]
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                self.assertEqual(main([*args, "jobs"]), 1)
                (root / "runs").mkdir()
                output.truncate(0)
                output.seek(0)
                self.assertEqual(main([*args, "jobs"]), 0)
                with self.assertRaises(SystemExit) as failure:
                    main([*args, "jobs", "--filter", "wrong=value"])
            self.assertEqual(failure.exception.code, 2)
            snapshot = json.loads(output.getvalue())
            self.assertEqual(snapshot["schema_version"], 1)
            self.assertTrue(snapshot["scan"]["complete"])
            self.assertEqual(snapshot["jobs"], [])

    def test_approval_commands_exact_and_no_side_effects(self):
        import builtins
        import os
        import shlex
        from unittest.mock import patch

        from runner.tests.test_dashboard_reader import write

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workdir, state, config = root / "target repo", root / "safe state", root / "explicit settings.toml"
            workdir.mkdir()
            config.write_text("[pipeline]\nmode = 'hitl'\n")
            cli = root / "trusted tools" / "devpack_approve.py"
            write(state / "approvals" / "pending" / "test-checkpoint.json",
                  {"id": "test-checkpoint", "status": "pending", "checkpoint": "brief"})
            output = io.StringIO()
            original_open, original_os_open = builtins.open, os.open

            def readonly(path, mode="r", *args, **kwargs):
                self.assertTrue(set(mode) <= set("rbt"), mode)
                return original_open(path, mode, *args, **kwargs)

            def readonly_fd(path, flags, *args, **kwargs):
                self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
                return original_os_open(path, flags, *args, **kwargs)

            args = ["--workdir", str(workdir), "--project-config", str(config), "--state-dir", str(state)]
            with (
                redirect_stdout(output), patch("runner.dash.reader.APPROVE_CLI", cli),
                patch("subprocess.Popen", side_effect=AssertionError("process launch")),
                patch("os.kill", side_effect=AssertionError("signal delivery")),
                patch("builtins.open", readonly), patch("io.open", readonly), patch("os.open", readonly_fd),
            ):
                self.assertEqual(main([*args, "approvals", "--plain"]), 0)
            commands = [shlex.split(line) for line in output.getvalue().splitlines() if line.startswith("python3 ")]
            prefix = ["python3", str(cli), *args]
            self.assertEqual(commands, [
                [*prefix, "approve", "test-checkpoint"],
                [*prefix, "reject", "test-checkpoint", "--note", "Reason for rejection"],
            ])
            self.assertIn("Brief text unavailable", output.getvalue())

    def test_json_tail_and_config_rejection(self):
        from runner.tests.test_dashboard_reader import write

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = root / "target"
            run = workdir / "runs" / "run"
            write(run / "run.json", {"kind": "dispatch", "outcome": "pass"})
            stream = run / "steps" / "01-implement-engine.jsonl"
            stream.parent.mkdir()
            stream.write_text('{"type":"message","content":"sample"}\n')
            args = ["--workdir", str(workdir), "--state-dir", str(root / "state"), "--json", "--once"]
            output = io.StringIO()
            invalid = root / "bad.toml"
            invalid.write_text('[dashboard]\nstream_poll_s = 1.5\n')
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                self.assertEqual(main([*args, "tail", "run"]), 0)
                self.assertEqual(main([*args, "show", "missing"]), 1)
                with self.assertRaises(SystemExit) as failure:
                    main([*args, "--dashboard-config", str(invalid)])
            self.assertEqual(failure.exception.code, 2)
            event, missing = map(json.loads, output.getvalue().splitlines())
            self.assertEqual((event["schema_version"], event["kind"], event["text"]), (1, "message", "sample"))
            self.assertEqual(missing["jobs"], [])

    def test_curses_wrapper_restores_on_interrupt_and_exception(self):
        import os
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch

        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            args = ["--workdir", str(target), "--state-dir", str(root / "state")]
            calls = []

            def wrapper(draw):
                calls.append("entered")
                try:
                    return draw(object())
                finally:
                    calls.append("restored")

            fake_curses = SimpleNamespace(wrapper=wrapper, error=type("ScreenError", (Exception,), {}))
            with (
                patch.dict(sys.modules, {"curses": fake_curses}), patch.dict(os.environ, {"TERM": "screen"}),
                patch("sys.stdin.isatty", return_value=True), patch("sys.stdout.isatty", return_value=True),
                redirect_stderr(io.StringIO()),
            ):
                with patch("runner.devpack_dash.interface", side_effect=KeyboardInterrupt):
                    self.assertEqual(main(args), 0)
                with patch("runner.devpack_dash.interface", side_effect=RuntimeError):
                    self.assertEqual(main(args), 1)
            self.assertEqual(calls, ["entered", "restored", "entered", "restored"])

    def test_curses_failure_after_entry(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        class TerminalError(Exception):
            pass

        def wrapper(run):
            return run(None)

        curses = SimpleNamespace(error=TerminalError, wrapper=wrapper)
        with (
            TemporaryDirectory() as directory,
            patch.dict("sys.modules", {"curses": curses}),
            patch.dict("os.environ", {"TERM": "test-terminal"}),
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
            patch("runner.devpack_dash.interface", side_effect=TerminalError("screen failed")),
            redirect_stderr(io.StringIO()) as errors,
        ):
            target = Path(directory) / "target"
            target.mkdir()
            self.assertEqual(main(["--workdir", str(target), "--state-dir", str(target.parent / "state")]), 1)
            self.assertEqual(errors.getvalue(), "dashboard read failed\n")

    def test_state_directory_under_linked_parent(self):
        from unittest.mock import patch

        from runner.tests.test_dashboard_reader import write

        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            parent = root / "linked-parent"
            parent.symlink_to(root, target_is_directory=True)
            state = parent / "state"
            write(root / "state" / "approvals" / "pending" / "brief-id.json",
                  {"id": "brief-id", "status": "PENDING", "checkpoint": "brief"})
            args = ["--workdir", str(target), "--json"]
            for options, environment in ((["--state-dir", str(state)], {}),
                                         ([], {"DEVPACK_STATE_DIR": str(state)})):
                with (
                    self.subTest(options=options),
                    patch.dict("os.environ", environment),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    self.assertEqual(main([*args, *options, "approvals"]), 0)
                    self.assertEqual(json.loads(output.getvalue())["pending_approvals"], 1)
            linked_state = root / "linked-state"
            linked_state.symlink_to(root / "state", target_is_directory=True)
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
                main([*args, "--state-dir", str(linked_state), "approvals"])
            self.assertEqual(failure.exception.code, 2)
