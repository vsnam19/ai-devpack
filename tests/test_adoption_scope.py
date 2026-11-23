"""Regression coverage for project adoption and fail-closed scan scope."""
from contextlib import redirect_stdout, redirect_stderr
import io
import os
from pathlib import Path
import tempfile
import shutil
import subprocess
import unittest
from unittest.mock import patch

from runner.workspace_scope import load_workspace_config, resolve_workspace_scope
from enforcement.lint import preflight_lint
from enforcement.static import cppcheck_check


class AdoptionScopeTests(unittest.TestCase):
    """Exercise nested discovery and consistent file target handling."""

    def make_project(self, root):
        """Create the nested layout documented for workspace consumers."""
        (root / '.devpack').mkdir()
        (root / '.devpack/project.toml').write_text(
            '[workspace]\nroot="src"\nexclude=["**/generated/**"]\n'
            '[[component]]\nname="ipc_bus"\npath="middleware/ipc_bus"\n'
            '[scan]\nselected=["ipc_bus"]\n')
        for name in ('middleware/ipc_bus', 'services/diag_svc'):
            comp = root / 'src' / name
            comp.mkdir(parents=True)
            (comp / 'CMakeLists.txt').write_text('# marker\n')
            (comp / 'unit.cpp').write_text('/** @file unit.cpp */\n')
        nested = root / 'src/middleware/ipc_bus/nested'
        nested.mkdir()
        (nested / 'Makefile').write_text('# marker\n')

    def test_documented_layout_fixture_is_discovered(self):
        """The policy's nested layout works without component declarations."""
        fixture = Path(__file__).parent / "fixtures/discovery_ws"
        scope = resolve_workspace_scope(fixture, env={})
        self.assertEqual({"ipc_bus", "diag_svc", "sensor_hub"},
                         {comp.name for comp in scope.active_components})

    def test_vendored_skill_grader_with_explicit_pack_scope(self):
        """A grader rejects bad input and accepts expected output in a scoped host repo."""
        pack = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            host = Path(tmp)
            self.make_project(host)
            subprocess.run(["git", "init", "-q", str(host)], check=True)
            vendor = host / "devpack"
            for rel in ("runner", "enforcement", "skilltest", "knowledge/skills/doxygen-naming"):
                shutil.copytree(pack / rel, vendor / rel, ignore=shutil.ignore_patterns("__pycache__"))
            tests = vendor / "knowledge/skills/doxygen-naming/tests"
            grader = tests / "graders/header-contract/check.sh"
            env = {**os.environ, "DEVPACK_ROOT": str(vendor), "DEVPACK_PROJECT_CONFIG": "none"}
            for relative, expected in (("fixtures/header-contract/input", False),
                                       ("expected/header-contract", True)):
                proc = subprocess.run(["bash", str(grader), str(tests / relative)],
                                      cwd=host, env=env, capture_output=True, text=True)
                self.assertEqual(expected, proc.returncode == 0, proc.stdout + proc.stderr)
                self.assertNotIn("NO_FILES_CHECKED", proc.stdout + proc.stderr)

    def test_discovery_merges_and_stops_at_component(self):
        """Explicit metadata must not hide discovered sibling components."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            cfg = load_workspace_config(root)
            self.assertEqual(['ipc_bus', 'diag_svc'], [c.name for c in cfg.components])
            self.assertEqual('services/diag_svc', cfg.components[1].path)

    def test_discovery_depth_and_disable(self):
        """Discovery limits and explicit opt-out are project configuration."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            config = root / '.devpack/project.toml'
            original = config.read_text()
            for setting in ('discovery_depth=1', 'discover=false'):
                config.write_text(original.replace('[workspace]', '[workspace]\n' + setting))
                self.assertEqual(['ipc_bus'], [c.name for c in load_workspace_config(root).components])

    def test_none_and_env_override(self):
        """Self-tests can explicitly opt out of an enclosing project's scope."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            self.assertFalse(load_workspace_config(root, 'none').configured)
            with patch.dict(os.environ, {'DEVPACK_PROJECT_CONFIG': 'none'}):
                self.assertFalse(load_workspace_config(root).configured)

    def test_explicit_unselected_and_excluded_files_fail_both_tools(self):
        """An explicit invalid scope target is an error rather than an empty pass."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            excluded = root / 'src/middleware/ipc_bus/generated/unit.cpp'
            excluded.parent.mkdir()
            excluded.write_text('/** @file unit.cpp */\n')
            for target in (root / 'src/services/diag_svc/unit.cpp', excluded):
                for module in (preflight_lint, cppcheck_check):
                    with self.subTest(tool=module.__name__, target=target):
                        out = io.StringIO()
                        with redirect_stdout(out), redirect_stderr(out):
                            code = module.main([str(target), '--repo-root', str(root)])
                        self.assertEqual(1, code, out.getvalue())
                        self.assertIn('OUT_OF_SCOPE', out.getvalue())

    def test_all_components_accepts_previously_unselected_target(self):
        """An explicit all-components scan includes a configured sibling."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            target = root / 'src/services/diag_svc/unit.cpp'
            for module in (preflight_lint, cppcheck_check):
                output = io.StringIO()
                # Only scope resolution is relevant; the real compiler/analyzer may report findings.
                with redirect_stdout(output), redirect_stderr(output):
                    code = module.main([str(target), '--repo-root', str(root), '--all-components'])
                self.assertNotIn('OUT_OF_SCOPE', output.getvalue())
                self.assertNotIn('NO_FILES_CHECKED', output.getvalue())
                self.assertIn("1 file(s) checked", output.getvalue())
                self.assertIn(code, (0, 1))

    def test_cppcheck_empty_changes_skip_without_analyzer(self):
        """A real no-change git diff does not require an analyzer executable."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            for args in (["init", "-q"], ["add", "."],
                         ["-c", "user.name=Tester", "-c", "user.email=test@local.invalid", "commit", "-qm", "fixture"]):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                code = cppcheck_check.main(['--repo-root', str(root), '--base', 'HEAD',
                                            '--cppcheck-bin', 'nonexistent-analyzer-for-empty-changes'])
            self.assertEqual(0, code, output.getvalue())
            self.assertIn('SKIP: no C/C++ changes', output.getvalue())

    def test_explicit_file_infers_project_above_component_build_marker(self):
        """A component CMakeLists must not hide its owning project's scope."""
        pack = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            target = root / 'src/services/diag_svc/unit.cpp'
            for script in ('enforcement/lint/preflight_lint.py', 'enforcement/static/cppcheck_check.py'):
                result = subprocess.run(['python3', str(pack / script), str(target)], cwd=root,
                                        capture_output=True, text=True)
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertIn('OUT_OF_SCOPE', result.stdout + result.stderr)

    def test_empty_directory_fails_unless_allowed(self):
        """Empty scans fail while an intentional empty change set is explicit."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            empty = root / 'src/middleware/ipc_bus/empty'
            empty.mkdir()
            for module in (preflight_lint, cppcheck_check):
                with self.subTest(tool=module.__name__):
                    out = io.StringIO()
                    with redirect_stdout(out), redirect_stderr(out):
                        code = module.main([str(empty), '--repo-root', str(root)])
                    self.assertNotEqual(0, code)
                    self.assertIn('NO_FILES_CHECKED', out.getvalue())
                    with redirect_stdout(out), redirect_stderr(out):
                        code = module.main([str(empty), '--repo-root', str(root), '--allow-empty'])
                    self.assertEqual(0, code, out.getvalue())


if __name__ == '__main__':
    unittest.main()
