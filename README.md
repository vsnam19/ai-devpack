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
| `policy/` | operating model, `policy/operating_modes.md` (`hitl` / `autonomous`), ASIL boundaries (company policy, needs safety approval), sandbox, data governance, provenance, failure handling, `policy/failure_attribution.md`, `policy/static_analysis.md`, `policy/workspace_scope.md`, tool confidence, deviations, `policy/logging.md`, `policy/local_models.md`, `policy/memory.md` |
| `knowledge/` | `lessons.md` (versioned lesson IDs), `context_template/AGENTS.md`, `skills/` (`automotive-cpp`, `review-taxonomy`, `skill-authoring`, `requirements-analysis`; deprecated: `misra-cpp-2023`, `doxygen-naming`), `engine_profiles/` |
| `workflow/templates/` | brief (implement, fix, review), task plan and ADR templates |
| `workflow/ci/` | `pipeline.md` and examples (`examples/gitlab-ci.yml`, `examples/Jenkinsfile`) |
| `runner/` | `agent_exec.py` (single executor + v2 engine loader), `checkpoints.py` (pipeline modes and approvals), `devpack_approve.py` (human/CI checkpoint CLI), `workspace_scope.py` (Yocto component scope), `brief_lint.py` (pre-dispatch brief linter), `repomap.py` (code index), `devpack_submit.py` (submission checks), `redaction.py` (shared secret rules), `safe_git.py` (hardened git access), `devpack_run.py`, `devpack_dispatch.py`, `devpack_broker.py`, `devpack_logs.py`, `memory.py`, `devpack_memory.py`, `engines.example.toml`, `quality_checklist.md` |
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

## Dashboard

Run `python3 runner/devpack_dash.py --workdir PATH` to inspect Pack runs in a
terminal. Use `--runs-root PATH` for a different run store, `--once` for a
plain snapshot or `--json` for structured output. Subcommands include
`jobs`, `show JOB`, `gate JOB`, `tail JOB --kind tool` and
`approvals --status pending --plain`. The approvals output prints full
operator commands on separate lines for copying.

Use arrows or `j/k` to move, Page Up/Down to scroll, Enter to open, Esc to go
back, `r` to refresh, `?` for help and `q` to quit. From Job Detail,
`l` opens Events, `g` opens Gates and `e` opens Diagnostics. In Events,
`c` cycles all/tool/message and `f` toggles follow. Use `a` from Jobs or
Detail to open Approvals, then Enter to open Evidence.

Use `/` or `jobs --filter 'state=failed role="broker-worker"'` to filter
Jobs. Terms are case-insensitive and combined with AND. Use `key=value`
with `backend`, `state`, `role`, `agent` or `model`, or bare text to match
against ID, role, agent or model. Double-quote values with spaces. In the
editor, Enter applies, Esc cancels and `C` clears the applied filter from
Jobs.

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

## Pipeline modes

Pipelines run in either **`hitl`** (human-in-the-loop, default when `[pipeline]` is absent)
or **`autonomous`** mode (`policy/operating_modes.md`), configured in `.devpack/project.toml`:

- **Blocking checkpoints (`plan`, `brief`, `deviation`, `selinux-manifest`):** pause with exit
  code `20` (`AWAITING_APPROVAL`) when human approval is required (in `hitl`, at hard-stops, or
  when gate/lint evidence is red) and write `<state_dir>/approvals/pending/<uuid>.json`. In
  `autonomous` mode, `brief` auto-approves when `runner/brief_lint.py` reports no error-severity
  findings.
- **Terminal checkpoints (`pre-merge`, `escalation`, `memory-decision`):** record pending or
  decided approval entries when a run finishes without altering the run's existing exit code (`0`
  when ready for merge approval; `1` on `ESCALATE_TO_HUMAN`).
- **Tightening only:** CLI `--mode` and `DEVPACK_MODE` may tighten `autonomous` to `hitl`, never
  loosen `hitl` to `autonomous`; `[pipeline.checkpoints]` cannot set hard-stops to `auto`.
