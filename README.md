# ai-devpack

A thin, portable **AI development harness** for large professional automotive C/C++
projects that may use only mid- and small-size models. It gives you policy, agent
knowledge, workflow templates, deterministic checks and an evaluation kit. Everything runs as
local scripts (bash + Python 3.10+ standard library) callable from any CI.

Design principles: small closed tasks; deterministic checks before any model judgment;
CI runs the gate (agent claims are not evidence); review is one exhaustive pass with a fixed
taxonomy; at most 2 fix rounds; escalation ends at a **human**; agents never merge; sandbox
by default; provenance recorded internally.

## Layout

| Path | Content |
|---|---|
| `policy/` | operating model, ASIL boundaries (company policy, needs safety approval), sandbox, data governance, provenance, failure handling, tool confidence, deviations |
| `knowledge/` | `lessons.md`, `context_template/AGENTS.md`, skills (MISRA C++:2023 digest, review taxonomy, Doxygen/naming) |
| `workflow/templates/` | brief (implement, fix, review), task plan and ADR templates |
| `workflow/ci/` | `pipeline.md` and examples (`examples/gitlab-ci.yml`, `examples/Jenkinsfile`) |
| `runner/` | `devpack_run.py` local runner, `engines.example.toml`, unit tests |
| `enforcement/gate/` | `gate.py` fail-closed gate and `gate.example.toml` (the pack's own gate config) |
| `enforcement/lint/` | `preflight_lint.py` C/C++ preflight linter and its README |
| `enforcement/checks/` | secret/PII, scope, trace and test-lock checks |
| `enforcement/hooks/` | `pre-commit` hook and `install.sh` |
| `enforcement/evidence/` | `collect.py` evidence collector |
| `eval/fixture/` | C++17 fixture with `preflight.toml` (lint runs without a baseline) |
| `eval/tasks/` | 10 graded tasks and `verify_tasks.sh` |
| `eval/` | `run_eval.py` runner, `Dockerfile`, unit tests in `eval/tests/` |
| `tests/` | repo-level tests (`test_no_project_leaks.py`, `test_doc_links.py`) and `run_all.sh` |

## Install into a project

1. **Day 0:** fill in `policy/data_governance.md` section 4 (data owner sign-off) and get
   `policy/asil_boundaries.md` approved by the safety manager.
2. Copy or vendor the pack at a pinned version (for example as a `devpack/` directory or
   a git subtree). Do not edit pack files in the project; open a change against the pack.
3. Copy `knowledge/context_template/AGENTS.md` to the project root and replace every
   `{{PLACEHOLDER}}`.
4. Copy `enforcement/gate/gate.example.toml` to the project's gate config and list the
   project's build, test and lint commands.
5. Install `enforcement/hooks/pre-commit` as the git pre-commit hook.
6. Set up the sandbox container (`policy/sandbox.md`) and the agent credentials.
7. Copy `runner/engines.example.toml` and set the role-to-command templates.
8. Wire CI using `workflow/ci/pipeline.md` and the examples.

## Quickstart

```bash
# install the pre-commit hook into a git repository (default: current directory)
bash enforcement/hooks/install.sh --repo /path/to/project

# run all pack unit tests (fails if a test directory runs 0 tests)
bash tests/run_all.sh

# run the gate (expect GATE_OK); the receipt is a JSON file
python3 enforcement/gate/gate.py --config enforcement/gate/gate.example.toml --receipt /tmp/devpack_receipt.json

# run one task through the local runner (implement -> gate -> review -> fix, max 2 rounds)
python3 runner/devpack_run.py --help
python3 runner/devpack_run.py --config runner/engines.example.toml --task-id task-001 \
  --title "Short title" --goal "One-paragraph goal" --allowed-files "src/*.cpp" --dry-run

# evaluation: validate tasks and plan without calling any model
python3 eval/run_eval.py --dry-run --tasks all --models sonnet,haiku --k 1
# docker isolation (default); credentials via flag or DEVPACK_CLAUDE_CREDENTIALS
python3 eval/run_eval.py --isolation docker --credentials /path/to/credentials.json \
  --tasks all --models sonnet,haiku --k 5
# local isolation (no container)
python3 eval/run_eval.py --isolation local --credentials /path/to/credentials.json \
  --tasks all --models sonnet --k 1
```

Write a brief from `workflow/templates/brief_implement.md` (the tech lead approves it), run
the task through the runner, let CI run the gate, then review, fix at most twice, and hand
to the human reviewer.

## Versioning

Semantic versioning; the current version is in `VERSION` and changes in `CHANGELOG.md`.
Projects pin a version and compare it in CI (LSN-011).

## Limits

- Standards statements in `policy/` are marked "not verified against the standard text".
- External claims carry a URL and the mark "external, verify".
- The ASIL table is a proposal and needs safety approval before use.

## License

See `LICENSE` (proprietary placeholder).
