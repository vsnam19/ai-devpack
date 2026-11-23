"""Tests for the deterministic skill test harness."""

import json
import os
import shutil
import subprocess
import tempfile
from skilltest.skilltest import load_config
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "skilltest" / "skilltest.py"


class SkilltestCliTests(unittest.TestCase):
    """Exercise linting, transcript detection, planning, and fake-agent execution."""

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the CLI from a temporary output location."""
        return subprocess.run(
            ["python3", str(CLI), *args], cwd=ROOT, text=True, capture_output=True, check=False
        )

    def test_detect_trigger_variants(self) -> None:
        """Exact Skill tool call name equality and Read of SKILL.md trigger; substrings do not."""
        from skilltest.skilltest import transcript_triggered

        skill = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "sample"}}]}})
        substring_skill = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "sample-other"}}]}})
        arg_substring = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "other", "args": "sample"}}]}})
        read = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": ".claude/skills/sample/SKILL.md"}}]}})
        read_other = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": ".claude/skills/sample-other/SKILL.md"}}]}})
        neither = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Glob", "input": {"pattern": "*.md"}}]}})
        self.assertTrue(transcript_triggered(skill, "sample"))
        self.assertFalse(transcript_triggered(substring_skill, "sample"))
        self.assertFalse(transcript_triggered(arg_substring, "sample"))
        self.assertTrue(transcript_triggered(read, "sample"))
        self.assertFalse(transcript_triggered(read_other, "sample"))
        self.assertFalse(transcript_triggered(neither, "sample"))

    def test_lint_accepts_existing_skills(self) -> None:
        """All shipped skills meet the deterministic contract."""
        for name in ("misra-cpp-2023", "doxygen-naming", "review-taxonomy", "skill-authoring"):
            result = self.run_cli("lint", f"knowledge/skills/{name}")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_lint_rejects_bad_skills(self) -> None:
        """Lint rejects frontmatter, path, size, and case-count violations."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bad = root / "wrong"
            (bad / "tests").mkdir(parents=True)
            (bad / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")
            (bad / "tests" / "skilltest.toml").write_text("", encoding="utf-8")
            result = self.run_cli("lint", str(bad))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("frontmatter", result.stderr)

    def test_lint_rejects_absolute_paths_multiple_broken_links_and_orphaned_inputs(self) -> None:
        """Lint examines every link and every declared fixture input directory."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "sample"
            (root / "tests" / "fixtures" / "used" / "input").mkdir(parents=True)
            (root / "tests" / "fixtures" / "orphan" / "input").mkdir(parents=True)
            (root / "tests" / "graders" / "used").mkdir(parents=True)
            (root / "tests" / "fixtures" / "used" / "input" / "input.txt").write_text("input\n", encoding="utf-8")
            (root / "tests" / "graders" / "used" / "check.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            (root / "SKILL.md").write_text(
                "---\nname: sample\ndescription: sample skill\n---\n"
                "[valid](SKILL.md) and [broken](missing.md) and [root](/arbitrary/file)\n"
                "Examples: /Users/example /opt/tool /usr/bin /var/log /etc/config /tmp/work /mnt/data C:\\\\work\\\\file\n",
                encoding="utf-8",
            )
            (root / "tests" / "skilltest.toml").write_text(
                "[[trigger]]\nexpect = true\n[[trigger]]\nexpect = true\n"
                "[[trigger]]\nexpect = false\n[[trigger]]\nexpect = false\n"
                "[[behaviour]]\nname = 'used'\nfixture = 'fixtures/used/input'\ngrader = 'graders/used'\ncheck = 'bash check.sh'\n",
                encoding="utf-8",
            )
            errors = __import__("skilltest.skilltest", fromlist=["lint_skill"]).lint_skill(root)
            self.assertIn("relative link does not resolve: missing.md", errors)
            self.assertGreaterEqual(errors.count("absolute path found"), 2)
            self.assertIn("unreferenced fixture directory: fixtures/orphan/input", errors)

    def test_aggregation_flags_no_benefit(self) -> None:
        """Equal, under-k, ceiling, and Wilson-CI crossing/non-crossing outcomes are classified accurately."""
        from skilltest.skilltest import aggregate_behaviour

        report = aggregate_behaviour([True, False], [True, True])
        self.assertEqual(report["delta"], -0.5)
        self.assertEqual(report["verdict"], "inconclusive")
        self.assertTrue(report["no_measurable_benefit"])

        # k=2 with 100% vs 0% is still inconclusive because k < 3 per arm.
        under_k = aggregate_behaviour([True, True], [False, False])
        self.assertEqual(under_k["verdict"], "inconclusive")
        self.assertTrue(under_k["no_measurable_benefit"])

        # k=3 both 1.0 -> ceiling.
        ceil = aggregate_behaviour([True, True, True], [True, True, True])
        self.assertEqual(ceil["verdict"], "ceiling")
        self.assertTrue(ceil["no_measurable_benefit"])

        # k=3 with 3/3 vs 1/3 crosses 0 under Newcombe-Wilson 95% CI -> inconclusive.
        cross = aggregate_behaviour([True, True, True], [True, False, False])
        self.assertEqual(cross["verdict"], "inconclusive")
        self.assertLess(cross["delta_ci_95"][0], 0.0)
        self.assertTrue(cross["no_measurable_benefit"])

        # k=3 with 3/3 vs 0/3 has positive lower CI bound -> benefit.
        sig = aggregate_behaviour([True, True, True], [False, False, False])
        self.assertEqual(sig["verdict"], "benefit")
        self.assertGreater(sig["delta_ci_95"][0], 0.0)
        self.assertFalse(sig["no_measurable_benefit"])

    def test_behaviour_checks_distinguish_fixture_from_expected(self) -> None:
        """Every declared check rejects its input and accepts its reference output."""
        for name in ("doxygen-naming", "misra-cpp-2023", "review-taxonomy", "skill-authoring"):
            tests = ROOT / "knowledge" / "skills" / name / "tests"
            config = load_config(tests.parent)
            self.assertGreaterEqual(len(config["behaviour"]), 2, name)
            for case in config["behaviour"]:
                fixture = case["fixture"].split("/")[-2]
                grader = tests / case["grader"]
                env = {**os.environ, "DEVPACK_ROOT": str(ROOT)}
                for key in ("category", "severity", "verdict"):
                    if key in case:
                        env[f"SKILLTEST_EXPECTED_{key.upper()}"] = case[key]
                failing = subprocess.run(["bash", "check.sh", str(tests / "fixtures" / fixture / "input")], cwd=grader, check=False, env=env)
                passing = subprocess.run(["bash", "check.sh", str(tests / "expected" / fixture)], cwd=grader, check=False, env=env)
                self.assertNotEqual(failing.returncode, 0, f"{name}/{fixture}")
                self.assertEqual(passing.returncode, 0, f"{name}/{fixture}")

    def test_case_check_rejects_unknown_cases_and_wrong_gate_taxonomy(self) -> None:
        """Case-specific graders fail closed and enforce the declared review taxonomy."""
        case_check = ROOT / "skilltest" / "case_check.py"
        env = {
            **os.environ,
            "DEVPACK_ROOT": str(ROOT),
            "SKILLTEST_EXPECTED_CATEGORY": "GAT",
            "SKILLTEST_EXPECTED_SEVERITY": "blocker",
            "SKILLTEST_EXPECTED_VERDICT": "REQUEST_CHANGES",
        }
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            unknown = subprocess.run(
                ["python3", str(case_check), "doxygen-naming", "misspelled", str(directory)],
                check=False, env=env,
            )
            self.assertEqual(unknown.returncode, 2)
            (directory / "REVIEW.md").write_text(
                "| ID | Category | Severity | file:line | Problem (with evidence) | Required fix |\n"
                "|---|---|---|---|---|---|\n"
                "| R1 | DOC | minor | module.cpp:1 | Missing gate receipt. | Add it. |\n"
                "VERDICT: APPROVE\n",
                encoding="utf-8",
            )
            wrong = subprocess.run(
                ["python3", str(case_check), "review-taxonomy", "gate-review", str(directory)],
                check=False, env=env,
            )
            self.assertNotEqual(wrong.returncode, 0)

    def test_trial_uses_external_grader_and_rejects_grader_tampering(self) -> None:
        """An agent cannot alter a copied grader because no grader is copied into its workspace."""
        from skilltest.skilltest import _execute_trial

        skill = ROOT / "knowledge" / "skills" / "doxygen-naming"
        fixture = skill / "tests" / "fixtures" / "header-contract" / "input"
        grader = skill / "tests" / "graders" / "header-contract"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = root / "tamper_agent"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "from pathlib import Path\n"
                "for base, _, names in os.walk('.'):\n"
                "    for name in names:\n"
                "        if name == 'check.sh': Path(base, name).write_text('exit 0\\n')\n"
                "print('{\\\"type\\\": \\\"result\\\", \\\"result\\\": \\\"done\\\"}')\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            _, passed, _ = _execute_trial(
                skill, "haiku", "do nothing", False, root / "run", fixture, str(fake), "local",
                "bash check.sh", grader,
            )
            self.assertFalse(passed)

    def test_behaviour_grader_uses_shared_isolation_and_records_mode(self) -> None:
        """Skill grading uses the eval sandbox and classifies authenticated exit-2 errors."""
        from skilltest import skilltest
        from types import SimpleNamespace

        skill = ROOT / "knowledge/skills/doxygen-naming"
        fixture = skill / "tests/fixtures/header-contract/input"
        grader = skill / "tests/graders/header-contract"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            transcript = root / "transcript.jsonl"
            transcript.write_text("", encoding="utf-8")
            result = SimpleNamespace(status="ok", tool_protocol_errors=0, transcript_path=transcript)
            for mode, rc, harness_error, expected in (
                ("docker", 0, False, "pass"),
                ("docker", 1, False, "fail"),
                ("docker", 2, True, "error"),
                ("local", 0, False, "pass"),
            ):
                with self.subTest(mode=mode, rc=rc), \
                        mock.patch.object(skilltest, "run_agent", return_value=result), \
                        mock.patch.object(skilltest, "asdict", return_value={}), \
                        mock.patch.object(skilltest, "_run_check_script", return_value=(rc, "GRADER_MISCONFIGURED: output", harness_error)) as check:
                    run_dir = root / f"{mode}-{rc}"
                    _, passed, telemetry = skilltest._execute_trial(
                        skill, "haiku", "fix contract", False, run_dir, fixture,
                        "fake-agent", mode, "bash check.sh", grader,
                        {"category": "COR"}, allow_unsandboxed_local=mode == "local",
                    )
                    self.assertEqual(passed, expected == "pass")
                    self.assertEqual(telemetry["outcome"], expected)
                    self.assertEqual(check.call_args.kwargs["isolation"], mode)
                    self.assertEqual(check.call_args.kwargs["allow_unsandboxed_local"], mode == "local")
                    self.assertEqual(check.call_args.kwargs["grader_command"], ["bash", "check.sh"])
                    self.assertEqual(check.call_args.kwargs["grader_env"], {"SKILLTEST_EXPECTED_CATEGORY": "COR"})
                    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
                    self.assertEqual(record["grader_isolation"], mode)

    def test_skill_grader_docker_boundaries_and_local_opt_in(self) -> None:
        """Run the real wrapper with fake Docker; never execute a candidate on the host."""
        from eval.run_eval import _run_check_script

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work, home, tmp = (root / name for name in ("work", "home", "tmp"))
            for directory in (work, home, tmp):
                directory.mkdir()
            grader = root / "grader"
            grader.mkdir()
            marker = root / "host-execution"
            check = grader / "check.sh"
            check.write_text(f"touch '{marker}'\n", encoding="utf-8")
            log = root / "docker-args.json"
            docker = root / "docker"
            docker.write_text(
                "#!/usr/bin/env python3\nimport json, pathlib, sys\n"
                f"pathlib.Path({str(log)!r}).write_text(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            docker.chmod(0o755)
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret-test-value"}):
                rc, output, error = _run_check_script(
                    check, work, home, tmp, 10, docker_bin=str(docker),
                    grader_command=["bash", "check.sh"],
                    grader_env={"SKILLTEST_EXPECTED_VERDICT": "APPROVE"},
                )
            self.assertEqual(rc, 0, output)
            self.assertFalse(error)
            self.assertFalse(marker.exists())
            args = json.loads(log.read_text(encoding="utf-8"))
            self.assertIn("--network=none", args)
            self.assertIn(f"{ROOT}:/workspace/pack:ro", args)
            self.assertIn(f"{grader}:/workspace/grader:ro", args)
            self.assertIn("/workspace/grader", args)
            self.assertIn("SKILLTEST_EXPECTED_VERDICT=APPROVE", args)
            self.assertEqual(args[-3:], ["bash", "check.sh", "/workspace/task"])
            self.assertNotIn("secret-test-value", " ".join(args))
            self.assertFalse(any("credentials" in arg for arg in args))
            with self.assertRaisesRegex(ValueError, "allow-unsandboxed-local"):
                _run_check_script(check, work, home, tmp, 10, isolation="local",
                                  grader_command=["bash", "check.sh"])
            self.assertFalse(marker.exists())

    def test_dry_run_prints_plan(self) -> None:
        """Dry-run displays every trial without requiring an agent binary."""
        result = self.run_cli(
            "run", "knowledge/skills/doxygen-naming", "--engines", "haiku,sonnet", "--k", "3", "--dry-run"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TRIAL PLAN", result.stdout)
        self.assertIn("haiku", result.stdout)
        self.assertIn("without_skill", result.stdout)

    def test_run_fake_agent_writes_summary_and_runs(self) -> None:
        """A fake claude executable completes a complete offline run."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = root / "claude"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Skill','input':{'skill':'doxygen-naming'}}]}}))\n"
                "print(json.dumps({'type':'result','result':'done','duration_ms':1}))\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            results = root / "results"
            result = self.run_cli(
                "run", "knowledge/skills/doxygen-naming", "--engines", "haiku", "--k", "1",
                "--isolation", "local", "--allow-unsandboxed-local",
                "--agent-bin", str(fake), "--results-root", str(results)
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(list(results.rglob("summary.md")))
            self.assertTrue(list(results.rglob("runs/*/steps/*.jsonl")))

    def test_trigger_competition_and_errored_trials_excluded_from_rates(self) -> None:
        """Trigger trials install non-deprecated siblings; errored trials are excluded from rates and reported."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skills_root = root / "skills"
            target = skills_root / "alpha-skill"
            neighbour = skills_root / "beta-skill"
            deprecated = skills_root / "old-skill"
            for sdir, body_extra in (
                (target, ""),
                (neighbour, ""),
                (deprecated, "Deprecated: superseded by alpha-skill\n"),
            ):
                (sdir / "tests" / "fixtures" / "case1" / "input").mkdir(parents=True)
                (sdir / "tests" / "graders" / "case1").mkdir(parents=True)
                (sdir / "tests" / "fixtures" / "case1" / "input" / "a.txt").write_text("x\n", encoding="utf-8")
                (sdir / "tests" / "graders" / "case1" / "check.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
                (sdir / "SKILL.md").write_text(
                    f"---\nname: {sdir.name}\ndescription: test {sdir.name}\n---\n{body_extra}Body.\n",
                    encoding="utf-8",
                )
                (sdir / "tests" / "skilltest.toml").write_text(
                    '[[trigger]]\nname = "pos1"\nprompt = "p1"\nexpect = true\n'
                    '[[trigger]]\nname = "pos2"\nprompt = "p2"\nexpect = true\n'
                    '[[trigger]]\nname = "neg1"\nprompt = "n1"\nexpect = false\n'
                    '[[trigger]]\nname = "neg2"\nprompt = "n2"\nexpect = false\n'
                    '[[behaviour]]\nname = "case1"\nfixture = "fixtures/case1/input"\ngrader = "graders/case1"\nprompt = "do"\ncheck = "bash check.sh"\n',
                    encoding="utf-8",
                )

            seen_installed = root / "installed.json"
            fake = root / "fake_claude.py"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "prompt = sys.argv[-1]\n"
                "skills_dir = pathlib.Path('.claude/skills')\n"
                "if prompt == 'p1' and skills_dir.is_dir():\n"
                f"    pathlib.Path({str(seen_installed)!r}).write_text(json.dumps(sorted(p.name for p in skills_dir.iterdir())), encoding='utf-8')\n"
                "if prompt == 'p2':\n"
                "    # Errored positive trigger trial: must NOT lower recall.\n"
                "    sys.exit(2)\n"
                "if prompt in ('p1', 'n1', 'n2'):\n"
                "    if prompt == 'p1':\n"
                "        print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Skill','input':{'skill':'alpha-skill'}}]}}))\n"
                "    else:\n"
                "        print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Skill','input':{'skill':'alpha-skill-other'}}]}}))\n"
                "print(json.dumps({'type':'result','result':'done','duration_ms':1}))\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            results_dir = root / "results"
            rc = self.run_cli(
                "run", str(target), "--engines", "haiku", "--k", "1",
                "--isolation", "local", "--allow-unsandboxed-local",
                "--agent-bin", str(fake), "--results-root", str(results_dir),
            )
            self.assertEqual(0, rc.returncode, rc.stderr)
            installed = json.loads(seen_installed.read_text(encoding="utf-8"))
            self.assertEqual(["alpha-skill", "beta-skill"], installed)
            summary = json.loads(next(results_dir.rglob("summary.json")).read_text(encoding="utf-8"))
            trig = summary["trigger"]["haiku"]
            self.assertEqual(1, trig["errors"])
            self.assertEqual(3, trig["valid"])
            self.assertAlmostEqual(1.0, trig["recall"])
            self.assertAlmostEqual(1.0, trig["precision"])
            self.assertIsNone(trig["per_case_rate"]["pos2"])

    def test_f5b_case_check_frontmatter_test_matrix_and_review_taxonomy(self) -> None:
        """Verify order-independent/10-word frontmatter, non-trivial test-matrix grader, and review-taxonomy checks."""
        case_check = ROOT / "skilltest" / "case_check.py"
        env_base = {**os.environ, "DEVPACK_ROOT": str(ROOT)}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            # 1. frontmatter-draft: description before name + quoted values passes; <10 words fails.
            fm_good = root / "fm_good" / "signal-helper"
            fm_good.mkdir(parents=True)
            (fm_good / "SKILL.md").write_text(
                "---\ndescription: 'Format a clear signal report when asked to summarize signal data.'\nname: \"signal-helper\"\n---\n# Body\n",
                encoding="utf-8",
            )
            res_good = subprocess.run(
                ["python3", str(case_check), "skill-authoring", "frontmatter-draft", str(fm_good.parent)],
                env=env_base, capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, res_good.returncode, res_good.stdout + res_good.stderr)

            fm_short = root / "fm_short" / "signal-helper"
            fm_short.mkdir(parents=True)
            (fm_short / "SKILL.md").write_text(
                "---\nname: signal-helper\ndescription: Short description.\n---\n# Body\n",
                encoding="utf-8",
            )
            res_short = subprocess.run(
                ["python3", str(case_check), "skill-authoring", "frontmatter-draft", str(fm_short.parent)],
                env=env_base, capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(0, res_short.returncode)

            # 2. test-matrix: a grader check.sh that just does `exit 0` fails.
            tm_bad = root / "tm_bad"
            shutil.copytree(ROOT / "knowledge/skills/skill-authoring/tests/expected/test-matrix", tm_bad)
            (tm_bad / "signal-helper/tests/graders/summary/check.sh").write_text(
                "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8",
            )
            res_tm = subprocess.run(
                ["python3", str(case_check), "skill-authoring", "test-matrix", str(tm_bad)],
                env=env_base, capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(0, res_tm.returncode)

            # 3. review-taxonomy: capitalized severity + trailing blank line passes; line 999 / lorem ipsum fails.
            rev_env = {
                **env_base,
                "SKILLTEST_EXPECTED_CATEGORY": "COR",
                "SKILLTEST_EXPECTED_SEVERITY": "major",
                "SKILLTEST_EXPECTED_VERDICT": "REQUEST_CHANGES",
            }
            rev_ok = root / "rev_ok"
            shutil.copytree(ROOT / "knowledge/skills/review-taxonomy/tests/expected/bounds-review", rev_ok)
            shutil.copy2(
                ROOT / "knowledge/skills/review-taxonomy/tests/fixtures/bounds-review/input/module.cpp",
                rev_ok / "module.cpp",
            )
            (rev_ok / "REVIEW.md").write_text(
                "| ID | Category | Severity | file:line | Problem (with evidence) | Required fix |\n"
                "|---|---|---|---|---|---|\n"
                "| R1 | COR | Major | module.cpp:1 | Unchecked array index access on values[index]. | Check index bounds. |\n"
                "VERDICT: REQUEST_CHANGES\n\n\n",
                encoding="utf-8",
            )
            res_rev_ok = subprocess.run(
                ["python3", str(case_check), "review-taxonomy", "bounds-review", str(rev_ok)],
                env=rev_env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, res_rev_ok.returncode, res_rev_ok.stdout + res_rev_ok.stderr)

            (rev_ok / "REVIEW.md").write_text(
                "| ID | Category | Severity | file:line | Problem (with evidence) | Required fix |\n"
                "|---|---|---|---|---|---|\n"
                "| R1 | COR | major | module.cpp:999 | Lorem ipsum dolor sit amet. | Fix it. |\n"
                "VERDICT: REQUEST_CHANGES\n",
                encoding="utf-8",
            )
            res_rev_bad = subprocess.run(
                ["python3", str(case_check), "review-taxonomy", "bounds-review", str(rev_ok)],
                env=rev_env, capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(0, res_rev_bad.returncode)

    def test_f5b_content_hash_contamination_and_per_case_stats(self) -> None:
        """Contamination uses SHA-256 file content hashes and behaviour CI is per-case (never pooled)."""
        from skilltest.skilltest import _execute_trial

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / "my-skill"
            for name in ("case-a", "case-b"):
                (skill / "tests/fixtures" / name / "input").mkdir(parents=True)
                (skill / "tests/graders" / name).mkdir(parents=True)
                (skill / "tests/fixtures" / name / "input/a.txt").write_text("in\n", encoding="utf-8")
                (skill / "tests/graders" / name / "check.sh").write_text(
                    "#!/usr/bin/env bash\ntest -f \"${@: -1}/OUT.txt\"\n", encoding="utf-8",
                )
            (skill / "SKILL.md").write_text(
                "---\nname: my-skill\ndescription: Reusable test skill for verifying per-case stats and content hashes.\n---\nBody.\n",
                encoding="utf-8",
            )
            (skill / "tests/skilltest.toml").write_text(
                '[[trigger]]\nname = "p1"\nprompt = "p1"\nexpect = true\n'
                '[[trigger]]\nname = "p2"\nprompt = "p2"\nexpect = true\n'
                '[[trigger]]\nname = "n1"\nprompt = "n1"\nexpect = false\n'
                '[[trigger]]\nname = "n2"\nprompt = "n2"\nexpect = false\n'
                '[[behaviour]]\nname = "case-a"\nfixture = "fixtures/case-a/input"\ngrader = "graders/case-a"\nprompt = "do case-a"\ncheck = "bash check.sh"\n'
                '[[behaviour]]\nname = "case-b"\nfixture = "fixtures/case-b/input"\ngrader = "graders/case-b"\nprompt = "do case-b"\ncheck = "bash check.sh"\n',
                encoding="utf-8",
            )

            # Worker writing its own helper named check.sh (different content) is NOT contaminated.
            fake_helper = root / "fake_helper.py"
            fake_helper.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib\n"
                "pathlib.Path('check.sh').write_text('#!/usr/bin/env bash\\necho helper\\n', encoding='utf-8')\n"
                "pathlib.Path('OUT.txt').write_text('ok\\n', encoding='utf-8')\n"
                "print(json.dumps({'type':'result','result':'done','duration_ms':1}))\n",
                encoding="utf-8",
            )
            fake_helper.chmod(0o755)
            _, passed_helper, _ = _execute_trial(
                skill, "haiku", "do case-a", True, root / "run_helper",
                skill / "tests/fixtures/case-a/input", str(fake_helper), "local",
                "bash check.sh", skill / "tests/graders/case-a", allow_unsandboxed_local=True,
            )
            self.assertTrue(passed_helper)

            # Copying grader file bytes under a different name IS contaminated.
            grader_bytes = (skill / "tests/graders/case-a/check.sh").read_text(encoding="utf-8")
            fake_leak = root / "fake_leak.py"
            fake_leak.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib\n"
                f"pathlib.Path('copied_grader.sh').write_text({grader_bytes!r}, encoding='utf-8')\n"
                "pathlib.Path('OUT.txt').write_text('ok\\n', encoding='utf-8')\n"
                "print(json.dumps({'type':'result','result':'done','duration_ms':1}))\n",
                encoding="utf-8",
            )
            fake_leak.chmod(0o755)
            _, passed_leak, telem_leak = _execute_trial(
                skill, "haiku", "do case-a", True, root / "run_leak",
                skill / "tests/fixtures/case-a/input", str(fake_leak), "local",
                "bash check.sh", skill / "tests/graders/case-a", allow_unsandboxed_local=True,
            )
            self.assertFalse(passed_leak)
            self.assertIn("grader material", telem_leak["reason"])

            # Per-case behaviour stats with k=3 across two cases: per_case has CIs; combined delta_ci_95 is None.
            fake_ab = root / "fake_ab.py"
            fake_ab.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "prompt = sys.argv[-1]\n"
                "if pathlib.Path('.claude/skills/my-skill/SKILL.md').is_file():\n"
                "    pathlib.Path('OUT.txt').write_text('ok\\n', encoding='utf-8')\n"
                "print(json.dumps({'type':'result','result':'done','duration_ms':1}))\n",
                encoding="utf-8",
            )
            fake_ab.chmod(0o755)
            results_dir = root / "results_ab"
            rc = self.run_cli(
                "run", str(skill), "--engines", "haiku", "--k", "3",
                "--isolation", "local", "--allow-unsandboxed-local",
                "--agent-bin", str(fake_ab), "--results-root", str(results_dir),
            )
            self.assertEqual(0, rc.returncode, rc.stderr)
            summary = json.loads(next(results_dir.rglob("summary.json")).read_text(encoding="utf-8"))
            beh = summary["behaviour"]["haiku"]
            self.assertIsNone(beh["delta_ci_95"])
            self.assertIn("not pooled", beh["note"])
            self.assertEqual(set(beh["per_case"].keys()), {"case-a", "case-b"})
            for cname in ("case-a", "case-b"):
                self.assertEqual("benefit", beh["per_case"][cname]["verdict"])
                self.assertIsNotNone(beh["per_case"][cname]["delta_ci_95"])
            summary_md = next(results_dir.rglob("summary.md")).read_text(encoding="utf-8")
            self.assertIn("not pooled", summary_md)
            self.assertIn("case-a:", summary_md)
            self.assertIn("case-b:", summary_md)


if __name__ == "__main__":
    unittest.main()
