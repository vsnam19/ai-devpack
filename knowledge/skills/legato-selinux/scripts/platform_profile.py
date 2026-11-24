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
    return line


def _parse_toml_scalar_or_array(raw):
    """Parse a scalar or array TOML value for the Python 3.10 fallback."""
    val = raw.strip()
    if val in ('true', 'false'):
        return val == 'true'
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        inner = val[1:-1]
        if val[0] == '"':
            return inner.replace('\\"', '"').replace('\\\\', '\\')
        return inner
    if val.startswith('[') and val.endswith(']'):
        inner = val[1:-1].strip()
        if not inner:
            return []
        items, buf, in_q, esc = [], [], None, False
        for ch in inner:
            if esc:
                buf.append(ch)
                esc = False
                continue
            if ch == '\\' and in_q == '"':
                buf.append(ch)
                esc = True
                continue
            if ch in ('"', "'"):
                if in_q is None:
                    in_q = ch
                elif in_q == ch:
                    in_q = None
                buf.append(ch)
                continue
            if ch == ',' and in_q is None:
                part = ''.join(buf).strip()
                if part:
                    items.append(_parse_toml_scalar_or_array(part))
                buf = []
                continue
            buf.append(ch)
        tail = ''.join(buf).strip()
        if tail:
            items.append(_parse_toml_scalar_or_array(tail))
        return items
    try:
        return int(val)
    except ValueError:
        return val


def loads_toml(text):
    """Parse TOML text supporting [table] and [[array_of_tables]] on Python 3.10+."""
    if tomllib is not None:
        data = tomllib.loads(text)
        if not isinstance(data, dict):
            raise ValueError('TOML root must be a table')
        return data
    root = {}
    current = root
    lines = text.splitlines()
    idx = 0
    while idx < len(lines):
        cleaned = _strip_toml_comment(lines[idx]).strip()
        idx += 1
        if not cleaned:
            continue
        if cleaned.startswith('[[') and cleaned.endswith(']]'):
            parts = [p.strip().strip('"').strip("'") for p in cleaned[2:-2].strip().split('.') if p.strip()]
            if not parts:
                raise ValueError(f'Invalid TOML array-of-tables header: {cleaned}')
            target = root
            for part in parts[:-1]:
                nxt = target.setdefault(part, {})
                if not isinstance(nxt, dict):
                    raise ValueError(f'Conflicting TOML table path: {cleaned}')
                target = nxt
            arr = target.setdefault(parts[-1], [])
            if not isinstance(arr, list):
                raise ValueError(f'Conflicting TOML array-of-tables path: {cleaned}')
            entry = {}
            arr.append(entry)
            current = entry
            continue
        if cleaned.startswith('[') and cleaned.endswith(']'):
            parts = [p.strip().strip('"').strip("'") for p in cleaned[1:-1].strip().split('.') if p.strip()]
            if not parts:
                raise ValueError(f'Invalid TOML table header: {cleaned}')
            target = root
            for part in parts:
                nxt = target.setdefault(part, {})
                if not isinstance(nxt, dict):
                    raise ValueError(f'Conflicting TOML table path: {cleaned}')
                target = nxt
            current = target
            continue
        if '=' not in cleaned:
            raise ValueError(f'Invalid TOML line: {cleaned}')
        key_raw, val_raw = cleaned.split('=', 1)
        key = key_raw.strip().strip('"').strip("'")
        val_str = val_raw.strip()
        while val_str.startswith('[') and not val_str.endswith(']') and idx < len(lines):
            nxt_line = _strip_toml_comment(lines[idx]).strip()
            idx += 1
            if nxt_line:
                val_str = val_str + ' ' + nxt_line
        current[key] = _parse_toml_scalar_or_array(val_str)
    return root


def load(path, unknown):
    raw = {}
    if path is not None:
        try:
            raw = loads_toml(path.read_text())
        except (OSError, UnicodeError, ValueError) as error:
            unknown('platform_profile: unknown; cannot read TOML profile (' + type(error).__name__ + ')')
    else:
        unknown('platform_profile: unknown; configure a workspace-relative TOML profile')
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
