import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]


class ToolFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.m = self.root / 'manifest.json'
        self.p = self.root / 'profile.toml'
        self.out = self.root / 'module'
        self.data = json.loads((BASE / 'references/example-manifest.yaml').read_text())
        for key, value in list(self.data.items()):
            if isinstance(value, list):
                self.data[key] = []
        self.data['approval'] = dict(by='Reviewer', date='2026-11-24', version='1')
        self.data['identity']['status'] = 'approved'
        self.data['inherited_grants'] = [dict(id='BASE1', why='reviewed base', evidence=['base:1'],
            status='approved', processes=['sample'], deviation_id='DEV1',
            grant='acme app baseline: logging IPC and creator-owned pipe access; verify linked expansion', source='base:1')]
        self.data['processes'] = [dict(id='P1', why='worker', evidence=['app.adef:1'],
                                      status='approved', executable='sample', declaration='sample')]
        self.profile = (BASE / 'references/example-platform-profile.toml').read_text() + '\n[bindings.P1]\ntype = "acme_sample_t"\n'
        self.save()

    def save(self):
        self.m.write_text(json.dumps(self.data))
        self.p.write_text(self.profile)

    def run_tool(self, name, *args, env=None):
        return subprocess.run([sys.executable, str(BASE / 'scripts' / name), *map(str, args)],
                              text=True, capture_output=True, cwd=self.root, env=env)

    def generate(self):
        self.save()
        return self.run_tool('gen_policy.py', '--manifest', self.m, '--profile', self.p, '--out', self.out)

    def gate(self, *args):
        return self.run_tool('sel_gate.py', '--manifest', self.m, '--profile', self.p,
                             '--module', self.out, *args, env={**os.environ, 'PATH': ''})


class ToolTests(ToolFixture):
    def test_generator_approval_and_unknown_mapping(self):
        self.data['processes'][0]['status'] = 'draft'
        self.assertEqual(self.generate().returncode, 2)
        self.assertFalse(self.out.exists())
        self.data['processes'][0]['status'] = 'approved'
        self.data['devices'] = [dict(id='D1', why='device', evidence=['a:1'], status='approved',
                                     path='/dev/sample', access=['read'], ioctls=[], processes=['sample'])]
        result = self.generate()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('D1', (self.out / 'QUESTIONS.md').read_text())
        self.assertIn('TODO', (self.out / 'sample.te').read_text())

    def test_generated_files_and_release_fail_closed(self):
        result = self.generate()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ('sample.te', 'sample.fc', 'sample.if', 'Makefile', 'Component.cdef'):
            self.assertTrue((self.out / name).is_file(), name)
        self.assertIn('# M:P1', (self.out / 'sample.te').read_text())
        self.assertIn('include $(SHAREDIR)/example-policy.mk', (self.out / 'Makefile').read_text())
        ordinary = self.gate()
        self.assertEqual(ordinary.returncode, 3, ordinary.stdout + ordinary.stderr)
        self.assertEqual(json.loads(ordinary.stdout)['status'], 'TOOL_MISSING')
        release = self.gate('--release')
        self.assertEqual(release.returncode, 1, release.stdout + release.stderr)
        self.assertEqual(json.loads(release.stdout)['status'], 'FAIL')

    def test_gate_source_failures(self):
        self.assertEqual(self.generate().returncode, 0)
        te = self.out / 'sample.te'
        original = te.read_text()
        for bad in ('allow acme_sample_t default_t:file read;\n',
                    'unconfined_domain(acme_sample_t) # M:P1\n'):
            te.write_text(original + bad)
            result = self.gate()
            self.assertEqual(result.returncode, 1, result.stdout)
        te.write_text('\n'.join(x for x in original.splitlines() if 'type_transition' not in x))
        self.assertEqual(self.gate().returncode, 1)
        te.write_text(original)
        (self.out / 'sample.fc').write_text('')
        self.assertEqual(self.gate().returncode, 1)

    def test_triage_four_classes_and_no_mutation(self):
        self.data['files'] = [dict(id='F1', why='read config', evidence=['a:1'], status='approved',
                                   path='/etc/config', access=['read'], processes=['sample'])]
        self.data['denied'] = [dict(id='N1', why='forbidden', evidence=['a:2'], status='approved',
                                    path='/etc/secret', access=['read'], processes=['sample'])]
        self.save()
        before = self.m.read_bytes()
        log = self.root / 'audit.log'
        log.write_text('\n'.join('avc: denied { read } for comm="sample" path="' + path +
                                 '" scontext=u:r:acme_sample_t:s0 tcontext=u:object_r:' + typ +
                                 ':s0 tclass=file' for path, typ in
                                 [('/etc/config', 'config_t'), ('/etc/secret', 'secret_t'),
                                  ('/etc/new', 'new_t'), ('/dev/bad', 'device_t')]))
        result = self.run_tool('avc_triage.py', '--log', log, '--manifest', self.m, '--profile', self.p)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual([x['classification'] for x in report['domains']['acme_sample_t']],
                         ['covered-by-approved-item', 'likely-code-bug', 'missing-in-manifest', 'label-problem'])
        patch = json.loads(Path(report['patch_file']).read_text())
        self.assertEqual(patch['files'][0]['status'], 'draft')
        self.assertEqual(self.m.read_bytes(), before)

    @unittest.skipUnless(shutil.which('sesearch') and shutil.which('seinfo'), 'SKIP linked policy: SETools absent')
    def test_real_setools_reject_invalid_linked_policy(self):
        self.profile = 'baseline = "baseline.json"\n' + self.profile
        (self.root / 'baseline.json').write_text('{}')
        self.assertEqual(self.generate().returncode, 0)
        linked = self.root / 'invalid.policy'
        linked.write_text('not a compiled policy')
        result = self.run_tool('sel_gate.py', '--manifest', self.m, '--profile', self.p,
                               '--module', self.out, '--linked-policy', linked)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('Tool failed', result.stdout)


