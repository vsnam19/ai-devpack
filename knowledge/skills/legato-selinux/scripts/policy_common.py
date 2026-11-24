"""Shared fail-closed manifest/profile handling. JSON flow YAML only."""
import json
from pathlib import Path
import re
from string import Formatter

import platform_profile
from extract_manifest import CATEGORIES


def records(manifest):
    yield 'identity', manifest['identity']
    for category in [*CATEGORIES, 'processes']:
        for item in manifest[category]:
            yield category, item


def load(manifest_path, profile_path, approved=False):
    manifest = json.loads(Path(manifest_path).read_text())
    # Reuse the precise stdlib schema validator shipped with this skill.
    import importlib.util
    base = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('manifest_validator', base / 'tests/validate_schema.py')
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    schema = json.loads((base / 'references/manifest.schema.json').read_text())
    validator.validate(manifest, schema, schema)
    ids = [item['id'] for _, item in records(manifest)]
    if any(not re.fullmatch(r'[A-Za-z0-9_-]+', identifier) for identifier in ids):
        raise ValueError('Unsafe manifest ID')
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate manifest IDs')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', manifest['app']):
        raise ValueError('Unsafe app identifier')
    for item in manifest['processes']:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', item['executable']):
            raise ValueError('Unsafe executable identifier')
    if approved:
        approval = manifest['approval']
        if (any(not str(approval.get(k, '')).strip() for k in ('by', 'date', 'version'))
                or approval['version'] != manifest['version']):
            raise ValueError('Manifest needs approval by/date/version for this version')
        if any(item['status'] != 'approved' for _, item in records(manifest)):
            raise ValueError('Every manifest item must have status approved')
        if manifest['unknowns']:
            raise ValueError('Approved manifest still contains unknowns')
    unknown = []
    profile = platform_profile.load(Path(profile_path), unknown.append)
    if unknown:
        raise ValueError('; '.join(unknown))
    raw = platform_profile.loads_toml(Path(profile_path).read_text())
    # Optional tooling data supplements the unchanged part-1 contract.
    platform_profile.check_types(raw, 'tooling',
        strings=('baseline', 'enforcing_evidence', 'platform_root', 'adef'),
        tables={'contexts': ('installed', 'writable')})
    for key in ('baseline', 'enforcing_evidence'):
        if key in raw and (Path(raw[key]).is_absolute() or '..' in Path(raw[key]).parts):
            raise ValueError(key + ' must be relative to the profile directory')
    if 'contexts' in raw and any(not platform_profile.template(x) for x in raw['contexts'].values()):
        raise ValueError('Invalid context template')
    if 'makefile' in raw.get('build', {}):
        if not platform_profile.template(raw['build']['makefile']):
            raise ValueError('Invalid build.makefile template')
        profile['build']['makefile'] = raw['build']['makefile']
    for key in ('contexts', 'bindings', 'baseline', 'enforcing_evidence', 'platform_root', 'adef'):
        if key in raw:
            profile[key] = raw[key]
    for template in profile['interfaces'].values():
        if re.search(r'\b(permissive|unconfined\w*|dontaudit)\b', template):
            raise ValueError('Unsafe interface template')
    if approved:
        for grant in profile['base_inherited_grants']:
            if not any(x['grant'] == grant and x.get('deviation_id')
                       for x in manifest['inherited_grants']):
                raise ValueError('Missing approved inherited-grant deviation: ' + grant)
    bindings = profile.get('bindings', {})
    if not isinstance(bindings, dict):
        raise ValueError('bindings must be a table')
    for identifier, binding in bindings.items():
        if not isinstance(binding, dict):
            raise ValueError('binding must be a table: ' + identifier)
        for key, value in binding.items():
            if key not in {'type', 'server', 'receiver', 'creator', 'cap', 'class', 'permissions'}:
                raise ValueError('Unsupported binding field: ' + key)
            if key == 'permissions':
                if not isinstance(value, list) or not value or any(
                        not isinstance(x, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', x) for x in value):
                    raise ValueError('Invalid binding permissions')
            elif not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
                raise ValueError('Invalid binding identifier: ' + key)
    return manifest, profile


def values(manifest, profile, executable=None):
    result = dict(prefix=profile['prefix'], app=manifest['app'],
                  executable=executable or manifest['app'],
                  admin_source_domain=profile['admin_source_domain'],
                  platform_root=profile.get('platform_root', ''), adef=profile.get('adef', ''))
    for key in ('domain', 'exec_type', 'data_type'):
        result[key] = profile[key + '_pattern'].format_map(result)
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', result[key]):
            raise ValueError('Invalid derived ' + key)
    return result


def render(template, context):
    for _, field, _, _ in Formatter().parse(template):
        if field and (field not in context or context[field] in ('', 'unknown')):
            raise ValueError('Unresolved template field: ' + field)
    return template.format_map(context)


def item_values(manifest, profile, item, executable):
    context = values(manifest, profile, executable)
    binding = profile.get('bindings', {}).get(item['id'], {})
    if not isinstance(binding, dict):
        raise ValueError('bindings.' + item['id'] + ' must be a table')
    for key, value in binding.items():
        if key in platform_profile.FIELDS and isinstance(value, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
            context[key] = value
    context['client'] = context.get('client', context['domain'])
    return context


def mapping(category, item):
    if category == 'processes':
        return ['entrypoint', 'admin_registration']
    if category == 'files':
        return ['file_rw' if set(item.get('access', [])) & {'write', 'create', 'unlink', 'rw', 'w'} else 'file_read']
    return [dict(data_dirs='private_data', ipc='ipc_' + item.get('role', 'unknown'),
                 devices='device_ioctl', unix_sockets='unix_socket', capabilities='capability',
                 exec='helper_exec', proc_sys='proc_global').get(category, category)]


NO_RULE = {'identity': 'identity is runtime configuration', 'denied': 'negative test obligation',
           'env_allowed': 'environment is runtime configuration', 'limits': 'limits are runtime configuration',
           'inherited_grants': 'base grant requires linked baseline verification'}


def traced(line, item):
    return line + ' # M:' + item['id']
