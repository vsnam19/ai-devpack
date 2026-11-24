#!/usr/bin/env python3
"""Generate reviewable policy from approved JSON-flow YAML and project TOML."""
import argparse
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

from policy_common import NO_RULE, item_values, load, mapping, records, render, traced, values


def generate(manifest, profile, output):
    if output.exists() and any(output.iterdir()):
        raise ValueError('Refusing to overwrite nonempty module directory')
    context = values(manifest, profile)
    files = {key: render(value, context) for key, value in profile['module_files'].items()}
    if len(set(files.values())) != len(files):
        raise ValueError('Module filenames must be distinct')
    te, fc, external, questions = [], [], [], []
    processes = {x['executable']: x for x in manifest['processes']}
    if not processes:
        raise ValueError('No approved process entrypoint')
    first = manifest['processes'][0]
    te.append(traced('policy_module(' + manifest['app'] + ', 1.0)', first))
    if profile.get('skeleton_generator'):
        with tempfile.TemporaryDirectory() as tmp:
            command_context = {**context, 'output': tmp}
            command = shlex.split(render(profile['skeleton_generator'], command_context))
            root = Path(profile.get('platform_root', '')).resolve()
            if not profile.get('platform_root'):
                raise ValueError('skeleton_generator requires explicit platform_root')
            command[0] = str(root / command[0])
            subprocess.run(command, cwd=root, check=True, timeout=60, capture_output=True, text=True)
            for path in Path(tmp).rglob('*.te'):
                for line in path.read_text().splitlines():
                    # Only single-line type declarations survive; headers use our app identity.
                    if re.fullmatch(r'\s*type\s+[A-Za-z_][A-Za-z0-9_]*\s*;\s*', line):
                        te.append(traced(line.strip(), first))
    for category, item in records(manifest):
        if category in NO_RULE:
            te.append('# M:' + item['id'] + ' no-rule-needed: ' + NO_RULE[category])
            continue
        owners = [item['executable']] if category == 'processes' else item.get('processes', [])
        if not owners or any(owner not in processes for owner in owners):
            questions.append(item['id'] + ': resolve process ownership')
            continue
        for owner in owners:
            ctx = item_values(manifest, profile, item, owner)
            if category == 'processes':
                for key in ('domain', 'exec_type', 'data_type'):
                    declaration = 'type ' + ctx[key] + ';'
                    if not any(x.split(' #')[0] == declaration for x in te):
                        te.append(traced(declaration, item))
            external_types = {ctx[key] for key in ('type', 'server', 'receiver', 'creator')
                              if key in ctx and ctx[key] not in {values(manifest, profile, p)[k]
                                  for p in processes for k in ('domain', 'exec_type', 'data_type')}}
            if external_types:
                te.append(traced("gen_require(`", item))
                te.extend(traced('    type ' + typ + ';', item) for typ in sorted(external_types))
                te.append(traced("')", item))
            for kind in mapping(category, item):
                try:
                    template = profile['interfaces'].get(kind)
                    if not template:
                        raise ValueError('unknown mapping ' + kind)
                    for line in render(template, ctx).splitlines():
                        if line.strip():
                            te.append(traced(line, item))
                except ValueError as error:
                    questions.append(item['id'] + ': ' + str(error))
            # Framework installation and writable paths are supplied explicitly by BSP profile.
            if category == 'processes':
                for key in ('installed', 'writable'):
                    pattern = profile.get('contexts', {}).get(key)
                    if not pattern:
                        questions.append(item['id'] + ': missing contexts.' + key)
                    else:
                        fc.append(traced(render(pattern, ctx), item))
            path = item.get('path', '')
            if path.startswith(('/etc/', '/data/', '/var/lib/', '/dev/')):
                typ = ctx.get('type')
                if not typ:
                    questions.append(item['id'] + ': external path requires explicit type binding')
                else:
                    external.append(traced(re.escape(path) + ' gen_context(system_u:object_r:' + typ + ',s0)', item))
    # transition_check is an expected linked statement, not necessarily valid source syntax.
    expected = render(profile['transition_check'], context)
    transition = profile['interfaces'].get('app_transition')
    if transition:
        te.extend(traced(line, first) for line in render(transition, context).splitlines())
    else:
        questions.append(first['id'] + ': missing app_transition mapping for ' + expected)
    labels, conflicts = {}, set()
    for line in fc + external:
        fields = line.split(' #', 1)[0].split()
        key, label = ' '.join(fields[:-1]), fields[-1]
        if key in labels and labels[key] != label:
            conflicts.add(key)
            questions.append('conflicting context: ' + key + '; resolve shared/per-process labels')
        labels[key] = label
    fc = list(dict.fromkeys(line for line in fc
              if ' '.join(line.split(' #', 1)[0].split()[:-1]) not in conflicts))
    external = list(dict.fromkeys(line for line in external
                    if ' '.join(line.split(' #', 1)[0].split()[:-1]) not in conflicts))
    build_template = profile['build'].get('makefile')
    if not build_template:
        questions.append(first['id'] + ': missing build.makefile SDK build template')
    makefile = '\n'.join('# Required SDK include variable: ' + x for x in profile['build']['include_variables'])
    makefile += '\n' + (render(build_template, context) if build_template else
                          '$(error Missing reviewed build.makefile in platform profile)')
    for question in questions:
        te.append('# TODO ' + question)
    bundle = render(profile['module_bundle_path'], context)
    output.mkdir(parents=True, exist_ok=True)
    content = {'te': '\n'.join(te), 'fc': '\n'.join(fc), 'if': '# Interfaces are project-owned; no public interface exported.',
               'makefile': makefile,
               'component': 'bundles:\n{\n    file:\n    {\n        [r] ' + manifest['app'] + '.pp /' + bundle + '\n    }\n}'}
    for key, name in files.items():
        (output / name).write_text(content[key] + '\n')
    (output / (manifest['app'] + '.platform.fc')).write_text('\n'.join(external) + '\n')
    (output / 'QUESTIONS.md').write_text('# Questions\n\n' + '\n'.join('- ' + x for x in questions) + '\n')
    return 3 if questions else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    try:
        manifest, profile = load(args.manifest, args.profile, approved=True)
        return generate(manifest, profile, Path(args.out))
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print('gen_policy: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
