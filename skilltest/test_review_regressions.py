"""Offline regression coverage for the second independent review."""

from pathlib import Path
import re
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from runner.agent_exec import AgentResult
from skilltest import skilltest as harness

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / 'knowledge/skills'


class ReviewRegressionTests(unittest.TestCase):
    """Exercise installation, validation, portability, grading, and configuration."""

    def test_enabled_trial_installs_assets_without_test_material(self):
        """A clean with-skill candidate passes without seeing hidden solutions."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / 'sample'
            for directory in ('references', 'assets', 'tests/graders', 'tests/expected'):
                (skill / directory).mkdir(parents=True)
            for filename in ('SKILL.md', 'references/rule.md', 'assets/data.txt', 'tests/graders/check.sh', 'tests/expected/answer.txt'):
                (skill / filename).write_text('example')
            grader = skill / 'tests/graders'
            (grader / 'check.sh').write_text('test -f "${@: -1}/OUTPUT"\n')

            def agent(request):
                """Inspect installed material and produce a valid candidate."""
                installed = request.workdir / '.claude/skills/sample'
                self.assertTrue((installed / 'SKILL.md').is_file())
                self.assertTrue((installed / 'references/rule.md').is_file())
                self.assertTrue((installed / 'assets/data.txt').is_file())
                self.assertFalse((installed / 'tests').exists())
                (request.workdir / 'OUTPUT').write_text('done')
                transcript = root / 'stream.jsonl'
                transcript.write_text('')
                return AgentResult(transcript_path=transcript)

            with patch.object(harness, 'run_agent', side_effect=agent):
                _, passed, _ = harness._execute_trial(skill, 'haiku', 'repair', True,
                    root / 'run', None, 'unused', 'local', 'bash check.sh ignored-option', grader)
            self.assertTrue(passed)

    def test_lint_checks_cases_without_input_directories(self):
        """Missing fixture, grader, and check are diagnosed without fixture discovery."""
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / 'sample'
            (skill / 'tests').mkdir(parents=True)
            (skill / 'SKILL.md').write_text('---\nname: sample\ndescription: Example\n---\n')
            (skill / 'tests/skilltest.toml').write_text('[[behaviour]]\nname="broken"\nfixture="fixtures/absent/input"\n')
            errors = harness.lint_skill(skill)
            self.assertTrue(any('fixture does not resolve' in error for error in errors))
            self.assertTrue(any('grader does not resolve' in error for error in errors))
            self.assertTrue(any('check' in error for error in errors))

    def test_expected_drafts_are_runnable(self):
        """Authoring reference drafts lint and have executable behaviour checks."""
        for draft in (SKILLS / 'skill-authoring/tests/expected').glob('*/signal-helper'):
            self.assertEqual(harness.lint_skill(draft), [], str(draft))
            for case in harness.load_config(draft)['behaviour']:
                grader = draft / 'tests' / case['grader']
                self.assertTrue((grader / 'check.sh').is_file())
                for candidate, passes in ((draft / 'tests' / case['fixture'], False),
                                           (draft / 'tests/expected/summary', True)):
                    result = subprocess.run(['bash', 'check.sh', 'ignored-option', str(candidate)],
                                            cwd=grader, check=False)
                    self.assertEqual(result.returncode == 0, passes)


    def test_python310_fallback_reads_all_configs(self):
        """Without tomllib, loading preserves booleans, strings, and case arrays."""
        configs = list(SKILLS.glob('*/tests/skilltest.toml'))
        configs += list((SKILLS / 'skill-authoring/tests/expected').glob('*/signal-helper/tests/skilltest.toml'))
        for config in configs:
            expected = harness.load_config(config.parent.parent)
            with patch.object(harness, 'tomllib', None):
                self.assertEqual(harness.load_config(config.parent.parent), expected)
        with patch.object(harness, 'tomllib', None):
            for skill in SKILLS.iterdir():
                if (skill / 'SKILL.md').is_file():
                    self.assertEqual(harness.lint_skill(skill), [])

    def test_check_contract_in_template_and_all_graders(self):
        """The documented command uses the last argument even with leading options."""
        checklist = (SKILLS / 'skill-authoring/references/checklist.md').read_text()
        self.assertIn('check = "bash check.sh"', checklist)
        self.assertIn('grader = ', checklist)
        self.assertIn('${@: -1}', (ROOT / 'skilltest/README.md').read_text())
        for script in SKILLS.glob('*/tests/graders/*/check.sh'):
            self.assertIn('${@: -1}', script.read_text(), str(script))
            tests = script.parents[2]
            expected = tests / 'expected' / script.parent.name
            case = next(item for item in harness.load_config(tests.parent)['behaviour']
                        if item['grader'] == f'graders/{script.parent.name}')
            env = {**os.environ, 'DEVPACK_ROOT': str(ROOT)}
            for key in ('category', 'severity', 'verdict'):
                if key in case:
                    env[f'SKILLTEST_EXPECTED_{key.upper()}'] = case[key]
            # Copy reference outputs because graders may compile within the candidate.
            with tempfile.TemporaryDirectory() as temp:
                candidate = Path(temp) / 'candidate'
                shutil.copytree(expected, candidate)
                result = subprocess.run(['bash', 'check.sh', 'ignored-option', str(candidate)],
                                        cwd=script.parent, env=env, check=False)
                self.assertEqual(result.returncode, 0, str(script))

    def test_runtime_counter_api_is_visible(self):
        """The fixture declares the function name required by the hidden grader."""
        fixture = SKILLS / 'misra-cpp-2023/tests/fixtures/runtime-counter/input/counter.cpp'
        self.assertRegex(fixture.read_text(), r'std::int32_t\s+readCounter\(const std::int32_t value\)')

    def test_documented_dry_run_executes(self):
        """The authoring command includes every required CLI argument."""
        text = (SKILLS / 'skill-authoring/SKILL.md').read_text()
        commands = re.findall(r'`([^`]*--dry-run[^`]*)`', text)
        self.assertTrue(commands)
        for command in commands:
            command = command.replace('<skill-dir>', str(SKILLS / 'skill-authoring'))
            result = subprocess.run(command.split(), cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('TRIAL PLAN', result.stdout)

    def test_local_engine_configuration_reaches_executor(self):
        """CLI engine selection preserves configured model, endpoint variables, and limits."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'engines.toml'
            config.write_text('version=2\n[engines.local]\nmodel="offline-coder"\nclass="local"\nmax_data_class="confidential"\nbase_url_env="TEST_ENDPOINT"\nauth_token_env="TEST_AUTH_TOKEN"\nmin_context_tokens=32768\ntimeout_s=42\n')
            seen = []

            def agent(request):
                """Capture the request without contacting an agent."""
                seen.append(request.engine)
                transcript = root / 'stream.jsonl'
                transcript.write_text('')
                return AgentResult(transcript_path=transcript)

            with patch.object(harness, 'run_agent', side_effect=agent):
                status = harness.main(['run', str(SKILLS / 'doxygen-naming'), '--engines', 'local',
                    '--engines-config', str(config), '--k', '1', '--results-root', str(root / 'results')])
            self.assertEqual(status, 0)
            self.assertEqual(len(seen), 8)
            for engine in seen:
                self.assertEqual((engine.name, engine.model, engine.engine_class), ('local', 'offline-coder', 'local'))
                self.assertEqual((engine.base_url_env, engine.auth_token_env), ('TEST_ENDPOINT', 'TEST_AUTH_TOKEN'))
                self.assertEqual((engine.min_context_tokens, engine.timeout_s), (32768, 42))
