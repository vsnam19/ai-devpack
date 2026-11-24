"""Offline behavioural coverage for the host dispatch broker."""

import importlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from runner.tests.brief_fixtures import valid_brief

ROOT = Path(__file__).resolve().parents[2]


class BrokerTests(unittest.TestCase):
    """Verify policy and credential separation with a fake worker executable."""

    def setUp(self):
        """Create a committed project and host-owned broker configuration."""
        self.assertTrue((ROOT / 'runner/devpack_broker.py').is_file(), 'host broker is missing')
        self.broker_module = importlib.import_module('runner.devpack_broker')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        (self.work / 'result.txt').write_text('before')
        (self.work / '.gitignore').write_text('.devpack/\nbriefs/\nruns/\n')
        for args in (['init', '-q'], ['add', '.'], ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base']):
            subprocess.run(['git', *args], cwd=self.work, check=True, capture_output=True)
        (self.work / 'briefs').mkdir()
        self.brief_text = valid_brief('Update result.txt', ['result.txt'])
        (self.work / 'briefs/job.md').write_text(self.brief_text)
        self.credentials = self.root / 'credentials.json'
        self.credentials.write_text('{"accessToken":"fake-broker-secret"}')
        self.worker = self.root / 'fake-worker'
        self.worker.write_text(
            '#!/usr/bin/env python3\nimport json,os,pathlib\np=pathlib.Path\n'
            'assert not p(".devpack").exists()\n'
            'assert "DEVPACK_CLAUDE_CREDENTIALS" not in os.environ\n'
            'assert "fake-broker-secret" in (p(os.environ["CLAUDE_CONFIG_DIR"])/".credentials.json").read_text()\n'
            'p("result.txt").write_text("after")\n'
            'print(json.dumps({"type":"result","result":"authenticated"}))\n'
        )
        self.worker.chmod(0o755)
        self.broker = self.broker_module.Broker(
            self.work, self.root / 'runs', credentials_path=self.credentials,
            isolation='local', allow_unsandboxed_local=True, agent_bin=str(self.worker), gate_cmd='true',
            mode='autonomous',
        )
        self.addCleanup(self.broker.close)

    def request(self, **updates):
        """Process a request and return its compact response."""
        request = {'engine': 'haiku', 'brief': 'briefs/job.md', 'allow': ['result.txt']}
        request.update(updates)
        return self.broker.execute(request)

    def test_worker_authentication_and_logs(self):
        """The broker provides worker credentials and records request and response."""
        code, payload = self.request()
        self.assertEqual(0, code, payload)
        self.assertEqual('ok', payload['status'])
        self.assertEqual('authenticated', payload['final_text_tail'])
        self.assertEqual('after', (self.work / 'result.txt').read_text())
        self.assertTrue(list((self.root / 'runs').rglob('*.request.json')))
        self.assertTrue(list((self.root / 'runs').rglob('*.response.json')))
        self.assertNotIn('fake-broker-secret', json.dumps(payload))

    def test_tampered_client_refused(self):
        """Client hash mismatch prevents any worker execution."""
        self.broker.client_path.chmod(0o644)
        self.broker.client_path.write_text('# tampered')
        code, payload = self.request()
        self.assertEqual(1, code)
        self.assertIn('client', payload['error'])
        self.assertEqual('before', (self.work / 'result.txt').read_text())

    def test_path_traversal_and_symlink_refused(self):
        """Both lexical traversal and resolved symlink escapes fail closed."""
        (self.root / 'outside.md').write_text('outside')
        (self.work / 'briefs/escape.md').symlink_to(self.root / 'outside.md')
        for brief in ('../outside.md', 'briefs/../briefs/job.md', 'briefs/escape.md', str(self.root / 'outside.md')):
            with self.subTest(brief=brief):
                code, payload = self.request(brief=brief)
                self.assertEqual(1, code)
                self.assertEqual('refused', payload['status'])
        self.assertEqual('before', (self.work / 'result.txt').read_text())

    def test_worker_control_file_is_scope_violation(self):
        """Ignored .devpack files still violate worker scope."""
        body = self.worker.read_text().replace(
            'p("result.txt").write_text("after")',
            'p(".devpack").mkdir(); p(".devpack/attack.py").write_text("attack")',
        )
        self.worker.write_text(body)
        code, payload = self.request()
        self.assertEqual(1, code)
        self.assertEqual('violation', payload['scope'])
        self.assertIn('.devpack/attack.py', payload['changed_files'])
        self.assertFalse((self.work / '.devpack/attack.py').exists())

    def test_unknown_engine_and_execution_overrides_refused(self):
        """Requests cannot select arbitrary engines or host executable options."""
        for update in ({'engine': 'unknown'}, {'credentials': 'stolen'}, {'gate_cmd': 'echo unsafe'}):
            code, payload = self.request(**update)
            self.assertEqual(1, code)
            self.assertEqual('refused', payload['status'])
        self.assertEqual('before', (self.work / 'result.txt').read_text())

    def test_data_class_policy_refused_before_execution(self):
        """The startup project policy blocks cloud jobs for confidential projects."""
        self.broker.close()
        (self.work / '.devpack/project.toml').write_text('data_class = "confidential"\n')
        self.broker = self.broker_module.Broker(self.work, self.root / 'runs')
        self.addCleanup(self.broker.close)
        code, payload = self.request()
        self.assertEqual(1, code)
        self.assertIn('not permitted', payload['error'])
        self.assertEqual('before', (self.work / 'result.txt').read_text())

    def test_singleton_and_concurrency_limit(self):
        """Only one broker and one active execution can own a project."""
        with self.assertRaisesRegex(ValueError, 'already running'):
            self.broker_module.Broker(self.work, self.root / 'other-runs')
        self.broker._execution_lock.acquire()
        try:
            code, payload = self.request()
        finally:
            self.broker._execution_lock.release()
        self.assertEqual(1, code)
        self.assertIn('concurrency limit', payload['error'])
        self.assertEqual('before', (self.work / 'result.txt').read_text())

    def test_full_dispatch_defaults_to_queue_client(self):
        """The full CLI submits through the broker and prints exactly one compact response."""
        import time
        command = ['python3', str(ROOT / 'runner/devpack_dispatch.py'), '--engine', 'haiku',
                   '--brief', 'briefs/job.md', '--workdir', str(self.work),
                   '--allow', 'result.txt', '--timeout', '5']
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env={**os.environ, 'DEVPACK_BROKER': '1'})
        try:
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                self.broker.poll()
                time.sleep(0.02)
            stdout, stderr = process.communicate(timeout=2)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(0, process.returncode, (stdout, stderr))
        self.assertEqual(1, len(stdout.splitlines()))
        payload = json.loads(stdout)
        self.assertEqual('ok', payload['status'])
        self.assertEqual('after', (self.work / 'result.txt').read_text())
        self.assertEqual(1, len(list(self.broker.queue.glob('*.response.json'))))

    def test_oversized_queue_request_refused(self):
        """The broker bounds request size before parsing or launching a worker."""
        request_id = 'a' * 32
        (self.broker.queue / (request_id + '.request.json')).write_text('x' * 65537)
        self.broker.poll()
        payload = json.loads((self.broker.queue / (request_id + '.response.json')).read_text())
        self.assertEqual('refused', payload['status'])
        self.assertEqual('before', (self.work / 'result.txt').read_text())

    def test_brief_without_expected_output_refused_with_findings(self):
        """A brief lacking an Expected output section is refused and its lint findings are returned."""
        (self.work / 'briefs/no-expected.md').write_text(
            self.brief_text.replace('## Expected output', '## Notes'))
        code, payload = self.request(brief='briefs/no-expected.md')
        self.assertEqual(1, code)
        self.assertEqual('refused', payload['status'])
        self.assertIn('brief_lint rejected brief', payload['error'])
        findings = [(item['rule'], item['severity'], item['message']) for item in payload['brief_lint']]
        self.assertIn(('BRIEF-HEADINGS', 'error', 'Missing required heading: Expected output'), findings)
        self.assertEqual('before', (self.work / 'result.txt').read_text())

    def test_internal_symlink_brief_refused(self):
        """Even symlinks pointing inside the project cannot supply a brief."""
        (self.work / 'briefs/link.md').symlink_to('job.md')
        code, payload = self.request(brief='briefs/link.md')
        self.assertEqual(1, code)
        self.assertEqual('refused', payload['status'])

    def test_brief_snapshot_precedes_copy(self):
        """A brief swapped during the product copy cannot change the worker prompt."""
        original_copy = self.broker_module.shutil.copytree
        outside = self.root / 'outside.md'
        outside.write_text('UNTRUSTED REPLACEMENT')
        prompts = []

        def swap_copy(*args, **kwargs):
            """Replace the brief after validation, before dispatch."""
            brief = self.work / 'briefs/job.md'
            brief.unlink()
            brief.symlink_to(outside)
            return original_copy(*args, **kwargs)

        def dispatch(**kwargs):
            """Capture the actual prompt passed across the dispatch boundary."""
            prompts.append(kwargs['brief_path'].read_text())
            return 0, {'status': 'ok', 'scope': 'ok', 'gate': 'ok', 'changed_files': []}

        with mock.patch.object(self.broker_module.shutil, 'copytree', side_effect=swap_copy), \
             mock.patch.object(self.broker_module, 'dispatch_job', side_effect=dispatch):
            code, payload = self.request()
        self.assertEqual(0, code, payload)
        self.assertEqual([self.brief_text], prompts)

    def test_worker_dispatch_receives_sandbox_repo_as_trusted_git_dir(self):
        """The broker passes the sandbox repository it created itself as the trusted git dir."""
        captured = []

        def dispatch(**kwargs):
            """Record the trusted git dir and confirm it is the sandbox repository's .git directory."""
            trusted = kwargs['trusted_git_dir']
            captured.append((trusted, kwargs['workdir'], trusted.is_dir()))
            return 0, {'status': 'ok', 'scope': 'ok', 'gate': 'ok', 'changed_files': []}

        with mock.patch.object(self.broker_module, 'dispatch_job', side_effect=dispatch):
            code, payload = self.request()
        self.assertEqual(0, code, payload)
        self.assertEqual(1, len(captured))
        trusted, workdir, is_dir = captured[0]
        self.assertTrue(is_dir)
        self.assertEqual('.git', trusted.name)
        self.assertEqual(Path(workdir).resolve(), trusted.parent)

    def test_destination_parent_swap_refused(self):
        """A destination parent replaced just before I/O cannot redirect a write."""
        nested = self.work / 'nested'
        nested.mkdir()
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'output.txt').write_text('protected')
        original_open = os.open
        original_copy = self.broker_module.shutil.copyfile
        swapped = []

        def swap():
            """Swap the destination directory once at its I/O boundary."""
            if not swapped:
                nested.rename(self.work / 'old-nested')
                nested.symlink_to(outside, target_is_directory=True)
                swapped.append(True)

        def opening(path, flags, *args, **kwargs):
            """Race the secure directory walk at the destination parent."""
            if str(path) == 'nested' and kwargs.get('dir_fd') is not None:
                swap()
            return original_open(path, flags, *args, **kwargs)

        def copying(source, destination, *args, **kwargs):
            """Race the old path-based copy after its containment checks."""
            if Path(destination) == nested / 'output.txt':
                swap()
            return original_copy(source, destination, *args, **kwargs)

        def dispatch(**kwargs):
            """Provide a worker output without invoking a model."""
            target = kwargs['workdir'] / 'nested/output.txt'
            target.write_text('worker output')
            return 0, {'status': 'ok', 'scope': 'ok', 'gate': 'ok',
                       'changed_files': ['nested/output.txt']}

        with mock.patch.object(self.broker_module, 'dispatch_job', side_effect=dispatch), \
             mock.patch.object(self.broker_module.os, 'open', side_effect=opening), \
             mock.patch.object(self.broker_module.shutil, 'copyfile', side_effect=copying):
            code, payload = self.request(allow=['nested/**'])
        self.assertTrue(swapped)
        self.assertEqual(1, code, payload)
        self.assertEqual('refused', payload['status'])
        self.assertEqual('protected', (outside / 'output.txt').read_text())

    def test_copy_back_deletion_and_new_parents(self):
        """Regular output creates parents and missing output deletes only its own name."""
        source = self.root / 'source'
        source.mkdir()
        (source / 'nested').mkdir()
        (source / 'nested/new.txt').write_text('new content')
        self.broker_module.copy_back(source, self.work, Path('nested/new.txt'))
        self.assertEqual('new content', (self.work / 'nested/new.txt').read_text())
        self.broker_module.copy_back(source, self.work, Path('result.txt'))
        self.assertFalse((self.work / 'result.txt').exists())
        outside = self.root / 'protected.txt'
        outside.write_text('protected')
        (self.work / 'link.txt').symlink_to(outside)
        self.broker_module.copy_back(source, self.work, Path('link.txt'))
        self.assertFalse((self.work / 'link.txt').is_symlink())
        self.assertEqual('protected', outside.read_text())

    def test_symlinked_output_parent_refused(self):
        """Worker output and deletion targets cannot walk through symlinked parents."""
        source = self.root / 'source'
        source.mkdir()
        outside = self.root / 'protected'
        outside.mkdir()
        (outside / 'file.txt').write_text('protected')
        (source / 'link').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.broker_module.copy_back(source, self.work, Path('link/file.txt'))
        (source / 'link').unlink()
        (self.work / 'link').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.broker_module.copy_back(source, self.work, Path('link/missing.txt'))
        self.assertEqual('protected', (outside / 'file.txt').read_text())

    def test_workspace_components_forwarded_and_project_config_copied_to_sandbox(self):
        """--submit forwards components/all_components, broker validates names, and copies workspace config into sandbox."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            work = base / 'yocto'
            work.mkdir()
            for sub in (
                'meta-org/src/ipc_bus/include',
                'meta-org/src/ipc_bus/src',
                'meta-org/src/diag_svc/include',
                'meta-org/src/diag_svc/src',
                'briefs',
                '.devpack',
            ):
                (work / sub).mkdir(parents=True)
            (work / '.gitignore').write_text('.devpack/\nbriefs/\nruns/\n', encoding='utf-8')
            (work / 'preflight.toml').write_text('[general]\nstd = "c++17"\n', encoding='utf-8')
            (work / '.devpack/project.toml').write_text(
                'data_class = "internal"\n'
                'allow_unsandboxed_local = true\n\n'
                '[pipeline]\n'
                'mode = "autonomous"\n\n'
                '[workspace]\n'
                'root = "meta-org/src"\n'
                'exclude = ["**/third_party/**"]\n\n'
                '[[component]]\n'
                'name = "ipc_bus"\n'
                'path = "ipc_bus"\n'
                'include_dirs = ["ipc_bus/include"]\n\n'
                '[[component]]\n'
                'name = "diag_svc"\n'
                'path = "diag_svc"\n'
                'include_dirs = ["diag_svc/include"]\n\n'
                '[scan]\n'
                'default = "selected"\n'
                'selected = ["ipc_bus"]\n',
                encoding='utf-8',
            )
            (work / 'meta-org/src/ipc_bus/src/ipc.cpp').write_text(
                '/**\n * @file ipc.cpp\n * @brief IPC module.\n */\n'
                '/**\n * @brief Get zero.\n * @return Zero.\n */\n'
                'int getZero() { return 0; }\n',
                encoding='utf-8',
            )
            (work / 'meta-org/src/diag_svc/src/diag.cpp').write_text(
                '/**\n * @file diag.cpp\n * @brief Diag module.\n */\n'
                '/**\n * @brief Get one.\n * @return One.\n */\n'
                'int getOne() { return 1; }\n',
                encoding='utf-8',
            )
            (work / 'briefs/diag.md').write_text(
                valid_brief('Update diag_svc/src/diag.cpp', ['diag_svc/**']),
                encoding='utf-8',
            )
            (work / 'briefs/wide.md').write_text(
                valid_brief('Update meta-org/src/diag_svc/src/diag.cpp', ['meta-org/src/**']),
                encoding='utf-8',
            )
            for args in (['init', '-q'], ['add', '.'], ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'base']):
                subprocess.run(['git', *args], cwd=work, check=True, capture_output=True)

            worker = base / 'ws-worker'
            worker.write_text(
                '#!/usr/bin/env python3\nimport json, pathlib\n'
                'p = pathlib.Path("meta-org/src/diag_svc/src/diag.cpp")\n'
                'p.write_text(p.read_text(encoding="utf-8").replace("return 1;", "return 2;"), encoding="utf-8")\n'
                'print(json.dumps({"type":"result","result":"updated diag"}))\n',
                encoding='utf-8',
            )
            worker.chmod(0o755)
            broker = self.broker_module.Broker(
                work, base / 'runs', credentials_path=self.credentials,
                isolation='local', allow_unsandboxed_local=True, agent_bin=str(worker), gate_cmd='true',
            )
            self.addCleanup(broker.close)

            # 1. Unknown component and mutually exclusive options are refused before running worker
            for bad_req in (
                {'engine': 'haiku', 'brief': 'briefs/diag.md', 'allow': ['diag_svc/**'], 'components': ['unknown_comp']},
                {'engine': 'haiku', 'brief': 'briefs/diag.md', 'allow': ['diag_svc/**'], 'components': ['diag_svc'], 'all_components': True},
                {'engine': 'haiku', 'brief': 'briefs/diag.md', 'allow': ['diag_svc/**'], 'extra_key': True},
            ):
                code, payload = broker.execute(bad_req)
                self.assertEqual(1, code, bad_req)
                self.assertEqual('refused', payload['status'])
            self.assertIn('return 1;', (work / 'meta-org/src/diag_svc/src/diag.cpp').read_text(encoding='utf-8'))

            # 2. Out-of-active-component edit via broker (--components ipc_bus, workspace-wide allow) is rejected and not copied back
            import time
            cmd_bad = [
                'python3', str(ROOT / 'runner/devpack_dispatch.py'),
                '--submit', '--engine', 'haiku', '--brief', 'briefs/wide.md',
                '--workdir', str(work), '--allow', 'meta-org/src/**',
                '--components', 'ipc_bus', '--timeout', '5',
            ]
            proc_bad = subprocess.Popen(cmd_bad, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while proc_bad.poll() is None and time.monotonic() < deadline:
                    broker.poll()
                    time.sleep(0.02)
                out_bad, err_bad = proc_bad.communicate(timeout=2)
            finally:
                if proc_bad.poll() is None:
                    proc_bad.kill()
                    proc_bad.communicate()
            self.assertEqual(1, proc_bad.returncode, out_bad + err_bad)
            res_bad = json.loads(out_bad.strip())
            self.assertEqual('violation', res_bad['scope'])
            self.assertEqual('fail', res_bad['gate'])
            self.assertIn('return 1;', (work / 'meta-org/src/diag_svc/src/diag.cpp').read_text(encoding='utf-8'))

            # 3. --submit --components diag_svc forwards component, copies workspace config into sandbox project.toml, passes, and copies back
            cmd_ok = [
                'python3', str(ROOT / 'runner/devpack_dispatch.py'),
                '--submit', '--engine', 'haiku', '--brief', 'briefs/diag.md',
                '--workdir', str(work), '--allow', 'diag_svc/**',
                '--components', 'diag_svc', '--timeout', '5',
            ]
            proc_ok = subprocess.Popen(cmd_ok, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while proc_ok.poll() is None and time.monotonic() < deadline:
                    broker.poll()
                    time.sleep(0.02)
                out_ok, err_ok = proc_ok.communicate(timeout=2)
            finally:
                if proc_ok.poll() is None:
                    proc_ok.kill()
                    proc_ok.communicate()
            self.assertEqual(0, proc_ok.returncode, out_ok + err_ok)
            res_ok = json.loads(out_ok.strip())
            self.assertEqual('ok', res_ok['status'])
            self.assertEqual('ok', res_ok['gate'])
            self.assertEqual('ok', res_ok['scope'])
            self.assertEqual(['diag_svc'], res_ok['components'])
            self.assertEqual('selected', res_ok['scan_mode'])
            self.assertIn('return 2;', (work / 'meta-org/src/diag_svc/src/diag.cpp').read_text(encoding='utf-8'))

            # 4. --submit --all-components forwards all_components=True and activates all configured components
            cmd_all = [
                'python3', str(ROOT / 'runner/devpack_dispatch.py'),
                '--submit', '--engine', 'haiku', '--brief', 'briefs/diag.md',
                '--workdir', str(work), '--allow', 'diag_svc/**',
                '--all-components', '--timeout', '5',
            ]
            proc_all = subprocess.Popen(cmd_all, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while proc_all.poll() is None and time.monotonic() < deadline:
                    broker.poll()
                    time.sleep(0.02)
                out_all, err_all = proc_all.communicate(timeout=2)
            finally:
                if proc_all.poll() is None:
                    proc_all.kill()
                    proc_all.communicate()
            self.assertEqual(0, proc_all.returncode, out_all + err_all)
            res_all = json.loads(out_all.strip())
            self.assertEqual('all', res_all['scan_mode'])
            self.assertEqual(['ipc_bus', 'diag_svc'], res_all['components'])

    def test_broker_refuses_symlink_project_toml(self):
        """A symlinked .devpack/project.toml is refused at broker startup."""
        self.broker.close()
        outside_cfg = self.root / 'outside_project.toml'
        outside_cfg.write_text('data_class = "internal"\n', encoding='utf-8')
        (self.work / '.devpack/project.toml').symlink_to(outside_cfg)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.broker_module.Broker(self.work, self.root / 'runs2')
