#!/usr/bin/env python3
# Credential-free broker queue client. Exit codes: 0 job passed; 1 refusal/failure/timeout; 2 invalid arguments.
"""Submit a bounded worker request without importing host execution code."""

import argparse
import json
import os
from pathlib import Path
import time
import uuid


def main(argv=None):
    """Submit one request, await its atomic response, and preserve dispatcher exit codes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--submit', action='store_true')
    parser.add_argument('--engine', required=True)
    parser.add_argument('--brief', required=True)
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--allow', action='extend', nargs='+', default=[])
    parser.add_argument('--gate-cmd', default='')
    parser.add_argument('--components', default=None)
    parser.add_argument('--all-components', action='store_true')
    parser.add_argument('--mode', choices=('hitl', 'autonomous'), default=None)
    parser.add_argument('--timeout', type=float, default=900)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    if args.all_components and args.components is not None and args.components.strip():
        parser.error('cannot specify both --components and --all-components')
    work = Path(args.workdir).resolve()
    queue = work / '.devpack/queue'
    payload = {'engine': args.engine, 'brief': args.brief, 'allow': args.allow}
    if args.gate_cmd:
        payload['gate_cmd'] = args.gate_cmd
    if args.mode is not None:
        payload['mode'] = args.mode
    if args.components is not None:
        comp_list = [item.strip() for item in args.components.split(',') if item.strip()]
        if not comp_list:
            parser.error('--components must specify at least one component name')
        payload['components'] = comp_list
    if args.all_components:
        payload['all_components'] = True
    request_id = uuid.uuid4().hex
    try:
        if not (work / '.devpack/broker.json').is_file() or not queue.is_dir():
            raise ValueError('broker is not running')
        temporary = queue / (request_id + '.tmp')
        with temporary.open('x', encoding='utf-8') as handle:
            json.dump(payload, handle, separators=(',', ':'))
        os.replace(temporary, queue / (request_id + '.request.json'))
        response = queue / (request_id + '.response.json')
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            if response.is_file():
                result = json.loads(response.read_text(encoding='utf-8'))
                print(json.dumps(result, separators=(',', ':')))
                if result.get('status') == 'AWAITING_APPROVAL':
                    return 20
                return 0 if result.get('status') == 'ok' and result.get('gate') == 'ok' and result.get('scope') == 'ok' else 1
            time.sleep(0.05)
        raise ValueError('broker response timeout; job may still be running')
    except (OSError, ValueError) as exc:
        print(json.dumps({'run_dir': '', 'status': 'harness_error', 'gate': 'skipped',
                          'scope': 'ok', 'changed_files': [], 'final_text_tail': '',
                          'cost_usd': None, 'error': str(exc)}, separators=(',', ':')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
