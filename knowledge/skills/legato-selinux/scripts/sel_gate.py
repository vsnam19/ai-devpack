#!/usr/bin/env python3
"""Supplementary source gate plus authoritative linked-policy baseline checks."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from policy_common import load, records, render, values


def run(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError('Tool failed: ' + command[0] + ': ' + result.stderr.strip())
    return result.stdout


def rules(text):
    return sorted(set(' '.join(line.strip().split()) for line in text.splitlines()
                      if line.strip().startswith(('allow ', 'allowxperm '))))


def has_transition(text, expected):
    def tokens(statement):
        return re.findall(r'[A-Za-z0-9_]+', re.sub(r'\btype_transition\b', 'typetransition', statement))
    wanted = tokens(expected)
    # Compare complete statements, preserving identifier boundaries.
    code = '\n'.join(line.split('#', 1)[0] for line in text.splitlines())
    statements = re.findall(r'(?m)^\s*(?:type_transition|typetransition)\b[^;]*;', code)
    return any(tokens(statement) == wanted for statement in statements)


def check(args):
    try:
        manifest, profile = load(args.manifest, args.profile, approved=True)
    except (ValueError, OSError, KeyError, TypeError) as error:
        return dict(status='FAIL', errors=[str(error)]), 2
    ctx = values(manifest, profile)
    module = Path(args.module)
    names = {k: render(v, ctx) for k, v in profile['module_files'].items()}
    errors, seen = [], set()
    items = {item['id']: item for _, item in records(manifest)}
    sources = []
    for path in sorted(module.rglob('*')):
        if path.is_file() and path.suffix in ('.te', '.if'):
            sources.extend((path.name, n, line) for n, line in enumerate(path.read_text().splitlines(), 1))
    source_text = '\n'.join(line.split('#', 1)[0] for _, _, line in sources)
    deviations = {x['deviation_id']: x for x in manifest['inherited_grants']}
    for filename, number, line in sources:
        code = line.split('#', 1)[0].strip()
        references = re.findall(r'\bM:([A-Za-z0-9_-]+)', line)
        if code:
            # All non-comment policy statements, including macros and multiline continuations, need provenance.
            if not references or any(ref not in items for ref in references):
                errors.append(f'UNTRACED {filename}:{number}')
            else:
                seen.update(references)
            if re.search(r'\b(permissive|unconfined\w*|dontaudit)\b', code):
                errors.append(f'UNSAFE {filename}:{number}')
            for banned in profile['banned']:
                matched = banned in code
                if banned == 'wildcard allows':
                    matched = bool(re.search(r'\ballow\b.*\*', code))
                if banned == 'generic writable labels':
                    matched = bool(re.search(r'\b(default_t|unlabeled_t|device_t)\b.*\b(write|create|manage)\b', code))
                if matched and not any(re.search(r'\bD:' + re.escape(d) + r'\b', line) for d in deviations):
                    errors.append(f'BANNED {banned} {filename}:{number}')
        elif 'no-rule-needed:' in line and line.split('no-rule-needed:', 1)[1].strip():
            seen.update(ref for ref in references if ref in items)
    for identifier in items.keys() - seen:
        errors.append('UNCOVERED ' + identifier)
    expected = render(profile['transition_check'], ctx)
    if not has_transition(source_text, expected):
        errors.append('MISSING_TRANSITION ' + expected)
    for key, name in names.items():
        if not (module / name).is_file():
            errors.append('MISSING_FILE ' + name)
    fc = module / names['fc']
    if not fc.is_file() or not any(x.strip() and not x.lstrip().startswith('#') for x in fc.read_text().splitlines()):
        errors.append('EMPTY_FC')
    component = module / names['component']
    bundle = render(profile['module_bundle_path'], ctx)
    if not component.is_file() or bundle not in component.read_text():
        errors.append('MISSING_BUNDLE ' + bundle)
    if any('TODO' in line for _, _, line in sources):
        errors.append('INCOMPLETE_MAPPING')
    missing = [x for x in ('sesearch', 'seinfo') if not shutil.which(x)]
    linked_checked = False
    if not args.linked_policy:
        missing.append('linked-policy')
    elif not Path(args.linked_policy).is_file():
        errors.append('LINKED_POLICY_NOT_FOUND')
    elif not missing:
        baseline_name = profile.get('baseline')
        if not baseline_name:
            errors.append('MISSING_BASELINE')
        else:
            baseline = json.loads((Path(args.profile).resolve().parent / baseline_name).read_text())
            owners = {p['executable'] for p in manifest['processes']}
            for denied in manifest['denied']:
                if not denied.get('processes') or any(x not in owners for x in denied['processes']):
                    errors.append('UNRESOLVED_DENIAL_OWNER ' + denied['id'])
            transitions = run(['sesearch', '-T', '-s', profile['admin_source_domain'], args.linked_policy])
            if not has_transition(transitions, expected):
                errors.append('MISSING_LINKED_TRANSITION')
            for process in manifest['processes']:
                domain = values(manifest, profile, process['executable'])['domain']
                effective = sorted(set(rules(run(['sesearch', '-A', '-s', domain, args.linked_policy])) +
                                       rules(run(['sesearch', '--allowxperm', '-s', domain, args.linked_policy]))))
                if domain not in baseline or effective != sorted(baseline[domain]):
                    errors.append('BASELINE_MISMATCH ' + domain)
                for denied in manifest['denied']:
                    # Negative queries require explicit reviewed target/class/permissions in bindings.
                    binding = profile.get('bindings', {}).get(denied['id'], {})
                    if process['executable'] not in denied.get('processes', []):
                        continue
                    if not all(binding.get(k) for k in ('type', 'class', 'permissions')):
                        errors.append('UNRESOLVED_DENIAL ' + denied['id'])
                        continue
                    for permission in binding['permissions']:
                        found = rules(run(['sesearch', '-A', '-s', domain, '-t', binding['type'],
                                           '-c', binding['class'], '-p', permission, args.linked_policy]))
                        if permission == 'ioctl':
                            found += rules(run(['sesearch', '--allowxperm', '-s', domain, '-t',
                                                binding['type'], '-c', binding['class'], args.linked_policy]))
                        if found:
                            errors.append('DENIED_ALLOWED ' + denied['id'])
            permissive = run(['seinfo', '--permissive', args.linked_policy])
            # SETools prints a heading and count even for an empty result.
            if not re.search(r'Permissive Types:\s*\d+', permissive):
                errors.append('UNRECOGNIZED_PERMISSIVE_REPORT')
            if re.search(r'Permissive Types:\s*[1-9]', permissive) or any(
                    re.fullmatch(r'\s*[A-Za-z_][A-Za-z0-9_]*\s*', x)
                    for x in permissive.splitlines() if x.strip()):
                errors.append('PERMISSIVE_DOMAINS')
            linked_checked = True
    if args.release:
        evidence = profile.get('enforcing_evidence')
        if not evidence or (Path(args.profile).resolve().parent / evidence).read_text().strip() != 'Enforcing':
            errors.append('ENFORCING_NOT_VERIFIED')
        if missing:
            errors.append('TOOL_MISSING ' + ', '.join(missing))
    status = 'FAIL' if errors else 'TOOL_MISSING' if missing else 'PASS'
    return dict(status=status, errors=errors, missing=missing, linked_checked=linked_checked,
                note='Source checks supplement linked policy and target release verification.'), (1 if errors else 3 if missing else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('module', 'manifest', 'profile'):
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--linked-policy')
    parser.add_argument('--release', action='store_true')
    args = parser.parse_args()
    try:
        report, code = check(args)
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        report, code = dict(status='FAIL', errors=[str(error)]), 1
    print(json.dumps(report, indent=2))
    return code


if __name__ == '__main__':
    sys.exit(main())
