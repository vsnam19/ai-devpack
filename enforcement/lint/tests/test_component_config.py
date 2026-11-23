"""Behavior coverage for different coding conventions inside one workspace."""
from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest

from enforcement.lint import preflight_lint


class ComponentConfigTests(unittest.TestCase):
    """Run the linter across camelCase and snake_case components together."""

    def test_mixed_naming_and_explicit_override(self):
        """A component config applies only to its files; explicit --config wins."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.devpack').mkdir()
            for name, function in [('camel', 'addValues'), ('snake', 'add_values')]:
                component = root / 'src' / name
                component.mkdir(parents=True)
                (component / 'unit.cpp').write_text(
                    '/** @file unit.cpp\n * @brief Example arithmetic.\n */\n'
                    '/** @brief Return a value.\n * @return The value.\n */\n'
                    'int ' + function + '() { return 1; }\n')
            cfg = root / '.devpack/project.toml'
            cfg.write_text('[workspace]\nroot="src"\n'
                           '[[component]]\nname="camel"\npath="camel"\n'
                           '[[component]]\nname="snake"\npath="snake"\n'
                           'preflight_config="snake.toml"\n')
            snake = root / 'snake.toml'
            snake.write_text('[naming]\nfunction_regex="^[a-z][a-z0-9_]*$"\n')
            args = ['--repo-root', str(root), '--all-components', '--no-cache',
                    '--skip-clang-tidy', '--allow-missing-tools']
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(out):
                rc = preflight_lint.main(args)
            self.assertEqual(0, rc, out.getvalue())
            self.assertNotIn('NAMING-CONVENTION', out.getvalue())
            # Auto-detection gives the same result without an explicit path.
            cfg.write_text(cfg.read_text().replace('preflight_config="snake.toml"\n', ''))
            snake.rename(root / 'src/snake/preflight.toml')
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(out):
                rc = preflight_lint.main(args)
            self.assertEqual(0, rc, out.getvalue())
            # An explicit global config deliberately applies to both components.
            global_cfg = root / 'global.toml'
            global_cfg.write_text('[naming]\nfunction_regex="^[a-z][A-Za-z0-9]*$"\n')
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(out):
                rc = preflight_lint.main([*args, '--config', str(global_cfg)])
            self.assertEqual(1, rc, out.getvalue())
            self.assertIn('add_values', out.getvalue())
