"""Regression coverage for adopting runner commands outside the pack root."""
from pathlib import Path
import contextlib
import io
import tempfile
import unittest

from runner import agent_exec, brief_lint, devpack_run


class AdoptionRunnerTests(unittest.TestCase):
    """Exercise project configuration and brief validation contracts."""

    def test_project_paths_relative_to_config(self):
        """Project-owned paths resolve against the project configuration directory."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / '.devpack' / 'project.toml'
            cfg.parent.mkdir()
            cfg.write_text('engines_config = "engines.toml"\ngate_config = "gate.toml"\n')
            result = agent_exec.load_project_config(root)
            self.assertEqual(str(cfg.parent / 'engines.toml'), result['engines_config'])
            self.assertEqual(str(cfg.parent / 'gate.toml'), result['gate_config'])

    def test_default_gate_requires_project_config(self):
        """The default gate cannot silently run the pack's self-test gate."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'gate_config'):
                agent_exec.resolve_project_gate_command(Path(tmp))

    def test_project_gate_command_uses_pack_script(self):
        """A configured project gate launches the installed pack's script."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / '.devpack' / 'project.toml'
            cfg.parent.mkdir()
            gate = cfg.parent / 'gate.toml'
            gate.write_text('[[check]]\nname="build"\ncmd=["true"]\n')
            cfg.write_text('gate_config="gate.toml"\n')
            command = agent_exec.resolve_project_gate_command(root)
            self.assertEqual(str(Path(agent_exec.__file__).resolve().parents[1] / 'enforcement/gate/gate.py'), command[1])
            self.assertIn(str(gate), command)
            self.assertIn('--all-components', command)

    def test_runner_accepts_gate_config(self):
        """The task runner exposes a project gate override."""
        args = devpack_run.build_arg_parser().parse_args(['--gate-config', 'gate.toml'])
        self.assertEqual('gate.toml', args.gate_config)

    def test_missing_engine_file_is_error(self):
        """A missing engines configuration must not disable budget validation."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'engines config'):
                brief_lint.resolve_engine_brief_budget('local', Path(tmp) / 'missing.toml')

    def test_unknown_engine_is_error(self):
        """Unknown engine identifiers are configuration errors."""
        with self.assertRaisesRegex(ValueError, 'Unknown engine'):
            brief_lint.resolve_engine_brief_budget('nonexistent')

    def test_created_symbol_is_not_missing(self):
        """Expected output can introduce a new interface."""
        with tempfile.TemporaryDirectory() as tmp:
            findings = brief_lint.lint_brief_text(
                '# Goal\nImplement widget.\n# Input\nExisting source.\n'
                '# Expected output\nCreate function `newWidget()`.\n'
                '# Files allowed\n- `widget.cpp`\n# Constraints\nKeep scope.\n# Gate\nRun tests.\n',
                repo_root=tmp,
            )
            self.assertFalse([f for f in findings if f['rule'] == 'BRIEF-SYMBOL-MISSING'])

    def test_excluded_allowed_path_warns(self):
        """Files-allowed entries that name excluded paths remain visible to authors."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.devpack').mkdir()
            (root / '.devpack/project.toml').write_text(
                '[workspace]\nroot="src"\nexclude=["src/generated/**"]\n'
            )
            findings = brief_lint.lint_brief_text(
                '# Goal\nChange widget.\n# Input\nExisting source.\n'
                '# Expected output\nWidget.\n# Files allowed\n- `src/generated/**`\n'
                '# Constraints\nKeep scope.\n# Gate\nRun tests.\n', repo_root=root,
            )
            self.assertTrue(any(f['rule'] == 'BRIEF-SCOPE-EXCLUDED' and f['severity'] == 'warning' for f in findings))

    def test_installer_copies_bundled_skills(self):
        """Installing skills creates project-local readable skill files before a task."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                rc = agent_exec.main(['--install-skills', str(root)])
            self.assertEqual(0, rc)
            self.assertTrue((root / '.claude/skills/review-taxonomy/SKILL.md').is_file())

    def test_task_refuses_modified_explicit_gate_config(self):
        """A worker cannot replace the project gate even when its path is explicitly allowed."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate = root / 'quality.toml'
            gate.write_text('[[check]]\nname="build"\ncmd=["false"]\n')
            engine = devpack_run.EngineConfig(name='local', model='local', engine_class='local')
            config = {'roles': {role: engine for role in ('implementer', 'reviewer', 'fixer')}, 'gate_cmd': ['true']}
            context = devpack_run.load_task_data(None, 'gate-guard', 'Gate guard', 'Update source', 'quality.toml')

            def mutate_gate(request):
                """Simulate an implementation that replaces its own quality controls."""
                gate.write_text('[[check]]\nname="build"\ncmd=["true"]\n')
                return devpack_run.agent_exec.AgentResult(status='ok', exit_code=0, final_text='VERDICT: APPROVE')

            with mock.patch.object(devpack_run, 'run_agent', side_effect=mutate_gate), contextlib.redirect_stdout(io.StringIO()):
                rc, summary = devpack_run.run_task_workflow(
                    config, context, root, root / 'logs', root, gate_config=gate,
                    isolation='local', allow_unsandboxed_local=True,
                )
            self.assertEqual(1, rc)
            self.assertEqual('violation', summary['scope'])
            self.assertIn('quality.toml', summary['gate_touched'])
