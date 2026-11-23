# Changelog

All notable changes to ai-devpack are recorded here. Versioning follows
[Semantic Versioning](https://semver.org/): MAJOR.MINOR.PATCH, see `VERSION`.

## [Unreleased]

### Added
- **Local static analysis with `cppcheck` (`enforcement/static/cppcheck_check.py`, `policy/static_analysis.md`)**:
  added `cppcheck` as the local early-warning stand-in for Coverity everywhere preflight
  lint runs — worker dispatch (`runner/devpack_dispatch.py`), runner gate and fix loop
  (`runner/devpack_run.py`), v2 eval grading (`eval/tasks/v2_check.sh`, `eval/Dockerfile`),
  eval metrics/summaries (`eval/run_eval.py`), and the pack gate (`cppcheck-fixture` in
  `enforcement/gate/gate.example.toml`). Fails closed with exit `3` (`TOOL_MISSING`) when
  `cppcheck` is absent, supports `[cppcheck]` configuration in `eval/fixture/preflight.toml`
  and `.devpack/project.example.toml`, and supports portable JSON baselines (`--baseline`).

## [0.2.0] - 2026-11-23

### Added
- **Unified executor & v2 engine config**: `runner/agent_exec.py` and
  `runner/engines.example.toml` (version 2) supporting cloud (`sonnet`, `haiku`) and
  on-premises `class = "local"` Anthropic-compatible endpoints (`base_url_env`,
  `auth_token_env`, `min_context_tokens = 32768`), always streaming `--output-format
  stream-json --verbose`, detecting `tool_protocol_errors`, and injecting per-role tool
  allowlists and active engine profiles.
- **Human-readable agent logs (`policy/logging.md`, `runner/devpack_logs.py`)**: shared
  run-directory layout (`runs/<run-id>/`) with `render`, `show`, `list`, and `site`
  subcommands producing redacted `transcript.md` and self-contained static `index.html`.
- **Dispatch & host broker (`runner/devpack_dispatch.py`, `runner/devpack_broker.py`)**:
  credential-free queue client in `.devpack/pack/` paired with a host broker that enforces
  `.devpack/project.toml` `data_class` limits, worker isolation, `scope_check.py`,
  `test_lock.py`, and `preflight_lint.py` before copying scoped regular files back.
- **Sonnet orchestrator (`orchestrator/`)**: `orchestrator/CLAUDE.md`, `orchestrator/README.md`,
  `orchestrator/briefs/`, scoped settings, and native sub-agent definitions supporting the
  three topologies (`Sonnet -> Haiku`, `Sonnet -> local`, `local -> local`).
- **Skill authoring & testing (`knowledge/skills/skill-authoring/`, `skilltest/`)**:
  `skilltest/skilltest.py` (`lint`, `run`, `report`) for deterministic skill linting and
  A/B behaviour/trigger experiments, with `<skill-dir>/tests/skilltest.toml` suites for all 4 skills.
- **Eval v2 (`eval/`)**: `dtc_debouncer` and `docs/spec.md` added to `eval/fixture/`;
  10 new `v2` tasks (`11`-`20`) across `vague-bugfix`, `multi-file-feature`, `spec-driven`,
  `review`, and `trap`; `eval/orchestrator_goals/`; `eval/memory_tasks/`; `--mode runner`,
  `--mode orchestrator`, `--mode memory`, `--tier`, `--record-qualification`,
  `tool_protocol_errors`, code-quality metrics, and reviewer `false_approve`/`false_reject`;
  structural grader meta-checks in `eval/tasks/verify_tasks.sh`.
- **Learning loop (`learning/`, `knowledge/engine_profiles/`)**: `learning/harvest.py`,
  `learning/propose.py` (`<= 40` line bounded candidate profiles), and
  `learning/validate.py` (A/B validation before human promotion).
- **Bounded project memory (`policy/memory.md`, `runner/memory.py`, `runner/devpack_memory.py`)**:
  `.devpack/memory/<scope>/<id>.md` store with proposal-only agent writes, human approval
  for `decision`/`fact` entries, secret/PII filtering, deterministic tag/scope retrieval
  within token budgets, and provenance hashes in step `.meta.json`.
- **Policy & lessons**: `policy/logging.md`, `policy/local_models.md`, `policy/memory.md`,
  and `knowledge/lessons.md` entries LSN-016..LSN-019.
- **Gate & test runner (`enforcement/gate/gate.example.toml`, `tests/run_all.sh`)**:
  offline gate checks covering `skilltest` and `learning` unit tests, `skilltest lint`
  across all skills, `verify_tasks.sh` (01-20), orchestrator-goal verification, `--mode
  runner` / `orchestrator` / `memory` dry-runs, and `devpack_logs.py render` on a fixture
  run directory.

### Fixed
- Extended redaction in `runner/agent_exec.py` and `runner/devpack_logs.py` to scrub
  current OS user, group, and host names (including `ls -l` columns in tool results) with
  `[USER]`, `[GROUP]`, and `[HOST]`.
- Scoped `tests/test_no_project_leaks.py` to committable files (`git ls-files -co
  --exclude-standard`) and word-boundary matching to avoid false positives on git-ignored
  output directories and base64 signatures.
- Caught `subprocess.TimeoutExpired` on `build_and_test.sh` before auto-accepting memory
  proposals in `eval/run_eval.py` as a trial-level `build_timeout` harness error.
- Passed `--no-deviation` from eval callers and graders to `enforcement/checks/test_lock.py`
  so agents under test cannot self-exempt protected tests.

## [0.1.0] - Unreleased

### Added
- `policy/`: operating model, ASIL boundaries (company policy draft), sandbox,
  data governance, provenance, failure handling, tool confidence worksheet, deviations.
- `knowledge/`: lessons learned (LSN-001..LSN-015), agent context template,
  skills (generic MISRA C++:2023 guidance, review taxonomy, Doxygen and naming).
- `workflow/`: brief/plan/ADR templates and CI pipeline description with GitLab CI
  and Jenkins examples.
- `enforcement/`: fail-closed gate (`gate.py`, `gate.example.toml`), preflight C/C++ lint,
  secret/PII, scope, trace and test-lock checks, git hooks, evidence collector.
- `runner/`: `devpack_run.py` local runner (implement, gate, review, fix, escalate) and
  `engines.example.toml`.
- `eval/`: C++17 fixture with preflight config, 10 graded tasks with
  `verify_tasks.sh`, `run_eval.py` (docker/local isolation), `Dockerfile`.
- `tests/test_no_project_leaks.py`, `tests/test_doc_links.py` and `tests/run_all.sh`.
- One gate config covering leak test, all unit tests, fixture build, task verification,
  eval dry run and fixture lint.

### Fixed
- MISRA-15.1.3 false positive on out-of-class constructor definitions fixed; the fixture
  lint baseline that hid it is removed.
- `tests/run_all.sh` now fails when a directory with `test_*.py` files runs 0 tests.
- Brief template STOP section now requires removing runtime resources the task installed
  (matches LSN-007).
