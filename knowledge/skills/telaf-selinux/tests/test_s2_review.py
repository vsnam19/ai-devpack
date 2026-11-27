"""Offline regression coverage for the part 2 review findings."""
import json
from pathlib import Path
import sys
from unittest import mock

from test_tooling import ToolFixture, BASE

sys.path.insert(0, str(BASE / 'scripts'))
import avc_triage
import extract_manifest
import policy_common
import sel_gate


class ReviewTests(ToolFixture):
    def test_relative_names_and_unknown_classes(self):
        item = dict(id='F1', path='sample', access=['read'], processes=['sample'])
        avc = dict(domain='acme_sample_t', target_type='tmpfs_t', tclass='file',
                   name='sample', permissions=['read'])
        manifest, profile = policy_common.load(self.m, self.p)
        self.assertFalse(avc_triage.match(item, avc, manifest, profile, 'files'))
        item['path'] = '/etc/sample'
        avc['path'] = '/etc/sample'
        self.assertFalse(avc_triage.match(item, avc, manifest, profile, 'config_tree'))
        item['class'] = 'file'
        self.assertTrue(avc_triage.match(item, avc, manifest, profile, 'config_tree'))
        self.data['config_tree'] = [dict(id='C1', why='reviewed', evidence=['a:1'],
            status='approved', path='sample', access=['read'], processes=['sample'])]
        self.save()
        log = self.root / 'audit.log'
        log.write_text('avc: denied { read } for name="sample" scontext=u:r:acme_sample_t:s0 '
                       'tcontext=u:object_r:tmpfs_t:s0 tclass=file')
        result = self.run_tool('avc_triage.py', '--log', log, '--manifest', self.m, '--profile', self.p)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['skipped_items'], [dict(id=identifier,
            reason='No known object class; provide an explicit class')
            for identifier in ('C1', 'BASE1', 'P1')])
        patch = json.loads(Path(report['patch_file']).read_text())
        self.assertEqual(patch['files'][0]['path'], 'unknown')
        self.assertEqual(patch['unknowns'],
                         ['AVC-0001: What absolute path identifies the denied object?'])
        avc.pop('path')
        avc['name'] = '/etc/sample'
        self.assertFalse(avc_triage.match(item, avc, manifest, profile, 'files'))

    def test_file_operations_fail_closed(self):
        for access in (['unknown'], [], ['execute'], ['manage'], ['read', 'unknown']):
            with self.subTest(access=access):
                self.out = self.root / ('module' + str(len(list(self.root.glob('module*')))))
                self.data['files'] = [dict(id='F1', why='reviewed', evidence=['a:1'],
                    status='approved', path='/etc/sample', processes=['sample'], access=access)]
                if '[bindings.F1]' not in self.profile:
                    self.profile += '\n[bindings.F1]\ntype = "sample_file_t"\n'
                result = self.generate()
                self.assertEqual(result.returncode, 3, result.stderr)
                te = (self.out / 'sample.te').read_text()
                self.assertIn('TODO', te)
                self.assertNotIn('acme_read_data(', te)
                self.assertNotIn('acme_rw_data(', te)
                self.assertIn('F1', (self.out / 'QUESTIONS.md').read_text())

    def test_known_file_mapping(self):
        for operation in ('write', 'create', 'unlink', 'append', 'rw'):
            self.assertEqual(policy_common.mapping('files', {'access': [operation]}), ['file_rw'])
        self.assertEqual(policy_common.mapping('files', {'access': ['read']}), ['file_read'])

    def test_empty_processes_rejected(self):
        self.assertEqual(self.generate().returncode, 0)
        self.data['processes'] = []
        self.save()
        schema = json.loads((BASE / 'references/manifest.schema.json').read_text())
        self.assertEqual(schema['properties']['processes'].get('minItems'), 1)
        with self.assertRaises(ValueError):
            policy_common.load(self.m, self.p)
        result = self.gate('--release')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_transition_exact_tokens_source_and_linked(self):
        self.assertEqual(self.generate().returncode, 0)
        te = self.out / 'sample.te'
        original = te.read_text()
        te.write_text(original.replace(':process acme_sample_t;', ':process acme_sample_t_v2;'))
        self.assertIn('MISSING_TRANSITION', self.gate().stdout)
        te.write_text(original)
        self.profile = 'baseline = "baseline.json"\n' + self.profile
        self.save()
        (self.root / 'baseline.json').write_text(json.dumps({'acme_sample_t': []}))
        linked = self.root / 'policy'
        linked.write_text('synthetic')
        args = mock.Mock(manifest=self.m, profile=self.p, module=self.out,
                         linked_policy=str(linked), release=False)
        def output(command):
            if '-T' in command:
                return 'type_transition acme_admin_t acme_sample_exec_t:process acme_sample_t_v2;'
            return 'Permissive Types: 0' if command[0] == 'seinfo' else ''
        with mock.patch.object(sel_gate.shutil, 'which', return_value='/synthetic'), \
                mock.patch.object(sel_gate, 'run', side_effect=output):
            report, code = sel_gate.check(args)
        self.assertEqual(code, 1)
        self.assertIn('MISSING_LINKED_TRANSITION', report['errors'])

    def test_received_files_unreadable_continues(self):
        root = self.root
        app = root / 'sample.adef'
        app.write_text('bindings: { sample.comp.feed -> peer.feed }')
        api = root / 'feed.api'
        api.write_bytes(b'\xff')
        text = 'requires: { api: { feed = feed.api } }'
        extractor = extract_manifest.Extractor.__new__(extract_manifest.Extractor)
        extractor.root = root
        extractor.config = {}
        extractor.data = {'unknowns': [], 'fd_passing': []}
        extractor.app_definition = app
        extractor.received_files(root / 'Component.cdef', text)
        self.assertTrue(any('feed.api' in x for x in extractor.data['unknowns']))
        self.assertEqual(extractor.data['fd_passing'], [])
        for unreadable in (api, app):
            extractor.data['unknowns'] = []
            original = Path.read_text
            def read(path, *args, **kwargs):
                if path == unreadable:
                    raise PermissionError('synthetic unreadable file')
                return original(path, *args, **kwargs)
            with mock.patch.object(Path, 'read_text', read):
                extractor.received_files(root / 'Component.cdef', text)
            self.assertTrue(any(unreadable.name in x for x in extractor.data['unknowns']))