- **Human/CI approvals:** `runner/devpack_approve.py` is restricted to human operators and CI
  (refused inside agent sandboxes and by `runner/devpack_broker.py`), with `--status pending`
  available on `list` and `--note <text>` required on `reject` (optional on `approve`). State
  defaults to `$XDG_STATE_HOME/devpack/<project-hash>` outside the worktree. Resuming verifies
  the brief SHA-256 is unchanged and consumes the approval once, and `devpack_run.py --target-branch`
  checks merge-target protection (`pre-merge` fails closed if unknown):

```bash
python3 runner/devpack_approve.py list
python3 runner/devpack_approve.py show <approval-id>
python3 runner/devpack_approve.py approve <approval-id>
python3 runner/devpack_approve.py reject <approval-id>
python3 runner/devpack_run.py --resume <approval-id>
python3 runner/devpack_dispatch.py --resume <approval-id>
```

## Plans

Put a plan in `plans/<plan-id>/plan.toml` and keep its Markdown briefs inside the
same directory. See [the three-task example](workflow/templates/plan.example/plan.toml).
The manifest records `id`, `title`, `goal`, `created` (an ISO date or timestamp),
and optional `owner_role`. Each `[[tasks]]` entry supplies `id`, `title`,
`depends_on`, and a relative Markdown `brief` path. Optional task metadata
includes `role`, `size_limit` (changed lines), and `requirement_ids`.

```bash
python3 runner/devpack_plan.py validate plans/<plan-id>
python3 runner/devpack_plan.py render-md plans/<plan-id>
python3 runner/devpack_plan.py status plans/<plan-id>
python3 runner/devpack_plan.py run-next plans/<plan-id>
python3 runner/devpack_dash.py --plain plans
python3 runner/devpack_dash.py --json plan <plan-id>
```

`render-md` writes to standard output. The first ready task in manifest order
runs through the normal task workflow with its brief and scope. Status uses the
latest attempt for each task. Add `--json` to `status` for structured output. A successful outcome satisfies dependencies, a failed
attempt can be retried, and a running or paused attempt holds the plan. Resume a
paused task with `devpack_run.py --resume <approval-id>`.

Plan commands use `runs/` under the workdir by default. Use the same `--runs-root`
for `run-next`, `status`, and the dashboard when storing attempts elsewhere.
The dashboard accepts `--plans-root` or `[dashboard].plans_root` for an alternate
plan directory. For direct task runs and dispatch jobs, pass `--plan-id` and
`--task-id` to record the link. Use `--plan-dir` for a nonstandard plan location.

## Engines

Use `runner/engines.example.toml` as the starting point. Select a configured engine
for each role or pass its name to dispatch. Broker deployments use an
operator-owned engine file outside the product worktree.

An engine with `class = "external"` runs an argv array in the task working
directory. Set `model` to the configured `agent:model` identity, and specify
`command`, `result`, `reply_from`, `max_data_class`, `timeout_s`, `env_allow`,
and `role_map`. Unmapped roles keep their pack names. Argument templates accept
`{brief}`, `{workdir}`, `{role}`, `{model}`, and `{run_dir}`. Use `{allow_args}`
as its own argument to expand the task scope into repeated `--allow <glob>`
pairs. The reviewer receives no writable scope.

With `result = "json-stdout"`, the command writes one JSON object containing
`status` and `exit_code`. When provided, `agent` and `model` must match the
configured identity. With `result = "exit-code"`, zero means success.
`reply_from` accepts `stdout`, `file:<relative>`, or `json:reply_path`.
Artifact paths are relative to the private `{run_dir}` output directory, or
may use an absolute path inside that directory as seen by the job. A reviewer
supplies a final `VERDICT: APPROVE` or `VERDICT: REQUEST_CHANGES` line, or the
equivalent JSON `verdict` value.

Set `events_from` to a JSONL file under the private `{run_dir}` to show progress
in the dashboard while the job runs. Append one JSON object per line and flush
the file as the job progresses. The runner redacts events before writing them
to the step transcript, reads any remaining events at exit, and records a
truncation notice if the source exceeds 4 MiB.

