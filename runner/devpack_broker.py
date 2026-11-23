#!/usr/bin/env python3
# Host dispatch broker. Exit codes: 0 stopped normally; 1 runtime/startup refusal; 2 invalid arguments.
"""Serialize validated queue requests using the host installation and its credentials."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

PACK_ROOT = Path(__file__).resolve().parent.parent
if str(PACK_ROOT) not in sys.path:
    sys.path.insert(0, str(PACK_ROOT))
from runner.agent_exec import DOCKER_IMAGE_TAG, check_data_class_allowed, load_engines, load_project_config, new_run_dir, redact_sensitive_text, update_run_json
from runner.brief_lint import has_errors as brief_has_errors, lint_brief_text
from runner.devpack_dispatch import dispatch_job, _render_run_dir_quietly
from runner.workspace_scope import load_workspace_config

CLIENT_SOURCE = Path(__file__).with_name('devpack_submit.py')
CLIENT_RELATIVE = Path('.devpack/pack/runner/devpack_dispatch.py')
REQUEST_LIMIT = 65536


def write_json(path, payload):
    """Atomically replace a JSON document without following a destination symlink."""
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('x', encoding='utf-8') as handle:
        json.dump(payload, handle, separators=(',', ':'))
        handle.write('\n')
    os.replace(temporary, path)


def relative_path(value):
    """Validate an unambiguous workdir-relative POSIX path."""
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('invalid relative path')
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or '\x00' in value:
        raise ValueError('path traversal refused')
    return path


@contextlib.contextmanager
def parent_fd(root, relative, *, create=False):
    """Pin each directory component without following links, optionally creating parents."""
    parts = relative_path(str(relative)).parts
    if not parts:
        raise ValueError('file name required')
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in parts[:-1]:
            if create:
                try:
                    os.mkdir(component, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def read_regular(root, relative):
    """Open a regular file once beneath root and immediately snapshot its bytes."""
    with parent_fd(root, relative) as (directory, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError('input must be a regular file')
            return handle.read()


def copy_back(source_root, destination_root, relative):
    """Publish regular output atomically or unlink it through pinned directory descriptors."""
    try:
        content = read_regular(source_root, relative)
    except FileNotFoundError:
        content = None
    try:
        with parent_fd(destination_root, relative, create=content is not None) as (directory, name):
            if content is None:
                try:
                    os.unlink(name, dir_fd=directory)
                except FileNotFoundError:
                    pass
                return
            temporary = '.broker-' + uuid.uuid4().hex + '.tmp'
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o666, dir_fd=directory)
            try:
                with os.fdopen(descriptor, 'wb') as handle:
                    handle.write(content)
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
    except FileNotFoundError:
        if content is not None:
            raise


def _format_toml_value(value):
    """Serialize a primitive or list value as valid TOML."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(_format_toml_value(item) for item in value) + ']'
    raise ValueError(f'unsupported TOML value type: {type(value).__name__}')


def _build_sandbox_project_toml(project_cfg):
    """Render trusted data_class and workspace/component/scan tables for the sandbox."""
    lines = ['data_class = ' + json.dumps(project_cfg['data_class'])]
    workspace = project_cfg.get('workspace')
    if isinstance(workspace, dict) and workspace:
        lines.extend(['', '[workspace]'])
        for key, value in workspace.items():
            lines.append(f'{key} = {_format_toml_value(value)}')
        for comp in project_cfg.get('components', ()):
            if not isinstance(comp, dict) or not comp:
                continue
            lines.extend(['', '[[component]]'])
            for key, value in comp.items():
                lines.append(f'{key} = {_format_toml_value(value)}')
        scan = project_cfg.get('scan')
        if isinstance(scan, dict) and scan:
            lines.extend(['', '[scan]'])
            for key, value in scan.items():
                lines.append(f'{key} = {_format_toml_value(value)}')
    return '\n'.join(lines) + '\n'


