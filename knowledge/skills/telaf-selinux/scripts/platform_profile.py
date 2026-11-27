"""Validate project-owned conventions without executing command templates."""
import re
from pathlib import PurePosixPath
from string import Formatter

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 fallback
    tomllib = None

STRINGS = ('domain_pattern', 'exec_type_pattern', 'data_type_pattern', 'prefix',
           'admin_source_domain', 'transition_check', 'module_bundle_path')
TABLES = {'module_files': ('te', 'fc', 'if', 'makefile', 'component'),
          'build': ('include_variables',)}
FIELDS = set(('prefix app executable domain exec_type data_type admin_source_domain '
              'client server type receiver creator cap adef output platform_root').split())


def template(value):
    if not isinstance(value, str) or not value.strip() or value == 'unknown':
        return False
    try:
        return all(field is None or (field in FIELDS and not spec and not conversion)
                   for _, field, spec, conversion in Formatter().parse(value))
    except ValueError:
        return False


def validate(raw, unknown):
    """Return only validated values; every absent/invalid required value is unknown."""
    clean = {}
    def accept(key, value, valid):
        if valid:
            return value
        unknown('platform_profile.' + key + ': unknown; supply a valid value before dependent steps')
        return 'unknown'
    for key in STRINGS:
        value = raw.get(key)
        valid = template(value)
        if key.endswith('_pattern'):
            valid = valid and ('{app}' in value or '{executable}' in value)
        if key in ('prefix', 'admin_source_domain'):
            valid = isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value))
        if key == 'module_bundle_path':
            valid = valid and not value.startswith('/') and '..' not in PurePosixPath(value).parts and '\\' not in value
        clean[key] = accept(key, value, valid)
    for key, members in TABLES.items():
        table = raw.get(key)
        if not isinstance(table, dict):
            clean[key] = accept(key, table, False)
            continue
        clean[key] = {}
        for member in members:
            value = table.get(member)
            valid = (isinstance(value, list) and bool(value) and all(
                isinstance(x, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', x)
                for x in value)) if key == 'build' else (
                    template(value) and '/' not in value and '\\' not in value and value not in ('.', '..'))
            clean[key][member] = accept(key + '.' + member, value, valid)
    for key in ('base_inherited_grants', 'banned'):
        value = raw.get(key)
        valid = isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value)
        clean[key] = accept(key, value, valid)
    value = raw.get('interfaces')
    if not isinstance(value, dict) or not value:
        clean['interfaces'] = accept('interfaces', value, False)
    else:
        clean['interfaces'] = {key: accept('interfaces.' + key, val, template(val))
                               for key, val in value.items()}
    for key in ('skeleton_generator', 'file_contexts_tool'):
        if key in raw:
            clean[key] = accept(key, raw[key], template(raw[key]))
    return clean


def _strip_toml_comment(line):
    """Strip an unquoted '#' comment from a TOML line (adapted from runner/workspace_scope.py)."""
    in_quote = None
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == '\\' and in_quote == '"':
            escaped = True
            continue
        if ch in ('"', "'"):
            if in_quote is None:
                in_quote = ch
            elif in_quote == ch:
                in_quote = None
            continue
        if ch == '#' and in_quote is None:
            return line[:idx]
    if in_quote is not None:
        raise ValueError('unterminated string')
    return line


def _parse_toml_scalar_or_array(raw):
    """Parse only scalar values and flat arrays; reject every leftover token."""
    scalar = r"(?:true|false|[+-]?(?:0|[1-9](?:_?[0-9])*))"
    string = r"(?:\"(?:[^\"\\\x00-\x08\x0a-\x1f\x7f]|\\(?:[\"\\btnfr]|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}))*\"|'[^'\x00-\x08\x0a-\x1f\x7f]*')"
    token = re.compile(string + '|' + scalar)

    def parse_one(text, offset):
        match = token.match(text, offset)
        if match is None:
            raise ValueError('unsupported or malformed value')
        value = match.group()
        if value.startswith('"'):
            escapes = {'b': '\b', 't': '\t', 'n': '\n', 'f': '\f', 'r': '\r',
                       '"': '"', '\\': '\\'}
            def decode(match):
                code = match.group()[1:]
                if code[0] in ('u', 'U'):
                    number = int(code[1:], 16)
                    if number > 0x10ffff or 0xd800 <= number <= 0xdfff:
                        raise ValueError('invalid Unicode escape')
                    return chr(number)
                return escapes[code]
            result = re.sub(r'\\(?:u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)', decode, value[1:-1])
        elif value.startswith("'"):
            result = value[1:-1]
        elif value in ('true', 'false'):
            result = value == 'true'
        else:
            result = int(value.replace('_', ''))
        return result, match.end()

    text = raw.strip()
    if not text.startswith('['):
        result, end = parse_one(text, 0)
        if text[end:].strip():
            raise ValueError('unexpected trailing value')
        return result
    if not text.endswith(']'):
        raise ValueError('unclosed array')
    inner = text[1:-1].strip()
    values = []
    while inner:
        result, end = parse_one(inner, 0)
        values.append(result)
        inner = inner[end:].strip()
        if inner:
            if not inner.startswith(','):
                raise ValueError('expected array comma')
            inner = inner[1:].strip()
    return values


