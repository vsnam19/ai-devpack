#!/usr/bin/env python3
"""Conservative Legato evidence extractor. No policy generation or approval.

Output is JSON, a YAML 1.2 subset, so no YAML dependency is necessary.
Definition parsing is lexical, not a replacement for the Legato build parser.
"""
import argparse
import fnmatch
import json
from pathlib import Path
import re
import sys

import platform_profile

CATEGORIES = ('files data_dirs devices unix_sockets ipc fd_passing network capabilities '
              'exec proc_sys config_tree env_allowed limits inherited_grants denied').split()
TOKEN = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|#[^\n]*|"(?:\\.|[^"\\])*"|[{}\n]|[^{}\n]+?(?=//|/\*|#|"|[{}\n]|$)')
CALL = re.compile(r'\b(open(?:64)?|fopen|opendir|ioctl|socket|bind|connect|memfd_create|socketpair|exec\w*|popen|system|getenv)\s*\(')


def yaml_text(value):
    """Emit readable YAML 1.2 using JSON flow syntax and quoted scalars."""
    return json.dumps(value, indent=2, ensure_ascii=True) + '\n'


def sections(text):
    """Yield (section stack, statement, line) with compact/nested block support."""
    stack, buffer, line, start = [], '', 1, 1
    for match in TOKEN.finditer(text):
        token = match.group()
        if token.startswith(('//', '/*', '#')):
            line += token.count('\n')
            continue
        if token in ('{', '}', '\n'):
            statement = buffer.strip()
            if token == '{':
                stack.append(statement.rstrip(':').strip())
                buffer = ''
            elif token == '\n' and statement.endswith(':'):
                # Keep section label across newlines/comments until its opening '{'.
                line += 1
                continue
            else:
                if statement:
                    yield tuple(stack), statement, start
                if token == '}' and stack:
                    stack.pop()
                buffer = ''
            line += token.count('\n')
            start = line
        else:
            if not buffer.strip():
                start = line
            buffer += token
    if buffer.strip():
        yield tuple(stack), buffer.strip(), start