The commented external job runner example shows how a wrapper can send a brief
to another agent and write a reply and an optional event stream. Package the
executable in the sandbox image selected with `--image-tag`. Select an independent reviewer
engine with a different name and, when both engines are external, a different
`agent:model`. Data classification, brief lint, checkpoint approvals, scope
checks, test locks, and pack-run gates still apply.

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
  python3 runner/devpack_memory.py query --task-file briefs/task.md
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

For offline installation from a verified release archive, follow the
[transfer guide](docs/TRANSFER.md).

1. **Day 0:** complete the data-owner sign-off in `policy/data_governance.md` and
   obtain safety approval for `policy/asil_boundaries.md`.
2. Choose one installation topology and pin the pack version:
   - **A: external pack (recommended; required for the broker).** Install the trusted
     pack outside the product repository, for example `/opt/devpack`. Keep engines,
     credentials, and run logs outside worker scope. Follow `orchestrator/README.md`
     for broker setup. Never vendor executable pack code into a broker workspace.
   - **B: vendored `devpack/` (CI-only).** Vendor the pinned pack under `devpack/`.
     CI executes this trusted copy; workers must never edit it. Use topology A for
     interactive runner/broker jobs. The CI examples assume this layout.
3. Create the project's `.devpack/` directory. Copy `.devpack/project.example.toml`
   to `.devpack/project.toml`, `enforcement/gate/gate.project.example.toml` to
   `.devpack/gate.toml`, and `runner/engines.example.toml` to `.devpack/engines.toml`.
   In project TOML set `gate_config = "gate.toml"`, `engines_config = "engines.toml"`,
   and the repository data class. These config paths resolve relative to the project
   config file; pack-relative defaults resolve against the installed pack. Do not
   edit the pack's engine example or reuse its self-test `gate.example.toml` for a product.
   Add `/gate_receipt.json` and `/runs/` (or your configured runs directory) to the
   product repository's `.gitignore` before running the gate.
4. Configure `[workspace]` and component build/test commands as described in
   `policy/workspace_scope.md`. Discovery is recursive (default depth 4) and merges
   explicit component metadata. Set `[scan] default = "all"` for team adoption;
   use selected components for local fast loops. CI always passes `--all-components`.
   Adapt coding conventions before the first run using the section below.
5. Copy `knowledge/context_template/AGENTS.md` to the project root and replace its
   project-level placeholders. Task role and task ID come from each brief. Before
   creating the task baseline, install skills with
   `python3 /opt/devpack/runner/agent_exec.py --install-skills /path/to/project`
   (substitute your pack path); `--skills` enables installed skills in runner jobs.
   Treat `.claude/skills/` as bootstrap context, outside worker edits. Install the hook using `enforcement/hooks/install.sh --repo /path/to/project`.
6. Set up container isolation and credentials using `policy/sandbox.md`. For local
   endpoints, edit the project's copy of the engines config to name environment
   variables and complete qualification in `policy/local_models.md`.
7. Wire CI using `workflow/ci/pipeline.md` and its examples. The project gate template
   lints and analyzes changed C/C++ files, then runs active components' build/test
   commands. Set `DEVPACK_BASE` to the trusted CI merge-base commit (for example,
   `export DEVPACK_BASE=$(git merge-base HEAD origin/main)` after fetching the target).
   CI rejects an unset base or a base resolving to HEAD. For a shallow clone, fetch
   sufficient history with `git fetch --depth=100 origin main` and increase depth
   until the merge base resolves. Missing commands and unexpectedly
   empty scans fail closed. Only legitimately empty changed-file sets may use
   `--allow-empty`; they print `SKIP: no C/C++ changes`. Changed workspace C/C++ files
   outside every component fail with a path list: increase `discovery_depth`, declare
   the components, or explicitly use `--allow-unowned` to acknowledge the omission.

### Adapting the coding rules