def loads_toml(text):
    """Use tomllib when available, otherwise parse the documented strict subset."""
    if tomllib is not None:
        return tomllib.loads(text)
    root = {}
    current = root
    declared = set()
    lines = iter(enumerate(text.splitlines(), 1))
    name = r'[A-Za-z0-9_-]+'
    for number, line in lines:
        try:
            cleaned = _strip_toml_comment(line).strip()
            if not cleaned:
                continue
            if cleaned.startswith('['):
                if not re.fullmatch(r'\[' + name + r'(?:\.' + name + r')*\]', cleaned):
                    raise ValueError('unsupported table header')
                path = tuple(cleaned[1:-1].split('.'))
                if path in declared:
                    raise ValueError('duplicate table')
                declared.add(path)
                current = root
                for part in path:
                    current = current.setdefault(part, {})
                    if not isinstance(current, dict):
                        raise ValueError('conflicting table path')
                continue
            match = re.fullmatch(r'(' + name + r')\s*=\s*(.*)', cleaned)
            if match is None:
                raise ValueError('expected bare key = value')
            key, value = match.groups()
            if key in current:
                raise ValueError('duplicate key')
            if value.startswith('['):
                # Ignore brackets inside strings when finding the array's closing line.
                quoted = r'''"(?:[^"\\]|\\.)*"|['][^']*[']'''
                while not re.sub(quoted, '', value).rstrip().endswith(']'):
                    next_line = next(lines, None)
                    if next_line is None:
                        raise ValueError('unclosed array')
                    value += ' ' + _strip_toml_comment(next_line[1]).strip()
            current[key] = _parse_toml_scalar_or_array(value)
        except ValueError as error:
            raise ValueError(f'line {number}: {error}') from error
    return root


def check_types(raw, scope, strings=(), lists=(), tables=None):
    """Reject supplied values of the wrong type; absent fields remain unknown."""
    if not isinstance(raw, dict):
        raise ValueError(scope + ' must be a table')
    for key in strings:
        if key in raw and not isinstance(raw[key], str):
            raise ValueError(scope + '.' + key + ' must be a string')
    for key in lists:
        if key in raw and (not isinstance(raw[key], list) or
                           not all(isinstance(item, str) for item in raw[key])):
            raise ValueError(scope + '.' + key + ' must be a list of strings')
    for key, members in (tables or {}).items():
        if key in raw:
            table = raw[key]
            check_types(table, scope + '.' + key,
                        strings=members if members is not None else tuple(table) if isinstance(table, dict) else ())
    return raw


def load(path, unknown):
    raw = {}
    if path is not None:
        try:
            raw = loads_toml(path.read_text())
        except (OSError, UnicodeError, ValueError) as error:
            unknown('platform_profile: unknown; cannot read TOML profile (' + type(error).__name__ + ')')
    else:
        unknown('platform_profile: unknown; configure a workspace-relative TOML profile')
    check_types(raw, 'platform_profile',
                strings=STRINGS + ('skeleton_generator', 'file_contexts_tool'),
                lists=('base_inherited_grants', 'banned'),
                tables={'module_files': TABLES['module_files'], 'interfaces': None, 'build': ()})
    if 'build' in raw:
        check_types(raw['build'], 'platform_profile.build', lists=('include_variables',))
    return validate(raw, unknown)


def review(profile, extractor, path):
    """Expose the profile contract for review and seed every inherited deviation."""
    extractor.unknown('Verify profile conventions against selected framework/SDK: ' + str(profile))
    grants = profile['base_inherited_grants']
    if isinstance(grants, list):
        for number, grant in enumerate(grants, 1):
            extractor.add('inherited_grants', path, 1, deviation_id=f'BASE-{number:03}',
                          grant=grant, source=extractor.relative(path) + ':1')
            extractor.unknown(f'Approve inherited deviation BASE-{number:03}: {grant}; resolve base-policy evidence and affected processes')


def reconcile(profile, extractor):
    """Ask about absent mappings used by this inventory; never choose a fallback."""
    kinds = {'files': ('file_read', 'file_rw'), 'data_dirs': ('private_data',),
             'devices': ('device_ioctl',), 'unix_sockets': ('unix_socket',),
             'ipc': ('ipc_client', 'ipc_server'), 'fd_passing': ('fd_passing',),
             'network': ('network',), 'capabilities': ('capability',),
             'exec': ('helper_exec',), 'proc_sys': ('proc_global', 'proc_state'),
             'config_tree': ('config_tree',), 'processes': ('entrypoint', 'admin_registration')}
    interfaces = profile['interfaces']
    for category, names in kinds.items():
        if extractor.data[category]:
            for name in names:
                if not isinstance(interfaces, dict) or name not in interfaces:
                    extractor.unknown('platform_profile.interfaces.' + name + ': unknown; resolve applicable mapping before policy authoring')
