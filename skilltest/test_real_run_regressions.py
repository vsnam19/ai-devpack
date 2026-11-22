"""Offline regressions for defects exposed by the first real skill trial."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from runner.agent_exec import AgentResult
from runner.devpack_logs import Redactor, load_run_dir
from skilltest import skilltest as harness

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'knowledge/skills/doxygen-naming'


class RealRunTests(unittest.TestCase):
    """Verify validation, final evidence, and token-aware grading."""

    def test_required_fields_are_validated_before_any_trial(self):
        """A malformed later case prevents even the first agent invocation."""
        for kind, field, value in [('trigger', 'name', None), ('trigger', 'prompt', ''),
                                   ('trigger', 'expect', 'true'), ('behaviour', 'name', 2),
                                   ('behaviour', 'prompt', None)]:
            with self.subTest(kind=kind, field=field), tempfile.TemporaryDirectory() as temp:
                skill = Path(temp) / SKILL.name
                shutil.copytree(SKILL, skill)
                config = harness.load_config(skill)
                if value is None:
                    del config[kind][-1][field]
                else:
                    config[kind][-1][field] = value
                with patch.object(harness, 'load_config', return_value=config), patch.object(harness, 'run_agent') as agent:
                    errors = harness.lint_skill(skill)
                    self.assertTrue(any(field in error for error in errors), errors)
                    self.assertEqual(harness.main(['run', str(skill), '--engines', 'haiku', '--k', '1',
                                                  '--results-root', str(Path(temp) / 'results')]), 1)
                    agent.assert_not_called()

    def test_trials_finalize_and_preserve_grader_evidence(self):
        """Both trial kinds record final verdicts and summaries explain failures."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def agent(request):
                """Produce an empty offline transcript without changing the fixture."""
                path = root / 'stream.jsonl'
                path.write_text('')
                return AgentResult(transcript_path=path)

            with patch.object(harness, 'run_agent', side_effect=agent):
                self.assertEqual(harness.main(['run', str(SKILL), '--engines', 'haiku', '--k', '1',
                                              '--results-root', str(root)]), 0)
            summary = json.loads(next(root.rglob('summary.json')).read_text())
            for trial, run in zip(summary['trials'], sorted(root.glob('*/doxygen-naming/runs/*'))):
                metadata = json.loads((run / 'run.json').read_text())
                self.assertTrue(metadata['finished'])
                self.assertEqual(metadata['outcome'], 'fail' if trial['arm'] != 'trigger' or trial['expected'] else 'pass')
                if trial['arm'] != 'trigger':
                    self.assertTrue((run / 'steps/02-grader.txt').is_file())
                    self.assertTrue(trial['reason'])
                    record = load_run_dir(run, Redactor())
                    self.assertTrue(any(step.is_gate for step in record.steps))
                    self.assertIn(trial['reason'], next(root.rglob('summary.md')).read_text())

    def test_grader_captures_both_streams_and_first_failure(self):
        """The summary reason selects a failing check rather than preceding chatter."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            grader = root / 'grader'
            grader.mkdir()
            (grader / 'check.sh').write_text('echo "checking candidate"\necho "FAIL: missing tags"\necho "details" >&2\nexit 1\n')
            stream = root / 'stream.jsonl'
            stream.write_text('')
            with patch.object(harness, 'run_agent', return_value=AgentResult(transcript_path=stream)):
                _, passed, telemetry = harness._execute_trial(SKILL, 'haiku', 'document', False,
                    root / 'run', None, 'unused', 'local', 'bash check.sh', grader)
            self.assertFalse(passed)
            self.assertEqual(telemetry['reason'], 'FAIL: missing tags')
            evidence = (root / 'run/steps/02-grader.txt').read_text()
            self.assertIn('checking candidate', evidence)
            self.assertIn('details', evidence)

    def test_runtime_counter_accepts_allocation_words_in_comments(self):
        """Comments and identifiers do not count as heap allocation tokens."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = ROOT / 'knowledge/skills/misra-cpp-2023/tests/expected/runtime-counter/counter.cpp'
            text = source.read_text()
            (root / 'counter.cpp').write_text(text + '\n// A new reading does not delete history.\n')
            result = subprocess.run(['python3', str(ROOT / 'skilltest/case_check.py'),
                                     'misra-cpp-2023', 'runtime-counter', str(root)],
                                    env={**os.environ, 'DEVPACK_ROOT': str(ROOT)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_header_prompt_permits_required_renames(self):
        """The naming grader's required API rename is authorized by the task."""
        case = harness.load_config(SKILL)['behaviour'][0]
        self.assertIn('rename', case['prompt'].lower())
        self.assertNotIn('preserve its interface', case['prompt'])
