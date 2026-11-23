"""Installed-project regressions for explicit diff baselines and component ownership."""
from contextlib import redirect_stdout, redirect_stderr
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from enforcement.gate import project_checks, gate

PACK = Path(__file__).resolve().parents[3]


class ProjectAdoptionTests(unittest.TestCase):
    """Test real git diffs without executing external analyzers or agents."""

    def make_repo(self, root):
        """Create a committed workspace with one configured component."""
        (root / '.devpack').mkdir()
        (root / 'src/libcore').mkdir(parents=True)
        (root / 'src/libcore/core.cpp').write_text('int value;\n')
        (root / '.devpack/project.toml').write_text(
            '[workspace]\nroot="src"\n[[component]]\nname="core"\npath="libcore"\n')
        for args in (['init', '-q'], ['add', '.'],
                     ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                      'commit', '-qm', 'initial']):
            subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)

    def invoke(self, root, *extra, env=None):
        """Capture project check output in an isolated environment."""
        out = io.StringIO()
        with patch.dict(os.environ, env or {}, clear=True), redirect_stdout(out), redirect_stderr(out):
            rc = project_checks.main(['--repo', str(root), '--check', 'lint', '--dry-run', *extra])
        return rc, out.getvalue()

    def test_empty_requires_explicit_opt_in(self):
        """A clean tree cannot report green silently."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            rc, output = self.invoke(root)
            self.assertNotEqual(0, rc)
            self.assertIn('--allow-empty', output)
            rc, output = self.invoke(root, '--allow-empty')
            self.assertEqual(0, rc, output)
            self.assertIn('SKIP', output)

    def test_ci_requires_meaningful_base_and_explains_shallow_clone(self):
        """CI rejects missing/HEAD baselines; unresolved refs give actionable diagnostics."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            for value in ('', 'HEAD'):
                rc, output = self.invoke(root, '--allow-empty', env={'CI': 'true', 'DEVPACK_BASE': value})
                self.assertNotEqual(0, rc)
                self.assertIn('DEVPACK_BASE', output)
            rc, output = self.invoke(root, env={'DEVPACK_BASE': 'origin/unfetched'})
            self.assertNotEqual(0, rc)
            for fragment in ('DEVPACK_BASE', 'origin/unfetched', 'git fetch --depth='):
                self.assertIn(fragment, output)

    def test_unowned_changes_fail_and_list_paths(self):
        """A change beyond discovery depth cannot disappear beside an owned change."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            (root / 'src/libcore/core.cpp').write_text('int changed;\n')
            deep = root / 'src/a/b/c/d/deep/unit.cpp'
            deep.parent.mkdir(parents=True)
            deep.write_text('int unowned;\n')
            rc, output = self.invoke(root)
            self.assertNotEqual(0, rc)
            self.assertIn('src/a/b/c/d/deep/unit.cpp', output)
            self.assertIn('discovery_depth', output)
            rc, output = self.invoke(root, '--allow-unowned')
            self.assertEqual(0, rc, output)
            self.assertIn('WARNING', output)

    def test_gate_forwards_explicit_exceptions(self):
        """The installed gate carries both opt-ins into project check children."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            cfg = root / '.devpack/gate.toml'
            cfg.write_text('[[check]]\nname="lint"\ncmd=["python3", "' +
                           (PACK / 'enforcement/gate/project_checks.py').as_posix() +
                           '", "--check", "lint", "--dry-run"]\n')
            out = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), redirect_stdout(out):
                rc = gate.main(['--repo', str(root), '--config', str(cfg),
                                '--allow-empty', '--allow-unowned',
                                '--receipt', str(root / 'receipt.json')])
            self.assertEqual(0, rc, out.getvalue())
            self.assertIn('GATE_OK', out.getvalue())