class Broker:
    """Own immutable launch configuration and execute at most one worker at a time."""

    def __init__(self, workdir, runs_root, *, credentials_path=None, engines_path=None,
                 isolation='docker', allow_unsandboxed_local=False, agent_bin='claude',
                 gate_cmd='', docker_bin='docker', image_tag=None, allowed_engines=None):
        """Snapshot trusted settings and install only the standalone client in the project."""
        self.workdir = Path(workdir).resolve()
        self.runs_root = Path(runs_root).resolve()
        if self.runs_root.is_relative_to(self.workdir):
            raise ValueError('broker run directory must be outside workdir')
        if PACK_ROOT.is_relative_to(self.workdir):
            raise ValueError('host pack installation must be outside workdir')
        self.credentials = Path(credentials_path).resolve() if credentials_path else None
        if self.credentials and self.credentials.is_relative_to(self.workdir):
            raise ValueError('broker credentials must be outside workdir')
        self.engines_path = Path(engines_path or PACK_ROOT / 'runner/engines.example.toml').resolve()
        if self.engines_path.is_relative_to(self.workdir):
            raise ValueError('engine configuration must be outside workdir')
        self.engines = load_engines(self.engines_path)
        self.allowed_engines = set(allowed_engines or self.engines.engines)
        control = self.workdir / '.devpack'
        if control.is_symlink():
            raise ValueError('control directory must not be a symlink')
        if (control / 'project.toml').is_symlink():
            raise ValueError('project config must not be a symlink')
        self.project = load_project_config(self.workdir)
        self.workspace_config = load_workspace_config(self.workdir)
        self.isolation = isolation
        self.allow_local = allow_unsandboxed_local
        self.agent_bin = agent_bin
        self.gate_cmd = gate_cmd
        self.docker_bin = docker_bin
        self.image_tag = image_tag
        control.mkdir(exist_ok=True)
        self.queue = control / 'queue'
        if self.queue.is_symlink():
            raise ValueError('queue must not be a symlink')
        self.queue.mkdir(exist_ok=True)
        self._lock_file = (control / 'broker.lock').open('a')
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            raise ValueError('a broker is already running for this workdir') from None
        self._execution_lock = threading.Lock()
        self._closed = False
        self.client_path = self.workdir / CLIENT_RELATIVE
        self.client_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.client_path.parent.resolve().is_relative_to(control.resolve()):
            self.close()
            raise ValueError('client directory escapes control directory')
        legacy = [path for path in (control / 'pack').rglob('*')
                  if path.is_file() and path != self.client_path]
        if legacy:
            self.close()
            raise ValueError('remove the legacy workdir pack before starting the broker')
        self.client_path.unlink(missing_ok=True)
        shutil.copyfile(CLIENT_SOURCE, self.client_path)
        self.client_path.chmod(0o444)
        self.client_hash = hashlib.sha256(CLIENT_SOURCE.read_bytes()).hexdigest()
        write_json(control / 'broker.json', {'version': 1, 'client_sha256': self.client_hash})

    def close(self):
        """Release the broker singleton lock and remove its readiness marker."""
        if not getattr(self, '_closed', True):
            self._closed = True
            (self.workdir / '.devpack/broker.json').unlink(missing_ok=True)
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()

    def _validate(self, request):
        """Refuse launch overrides, unknown engines, unknown components, forbidden data classes, and path escapes."""
        if not isinstance(request, dict) or set(request) - {'engine', 'brief', 'allow', 'gate_cmd', 'components', 'all_components'}:
            raise ValueError('unsupported request fields')
        if self.client_path.is_symlink() or hashlib.sha256(self.client_path.read_bytes()).hexdigest() != self.client_hash:
            raise ValueError('client integrity mismatch')
        engine = request.get('engine')
        if not isinstance(engine, str) or engine not in self.allowed_engines or engine not in self.engines.engines:
            raise ValueError('unknown or disallowed engine')
        allowed, reason = check_data_class_allowed(self.engines.engines[engine], self.project['data_class'])
        if not allowed:
            raise ValueError(reason)
        brief = read_regular(self.workdir, relative_path(request.get('brief')))
        globs = request.get('allow')
        if not isinstance(globs, list) or not globs or len(globs) > 128:
            raise ValueError('nonempty allow globs required')
        for glob in globs:
            relative_path(glob)
        if request.get('gate_cmd', self.gate_cmd) != self.gate_cmd:
            raise ValueError('gate_cmd must match broker startup configuration')
        all_components = request.get('all_components', False)
        if not isinstance(all_components, bool):
            raise ValueError('all_components must be a boolean')
        components = None
        if 'components' in request and request['components'] is not None:
            raw_components = request['components']
            if (
                not isinstance(raw_components, list)
                or not raw_components
                or len(raw_components) > 128
                or any(not isinstance(item, str) or not item.strip() for item in raw_components)
            ):
                raise ValueError('components must be a non-empty list of component names')
            components = [item.strip() for item in raw_components]
            if all_components:
                raise ValueError('cannot specify both components and all_components')
            valid_names = {comp.name for comp in self.workspace_config.components}
            unknown = [name for name in components if name not in valid_names]
            if unknown:
                valid_list = ', '.join(sorted(valid_names)) if valid_names else '(none)'
                raise ValueError(f"Unknown component(s): {', '.join(unknown)}. Valid components: {valid_list}")
        return brief, globs, components, all_components

    def execute(self, request, request_id=None):
        """Log and execute one request; never accept worker launch configuration from it."""
        request_id = request_id or uuid.uuid4().hex
        run = new_run_dir(self.runs_root, kind='dispatch', task='broker-request')
        redacted = json.loads(redact_sensitive_text(json.dumps(request)))
        write_json(run / (request_id + '.request.json'), redacted)
        payload = {'run_dir': str(run), 'status': 'refused', 'gate': 'skipped',
                   'scope': 'ok', 'changed_files': [], 'final_text_tail': '', 'cost_usd': None}
        acquired = self._execution_lock.acquire(blocking=False)
        brief_findings = []
        try:
            if not acquired:
                raise ValueError('broker concurrency limit is 1')
            brief, globs, components, all_components = self._validate(request)
            brief_findings = lint_brief_text(
                brief.decode('utf-8', errors='replace'),
                repo_root=self.workdir,
                engine=request['engine'],
                engines_config=self.engines_path,
                author='orchestrator',
            )
            if brief_has_errors(brief_findings):
                payload['brief_lint'] = brief_findings
                err_count = sum(1 for item in brief_findings if item.get('severity') == 'error')
                raise ValueError(f'brief_lint rejected brief with {err_count} error(s)')
            with tempfile.TemporaryDirectory(prefix='devpack-broker-') as temporary:
                sandbox = Path(temporary)
                work = sandbox / 'work'
                # Never expose the queue, installed client, orchestrator configuration, or host Git metadata.
                shutil.copytree(self.workdir, work, symlinks=True,
                                ignore=shutil.ignore_patterns('.devpack', '.git', '.claude', 'CLAUDE.md', 'briefs', 'runs', '__pycache__'))
                for entry in work.rglob('*'):
                    if entry.is_symlink():
                        raise ValueError('worker inputs must not contain symlinks')
                git_env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(sandbox),
                           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}
                for args in (['init', '-q', '--template='], ['add', '-A'],
                             ['-c', 'user.name=Broker', '-c', 'user.email=broker@example.invalid', 'commit', '-qm', 'worker base', '--allow-empty']):
                    subprocess.run(['git', *args], cwd=work, env=git_env, check=True, capture_output=True)
                prompt = sandbox / 'brief.md'
                prompt.write_bytes(brief)
                project = sandbox / 'project.toml'
                project.write_text(_build_sandbox_project_toml(self.project), encoding='utf-8')
                # Freeze the engine file for this launch as well as policy resolution.
                engines = sandbox / 'engines.toml'
                shutil.copyfile(self.engines_path, engines)
                host_memory = self.workdir / '.devpack' / 'memory'
                sandbox_memory = sandbox / 'memory'
                if host_memory.is_dir() and not host_memory.is_symlink():
                    shutil.copytree(host_memory, sandbox_memory, symlinks=False)
                before_mem = {
                    p.relative_to(sandbox_memory).as_posix(): p.read_bytes()
                    for p in sandbox_memory.rglob('*')
                    if p.is_file()
                } if sandbox_memory.is_dir() else {}
                with contextlib.redirect_stdout(io.StringIO()):
                    code, payload = dispatch_job(
                        engine_name=request['engine'], brief_path=prompt, workdir=work, allow_globs=globs,
                        gate_cmd=self.gate_cmd, runs_root=self.runs_root, engines_config_path=engines,
                        project_config_path=project, isolation=self.isolation,
                        allow_unsandboxed_local=self.allow_local, credentials_path=self.credentials,
                        agent_bin=self.agent_bin, docker_bin=self.docker_bin,
                        image_tag=self.image_tag or DOCKER_IMAGE_TAG, role='broker-worker', pack_root=PACK_ROOT,
                        memory_root=sandbox_memory,
                        brief_lint_findings=brief_findings,
                        components=components,
                        all_components=all_components,
                    )
                if payload.get('gate') == 'ok' and payload.get('scope') == 'ok' and sandbox_memory.is_dir():
                    changed_mem = [
                        mem_file for mem_file in sorted(sandbox_memory.rglob('*'))
                        if mem_file.is_file() and before_mem.get(mem_file.relative_to(sandbox_memory).as_posix()) != mem_file.read_bytes()
                    ]
                    if changed_mem:
                        host_memory.mkdir(parents=True, exist_ok=True)
                        for mem_file in changed_mem:
                            copy_back(sandbox_memory, host_memory, mem_file.relative_to(sandbox_memory))
                if payload['scope'] == 'ok':
                    for name in payload['changed_files']:
                        relative = relative_path(name)
                        if relative.parts[0] in {'.git', '.devpack', '.claude'}:
                            raise ValueError('worker control path refused')
                        copy_back(work, self.workdir, relative)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            code = 1
            payload.update(status='refused', error=redact_sensitive_text(str(exc)))
        finally:
            if acquired:
                self._execution_lock.release()
        payload = json.loads(redact_sensitive_text(json.dumps(payload)))
        write_json(run / (request_id + '.response.json'), payload)
        run_updates = {'outcome': 'pass' if code == 0 else 'fail', 'response': payload}
        if brief_findings:
            run_updates['brief_lint'] = brief_findings
        update_run_json(run, **run_updates)
        _render_run_dir_quietly(run)
        return code, payload

    def poll(self):
        """Consume complete queue documents serially, at a maximum of one job per poll."""
        for path in sorted(self.queue.glob('*.request.json')):
            request_id = path.name.removesuffix('.request.json')
            if not re.fullmatch('[a-f0-9]{32}', request_id):
                continue
            response = self.queue / (request_id + '.response.json')
            if response.exists():
                continue
            try:
                if path.is_symlink() or path.stat().st_size > REQUEST_LIMIT:
                    raise ValueError('invalid or oversized request')
                request = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                request = {'invalid_request': str(exc)}
            _, payload = self.execute(request, request_id)
            write_json(response, payload)
            return


