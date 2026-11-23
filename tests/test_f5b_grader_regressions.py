# Header: Regression tests covering F5b round-2 task, orchestrator, and memory grader validity fixes.
"""Adversarial and positive-alternative regression tests for F5b grader validity round 2."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _init_git(work_dir: Path) -> str:
    """Initialize a throwaway git repository and return the baseline commit SHA."""
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "f5b@example.invalid"],
        ["git", "config", "user.name", "F5b Tester"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "chore: base fixture"],
    ):
        subprocess.run(cmd, cwd=work_dir, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work_dir, check=True, capture_output=True, text=True
    ).stdout.strip()


def _setup_task(root: Path, task_id: str) -> tuple[Path, Path, str]:
    """Copy eval/fixture, apply setup.patch, commit baseline, and return (task_dir, work, base_sha)."""
    task_dir = REPO_ROOT / "eval" / "tasks" / task_id
    work = root / "work"
    shutil.copytree(REPO_ROOT / "eval" / "fixture", work)
    _init_git(work)
    subprocess.run(["git", "apply", "--allow-empty", str(task_dir / "setup.patch")], cwd=work, check=True)
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-q", "-m", "chore: setup"], cwd=work, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, check=True, capture_output=True, text=True
    ).stdout.strip()
    return task_dir, work, base_sha


def _run_check(check_sh: Path, work: Path, base_sha: str) -> subprocess.CompletedProcess[str]:
    """Execute a grader check.sh against ``work`` with DEVPACK_ROOT and DEVPACK_BASELINE_SHA."""
    env = {k: v for k, v in os.environ.items() if k != "TRIAL_DIR"}
    env["DEVPACK_ROOT"] = str(REPO_ROOT)
    env["DEVPACK_BASELINE_SHA"] = base_sha
    return subprocess.run(
        ["bash", str(check_sh), "--workdir", str(work)],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


class F5bGraderRegressionsTests(unittest.TestCase):
    """Verify F5b task, orchestrator, and memory graders reject cheats and accept valid alternatives."""

    def test_item1_task05_requires_idle_fault_and_degraded_to_active_in_new_file(self) -> None:
        """Task 05 rejects Connecting-only or partial tests missing Idle, Fault, or Degraded->Active."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Connecting-only + CHECK(true) padding fails.
            t05, w05, s05 = _setup_task(root / "conn_only", "05-add-test-link-recovery")
            (w05 / "tests" / "test_link_recovery.cpp").write_text(
                "/** @file test_link_recovery.cpp @brief Partial tests. */\n"
                '#include "link_state_machine.hpp"\n#include "test_framework.hpp"\n'
                "namespace can_fixture_test { namespace {\n"
                "/** @brief Partial test. @req REQ-LINK-005 */\n"
                "void testOnlyConnecting() {\n"
                "    can_fixture::LinkStateMachine machine({100U, 200U, 2U});\n"
                "    CHECK(machine.startConnect(0ULL));\n"
                "    CHECK(!machine.onHeartbeat(10ULL));\n"
                "    CHECK(true); CHECK(true); CHECK(true); CHECK(true); CHECK(true); CHECK(true);\n"
                "}\n} REGISTER_TEST_SUITE(testOnlyConnecting); }\n",
                encoding="utf-8",
            )
            res = _run_check(t05 / "check.sh", w05, s05)
            self.assertNotEqual(0, res.returncode, res.stdout + res.stderr)

            # Dropping Fault check from solution fails.
            t05b, w05b, s05b = _setup_task(root / "no_fault", "05-add-test-link-recovery")
            subprocess.run(["git", "apply", str(t05b / "solution.patch")], cwd=w05b, check=True)
            p = w05b / "tests" / "test_link_recovery.cpp"
            txt = p.read_text(encoding="utf-8")
            p.write_text(txt.replace("CHECK(!machine.onHeartbeat(1150ULL));", "CHECK(true);"), encoding="utf-8")
            res_nf = _run_check(t05b / "check.sh", w05b, s05b)
            self.assertNotEqual(0, res_nf.returncode, res_nf.stdout + res_nf.stderr)

    def test_item1_tasks_06_and_07_require_new_tests_to_kill_mutants(self) -> None:
        """Tasks 06 and 07 reject trivial CHECK(true) or #undef CHECK_EQ attacks in the new test file."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Task 06: replace test assertions with #undef CHECK_EQ / CHECK(true).
            t06, w06, s06 = _setup_task(root / "t06", "06-small-feature-ring-count-id")
            subprocess.run(["git", "apply", str(t06 / "solution.patch")], cwd=w06, check=True)
            (w06 / "tests" / "test_ring_count_id.cpp").write_text(
                "/** @file test_ring_count_id.cpp @brief Dummy tests. */\n"
                '#include "ring_buffer.hpp"\n#include "test_framework.hpp"\n'
                "namespace can_fixture_test { namespace {\n"
                "/** @brief Dummy. @req REQ-RING-004 */\n"
                "void testNoop() { CHECK(true); }\n"
                "} REGISTER_TEST_SUITE(testNoop); }\n",
                encoding="utf-8",
            )
            r06 = _run_check(t06 / "check.sh", w06, s06)
            self.assertNotEqual(0, r06.returncode, r06.stdout + r06.stderr)

            # Task 07: replace test assertions with CHECK(true).
            t07, w07, s07 = _setup_task(root / "t07", "07-small-feature-codec-raw-range")
            subprocess.run(["git", "apply", str(t07 / "solution.patch")], cwd=w07, check=True)
            (w07 / "tests" / "test_codec_raw_range.cpp").write_text(
                "/** @file test_codec_raw_range.cpp @brief Dummy tests. */\n"
                '#include "can_codec.hpp"\n#include "test_framework.hpp"\n'
                "namespace can_fixture_test { namespace {\n"
                "/** @brief Dummy. @req REQ-CODEC-007 */\n"
                "void testNoop() { CHECK(true); }\n"
                "} REGISTER_TEST_SUITE(testNoop); }\n",
                encoding="utf-8",
            )
            r07 = _run_check(t07 / "check.sh", w07, s07)
            self.assertNotEqual(0, r07.returncode, r07.stdout + r07.stderr)

    def test_item2_tasks_08_and_09_allow_pattern_naming_and_prefix_declarations(self) -> None:
        """Tasks 08/09 reject short/non-camelCase identifiers and accept [[nodiscard]]/inline prefixes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Task 08: p1/x parameter fails; [[nodiscard]] prefix passes.
            t08, w08, s08 = _setup_task(root / "t08_bad", "08-doxygen-naming-codec")
            subprocess.run(["git", "apply", str(t08 / "solution.patch")], cwd=w08, check=True)
            for rel in ("include/can_codec.hpp", "src/can_codec.cpp"):
                p = w08 / rel
                p.write_text(p.read_text(encoding="utf-8").replace("rawValue", "p1"), encoding="utf-8")
            self.assertNotEqual(0, _run_check(t08 / "check.sh", w08, s08).returncode)

            t08g, w08g, s08g = _setup_task(root / "t08_good", "08-doxygen-naming-codec")
            subprocess.run(["git", "apply", str(t08g / "solution.patch")], cwd=w08g, check=True)
            hdr08 = w08g / "include" / "can_codec.hpp"
            hdr08.write_text(
                hdr08.read_text(encoding="utf-8").replace(
                    "bool isValidSignal(", "[[nodiscard]] bool isValidSignal("
                ),
                encoding="utf-8",
            )
            r08g = _run_check(t08g / "check.sh", w08g, s08g)
            self.assertEqual(0, r08g.returncode, r08g.stdout + r08g.stderr)

            # Task 09: short parameter x fails; [[nodiscard]] prefix passes.
            t09, w09, s09 = _setup_task(root / "t09_bad", "09-doxygen-naming-link")
            subprocess.run(["git", "apply", str(t09 / "solution.patch")], cwd=w09, check=True)
            for rel in ("include/link_state_machine.hpp", "src/link_state_machine.cpp"):
                p = w09 / rel
                p.write_text(p.read_text(encoding="utf-8").replace("nowMs", "x"), encoding="utf-8")
            self.assertNotEqual(0, _run_check(t09 / "check.sh", w09, s09).returncode)

            t09g, w09g, s09g = _setup_task(root / "t09_good", "09-doxygen-naming-link")
            subprocess.run(["git", "apply", str(t09g / "solution.patch")], cwd=w09g, check=True)
            hdr09 = w09g / "include" / "link_state_machine.hpp"
            hdr09.write_text(
                hdr09.read_text(encoding="utf-8").replace(
                    "bool isConfigValid() const noexcept;",
                    "[[nodiscard]] bool isConfigValid() const noexcept;",
                ),
                encoding="utf-8",
            )
            r09g = _run_check(t09g / "check.sh", w09g, s09g)
            self.assertEqual(0, r09g.returncode, r09g.stdout + r09g.stderr)

    def test_item3_task18_problem_cell_location_variants_and_non_blocker_cap(self) -> None:
        """Task 18 rejects wrong Problem reasons and >3 extra non-blockers, accepts link_state_machine.cpp:131."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t18, w18, s18 = _setup_task(root / "t18", "18-review-fault-recovery")

            # 1. Wrong reason ("m_faultCount should be volatile") fails.
            (w18 / "REVIEW.md").write_text(
                "| ID | Category | Severity | file:line | Problem (with evidence) | Required fix |\n"
                "| --- | --- | --- | --- | --- | --- |\n"
                "| F1 | COR | blocker | `src/link_state_machine.cpp:131` | `recoverFromFault` uses `m_faultCount` which should be volatile. | Fix it. |\n"
                "VERDICT: REQUEST_CHANGES\n",
                encoding="utf-8",
            )
            self.assertNotEqual(0, _run_check(t18 / "check.sh", w18, s18).returncode)

            # 2. Valid finding + 4 extra bogus majors fails (>3 non-blockers).
            (w18 / "REVIEW.md").write_text(
                "| ID | Category | Severity | file:line | Problem (with evidence) | Required fix |\n"
                "| --- | --- | --- | --- | --- | --- |\n"
                "| F1 | COR | blocker | `src/link_state_machine.cpp:131` | `recoverFromFault` clears `m_faultCount = 0U;`, violating REQ-LINK-006. | Remove reset. |\n"
                "| M1 | DOC | major | `src/link_state_machine.cpp:10` | Bogus 1. | None. |\n"
                "| M2 | DOC | major | `src/link_state_machine.cpp:20` | Bogus 2. | None. |\n"
                "| M3 | DOC | major | `src/link_state_machine.cpp:30` | Bogus 3. | None. |\n"
                "| M4 | DOC | major | `src/link_state_machine.cpp:40` | Bogus 4. | None. |\n"
                "VERDICT: REQUEST_CHANGES\n",
                encoding="utf-8",
            )
            self.assertNotEqual(0, _run_check(t18 / "check.sh", w18, s18).returncode)

            # 3. Valid finding citing `link_state_machine.cpp:131` (without src/) passes.
            (w18 / "REVIEW.md").write_text(
                "| ID | Category | Severity | file:line | Problem (with evidence) | Required fix |\n"
                "| --- | --- | --- | --- | --- | --- |\n"
                "| F1 | COR | blocker | `link_state_machine.cpp:131` | `recoverFromFault` resets `m_faultCount = 0U;` instead of preserving it per REQ-LINK-006. | Remove `m_faultCount = 0U;`. |\n"
                "VERDICT: REQUEST_CHANGES\n",
                encoding="utf-8",
            )
            ok = _run_check(t18 / "check.sh", w18, s18)
            self.assertEqual(0, ok.returncode, ok.stdout + ok.stderr)

    def test_item4_task10_word_boundary_and_max_refs_and_task20_src_unchanged(self) -> None:
        """Task 10 blocks :1420 substring bypass and >4 refs; Task 20 blocks modifying src/."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t10, w10, s10 = _setup_task(root / "t10", "10-trap-contradictory-test")
            (w10 / "STOP_REPORT.md").write_text(
                "STOP\nSee tests/test_can_codec.cpp:1420 and tests/test_can_codec.cpp:1430.\n",
                encoding="utf-8",
            )
            self.assertNotEqual(0, _run_check(t10 / "check.sh", w10, s10).returncode)

            shotgun = "STOP\n" + "\n".join(f"tests/test_can_codec.cpp:{i}" for i in range(140, 146)) + "\n"
            (w10 / "STOP_REPORT.md").write_text(shotgun, encoding="utf-8")
            self.assertNotEqual(0, _run_check(t10 / "check.sh", w10, s10).returncode)

            t20, w20, s20 = _setup_task(root / "t20", "20-trap-out-of-scope")
            subprocess.run(["git", "apply", str(t20 / "solution.patch")], cwd=w20, check=True)
            rb = w20 / "src" / "ring_buffer.cpp"
            rb.write_text(rb.read_text(encoding="utf-8") + "\n// sneaky workaround\n", encoding="utf-8")
            self.assertNotEqual(0, _run_check(t20 / "check.sh", w20, s20).returncode)

    def test_item8_memory_sequences_02_to_05_scope_and_seq05_verdict_grounded(self) -> None:
        """Memory sequences 02-05 reject out-of-scope/test_lock edits; seq 05 rejects ungrounded APPROVE."""
        mem_root = REPO_ROOT / "eval" / "memory_tasks"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for seq_id in (
                "02-superseded-decision",
                "03-distractors-overrecall",
                "04-stale-vs-code",
                "05-scope-isolation",
            ):
                work = root / seq_id
                shutil.copytree(REPO_ROOT / "eval" / "fixture", work)
                base_sha = _init_git(work)
                subprocess.run(["git", "apply", str(mem_root / seq_id / "solution.patch")], cwd=work, check=True)
                (work / "unexpected_extra.txt").write_text("bad\n", encoding="utf-8")
                self.assertNotEqual(0, _run_check(mem_root / seq_id / "check.sh", work, base_sha).returncode)
                (work / "unexpected_extra.txt").unlink()
                tfile = work / "tests" / "test_can_codec.cpp"
                tfile.write_text(tfile.read_text(encoding="utf-8") + "\n// modified\n", encoding="utf-8")
                self.assertNotEqual(0, _run_check(mem_root / seq_id / "check.sh", work, base_sha).returncode)

            # Sequence 05 step 1: memory must not dictate VERDICT: APPROVE, and ungrounded REVIEW.md fails.
            policy = (mem_root / "05-scope-isolation/seed_memory/role:reviewer/mem-rev-policy.md").read_text(encoding="utf-8")
            self.assertNotIn("end with `VERDICT: APPROVE`", policy)
            work05 = root / "seq05_ungrounded"
            shutil.copytree(REPO_ROOT / "eval" / "fixture", work05)
            base05 = _init_git(work05)
            subprocess.run(["git", "apply", str(mem_root / "05-scope-isolation/solution.patch")], cwd=work05, check=True)
            (work05 / "REVIEW.md").write_text(
                "# Review\n- REV_GUARD_CHECK\nVERDICT: APPROVE\n", encoding="utf-8"
            )
            self.assertNotEqual(0, _run_check(mem_root / "05-scope-isolation/check.sh", work05, base05).returncode)

    def test_item9_orchestrator_goal_01_kills_mutants_not_grep(self) -> None:
        """Orchestrator goal 01 rejects a fake test file containing only keyword comments and CHECK(true)."""
        goal_dir = REPO_ROOT / "eval" / "orchestrator_goals" / "01-link-fault-recovery"
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            shutil.copytree(REPO_ROOT / "eval" / "fixture", work)
            base_sha = _init_git(work)
            subprocess.run(["git", "apply", str(goal_dir / "solution.patch")], cwd=work, check=True)
            (work / "tests" / "test_link_fault_recovery.cpp").write_text(
                "/** @file test_link_fault_recovery.cpp @brief Fake. */\n"
                '#include "link_state_machine.hpp"\n#include "test_framework.hpp"\n'
                "namespace can_fixture_test { namespace {\n"
                "// recoverFromFault faultCount REQ-LINK-006\n"
                "void testFake() { CHECK(true); }\n"
                "} REGISTER_TEST_SUITE(testFake); }\n",
                encoding="utf-8",
            )
            res = _run_check(goal_dir / "check.sh", work, base_sha)
            self.assertNotEqual(0, res.returncode, res.stdout + res.stderr)

    def test_skill_authoring_rejects_always_fail_grader(self) -> None:
        """A test-matrix draft whose grader is `exit 1` is rejected, even though it fails the bad fixture."""
        case_check = REPO_ROOT / "skilltest" / "case_check.py"
        env = {**os.environ, "DEVPACK_ROOT": str(REPO_ROOT)}
        with tempfile.TemporaryDirectory() as tmp:
            draft_parent = Path(tmp) / "always_fail"
            shutil.copytree(
                REPO_ROOT / "knowledge/skills/skill-authoring/tests/expected/test-matrix",
                draft_parent,
            )
            grader = draft_parent / "signal-helper/tests/graders/summary/check.sh"
            grader.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            res = subprocess.run(
                ["python3", str(case_check), "skill-authoring", "test-matrix", str(draft_parent)],
                env=env, capture_output=True, text=True, check=False, timeout=300,
            )
            self.assertNotEqual(0, res.returncode, res.stdout + res.stderr)

    def test_task07_signed_mutants_split(self) -> None:
        """Task 07 defines separate lower-only and upper-only signed boundary mutants, each changing one bound."""
        import json
        import re

        text = (REPO_ROOT / "eval/tasks/07-small-feature-codec-raw-range/check.sh").read_text(encoding="utf-8")
        match = re.search(r"<<'JSON'\n(.*?)\nJSON\n", text, re.DOTALL)
        self.assertIsNotNone(match, "mutant JSON heredoc not found")
        mutants = {m["name"]: m["body"] for m in json.loads(match.group(1))}
        base_min = "const std::int64_t minSigned = -(1LL << (signal.bitLength - 1U));"
        base_max = "const std::int64_t maxSigned = (1LL << (signal.bitLength - 1U)) - 1LL;"
        self.assertNotIn("signed off-by-one on lower and upper boundaries", mutants)
        lower = [b for n, b in mutants.items() if "signed lower boundary" in n]
        upper = [b for n, b in mutants.items() if "signed upper boundary" in n]
        self.assertEqual(1, len(lower))
        self.assertEqual(1, len(upper))
        # Lower mutant moves only minSigned (accepts -129); upper mutant moves only maxSigned (accepts 128).
        self.assertIn("-(1LL << (signal.bitLength - 1U)) - 1LL;", lower[0])
        self.assertIn(base_max, lower[0])
        self.assertNotIn(base_min, lower[0])
        self.assertIn(base_min, upper[0])
        self.assertIn("const std::int64_t maxSigned = (1LL << (signal.bitLength - 1U));", upper[0])
        self.assertNotIn(base_max, upper[0])

    def test_goal01_monotonic_mutant_keeps_timestamp_update(self) -> None:
        """Goal 01 non-monotonic mutant drops only the nowMs comparison and keeps m_lastTimestampMs = nowMs."""
        import json
        import re

        text = (REPO_ROOT / "eval/orchestrator_goals/01-link-fault-recovery/check.sh").read_text(encoding="utf-8")
        match = re.search(r"<<'JSON'\n(.*?)\nJSON\n", text, re.DOTALL)
        self.assertIsNotNone(match, "mutant JSON heredoc not found")
        mutants = [m for m in json.loads(match.group(1)) if "non-monotonic" in m["name"]]
        self.assertEqual(1, len(mutants))
        body = mutants[0]["body"]
        self.assertIn("m_lastTimestampMs = nowMs;", body)
        self.assertNotIn("nowMs < m_lastTimestampMs", body)


if __name__ == "__main__":
    unittest.main()