class PilotTests(unittest.TestCase):
    def test_sim_macro_and_received_api_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'sample.adef').write_text('executables: { sample = ( comp ) }\nbindings: { sample.comp.feed -> peer.feed }\n')
            (root / 'comp').mkdir()
            (root / 'comp/Component.cdef').write_text('sources: { main.c }\nrequires: { api: { feed = feed.api } }\n')
            (root / 'comp/feed.api').write_text('FUNCTION GetStream(file stream OUT);\nFUNCTION Send(file input IN);\n')
            (root / 'comp/main.c').write_text('#ifdef PROJECT_REPLAY\nopen("/etc/fake", 0);\n#else\nopen("/etc/real", 0);\n#endif\n')
            (root / 'config.toml').write_text('[selinux]\nsim_macros = ["PROJECT_REPLAY"]\n')
            result = subprocess.run([sys.executable, str(BASE / 'scripts/extract_manifest.py'),
                                     'sample.adef', '--workspace', str(root), '--config', 'config.toml',
                                     '--output', 'out'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads((root / 'out/access-manifest.yaml').read_text())
            self.assertTrue(any(x['path'] == '/etc/fake' and x['candidate_denied'] for x in data['denied']))
            self.assertTrue(any(x['path'] == '/etc/real' for x in data['files']))
            self.assertTrue(any('GetStream' in x['object'] for x in data['fd_passing']))
            self.assertFalse(any('Send' in x['object'] for x in data['fd_passing']))

class MappingTests(ToolFixture):
    def test_all_profile_interfaces_and_external_contexts(self):
        entries = {
            'files': dict(path='/etc/config', access=['read']),
            'data_dirs': dict(path='/data/private', access=['write']),
            'ipc': dict(api='feed', role='client', peers=['peer']),
            'network': dict(proto='tcp', addr='127.0.0.1', port=1234, direction='outbound'),
            'config_tree': dict(path='sample', access=['read']),
        }
        for number, (category, fields) in enumerate(entries.items()):
            identifier = 'X' + str(number)
            self.data[category] = [dict(id=identifier, why='reviewed access', evidence=['a:1'],
                                       status='approved', processes=['sample'], **fields)]
            self.profile += '\n[bindings.' + identifier + ']\ntype = "sample_object_t"\nserver = "peer_t"\n'
        self.assertEqual(self.generate().returncode, 0)
        te = (self.out / 'sample.te').read_text()
        for macro in ('init_acme_app_domain', 'acme_admin_manage', 'acme_read_data',
                      'acme_manage_data', 'acme_ipc_bind', 'acme_create_sockets', 'acme_use_config_tree'):
            self.assertIn(macro + '(', te)
        platform_fc = (self.out / 'sample.platform.fc').read_text()
        self.assertIn('/etc/config', platform_fc)
        self.assertIn('/data/private', platform_fc)
        self.assertNotIn('/etc/config', (self.out / 'sample.fc').read_text())

    def test_file_rw_mapping_and_no_approval(self):
        self.data['files'] = [dict(id='RW', why='write', evidence=['a:1'], status='approved',
                                   path='/etc/config', access=['write'], processes=['sample'])]
        self.profile += '\n[bindings.RW]\ntype = "config_t"\n'
        self.data['approval']['by'] = ''
        self.assertEqual(self.generate().returncode, 2)
        self.data['approval']['by'] = 'Reviewer'
        self.assertEqual(self.generate().returncode, 0)
        self.assertIn('acme_rw_data(', (self.out / 'sample.te').read_text())

    def test_linked_checks_with_synthetic_tools(self):
        self.assertEqual(self.generate().returncode, 0)
        tools = self.root / 'tools'
        tools.mkdir()
        # Local deterministic command fixtures: never call installed services or policy tools.
        for name, body in {
            'sesearch': '#!/bin/sh\nif [ "$1" = "-T" ]; then echo "type_transition acme_admin_t acme_sample_exec_t:process acme_sample_t;"; exit; fi\nprintf "%s\\n" "allow acme_sample_t config_t:file read;"\n',
            'seinfo': '#!/bin/sh\nprintf "%s\\n" "Permissive Types: 0"\n',
        }.items():
            path = tools / name
            path.write_text(body)
            path.chmod(0o755)
        linked = self.root / 'policy.bin'
        linked.write_text('synthetic policy')
        baseline = self.root / 'baseline.json'
        baseline.write_text(json.dumps({'acme_sample_t': ['allow acme_sample_t config_t:file read;']}))
        # baseline belongs at top level, before the first profile table.
        self.profile = 'baseline = "baseline.json"\nenforcing_evidence = "enforcing.txt"\n' + self.profile
        (self.root / 'enforcing.txt').write_text('Enforcing\n')
        self.save()
        def linked_gate():
            return self.run_tool('sel_gate.py', '--manifest', self.m, '--profile', self.p,
                                 '--module', self.out, '--linked-policy', linked, '--release',
                                 env={**os.environ, 'PATH': str(tools)})
        result = linked_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        baseline.write_text('{}')
        self.assertEqual(linked_gate().returncode, 1)
        baseline.write_text(json.dumps({'acme_sample_t': ['allow acme_sample_t config_t:file read;']}))
        (tools / 'seinfo').write_text('#!/bin/sh\nprintf "%s\\n" "Permissive Types: 1" " acme_sample_t"\n')
        self.assertEqual(linked_gate().returncode, 1)

    def test_generator_rejects_unsafe_profile_and_bad_identifiers(self):
        self.profile = self.profile.replace('init_{prefix}app_domain({domain}, {exec_type})', 'permissive {domain};')
        self.assertEqual(self.generate().returncode, 2)
        self.assertFalse(self.out.exists())

    def test_linked_denial_queries_fail_on_inherited_allow(self):
        self.data['denied'] = [dict(id='N1', why='no reads', evidence=['a:1'], status='approved',
                                    path='/etc/secret', access=['read'], processes=['sample'])]
        self.profile = 'baseline = "baseline.json"\n' + self.profile
        self.profile += '\n[bindings.N1]\ntype = "secret_t"\nclass = "file"\npermissions = ["read"]\n'
        self.assertEqual(self.generate().returncode, 0)
        (self.root / 'baseline.json').write_text(json.dumps({'acme_sample_t': ['allow acme_sample_t secret_t:file read;']}))
        tools = self.root / 'tools'
        tools.mkdir()
        for name, body in [('sesearch', 'allow acme_sample_t secret_t:file read;'), ('seinfo', 'Permissive Types: 0')]:
            path = tools / name
            path.write_text('#!/bin/sh\nif [ "$1" = "-T" ]; then echo "type_transition acme_admin_t acme_sample_exec_t:process acme_sample_t;"; exit; fi\nprintf "%s\\n" "' + body + '"\n')
            path.chmod(0o755)
        linked = self.root / 'policy'
        linked.write_text('synthetic')
        result = self.run_tool('sel_gate.py', '--manifest', self.m, '--profile', self.p,
                               '--module', self.out, '--linked-policy', linked,
                               env={**os.environ, 'PATH': str(tools)})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('DENIED_ALLOWED N1', result.stdout)

    def test_platform_ban_requires_approved_deviation_on_line(self):
        self.profile = self.profile.replace('banned = [', 'banned = ["acme_admin_manage", ')
        self.assertEqual(self.generate().returncode, 0)
        result = self.gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn('BANNED acme_admin_manage', result.stdout)
        te = self.out / 'sample.te'
        te.write_text(te.read_text().replace('acme_admin_manage(acme_sample_t) # M:P1',
                                            'acme_admin_manage(acme_sample_t) # M:P1 D:DEV1'))
        self.assertEqual(self.gate().returncode, 3)

    def test_skeleton_merges_types_but_never_grants(self):
        hook = self.root / 'skeleton.py'
        hook.write_text('from pathlib import Path\nimport sys\nPath(sys.argv[1], "generated.te").write_text("type extra_t;\\nallow extra_t device_t:file write;\\n")\n')
        import shlex
        command = shlex.join([sys.executable, str(hook), '{output}'])
        self.profile = ('platform_root = ' + json.dumps(str(self.root)) + '\nskeleton_generator = ' +
                        json.dumps(command) + '\n' + self.profile)
        self.assertEqual(self.generate().returncode, 0)
        te = (self.out / 'sample.te').read_text()
        self.assertIn('type extra_t; # M:P1', te)
        self.assertNotIn('allow extra_t', te)


if not (shutil.which('sesearch') and shutil.which('seinfo')):
    print('SKIP real linked-policy tests: SETools absent; synthetic linked checks and release fail-closed tests still run.')

class LinkedXpermTests(ToolFixture):
    def test_xperm_outside_allow_query_cannot_escape_baseline(self):
        self.profile = 'baseline = "baseline.json"\n' + self.profile
        self.assertEqual(self.generate().returncode, 0)
        (self.root / 'baseline.json').write_text(json.dumps({'acme_sample_t': []}))
        linked = self.root / 'policy'
        linked.write_text('synthetic')
        tools = self.root / 'tools'
        tools.mkdir()
        (tools / 'sesearch').write_text('#!/bin/sh\ncase "$1" in\n-T) echo "type_transition acme_admin_t acme_sample_exec_t:process acme_sample_t;";;\n--allowxperm) echo "allowxperm acme_sample_t device_t:chr_file ioctl 0x1234;";;\nesac\n')
        (tools / 'seinfo').write_text('#!/bin/sh\necho "Permissive Types: 0"\n')
        for path in tools.iterdir():
            path.chmod(0o755)
        result = self.run_tool('sel_gate.py', '--manifest', self.m, '--profile', self.p,
                               '--module', self.out, '--linked-policy', linked,
                               env={**os.environ, 'PATH': str(tools)})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('BASELINE_MISMATCH', result.stdout)

    def test_conflicting_contexts_are_incomplete(self):
        self.data['processes'].append(dict(id='P2', why='second worker', evidence=['a:1'],
                                          status='approved', executable='second', declaration='second'))
        self.profile += '\n[bindings.P2]\ntype = "acme_second_t"\n'
        result = self.generate()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('conflicting context', (self.out / 'QUESTIONS.md').read_text())
