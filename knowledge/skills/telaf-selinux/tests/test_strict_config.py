"""Strict input checks, with no workspace output outside temporary directories."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('strict_profile', BASE / 'scripts/platform_profile.py')
PROFILE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROFILE)


class StrictConfigTests(unittest.TestCase):
    def test_fallback_rejects_unsupported_or_malformed_input(self):
        values = ['bare', '"open', "'open", '[1, 2', 'a.b = 1', '{a = 1}',
                  '"""multi"""', '[1,,2]', '[1 2]', '1.2', '[[]]', '"x" junk',
                  '"bad\\q"', '01', '["open\nclose"]']
        with patch.object(PROFILE, 'tomllib', None):
            for value in values:
                text = value if value.startswith('a.b') else 'key = ' + value
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'line 2'):
                    PROFILE.loads_toml('# comment\n' + text)
            for text in ('[[a]]', '[a..b]', 'x = 1\nx = 2', '[a]\n[a]'):
                with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'line'):
                    PROFILE.loads_toml(text)

    def test_fallback_subset(self):
        with patch.object(PROFILE, 'tomllib', None):
            self.assertEqual(PROFILE.loads_toml(
                '[a.b] # table\nx = [\n"#", \'literal\', -12, true, false,\n] # end\n'),
                {'a': {'b': {'x': ['#', 'literal', -12, True, False]}}})
            self.assertEqual(PROFILE.loads_toml('x = ["ends]"\n]'), {'x': ['ends]']})

    def test_wrong_types_exit_two(self):
        configs = [[], {'selinux': []}]
        configs += [{'selinux': {key: value}} for key, value in (
            ('app_globs', '*.adef'), ('app_globs', [1]), ('exclude', 'x'),
            ('exclude', [False]), ('platform_root', 1), ('sdk_policy_include', []),
            ('linked_policy', False), ('platform_profile', {}), ('manifest_name', 1))]
        profiles = ['prefix = 42', 'interfaces = []', '[interfaces]\nx = 42',
                    'banned = "x"', '[build]\ninclude_variables = [1]',
                    '[module_files]\nte = 42']
        for config, profile in [(c, None) for c in configs] + [
                ({'selinux': {'platform_profile': 'profile.toml'}}, p) for p in profiles]:
            with self.subTest(config=config, profile=profile), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'config.json').write_text(json.dumps(config))
                if profile is not None:
                    (root / 'profile.toml').write_text(profile)
                result = subprocess.run([sys.executable, str(BASE / 'scripts/extract_manifest.py'),
                    'missing.adef', '--workspace', directory, '--config', 'config.json',
                    '--output', 'out'], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('must be', result.stderr)
                self.assertFalse((root / 'out').exists())