class Extractor:
    def __init__(self, root, config, app):
        self.root, self.config = root, config
        self.data = dict(app=app, version='1', approval=dict(by='', date='', version=''),
                         identity=dict(id='IDENTITY-001', why='', evidence=[], status='draft',
                                       runs_as='unknown', sandboxed='unknown', groups=[]),
                         processes=[], unknowns=[])
        self.data.update({key: [] for key in CATEGORIES})
        self.seen = set()

    def relative(self, path):
        return path.resolve().relative_to(self.root).as_posix()

    def unknown(self, message):
        if message not in self.data['unknowns']:
            self.data['unknowns'].append(message)

    def safe(self, path):
        path = path.resolve()
        if not path.is_relative_to(self.root):
            self.unknown('Reference outside workspace; provide an in-workspace mapping')
            return None
        rel = self.relative(path)
        if any(fnmatch.fnmatch(rel, pat) for pat in self.config.get('exclude', [])):
            self.unknown('Excluded reference: ' + rel)
            return None
        return path

    def add(self, category, source_path, line, **fields):
        item = dict(id=f'{category.upper()}-{len(self.data[category]) + 1:03}', why='',
                    evidence=[f'{self.relative(source_path)}:{line}'], status='draft',
                    processes=['unknown'])
        item.update(fields)
        self.data[category].append(item)
        return item

    def resolve(self, name, parent, suffix=None):
        if '$' in name:
            self.unknown('Unresolved definition variable: ' + name)
            return None
        candidate = self.safe(parent / name)
        if candidate is None:
            return None
        if suffix and candidate.is_dir():
            candidate = candidate / suffix
        if candidate.exists():
            return candidate
        # Company layouts can place shared components anywhere; ambiguity is not guessed.
        matches = [p for p in self.root.rglob(suffix or Path(name).name)
                   if (not suffix or p.parent.name == Path(name).name) and self.safe(p)]
        if len(matches) == 1:
            return matches[0]
        self.unknown('Missing or ambiguous reference: ' + name)
        return None

    def definition(self, path):
        path = self.safe(path)
        if path is None or path in self.seen:
            return
        self.seen.add(path)
        if path.suffix == '.adef':
            self.app_definition = path
        try:
            text = path.read_text()
        except (OSError, UnicodeError):
            self.unknown('Missing/unreadable definition: ' + self.relative(path))
            return
        for context, statement, line in sections(text):
            key, sep, value = statement.partition(':')
            key, value = key.strip(), value.strip()
            leaf = context[-1] if context else ''
            words = lambda s: re.findall(r'"[^"]*"|[^\s,]+', s)
            if key in ('username', 'sandboxed'):
                field = 'runs_as' if key == 'username' else key
                self.data['identity'][field] = ({'true': True, 'false': False}.get(value, value))
                self.data['identity']['evidence'].append(f'{self.relative(path)}:{line}')
            elif leaf == 'groups' or key == 'groups':
                self.data['identity']['groups'].extend(words(value if sep else statement))
                self.data['identity']['evidence'].append(f'{self.relative(path)}:{line}')
            elif leaf == 'executables':
                match = re.match(r'(\S+)\s*=\s*\((.*?)\)', statement)
                if match:
                    self.add('processes', path, line, executable=match[1], declaration=statement)
                    for component in words(match[2]):
                        resolved = self.resolve(component.strip('"'), path.parent, 'Component.cdef')
                        if resolved:
                            self.definition(resolved)
                else:
                    self.unknown('Unparsed executable: ' + statement)
            elif leaf in ('components', 'component'):
                for name in words(statement):
                    resolved = self.resolve(name.strip('"'), path.parent, 'Component.cdef')
                    if resolved:
                        self.definition(resolved)
            elif leaf == 'sources':
                for name in words(statement):
                    resolved = self.resolve(name.strip('"'), path.parent)
                    if resolved:
                        self.source(resolved)
            elif leaf in ('file', 'dir', 'device') and 'requires' in context:
                paths = re.findall(r'"([^"]+)"|(/[^\s]+)', statement)
                access = re.search(r'\[([^]]+)\]', statement)
                category = {'file': 'files', 'dir': 'data_dirs', 'device': 'devices'}[leaf]
                self.add(category, path, line, path=next((a or b for a, b in paths), 'unknown'),
                         access=[access[1] if access else 'unknown'],
                         **({'ioctls': []} if leaf == 'device' else {}))
            elif leaf in ('bindings', 'extern', 'api'):
                server = leaf == 'extern' or 'provides' in context
                self.add('ipc', path, line, api=statement, role='server' if server else 'client',
                         peers=[statement.split('->')[-1].strip() if '->' in statement else 'unknown'])
            elif leaf in ('capability', 'capabilities') or key in ('capability', 'capabilities'):
                for cap in words(value if sep else statement):
                    self.add('capabilities', path, line, name=cap)
            elif leaf == 'envVars':
                self.add('env_allowed', path, line, name=statement.split('=')[0].strip())
            elif key in ('start', 'faultAction') or leaf == 'run' or key.startswith(('max', 'min')):
                self.add('limits', path, line, name=key if sep else 'run', value=value if sep else statement)
            elif leaf == 'configTree':
                self.add('config_tree', path, line, path=statement, access=['unknown'])
        self.received_files(path, text)
        self.unknown('Verify build-expanded definitions, includes, shared libraries and per-process component ownership: ' + self.relative(path))

    def resolve_api(self, name, parent):
        """Search only explicit interface directories, preserving their order."""
        directories = [parent]
        directories.extend(self.root / entry for entry in self.config.get('api_search_paths', []))
        for pattern in self.config.get('sdef_globs', []):
            for definition in sorted(self.root.glob(pattern)):
                definition = self.safe(definition)
                if definition is None:
                    continue
                try:
                    text = definition.read_text()
                except (OSError, UnicodeError):
                    self.unknown('Missing/unreadable definition: ' + self.relative(definition))
                    continue
                for context, statement, _ in sections(text):
                    if context and context[-1] == 'interfaceSearch':
                        directories.append(definition.parent / statement.strip('"'))
        tried = []
        for directory in directories:
            candidate = directory / name
            tried.append(str(candidate.relative_to(self.root))
                         if candidate.is_relative_to(self.root) else str(candidate))
            if '$' in str(candidate):
                continue
            candidate = self.safe(candidate)
            if candidate is not None and candidate.is_file():
                return candidate
        self.unknown('API ' + name + ': unknown; which interface path resolves it? Paths tried: '
                     + ', '.join(tried))
        return None

    def received_files(self, path, text):
        """Inventory client and server API reception; labels remain unknown."""
        for context, statement, _ in sections(text):
            if not context or context[-1] != 'api' or not any(
                    role in context for role in ('requires', 'provides')):
                continue
            server = 'provides' in context
            match = re.search(r'(?:(\w+)\s*=\s*)?([\w./-]+\.api)\b', statement)
            if not match:
                continue
            alias = match[1] or Path(match[2]).stem
            bindings = []
            for definition in [self.app_definition] if hasattr(self, 'app_definition') else []:
                safe = self.safe(definition)
                if safe:
                    try:
                        definition_text = safe.read_text()
                    except (OSError, UnicodeError):
                        self.unknown('Missing/unreadable definition: ' + self.relative(safe))
                        continue
                    for ctx, stmt, _ in sections(definition_text):
                        if ctx and ctx[-1] == 'bindings' and '->' in stmt:
                            client, peer = (part.strip() for part in stmt.split('->', 1))
                            endpoint = peer if server else client
                            # Component-qualified endpoints prevent same-alias cross attribution.
                            parts = endpoint.split('.')
                            if (parts[-1] == alias and
                                    (path.parent == self.root or len(parts) < 3 or parts[-2] == path.parent.name)):
                                bindings.append((parts[0], client if server else peer))
            if not bindings:
                self.unknown('Resolve binding for ' + ('provided' if server else 'required')
                             + ' API: ' + alias)
                continue
            api = self.resolve_api(match[2], path.parent)
            if not api:
                continue
            try:
                content = api.read_text()
            except (OSError, UnicodeError):
                self.unknown('Missing/unreadable API: ' + self.relative(api))
                continue
            content = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/',
                             lambda m: '\n' * m[0].count('\n'), content)
            signatures = list(re.finditer(
                r'\b(FUNCTION|HANDLER|EVENT)\s+(\w+)\s*\((.*?)\)\s*;', content, re.S))
            registered = {handler[2] for handler in signatures if handler[1] == 'HANDLER'
                          and any(re.search(r'\b' + re.escape(handler[2]) + r'\b', sig[3])
                                  for sig in signatures if sig[1] in ('FUNCTION', 'EVENT'))}
            for signature in signatures:
                kind, function, parameters = signature.groups()
                if kind == 'HANDLER' and function not in registered:
                    continue
                for parameter in parameters.split(','):
                    fd = re.search(r'\bfile\s+\w+\s+(OUT|INOUT|IN)\b', parameter)
                    if not fd:
                        continue
                    # Functions flow server-to-client on OUT; callbacks reverse that flow.
                    incoming = ('IN', 'INOUT') if server else ('OUT', 'INOUT')
                    if kind != 'FUNCTION':
                        incoming = ('OUT', 'INOUT') if server else ('IN', 'INOUT')
                    if fd[1] not in incoming:
                        continue
                    for process, peer in bindings:
                        self.add('fd_passing', api, content[:signature.start()].count('\n') + 1,
                                 object=function + ': ' + parameter.strip(), api=alias,
                                 function=function, direction='receive',
                                 **{'class': 'unknown', 'to': [process], 'from': [peer]},
                                 processes=[process])
                    self.unknown('Resolve received FD creator domain and underlying object class: ' + function)

    def source(self, path):
        path = self.safe(path)
        if path is None or path in self.seen:
            return
        self.seen.add(path)
        try:
            text = path.read_text()
        except (OSError, UnicodeError):
            self.unknown('Missing/unreadable source: ' + self.relative(path))
            return
        # Preserve newlines while removing comments; keep quoted strings intact.
        text = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*[\s\S]*?\*/',
                      lambda m: m[0] if m[0].startswith('"') else '\n' * m[0].count('\n'), text)
        conditional = []
        for line, content in enumerate(text.splitlines(), 1):
            directive = re.match(r'\s*#\s*(if\w*|elif|else|endif)\b(.*)', content)
            if directive:
                op, condition = directive.groups()
                marked = any(re.search(r'\b' + re.escape(macro) + r'\b', condition)
                             for macro in self.config.get('sim_macros', [])) or bool(re.search(r'\b\w*(?:SIMULATION|TEST|MOCK)\w*\b', condition, re.I))
                if op in ('if', 'ifdef', 'ifndef'):
                    conditional.append((marked, marked and op != 'ifndef' and '!' not in condition))
                elif op == 'endif' and conditional:
                    conditional.pop()
                elif op in ('else', 'elif') and conditional:
                    previous, active = conditional[-1]
                    conditional[-1] = (previous or marked, (previous and not active) if op == 'else' else marked)
                continue
            denied = any(active for _, active in conditional)
            calls = list(CALL.finditer(content))
            literals = re.findall(r'"((?:\\.|[^"\\])*)"', content)
            paths = [s for s in literals if re.match(r'^/(dev|etc|data|var|proc|sys|tmp)(/|$)', s)]
            for resource in paths:
                category = ('devices' if resource.startswith('/dev/') else
                            'proc_sys' if resource.startswith(('/proc/', '/sys/')) else 'files')
                self.add('denied' if denied else category, path, line, path=resource,
                         access=['unknown'], candidate_denied=denied,
                         **({'ioctls': []} if category == 'devices' else {}))
            for call in calls:
                operation = call[1]
                if paths and operation in ('open', 'open64', 'fopen', 'opendir'):
                    continue
                category = ('env_allowed' if operation == 'getenv' else 'devices' if operation == 'ioctl'
                            else 'exec' if operation.startswith('exec') or operation in ('popen', 'system')
                            else 'fd_passing' if operation in ('memfd_create', 'socketpair')
                            else 'unix_sockets' if 'AF_UNIX' in content else 'network'
                            if operation in ('socket', 'bind', 'connect') else 'files')
                fields = dict(operation=operation, expression=content.strip())
                if category == 'env_allowed':
                    fields['name'] = literals[0] if literals else 'unknown'
                if category == 'devices':
                    fields.update(path='unknown', access=['ioctl'], ioctls=[content[call.end():].split(',')[1].strip() if ',' in content[call.end():] else 'unknown'])
                if category == 'fd_passing':
                    fields.update(object=operation, **{'class': 'unknown', 'to': ['unknown'], 'from': ['unknown']})
                if category == 'network':
                    fields.update(proto='unknown', addr='unknown', port='unknown', direction='unknown')
                if category in ('files', 'exec', 'unix_sockets'):
                    fields.update(path=paths[0] if paths else 'unknown', access=['unknown'])
                if denied:
                    fields.setdefault('path', paths[0] if paths else 'unknown')
                self.add('denied' if denied else category, path, line, candidate_denied=denied, **fields)
                self.unknown(f'Resolve runtime arguments, process and object labels for {operation} at {self.relative(path)}:{line}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('adef', nargs='?')
    parser.add_argument('--app-name')
    parser.add_argument('--workspace', default='.')
    parser.add_argument('--config', default='.devpack/project.toml', help='TOML or JSON project config')
    parser.add_argument('--output', required=True, help='Workspace-relative output directory; must not contain an existing manifest')
    args = parser.parse_args()
    root = Path(args.workspace).resolve()
    def contained(raw):
        result = (root / raw).resolve()
        if not result.is_relative_to(root):
            raise ValueError('Path escapes workspace')
        return result
    try:
        config_path = contained(args.config)
        config = {}
        if config_path.exists():
            if config_path.suffix == '.json':
                document = json.loads(config_path.read_text())
            else:
                document = platform_profile.loads_toml(config_path.read_text())
            platform_profile.check_types(document, 'config')
            config = document.get('selinux', {})
        platform_profile.check_types(config, 'selinux',
            strings=('platform_root', 'sdk_policy_include', 'linked_policy',
                     'platform_profile', 'manifest_name'), lists=('app_globs', 'exclude', 'sim_macros', 'api_search_paths', 'sdef_globs'))
        for entry in config.get('api_search_paths', []):
            if not entry.strip() or Path(entry).is_absolute() or entry.startswith('\\'):
                raise ValueError('api_search_paths must be workspace-relative directories')
            contained(entry)
        for pattern in config.get('sdef_globs', []):
            if (not pattern.strip() or Path(pattern).is_absolute() or pattern.startswith('\\')
                    or '..' in Path(pattern.replace('\\', '/')).parts):
                raise ValueError('sdef_globs must be workspace-relative without ".."')
        if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', macro) for macro in config.get('sim_macros', [])):
            raise ValueError('sim_macros must contain macro identifiers')
        for key in ('platform_root', 'sdk_policy_include', 'linked_policy'):
            if config.get(key):
                contained(config[key])
        if bool(args.adef) == bool(args.app_name):
            raise ValueError('Supply one .adef OR --app-name')
        if args.adef:
            adef = contained(args.adef)
        else:
            globs = config.get('app_globs', ['**/apps/*.adef'])
            for glob in globs:
                if (not isinstance(glob, str) or not glob.strip()
                        or Path(glob).is_absolute() or glob.startswith('\\')
                        or '..' in Path(glob.replace('\\', '/')).parts):
                    raise ValueError(f'app_globs pattern must be workspace-relative without "..": {glob}')
            matches = sorted({contained(p) for glob in globs
                              for p in root.glob(glob) if p.stem == args.app_name
                              and not any(fnmatch.fnmatch(p.relative_to(root).as_posix(), pat)
                                          for pat in config.get('exclude', []))})
            if len(matches) != 1:
                raise ValueError('App discovery needs exactly one match; supply an explicit .adef')
            adef = matches[0]
        extractor = Extractor(root, config, args.app_name or adef.stem)
        if config.get('platform_profile') and Path(config['platform_profile']).is_absolute():
            raise ValueError('platform_profile must be workspace-relative')
        profile_path = contained(config['platform_profile']) if config.get('platform_profile') else None
        profile = platform_profile.load(profile_path, extractor.unknown)
        if profile_path is not None and profile_path.is_file():
            platform_profile.review(profile, extractor, profile_path)
        extractor.definition(adef)
        platform_profile.reconcile(profile, extractor)
        for key in ('platform_root', 'sdk_policy_include', 'linked_policy'):
            if not config.get(key) or not contained(config[key]).exists():
                extractor.unknown(key + ': unknown; supply before dependent policy/build steps')
        extractor.unknown('Decide dedicated user, sandboxed, requires, groups, start and faultAction BEFORE AVC collection')
        extractor.unknown('Assign every resource to actual processes; resolve dynamic paths, FD classes, network endpoints and inherited grants')
        output = contained(args.output)
        name = config.get('manifest_name', 'access-manifest.yaml')
        if Path(name).name != name or name in ('', '.', '..', 'QUESTIONS.md'):
            raise ValueError('manifest_name must be a plain filename distinct from QUESTIONS.md')
        output.mkdir(parents=True, exist_ok=True)
        manifest = output / name
        questions = output / 'QUESTIONS.md'
        if manifest.exists() or questions.exists():
            raise ValueError('Refusing to overwrite existing manifest or questions')
        manifest.write_text(yaml_text(extractor.data))
        questions.write_text('# Questions\n\nDRAFT: human approval checkpoint `selinux-manifest` is required.\n\n' +
                             '\n'.join('- ' + q for q in extractor.data['unknowns']) +
                             '\n- Explain why and review evidence for every item; approve a version explicitly.\n')
        return 0
    except (ValueError, OSError, TypeError) as error:
        print(f'extract_manifest: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