def parse_worker_names(text):
    """Split a comma-separated worker list, strip whitespace and drop empty names.

    Returns the list of names in order. Raises ValueError when the text names no engine at all.
    """
    names = [name.strip() for name in text.split(',') if name.strip()]
    if not names:
        raise ValueError('--workers names no engine')
    return names


def main(argv=None):
    """Run a host broker until interrupted, keeping launch options outside the queue."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--runs-root', required=True)
    parser.add_argument('--credentials', default=os.environ.get('DEVPACK_CLAUDE_CREDENTIALS'))
    parser.add_argument('--engines')
    parser.add_argument('--workers', help='Comma-separated permitted engine names')
    parser.add_argument('--gate-cmd', default='')
    parser.add_argument('--agent-bin', default='claude')
    parser.add_argument('--docker-bin', default='docker')
    parser.add_argument('--image-tag', default=DOCKER_IMAGE_TAG)
    parser.add_argument('--isolation', choices=('docker', 'local'), default='docker')
    parser.add_argument('--allow-unsandboxed-local', action='store_true')
    args = parser.parse_args(argv)
    stop = threading.Event()

    def stop_broker(_signal, _frame):
        """Request orderly shutdown after the current bounded job."""
        stop.set()

    signal.signal(signal.SIGTERM, stop_broker)
    signal.signal(signal.SIGINT, stop_broker)
    try:
        allowed = parse_worker_names(args.workers) if args.workers else None
        broker = Broker(args.workdir, args.runs_root, credentials_path=args.credentials,
                        engines_path=args.engines, isolation=args.isolation,
                        allow_unsandboxed_local=args.allow_unsandboxed_local,
                        agent_bin=args.agent_bin, gate_cmd=args.gate_cmd,
                        docker_bin=args.docker_bin, image_tag=args.image_tag,
                        allowed_engines=allowed)
        try:
            while not stop.is_set():
                broker.poll()
                stop.wait(0.1)
        finally:
            broker.close()
        return 0
    except (OSError, ValueError) as exc:
        print('broker: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
