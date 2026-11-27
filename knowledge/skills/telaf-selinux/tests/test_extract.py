import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
SCRIPT = BASE / 'scripts/extract_manifest.py'


class ExtractTests(unittest.TestCase):
    def run_extract(self, root, *args):
        result = subprocess.run([sys.executable, str(SCRIPT), '--workspace', str(root),
                                 '--output', 'out', *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((root / 'out/access-manifest.yaml').read_text())

    def test_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(BASE / 'tests/fixtures/app', root / 'app')
            data = self.run_extract(root, 'app/sample.adef')
            self.assertEqual(data['identity']['runs_as'], 'sample-user')
            self.assertTrue(data['identity']['sandboxed'])
            self.assertTrue(data['ipc'])
            self.assertTrue(data['devices'])
            self.assertTrue(any(x.get('operation') == 'ioctl' for x in data['devices']))
            self.assertTrue(any(x.get('name') == 'MODE' for x in data['env_allowed']))
            denied = [x for x in data['denied'] if x.get('path') == '/tmp/fake-device']
            self.assertEqual(len(denied), 1)
            self.assertTrue(denied[0]['candidate_denied'])
            self.assertEqual(denied[0]['evidence'], ['app/components/worker/worker.c:7'])
            self.assertTrue(any(x.get('path') == '/dev/sample' for x in data['devices']))
            self.assertTrue(all(x['status'] == 'draft' and x['why'] == ''
                                for v in data.values() if isinstance(v, list)
                                for x in v if isinstance(x, dict)))
            self.assertIn('selinux-manifest', (root / 'out/QUESTIONS.md').read_text())

    def test_missing_and_config_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config.json').write_text(json.dumps({'selinux': {'app_globs': ['apps/*.adef']}}))
            (root / 'apps').mkdir()
            (root / 'apps/demo.adef').write_text('executables: { main = ( missing ) }')
            data = self.run_extract(root, '--app-name', 'demo', '--config', 'config.json')
            self.assertTrue(data['unknowns'])
            self.assertEqual(data['approval']['by'], '')
            self.assertEqual(data['identity']['runs_as'], 'unknown')

    def test_example_schema_and_rejections(self):
        spec = importlib.util.spec_from_file_location('validator', BASE / 'tests/validate_schema.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        schema = json.loads((BASE / 'references/manifest.schema.json').read_text())
        example = json.loads((BASE / 'references/example-manifest.yaml').read_text())
        module.validate(example, schema, schema)
        broken = json.loads(json.dumps(example))
        broken['files'][0]['status'] = 'automatic'
        with self.assertRaises(ValueError):
            module.validate(broken, schema, schema)
        del broken['files'][0]['evidence']
        with self.assertRaises(ValueError):
            module.validate(broken, schema, schema)

    def test_behaviour_ab(self):
        grader = BASE / 'tests/graders/approval/check.sh'
        for name, code in [('fixtures/approval/input', 1), ('expected/approval', 0)]:
            with tempfile.TemporaryDirectory() as directory:
                shutil.copytree(BASE / 'tests' / name, Path(directory) / 'candidate')
                result = subprocess.run(['bash', str(grader), str(Path(directory) / 'candidate')],
                                        capture_output=True)
                self.assertEqual(result.returncode, code, result.stderr)

    def test_allman_style_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            comp = root / 'app/components/worker'
            comp.mkdir(parents=True)
            (comp / 'worker.c').write_text('int main(void) { return 0; }\n')
            (comp / 'Component.cdef').write_text(
                'sources:\n'
                '// component sources\n'
                '\n'
                '{\n'
                '    worker.c\n'
                '}\n'
                'requires:\n'
                '{\n'
                '    file:\n'
                '    {\n'
                '        [r] /etc/comp.conf /etc/comp.conf\n'
                '    }\n'
                '}\n'
            )
            (root / 'app/allman.adef').write_text(
                'sandboxed: true\n'
                'username: allman-user\n'
                'executables:\n'
                '/* executable list */\n'
                '\n'
                '{\n'
                '    worker = ( components/worker )\n'
                '}\n'
                'processes:\n'
                '{\n'
                '    run:\n'
                '    {\n'
                '        (worker)\n'
                '    }\n'
                '    envVars:\n'
                '    {\n'
                '        MODE = production\n'
                '    }\n'
                '}\n'
                'requires:\n'
                '// required resources\n'
                '{\n'
                '    file:\n'
                '\n'
                '    {\n'
                '        [r] /etc/sample.conf /etc/sample.conf\n'
                '    }\n'
                '    dir:\n'
                '    {\n'
                '        [rw] /data/sample /data/sample\n'
                '    }\n'
                '    device:\n'
                '    {\n'
                '        [rw] /dev/sample /dev/sample\n'
                '    }\n'
                '}\n'
                'bindings:\n'
                '{\n'
                '    worker.worker.service -> peer.service\n'
                '}\n'
                'extern:\n'
                '{\n'
                '    published = worker.worker.published\n'
                '}\n'
                'capabilities:\n'
                '{\n'
                '    CAP_NET_BIND_SERVICE\n'
                '}\n'
            )
            data = self.run_extract(root, 'app/allman.adef')
            self.assertEqual([p['executable'] for p in data['processes']], ['worker'])
            self.assertIn('/etc/sample.conf', [f['path'] for f in data['files']])
            self.assertIn('/etc/comp.conf', [f['path'] for f in data['files']])
            self.assertIn('/data/sample', [d['path'] for d in data['data_dirs']])
            self.assertIn('/dev/sample', [d['path'] for d in data['devices']])
            self.assertEqual(len(data['ipc']), 2)
            self.assertEqual([c['name'] for c in data['capabilities']], ['CAP_NET_BIND_SERVICE'])
            self.assertEqual([e['name'] for e in data['env_allowed']], ['MODE'])

    def test_denied_entries_match_schema(self):
        spec = importlib.util.spec_from_file_location('validator', BASE / 'tests/validate_schema.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        schema = json.loads((BASE / 'references/manifest.schema.json').read_text())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            comp = root / 'app/components/worker'
            comp.mkdir(parents=True)
            (comp / 'worker.c').write_text(
                '#ifdef SIMULATION\n'
                'void sim(void) {\n'
                '    getenv("MODE");\n'
                '    socket(AF_INET, SOCK_STREAM, 0);\n'
                '}\n'
                '#endif\n'
            )
            (comp / 'Component.cdef').write_text('sources: { worker.c }\n')
            (root / 'app/sample.adef').write_text('executables: { worker = ( components/worker ) }\n')
            data = self.run_extract(root, 'app/sample.adef')
            self.assertEqual(len(data['denied']), 2)
            self.assertTrue(all('path' in entry for entry in data['denied']))
            module.validate(data, schema, schema)

    def test_app_name_discovery_rejects_escape_globs_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'ws'
            outside = Path(directory) / 'outside.adef'
            outside.write_text('executables: { ext = ( missing ) }\n')
            (root / 'apps').mkdir(parents=True)
            for bad_glob in ('/etc/*.adef', '../apps/*.adef', 'apps/../*.adef'):
                (root / 'config.json').write_text(json.dumps({'selinux': {'app_globs': [bad_glob]}}))
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), '--workspace', str(root),
                     '--app-name', 'demo', '--config', 'config.json', '--output', 'out'],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('workspace-relative', result.stderr)
            (root / 'config.json').write_text(json.dumps({'selinux': {'app_globs': ['apps/*.adef']}}))
            (root / 'apps/demo.adef').symlink_to(outside)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), '--workspace', str(root),
                 '--app-name', 'demo', '--config', 'config.json', '--output', 'out'],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('escapes workspace', result.stderr)
            self.assertFalse((root / 'out').exists())
