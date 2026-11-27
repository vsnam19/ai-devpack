#!/usr/bin/env python3
"""Classify AVC evidence without changing policy or the input manifest."""
import argparse
import json
from pathlib import Path
import re
import sys

from policy_common import load, values


def parse(line):
    if not re.search(r'avc:\s*denied\s*\{', line, re.I):
        return None
    fields = {m[1]: m[2] if m[2] is not None else m[3]
              for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^\s]+))', line)}
    if not all(k in fields for k in ('scontext', 'tcontext', 'tclass')):
        return None
    fields['domain'] = fields['scontext'].split(':')[2]
    fields['target_type'] = fields['tcontext'].split(':')[2]
    fields['permissions'] = re.search(r'\{([^}]+)\}', line)[1].split()
    return fields


def object_class(item, profile, category):
    klass = profile.get('bindings', {}).get(item['id'], {}).get('class', item.get('class',
        dict(files='file', data_dirs='dir', devices='chr_file', unix_sockets='sock_file',
             denied='file').get(category)))
    return None if klass == 'unknown' else klass


def match(item, avc, manifest, profile, category=None):
    domains = [values(manifest, profile, x)['domain'] for x in item.get('processes', []) if x != 'unknown']
    if avc['domain'] not in domains:
        return False
    path = avc.get('path', '')
    path = path if Path(path).is_absolute() else None
    binding = profile.get('bindings', {}).get(item['id'], {})
    object_match = (path and path == item.get('path')) or (binding.get('type') == avc['target_type'])
    permissions = binding.get('permissions', item.get('access', []))
    klass = object_class(item, profile, category)
    return bool(object_match and (klass == avc['tclass']) and
                set(avc['permissions']) <= set(permissions))


def triage(args):
    manifest, profile = load(args.manifest, args.profile)
    domains, patch = {}, {}
    approved = [(key, x) for key, items in manifest.items() if isinstance(items, list) and key != 'denied'
                for x in items if isinstance(x, dict) and x.get('status') == 'approved']
    known = {values(manifest, profile, p['executable'])['domain']: p['executable'] for p in manifest['processes']}
    for number, line in enumerate(Path(args.log).read_text().splitlines(), 1):
        avc = parse(line)
        if avc is None:
            continue
        if avc['target_type'] in ('default_t', 'unlabeled_t', 'device_t', 'file_t'):
            category = 'label-problem'
        elif any(item['status'] == 'approved' and match(item, avc, manifest, profile, 'denied')
                 for item in manifest['denied']):
            category = 'likely-code-bug'
        elif any(match(item, avc, manifest, profile, key) for key, item in approved):
            category = 'covered-by-approved-item'
        else:
            category = 'missing-in-manifest'
            item = dict(id=f'AVC-{number:04}', why='', evidence=[str(Path(args.log)) + ':' + str(number)],
                        status='draft', processes=[known.get(avc['domain'], 'unknown')],
                        path=avc.get('path') if Path(avc.get('path', '')).is_absolute() else 'unknown',
                        access=avc['permissions'], expression=line)
            if item['path'] == 'unknown':
                patch.setdefault('unknowns', []).append(
                    item['id'] + ': What absolute path identifies the denied object?')
            patch.setdefault('files', []).append(item)
        domains.setdefault(avc['domain'], []).append({**avc, 'classification': category, 'evidence': line})
    # Exclusive creation prevents accidental replacement of any evidence or policy input.
    patch_path = Path(args.patch_out or str(args.log) + '.draft-patch.json')
    report_path = Path(args.report_out or str(args.log) + '.triage.json')
    if patch_path.resolve() == report_path.resolve() or patch_path.exists() or report_path.exists():
        raise ValueError('Output files must be distinct new files')
    skipped = [dict(id=item['id'], reason='No known object class; provide an explicit class')
               for key, item in approved if not object_class(item, profile, key)]
    report = dict(domains=domains, patch_file=str(patch_path.resolve()), skipped_items=skipped)
    with patch_path.open('x') as stream:
        json.dump(patch, stream, indent=2)
    with report_path.open('x') as stream:
        json.dump(report, stream, indent=2)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('log', 'manifest', 'profile'):
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--patch-out')
    parser.add_argument('--report-out')
    args = parser.parse_args()
    try:
        print(json.dumps(triage(args), indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError, IndexError) as error:
        print('avc_triage: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
