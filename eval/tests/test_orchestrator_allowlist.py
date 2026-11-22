"""Regression tests for transcript Bash permission audits."""
import json
import unittest
from eval.run_eval import analyze_orchestrator_transcript


class TranscriptAllowlistTests(unittest.TestCase):
    """Reject forbidden commands even when the product and dispatch checks pass."""

    def test_forbidden_bash_commands_reported(self):
        """Shell composition, scans, writes, and direct dispatcher launches fail."""
        submit = 'python3 .devpack/pack/runner/devpack_dispatch.py --submit --engine haiku'
        for command in ('ls', 'cat src/file.cpp', 'grep x src/file.cpp',
                        "cat > briefs/x.md <<'EOF'\nx\nEOF", 'bash build_and_test.sh --no-cmake',
                        'find / -name preflight_lint.py', 'git status; ls',
                        'git diff $(cat secret)', 'git log > result',
                        'python3 runner/devpack_dispatch.py --engine haiku'):
            with self.subTest(command=command):
                stream = '\n'.join(json.dumps({'type': 'assistant', 'message': {'content': [
                    {'type': 'tool_use', 'name': 'Bash', 'input': {'command': cmd}}]}})
                                   for cmd in (submit, command))
                result = analyze_orchestrator_transcript(stream)
                self.assertFalse(result['compliant'])
                self.assertEqual([command], result['disallowed_bash_commands'])

    def test_allowed_bash_commands(self):
        """The exact submit prefix and read-only Git commands remain accepted."""
        commands = ['python3 .devpack/pack/runner/devpack_dispatch.py --submit --allow "src/**"',
                    'git status', 'git diff --stat', 'git log -5']
        stream = '\n'.join(json.dumps({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'name': 'Bash', 'input': {'command': cmd}}]}}) for cmd in commands)
        result = analyze_orchestrator_transcript(stream)
        self.assertTrue(result['compliant'])
        self.assertEqual([], result['disallowed_bash_commands'])
