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
                events = []
                for index, cmd in enumerate((submit, command)):
                    events.extend([
                        {'type': 'assistant', 'message': {'content': [
                            {'type': 'tool_use', 'id': f'call-{index}', 'name': 'Bash',
                             'input': {'command': cmd}}]}},
                        {'type': 'user', 'message': {'content': [
                            {'type': 'tool_result', 'tool_use_id': f'call-{index}',
                             'content': 'output'}]}},
                    ])
                stream = '\n'.join(map(json.dumps, events))
                result = analyze_orchestrator_transcript(stream)
                self.assertFalse(result['compliant'])
                self.assertEqual([command], result['disallowed_bash_commands'])
                self.assertEqual({'count': 0, 'commands': []}, result['denied_tool_calls'])

    def test_allowed_bash_commands(self):
        """The exact submit prefix and read-only Git commands remain accepted."""
        commands = ['python3 .devpack/pack/runner/devpack_dispatch.py --submit --allow "src/**"',
                    'git status', 'git diff --stat', 'git log -5']
        stream = '\n'.join(json.dumps({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'name': 'Bash', 'input': {'command': cmd}}]}})
                           for cmd in commands)
        result = analyze_orchestrator_transcript(stream)
        self.assertTrue(result['compliant'])
        self.assertEqual([], result['disallowed_bash_commands'])

    def test_denied_attempt_is_metric_not_failure(self):
        """A denied scan followed by dispatch remains compliant."""
        events = [
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'id': 'scan', 'name': 'Bash',
                 'input': {'command': 'ls; grep x src/file.cpp'}},
                {'type': 'tool_use', 'id': 'submit', 'name': 'Bash',
                 'input': {'command': 'python3 .devpack/pack/runner/devpack_dispatch.py --submit'}}]}},
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'scan', 'is_error': True,
                 'content': [{'type': 'text', 'text': "Permission to use Bash has been denied because Claude Code is running in don't ask mode"}]}]}},
        ]
        result = analyze_orchestrator_transcript('\n'.join(map(json.dumps, events)))
        self.assertTrue(result['compliant'])
        self.assertEqual([], result['disallowed_bash_commands'])
        self.assertEqual(1, result['denied_tool_calls']['count'])
        self.assertEqual(['ls; grep x src/file.cpp'], result['denied_tool_calls']['commands'])

    def test_unanswered_forbidden_attempt_is_not_execution(self):
        """A tool use without a result cannot prove command execution."""
        stream = json.dumps({'type': 'assistant', 'message': {'content': [
            {'type': 'tool_use', 'id': 'scan', 'name': 'Bash', 'input': {'command': 'ls'}},
            {'type': 'tool_use', 'name': 'Bash', 'input': {
                'command': 'python3 .devpack/pack/runner/devpack_dispatch.py --submit'}}]}})
        result = analyze_orchestrator_transcript(stream)
        self.assertTrue(result['compliant'])
        self.assertEqual([], result['disallowed_bash_commands'])

    def test_command_error_is_still_execution(self):
        """An OS permission error is not a tool-policy denial."""
        events = [
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'id': 'scan', 'name': 'Bash',
                 'input': {'command': 'cat protected.txt'}}]}},
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'scan', 'is_error': True,
                 'content': 'cat: protected.txt: Permission denied'}]}},
        ]
        result = analyze_orchestrator_transcript('\n'.join(map(json.dumps, events)))
        self.assertEqual(['cat protected.txt'], result['disallowed_bash_commands'])
        self.assertEqual({'count': 0, 'commands': []}, result['denied_tool_calls'])
