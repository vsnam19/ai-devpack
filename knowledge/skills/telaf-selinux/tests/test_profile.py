"""Offline profile contract regressions; all output stays in temporary workspaces."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]


class ProfileTests(unittest.TestCase):
    def extract(self, profile=None, expected_code=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(BASE / 'tests/fixtures/app', root / 'app')
            if profile is not None:
                (root / 'profile.toml').write_text(profile)
            (root / 'project.toml').write_text('[selinux]\nplatform_profile = "profile.toml"\n')
            result = subprocess.run([sys.executable, str(BASE / 'scripts/extract_manifest.py'),
                                     'app/sample.adef', '--workspace', str(root), '--config',
                                     'project.toml', '--output', 'out'], capture_output=True, text=True)
            self.assertEqual(result.returncode, expected_code, result.stderr)
            if expected_code:
                self.assertIn('must be a string', result.stderr)
                self.assertFalse((root / 'out').exists())
                return None
            return (json.loads((root / 'out/access-manifest.yaml').read_text()),
                    (root / 'out/QUESTIONS.md').read_text())

    def test_profile_drives_conventions_and_deviations(self):
        data, questions = self.extract((BASE / 'references/example-platform-profile.toml').read_text())
        self.assertTrue(data['inherited_grants'])
        self.assertTrue(all(x['deviation_id'] and x['status'] == 'draft'
                            for x in data['inherited_grants']))
        self.assertIn('acme_', questions)
        self.assertNotIn('platform_profile.domain_pattern: unknown', questions)

    def test_missing_keys_are_unknown(self):
        for text in (None, 'prefix = "demo_"\n'):
            with self.subTest(text=text):
                _, questions = self.extract(text)
                self.assertIn('platform_profile.domain_pattern: unknown', questions)
                self.assertIn('platform_profile.interfaces: unknown', questions)
                self.assertIn('platform_profile.build: unknown', questions)

    def test_invalid_profile_is_not_guessed(self):
        self.extract('prefix = 42\n', expected_code=2)
        _, questions = self.extract('domain_pattern = "{bogus}"\n')
        self.assertIn('platform_profile.domain_pattern: unknown', questions)

    def test_invalid_nested_values(self):
        text = (BASE / 'references/example-platform-profile.toml').read_text()
        _, questions = self.extract(text.replace('te = "{app}.te"', 'te = "../escape.te"')
                                    .replace('{prefix}ipc_bind({client}, {server})', '{missing}(x)'))
        self.assertIn('platform_profile.module_files.te: unknown', questions)
        self.assertIn('platform_profile.interfaces.ipc_client: unknown', questions)

    def test_profile_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'project.toml').write_text('[selinux]\nplatform_profile = "../escape.toml"\n')
            result = subprocess.run([sys.executable, str(BASE / 'scripts/extract_manifest.py'),
                                     'missing.adef', '--workspace', str(root), '--config',
                                     'project.toml', '--output', 'out'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('escapes workspace', result.stderr)
            self.assertFalse((root / 'out').exists())

    def test_optional_tools_and_bundle_validation(self):
        text = (BASE / 'references/example-platform-profile.toml').read_text()
        _, questions = self.extract(text.replace('read-only/{app}.pp', '../{app}.pp') +
                                    '\n')
        self.assertIn('platform_profile.module_bundle_path: unknown', questions)
        self.extract('file_contexts_tool = 2\n', expected_code=2)
        _, questions = self.extract('skeleton_generator = "{bad.attr}"\nfile_contexts_tool = "{bad.attr}"\n')
        self.assertIn('platform_profile.skeleton_generator: unknown', questions)
        self.assertIn('platform_profile.file_contexts_tool: unknown', questions)

    def test_relevant_missing_mapping_is_question(self):
        text = (BASE / 'references/example-platform-profile.toml').read_text()
        _, questions = self.extract(text.replace('ipc_client = "{prefix}ipc_bind({client}, {server})"', ''))
        self.assertIn('platform_profile.interfaces.ipc_client: unknown', questions)

    def test_malformed_profile_keeps_draft_inventory(self):
        data, questions = self.extract('not valid TOML !')
        self.assertTrue(data['processes'])
        self.assertIn('cannot read TOML profile', questions)
        self.assertEqual(data['approval']['by'], '')

    def test_toml_python310_fallback_parses_profile_and_config(self):
        sys.path.insert(0, str(BASE / 'scripts'))
        try:
            import platform_profile
            original = platform_profile.tomllib
            platform_profile.tomllib = None
            try:
                profile = platform_profile.loads_toml(
                    (BASE / 'references/example-platform-profile.toml').read_text()
                )
                self.assertEqual(profile['prefix'], 'acme_')
                self.assertEqual(profile['module_files']['te'], '{app}.te')
                self.assertIn('SHAREDIR', profile['build']['include_variables'])
                self.assertIn('ipc_client', profile['interfaces'])
                proj = platform_profile.loads_toml('[selinux]\nplatform_profile = "profile.toml"\n')
                self.assertEqual(proj['selinux']['platform_profile'], 'profile.toml')
                with self.assertRaises(ValueError):
                    platform_profile.loads_toml('not valid TOML !')
            finally:
                platform_profile.tomllib = original
        finally:
            sys.path.pop(0)
