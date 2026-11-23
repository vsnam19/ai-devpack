"""Adoption documentation regressions for installed commands and release boundaries."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AdoptionDocumentationTests(unittest.TestCase):
    """Keep installation guidance aligned with project-facing entry points."""

    def test_installed_quickstart_and_layout(self):
        """Quickstart resolves the pack path and generated files are explicitly ignored."""
        text = (ROOT / 'README.md').read_text()
        quickstart = text.split('## Quickstart', 1)[1].split('## Versioning', 1)[0]
        self.assertIn('"$DEVPACK_ROOT/runner/devpack_run.py"', quickstart)
        self.assertNotIn('--config runner/engines.example.toml', quickstart)
        layout = text.split('## Layout', 1)[1].split('\n## ', 1)[0]
        for script in ('repomap.py', 'devpack_submit.py', 'redaction.py', 'safe_git.py',
                       'workspace_scope.py', 'brief_lint.py'):
            self.assertIn('`' + script + '`', layout)
        install = text.split('3. Create', 1)[1].split('### Adapting', 1)[0]
        for item in ('/gate_receipt.json', '/runs/', '.gitignore', 'DEVPACK_BASE', '--allow-unowned'):
            self.assertIn(item, install)

    def test_f1_to_f3_are_released_and_f5c_is_unreleased(self):
        """Already released adoption fixes belong to the dated 0.2.1 entry."""
        text = (ROOT / 'CHANGELOG.md').read_text()
        unreleased, release = text.split('## [0.2.1] - 2026-11-23', 1)
        self.assertIn('component-specific tests', unreleased)
        self.assertNotIn('- Fail closed on unexpectedly empty scans', unreleased)
        self.assertIn('- Fail closed on unexpectedly empty scans', release)
        self.assertIn('### Security', unreleased)

    def test_component_config_and_legacy_guidance(self):
        """Mixed naming, relative paths, and the project gate's workspace requirement are documented."""
        text = (ROOT / 'README.md').read_text()
        for item in ('addValues()', 'add_values()', 'preflight_config', 'protected_test_globs', 'test_globs'):
            self.assertIn(item, text)
        policy = (ROOT / 'policy/workspace_scope.md').read_text()
        self.assertIn('project gate template requires an explicit `[workspace]`', policy)
