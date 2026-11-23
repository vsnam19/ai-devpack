# ai-devpack

A thin, portable **AI development harness** for large professional automotive C/C++
projects that may use mid-, small-size, or on-premises models. It provides policy, agent
knowledge and skills, workflow templates, deterministic enforcement checks, a sandboxed
runner/broker, human-readable logs, bounded project memory, a learning loop, and an
evaluation kit. Everything runs as local scripts (bash + Python 3.10+ standard library)
callable from any CI.

Design principles: small closed tasks; deterministic checks before any model judgment;
CI and the host broker run the gate (agent claims are not evidence); review is one
exhaustive pass with a fixed taxonomy; at most 2 fix rounds; escalation ends at a
**human**; agents never merge; sandbox by default; provenance and transcripts recorded
internally.

## Layout

| Path | Content |
|---|---|
| `policy/` | operating model, ASIL boundaries (company policy, needs safety approval), sandbox, data governance, provenance, failure handling, `policy/failure_attribution.md`, `policy/static_analysis.md`, tool confidence, deviations, `policy/logging.md`, `policy/local_models.md`, `policy/memory.md` |
| `knowledge/` | `lessons.md` (LSN-001..LSN-019), `context_template/AGENTS.md`, `skills/` (`automotive-cpp`, `review-taxonomy`, `skill-authoring`, `requirements-analysis`; deprecated: `misra-cpp-2023`, `doxygen-naming`), `engine_profiles/` |
| `workflow/templates/` | brief (implement, fix, review), task plan and ADR templates |
| `workflow/ci/` | `pipeline.md` and examples (`examples/gitlab-ci.yml`, `examples/Jenkinsfile`) |
| `runner/` | `agent_exec.py` (single executor + v2 engine loader), `brief_lint.py` (pre-dispatch brief linter), `devpack_run.py`, `devpack_dispatch.py`, `devpack_broker.py`, `devpack_logs.py`, `memory.py`, `devpack_memory.py`, `engines.example.toml`, `quality_checklist.md` |
| `orchestrator/` | drop-in Sonnet orchestrator config (`orchestrator/CLAUDE.md`, `orchestrator/README.md`, `orchestrator/briefs/`) |
| `skilltest/` | `skilltest.py` (`lint`, `run`, `report`) for deterministic skill linting and A/B experiments |
| `learning/` | `harvest.py`, `propose.py`, `validate.py` for mining run logs and validating bounded engine profiles |
| `enforcement/gate/` | `gate.py` fail-closed gate and `gate.example.toml` (the pack's own offline gate config) |
| `enforcement/lint/` | `preflight_lint.py` C/C++ preflight linter and its README |
| `enforcement/static/` | `cppcheck_check.py` C/C++ static analysis (local stand-in for Coverity; `policy/static_analysis.md`) |
| `enforcement/checks/` | secret/PII, scope, trace and test-lock checks |
| `enforcement/hooks/` | `pre-commit` hook and `install.sh` |
| `enforcement/evidence/` | `collect.py` evidence collector |
| `eval/fixture/` | C++17 fixture (`can_codec`, `ring_buffer`, `link_state_machine`, `dtc_debouncer`, `docs/spec.md`) with `preflight.toml` |
| `eval/tasks/` | 20 graded tasks (`01`-`10` `smoke`, `11`-`20` `v2`) and `verify_tasks.sh` |
| `eval/orchestrator_goals/` | multi-subtask orchestrator goals and deterministic graders |
| `eval/memory_tasks/` | multi-step memory evaluation sequences (`01`-`05`) |
| `eval/` | `run_eval.py` (`single`, `runner`, `orchestrator`, `memory`), `Dockerfile`, `eval/README.md`, unit tests in `eval/tests/` |
| `tests/` | repo-level tests (`test_no_project_leaks.py`, `test_doc_links.py`, `verify_orchestrator_goals.py`, etc.) and `run_all.sh` |

## Orchestration topologies

Three deployment topologies are supported through `runner/engines.example.toml` and `orchestrator/`:

1. **Cloud orchestrator -> cloud workers (`Sonnet -> Haiku`).**
   Sonnet decomposes a goal into closed briefs under `briefs/` and dispatches Haiku
   workers through `runner/devpack_dispatch.py` and `runner/devpack_broker.py` (or native
   sub-agents under `orchestrator/README.md` when sharing a single cloud endpoint).
2. **Cloud orchestrator -> local workers (`Sonnet -> local`).**
   When mixing endpoints or keeping `confidential` code on-premises, Sonnet runs with
   `orchestrator/CLAUDE.md` permissions (no direct product edits) and dispatches every
   worker job through the host broker (`runner/devpack_broker.py`), which keeps
   credentials, engine configs, and local endpoint secrets outside the worker workspace.
3. **Local AI alone (`local -> local`).**
   Runs entirely against an on-premises Anthropic-compatible endpoint (`class = "local"`
   in `runner/engines.example.toml`) for `confidential` repositories. Requires prior
   qualification against `policy/local_models.md`.

## Reading agent logs

Every execution via `runner/agent_exec.py` writes a redacted run directory under
`runs/<run-id>/` (`run.json`, `steps/NN-<role>-<engine>.{prompt.md,jsonl,meta.json}`,
`steps/NN-gate.txt`, `transcript.md`, `index.html`; see `policy/logging.md`).

```bash
# Build a static HTML site linking all runs, then open runs/index.html in any browser
python3 runner/devpack_logs.py site runs/
# Inspect or render a single run or standalone Claude Code session JSONL
python3 runner/devpack_logs.py list runs/
python3 runner/devpack_logs.py show runs/<run-id> --step 01
python3 runner/devpack_logs.py render runs/<run-id>
python3 runner/devpack_logs.py render --session session.jsonl --out runs/session-view
# Lint a brief before dispatch and attribute failed runs (policy/failure_attribution.md)
python3 runner/brief_lint.py orchestrator/briefs/example_implement.md --repo eval/fixture --engine haiku
python3 runner/devpack_logs.py analyze runs/<run-id> --repo .
python3 runner/devpack_logs.py attribute runs/<run-id> --cause BRIEF --note "Missing interface" --by tech-lead
```

## Writing and testing a skill

Follow `knowledge/skills/skill-authoring/SKILL.md` (frontmatter `name`/`description`,
body `<= 200` lines, relative links only, `<skill-dir>/tests/skilltest.toml` with `>= 2`
positive trigger cases, `>= 2` negative trigger cases, and `>= 1` behaviour A/B case):

```bash
# Deterministic lint (required in the pack gate for every skill under knowledge/skills/)
python3 skilltest/skilltest.py lint knowledge/skills/<skill-name>

# Plan or run an A/B experiment (with_skill vs without_skill) across engines
python3 skilltest/skilltest.py run knowledge/skills/<skill-name> --engines haiku,sonnet --k 3 --dry-run
python3 skilltest/skilltest.py run knowledge/skills/<skill-name> --engines haiku,sonnet --k 3
```

## Local model qualification

Before enabling any `class = "local"` engine (or after changing weights, quantization, or
server settings), qualify it against `policy/local_models.md` (`smoke` tier `pass^3 >= 0.8`,
`tool_protocol_errors == 0`, and `lint_clean_final >= 90%` of passing trials):

```bash
python3 eval/run_eval.py --mode single --tier smoke --engines local --k 3 \
  --engines-config runner/engines.example.toml --record-qualification
```

Dispatches fail closed (`runner/devpack_dispatch.py`) if the served model ID changes or a
qualified engine fails re-qualification.

## Memory store and learning loop

- **Bounded project memory (`policy/memory.md`, `runner/memory.py`, `runner/devpack_memory.py`):**
  Entries live under `.devpack/memory/<scope>/<id>.md` (`decision`, `fact`, `handoff`,
  `known-gap`). Agents may only propose entries (`## Memory proposals`); the runner
  auto-accepts `handoff`/`known-gap` after the gate passes while `decision`/`fact`
  entries stay `pending` until a human approves them:
  ```bash
  python3 runner/devpack_memory.py list
  python3 runner/devpack_memory.py approve <id>
  python3 runner/devpack_memory.py query --brief briefs/task.md
  ```
- **Learning loop (`learning/README.md`, `knowledge/engine_profiles/`):**
  Mine recurring failures from logs, draft a bounded candidate profile (`<= 40` lines),
  and A/B validate before a human commits the promoted profile:
  ```bash
  python3 learning/harvest.py runs
  python3 learning/propose.py --harvest <harvest-report.json> --engine local
  python3 learning/validate.py --engine local --candidate <candidate-profile.md> --k 3
  ```

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
6. Set up the sandbox container (`policy/sandbox.md`) and agent credentials; for local
   endpoints, configure environment variable names in `runner/engines.example.toml`
   (`policy/local_models.md`) and project data class in `.devpack/project.toml`.
7. Wire CI using `workflow/ci/pipeline.md` and the examples.

## Quickstart

```bash
# install the pre-commit hook into a git repository (default: current directory)
bash enforcement/hooks/install.sh --repo /path/to/project

# run all pack unit tests (fails if a test directory runs 0 tests)
bash tests/run_all.sh

# run the offline pack gate (expect GATE_OK); the receipt is a JSON file
python3 enforcement/gate/gate.py --config enforcement/gate/gate.example.toml --receipt /tmp/devpack_receipt.json

# run one task through the local runner (implement -> gate -> review -> fix, max 2 rounds)
python3 runner/devpack_run.py --config runner/engines.example.toml --task-id task-001 \
  --title "Short title" --goal "One-paragraph goal" --allowed-files "src/*.cpp" --dry-run

# evaluation dry-runs (no model calls)
python3 eval/run_eval.py --dry-run --mode single --tier all --engines sonnet,haiku --k 1
python3 eval/run_eval.py --dry-run --mode runner --pairs haiku:sonnet --tier all --k 1
python3 eval/run_eval.py --dry-run --mode orchestrator --orchestrator sonnet --workers haiku --k 1
python3 eval/run_eval.py --dry-run --mode memory --engines haiku --k 1
```

Write a brief from `workflow/templates/brief_implement.md` (the tech lead approves it), run
the task through the runner or orchestrator broker, let CI/broker run the gate, then
review, fix at most twice, and hand to the human reviewer.

## Versioning

Semantic versioning; the current version is in `VERSION` and changes in `CHANGELOG.md`.
Projects pin a version and compare it in CI (LSN-011).

## Limits

- Standards statements in `policy/` are marked "not verified against the standard text".
- External claims carry a URL and the mark "external, verify".
- The ASIL table is a proposal and needs safety approval before use.

## License

See `LICENSE` (proprietary placeholder).
