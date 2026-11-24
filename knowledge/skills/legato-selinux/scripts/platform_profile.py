"""Validate project-owned conventions without executing command templates."""
import re
from pathlib import PurePosixPath
from string import Formatter

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


def load(path, unknown):
    raw = {}
    if path is not None:
        try:
            import tomllib
            raw = tomllib.loads(path.read_text())
        except (OSError, UnicodeError, ValueError, ImportError) as error:
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
