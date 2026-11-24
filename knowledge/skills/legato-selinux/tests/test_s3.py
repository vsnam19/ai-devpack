"""Offline API search and FD direction regressions."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from test_extract import ExtractTests, BASE

sys.path.insert(0, str(BASE / 'scripts'))
import avc_triage


class ApiTests(ExtractTests):
    def fixture(self, root):
        for component, role in [('client', 'requires'), ('server', 'provides')]:
            directory = root / component
            directory.mkdir()
            (directory / 'Component.cdef').write_text(
                role + ': { api: { feed = feed.api } }')
        (root / 'demo.adef').write_text(
            'executables: {\n client = ( client )\n server = ( server )\n}\n'
            'bindings: { client.client.feed -> server.server.feed }')
        (root / 'interfaces').mkdir()
        (root / 'interfaces/feed.api').write_text(
            'FUNCTION Fetch(file result OUT);\nFUNCTION Store(file input IN);\n'
            'HANDLER Update(file incoming IN);\nEVENT Changed(Update callback);\n')

    def extract_config(self, root, config):
        (root / 'config.json').write_text(json.dumps({'selinux': config}))
        return self.run_extract(root, 'demo.adef', '--config', 'config.json')

    def test_search_and_symmetric_directions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            data = self.extract_config(root, {'api_search_paths': ['interfaces']})
            self.assertEqual({(x['processes'][0], x['function'], x['direction'])
                              for x in data['fd_passing']},
                             {('client', 'Fetch', 'receive'), ('client', 'Update', 'receive'),
                              ('server', 'Store', 'receive')})
            self.assertTrue(all(x['api'] == 'feed' and x['status'] == 'draft'
                                and x['evidence'][0].startswith('interfaces/feed.api:')
                                for x in data['fd_passing']))

    def test_sdef_search(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            (root / 'system.sdef').write_text('interfaceSearch: { interfaces }')
            data = self.extract_config(root, {'sdef_globs': ['*.sdef']})
            self.assertEqual(len(data['fd_passing']), 3)

    def test_no_global_fallback_and_tried_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            data = self.extract_config(root, {'api_search_paths': ['missing']})
            self.assertEqual(data['fd_passing'], [])
            self.assertTrue(any('feed.api' in q and 'client/feed.api' in q
                                and 'missing/feed.api' in q for q in data['unknowns']))

    def test_precedence_and_containment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'workspace'
            root.mkdir()
            self.fixture(root)
            (root / 'second').mkdir()
            (root / 'second/feed.api').write_text('FUNCTION Wrong(file fd OUT);')
            (root / 'client/feed.api').write_text('FUNCTION Local(file fd OUT);')
            data = self.extract_config(root, {'api_search_paths': ['interfaces', 'second']})
            self.assertEqual({x['function'] for x in data['fd_passing']}, {'Local', 'Store'})
            (root / 'out/access-manifest.yaml').unlink()
            (root / 'out/QUESTIONS.md').unlink()
            (root / 'client/feed.api').unlink()
            (root / 'escape').symlink_to(Path(directory), target_is_directory=True)
            data = self.extract_config(root, {'api_search_paths': ['interfaces'],
                                              'sdef_globs': ['*.sdef']})
            self.assertEqual(len(data['fd_passing']), 3)
            import subprocess
            for config in ({'api_search_paths': ['escape']},
                           {'sdef_globs': ['../*.sdef']},
                           {'api_search_paths': 'interfaces'}):
                (root / 'config.json').write_text(json.dumps({'selinux': config}))
                result = subprocess.run([sys.executable, str(BASE / 'scripts/extract_manifest.py'),
                    'demo.adef', '--workspace', str(root), '--config', 'config.json',
                    '--output', 'new'], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse((root / 'new').exists())

    def test_unknown_class_sentinel(self):
        item = {'id': 'FD1', 'class': 'unknown'}
        self.assertIsNone(avc_triage.object_class(item, {}, 'fd_passing'))
        self.assertIsNone(avc_triage.object_class({'id': 'F1'},
            {'bindings': {'F1': {'class': 'unknown'}}}, 'files'))