The linter automatically loads **the product repository root's `preflight.toml`**;
`--config` chooses an explicit file. Its strict defaults require camelCase parameters,
meaningful names (`NAMING-MEANINGFUL` rejects placeholders such as `tmp` and `buf`),
and Doxygen `@file` / `@brief` blocks in **tests as well as production files**.
Document each parameter direction using `@param[in]`, `@param[out]`, or
`@param[in,out]`. Add these comments to tests during adoption; test files are not
implicitly exempt. Agree project conventions with the team before recording a baseline.

For an existing snake_case project, copy
`enforcement/lint/preflight.snake_case.example.toml` to the product root as
`preflight.toml`. This ready variant changes naming regexes while retaining meaningful
names, Doxygen checks, and compiler/static-analysis checks. Adjust `[naming]` regexes,
`disallowed_names`, and `allowed_loop_indices` to the project's approved conventions.
`[doxygen] require_file_doxygen` and `require_param_direction` control documentation
requirements; exceptions need the project's documented deviation policy. See
`enforcement/lint/README.md` for all options and portable baseline workflows.

For mixed conventions, retain camelCase defaults for one component and select the
snake_case configuration for another in `.devpack/project.toml`:

```toml
[[component]]
name = "libcore"
path = "libcore"
# camelCase: addValues(), default rules

[[component]]
name = "sensor_hub"
path = "apps/sensor_hub"
preflight_config = "config/sensor_preflight.toml"
# snake_case: add_values(); copy the snake_case example to this config path
```

`preflight_config` paths resolve relative to the product repository root. Without
that setting, a component's own `preflight.toml` is used when present; otherwise the
repository-root config or strict defaults apply. An explicit lint `--config` overrides
component selection; CLI rule overrides apply to every component.

Protected tests always include `tests/**`, `**/tests/**`, `**/test/**`, `**/*_test.cpp`,
and `**/test_*.cpp`. Add repository-relative patterns with top-level
`protected_test_globs = ["checks/**"]` in project TOML (before any table), or
component-relative `test_globs = ["specs/**"]` inside `[[component]]`. Both runners'
`--protected` flags add patterns; defaults remain protected.

## Quickstart

Run these from the installed product repository root after the install steps above.
Set `DEVPACK_ROOT` to your trusted pack location (`/opt/devpack` for topology A,
`$PWD/devpack` for topology B's CI checks).

```bash
export DEVPACK_ROOT=/opt/devpack

# Install the hook into this project.
bash "$DEVPACK_ROOT/enforcement/hooks/install.sh" --repo "$PWD"

# Verify the installed pack itself offline.
(cd "$DEVPACK_ROOT" && bash tests/run_all.sh)
(cd "$DEVPACK_ROOT" && python3 enforcement/gate/gate.py \
  --config enforcement/gate/gate.example.toml --receipt /tmp/devpack_receipt.json)

# Plan a task; engines and gate paths come from this project's .devpack/project.toml.
# --allowed-files globs are relative to [workspace].root (for example root =
# "meta-org/recipes-org/src" makes "middleware/ipc_bus/src/*.cpp" the glob). The dry-run
# prints a WARNING for any glob outside the workspace root or matching no file inside it.
python3 "$DEVPACK_ROOT/runner/devpack_run.py" --task-id task-001 \
  --title "Short title" --goal "One-paragraph goal" --allowed-files "middleware/ipc_bus/src/*.cpp" --dry-run

# Both runners check allowed scope, protected nested tests (--no-deviation),
# and gate-protected control files after each implementer/fixer step.
# --protected adds project-specific test patterns.

# Evaluation plans use the pack's fixtures (no model calls).
(cd "$DEVPACK_ROOT" && python3 eval/run_eval.py --dry-run --mode single --tier all --engines sonnet,haiku --k 1)
(cd "$DEVPACK_ROOT" && python3 eval/run_eval.py --dry-run --mode runner --pairs haiku:sonnet --tier all --k 1)
(cd "$DEVPACK_ROOT" && python3 eval/run_eval.py --dry-run --mode orchestrator --orchestrator sonnet --workers haiku --k 1)
(cd "$DEVPACK_ROOT" && python3 eval/run_eval.py --dry-run --mode memory --engines haiku --k 1)
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
