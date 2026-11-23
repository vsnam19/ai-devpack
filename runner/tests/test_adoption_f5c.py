"""Adoption regression tests for nested tests, briefs, skills, and secret rules."""
from pathlib import Path
import tempfile
import unittest

from runner import agent_exec, brief_lint, redaction, workspace_scope
from enforcement.checks import secret_pii_check

PACK = Path(__file__).resolve().parents[2]


class AdoptionRegressionTests(unittest.TestCase):
    """Exercise public adoption helpers with project-owned temporary files."""

    def test_skills_skip_deprecated(self):
        """Deprecated bundles must never appear in a fresh installation."""
        with tempfile.TemporaryDirectory() as tmp:
            installed = agent_exec.install_project_skills(Path(tmp))
            self.assertIn('automotive-cpp', installed)
            self.assertNotIn('misra-cpp-2023', installed)
            self.assertNotIn('doxygen-naming', installed)

    def test_shared_secret_rules(self):
        """Both scanner and redactor catch each supported credential family."""
        samples = ['sk-ant-' + 'a' * 24, 'ghp_' + 'b' * 30,
                   'Bearer ' + 'c' * 20, 'password' + ': hunter2', 'passwd' + ': hunter2',
                   'https://' + 'user:credential' + '@host.invalid/path',
                   'Authorization: Basic ' + 'dXNlcjpwYXNz',
                   'eyJhbGciOiJIUzI1NiJ9' + '.eyJzdWIiOiIxIn0' + '.signature',
                   'AIza' + 'd' * 35]
        rules = secret_pii_check.get_builtin_rules()
        for sample in samples:
            with self.subTest(sample=sample[:8]):
                self.assertTrue(any(rx.search(sample) for _, rx in rules))
                self.assertNotEqual(sample, redaction.Redactor().redact(sample))

    def test_example_brief_has_no_warnings(self):
        """The shipped implementation example is clean, including proposed symbols."""
        findings = brief_lint.lint_brief_text(
            (PACK / 'orchestrator/briefs/example_implement.md').read_text(),
            repo_root=PACK / 'eval/fixture')
        self.assertEqual([], findings)

    def test_brief_creation_verbs_and_path_deduplication(self):
        """Proposed symbols are exempt and missing paths produce one diagnostic."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'src').mkdir()
            text = ('## Goal\nIntroduce `brandNewSymbol()` in `src/missing.cpp`.\n'
                    '## Input\n`src/missing.cpp`\n## Expected output\n'
                    'Define `anotherNewSymbol()`.\n## Files allowed\n`src/missing.cpp`\n'
                    '## Constraints\nNone.\n## Gate\nRun tests.\n')
            findings = brief_lint.lint_brief_text(text, repo_root=tmp)
            self.assertFalse([f for f in findings if f['rule'] == 'BRIEF-SYMBOL-MISSING'])
            paths = [f for f in findings if f['rule'] == 'BRIEF-PATH-MISSING']
            self.assertEqual(1, len(paths))
            self.assertEqual('error', paths[0]['severity'])

    def test_protected_configuration(self):
        """Defaults cover nested tests and component globs are rooted to the component."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.devpack').mkdir()
            (root / '.devpack/project.toml').write_text(
                'protected_test_globs=["checks/**"]\n[workspace]\nroot="src"\n'
                '[[component]]\nname="core"\npath="libcore"\ntest_globs=["specs/**"]\n')
            globs = workspace_scope.resolve_protected_test_globs(root)
            self.assertIn('checks/**', globs)
            self.assertIn('src/libcore/specs/**', globs)
            self.assertIn('**/tests/**', globs)
            self.assertIn('**/test/**', globs)
            self.assertIn('**/*_test.cpp', globs)
            self.assertIn('**/test_*.cpp', globs)

    def test_yocto_test_weakening_is_a_violation(self):
        """A changed existing check beneath libcore/tests must be test-locked."""
        from contextlib import redirect_stdout
        import io
        import subprocess
        from enforcement.checks import test_lock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'meta-org/recipes-org/src/libcore/tests/test_libcore.cpp'
            target.parent.mkdir(parents=True)
            target.write_text('int main() {\n    if (2 + 3 != 5) { return 1; }\n    return 0;\n}\n')
            for args in (['init', '-q'], ['add', '.'],
                         ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                          'commit', '-qm', 'initial']):
                subprocess.run(['git', '-C', tmp, *args], check=True, capture_output=True)
            target.write_text(target.read_text().replace('!= 5', '!= 6'))
            out = io.StringIO()
            with redirect_stdout(out):
                rc = test_lock.main(['--repo', tmp, '--base', 'HEAD', '--no-deviation',
                                     '--protected', *workspace_scope.resolve_protected_test_globs(root)])
            self.assertEqual(1, rc, out.getvalue())
            self.assertIn('TEST_LOCK_VIOLATION', out.getvalue())
            self.assertIn('libcore/tests/test_libcore.cpp', out.getvalue())

    def test_runner_cli_plan_has_scope_and_protection(self):
        """The run CLI accepts additions and prints both deterministic protection steps."""
        from contextlib import redirect_stdout
        import io
        from runner import devpack_run
        from runner.tests.test_devpack_run import _write_fake_driver_script, _write_test_toml
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            driver = root / 'driver.py'
            _write_fake_driver_script(driver, root / 'state.json')
            cfg = root / 'engines.toml'
            _write_test_toml(cfg, driver)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = devpack_run.main(['--config', str(cfg), '--worktree', tmp,
                                      '--task-id', 'adoption', '--protected', 'specs/**', '--dry-run'])
            self.assertEqual(0, rc, out.getvalue())
            for text in ('scope_check.py', 'test_lock.py', '**/tests/**', 'specs/**'):
                self.assertIn(text, out.getvalue())
            self.assertFalse((root / 'state.json').exists())

    def test_dispatch_plan_uses_nested_protection(self):
        """Dispatch dry-run reports scope/test-lock steps and additive CLI patterns."""
        from contextlib import redirect_stdout
        import io
        import json
        from runner import devpack_dispatch
        from runner.tests.test_devpack_dispatch import _init_git_repo
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / 'project'
            _init_git_repo(work)
            brief = root / 'brief.md'
            brief.write_text('# Brief\nUpdate the component.\n')
            out = io.StringIO()
            with redirect_stdout(out):
                rc = devpack_dispatch.main(['--engine', 'haiku', '--brief', str(brief),
                                            '--workdir', str(work), '--allow', 'src/**',
                                            '--protected', 'specs/**', '--dry-run'])
            self.assertEqual(0, rc, out.getvalue())
            plan = json.loads(out.getvalue())
            self.assertIn('**/tests/**', plan['protected'])
            self.assertIn('specs/**', plan['protected'])
            self.assertTrue(any('scope_check.py' in step for step in plan['steps']))
            self.assertTrue(any('test_lock.py' in step for step in plan['steps']))

    def test_staged_hook_rules_do_not_echo_credentials(self):
        """The hook's actual staged-index scanner rejects tokens without printing them."""
        from contextlib import redirect_stdout
        import io
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = 'sk-ant-' + 'z' * 32
            (root / 'sample.txt').write_text(token + '\n' + 'ghp_' + 'y' * 32 + '\n')
            subprocess.run(['git', 'init', '-q', tmp], check=True, capture_output=True)
            subprocess.run(['git', '-C', tmp, 'add', 'sample.txt'], check=True, capture_output=True)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = secret_pii_check.main(['--repo', tmp, '--staged'])
            self.assertEqual(1, rc, out.getvalue())
            self.assertIn('sample.txt:1:', out.getvalue())
            self.assertIn('sample.txt:2:', out.getvalue())
            self.assertNotIn(token, out.getvalue())
