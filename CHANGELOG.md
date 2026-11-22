# Changelog

All notable changes to ai-devpack are recorded here. Versioning follows
[Semantic Versioning](https://semver.org/): MAJOR.MINOR.PATCH, see `VERSION`.

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
- `eval/`: C++17 fixture with preflight config and lint baseline, 10 graded tasks with
  `verify_tasks.sh`, `run_eval.py` (docker/local isolation), `Dockerfile`.
- `tests/test_no_project_leaks.py`, `tests/test_doc_links.py` and `tests/run_all.sh`.
- One gate config covering leak test, all unit tests, fixture build, task verification,
  eval dry run and fixture lint.

### Fixed
- `tests/run_all.sh` now fails when a directory with `test_*.py` files runs 0 tests.
- Brief template STOP section now requires removing runtime resources the task installed
  (matches LSN-007).
