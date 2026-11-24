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
