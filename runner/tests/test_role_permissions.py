"""Role permission regression tests; exit through unittest's standard status."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from runner.agent_exec import AgentRequest, EngineConfig, HarnessError, build_invocation, load_engines


class RolePermissionTests(unittest.TestCase):
    """Assert the actual argv passed to a fake Claude executable."""

    def test_role_defaults(self):
        """Restricted roles never inherit a worker's bare Bash permission."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root / 'claude'
            fake.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
            fake.chmod(0o755)
            for role in ('implementer', 'fixer', 'reviewer', 'orchestrator'):
                request = AgentRequest(engine=EngineConfig(), role=role, workdir=root,
                                       agent_bin=str(fake), isolation='local')
                argv, env = build_invocation(request)
                actual = json.loads(subprocess.check_output(argv, env=env, text=True))
                start = actual.index('--allowedTools') + 1
                end = next(i for i in range(start, len(actual)) if actual[i].startswith('--'))
                tools = actual[start:end]
                self.assertEqual('dontAsk', actual[actual.index('--permission-mode') + 1])
                if role in ('implementer', 'fixer'):
                    self.assertIn('Bash', tools)
                elif role == 'reviewer':
                    self.assertEqual(['Read', 'Glob', 'Grep'], tools)
                    self.assertEqual('Read,Glob,Grep', actual[actual.index('--tools') + 1])
                else:
                    self.assertNotIn('Bash', tools)
                    self.assertEqual(['Bash(python3 .devpack/pack/runner/devpack_dispatch.py --submit:*)',
                                      'Bash(git status:*)', 'Bash(git diff:*)', 'Bash(git log:*)'],
                                     [tool for tool in tools if tool.startswith('Bash')])
                    self.assertIn('Write(briefs/**)', tools)

    def test_configured_role_tools(self):
        """Role tables select an engine and independently configure allow and deny lists."""
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / 'engines.toml'
            config.write_text('version = 2\n[engines.test]\nmodel = "test"\n'
                              '[roles.reviewer]\nengine = "test"\n'
                              'allowed_tools = ["Read"]\ndisallowed_tools = ["Bash", "Write"]\n')
            loaded = load_engines(config)
            request = AgentRequest(engine=loaded.engine_for_role('reviewer'), role='reviewer')
            self.assertEqual(('Read',), request.allowed_tools)
            self.assertEqual(('Bash', 'Write'), request.disallowed_tools)

    def test_legacy_claude_template_keeps_role_boundary(self):
        """Legacy Claude templates receive the same role permission flags."""
        request = AgentRequest(engine=EngineConfig(cmd=['claude', '-p', '{prompt}']),
                               role='orchestrator', prompt='goal', isolation='local')
        argv, _ = build_invocation(request)
        self.assertIn('--permission-mode', argv)
        self.assertIn('Bash(git status:*)', argv)
        self.assertNotIn('Bash', argv)

    def test_legacy_template_cannot_override_role_permissions(self):
        """Conflicting legacy Claude permission flags fail closed."""
        request = AgentRequest(engine=EngineConfig(cmd=['claude', '-p', '--allowedTools', 'Bash']),
                               role='orchestrator', isolation='local')
        with self.assertRaisesRegex(HarnessError, 'role table'):
            build_invocation(request)
