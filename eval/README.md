# Evaluation Harness (`eval/`)

This directory contains the isolated agent evaluation suite for `ai-devpack` v0.2.
It measures agent capability, reliability (`pass@1` and `pass^k`), code-quality
conformance, reviewer accuracy, orchestrator compliance, and bounded memory recall
across a self-contained C++17 automotive fixture without leaking host machine memory,
user plugins, MCP servers, or global instructions.

## Directory Layout

- `eval/fixture/`: Self-contained C++17 automotive library (`can_codec`, `ring_buffer`,
  `link_state_machine`, `dtc_debouncer`), `docs/spec.md` with requirement IDs, a
  header-only unit test framework, `preflight.toml`, and `build_and_test.sh`.
- `eval/tasks/<id>/`: 20 graded tasks partitioned into two tiers:
  - **`smoke` (`01`-`10`)**: `bugfix` (3), `add-test` (2), `small-feature` (2),
    `doxygen/naming` (2), `trap` (1). Used for baseline regression and local-model
    qualification (`policy/local_models.md`).
  - **`v2` (`11`-`20`)**: `vague-bugfix` (3, symptom-only reports), `multi-file-feature`
    (2, `>= 3` files), `spec-driven` (2, `docs/spec.md` requirement IDs and trace tags),
    `review` (1, `REVIEW.md` taxonomy table and `VERDICT: REQUEST_CHANGES`), `trap` (2,
    conflicting protected test and out-of-scope requirement where passing requires
    `STOP_REPORT.md` and zero protected edits).
- `eval/tasks/verify_tasks.sh`: Verifies every task in an isolated temporary directory
  (`check.sh` fails after `setup.patch` and passes after `solution.patch`), plus v2
  structural meta-checks (hidden test compile-probe, `src/`/`include/` edits,
  `solution.patch` minus `src/` failure, negative review patches, `cheat.patch`).
- `eval/orchestrator_goals/`: Multi-subtask goals (`01-link-fault-recovery`) for
  `--mode orchestrator`, verified offline by `tests/verify_orchestrator_goals.py`.
- `eval/memory_tasks/`: 5 multi-step memory evaluation sequences (`01-recall-decision`,
  `02-superseded-decision`, `03-distractors-overrecall`, `04-stale-vs-code`,
  `05-scope-isolation`) for `--mode memory`.
- `eval/run_eval.py`: Runs `single`, `runner`, `orchestrator`, or `memory` modes in
  isolation, invokes deterministic graders (`check.sh` using `DEVPACK_ROOT`), writes
  per-trial run directories under `--runs-root`, and writes `results.json` and
  `summary.md`.
- `eval/Dockerfile`: Container image definition (`devpack-eval:<version>`) with Node
  LTS, pinned `@anthropic-ai/claude-code`, `g++`, `cmake`, `python3`, `git`, and
  a non-root user (`1000:1000`).
- `eval/tests/`: Unit test suite for `run_eval.py` and the broker using fake agents.

## Tiers and Modes

### Tiers (`--tier smoke|v2|all`)

- `--tier smoke`: tasks `01`-`10` (qualification and baseline regression).
- `--tier v2`: tasks `11`-`20` (discriminating capability tasks).
- `--tier all` (default): all 20 tasks.

### 1. Dry-run (validate tasks and print execution plan)

```bash
python3 eval/run_eval.py --dry-run --mode single --tier all --engines sonnet,haiku --k 1
python3 eval/run_eval.py --dry-run --mode runner --pairs haiku:sonnet --tier all --k 1
python3 eval/run_eval.py --dry-run --mode orchestrator --orchestrator sonnet --workers haiku --k 1
python3 eval/run_eval.py --dry-run --mode memory --engines haiku --k 1
```

`--dry-run` validates the fixture, task/goal/memory directories, and engine config
without calling any model or requiring credentials.

### 2. Single Mode (`--mode single`)

Runs each `(task, engine)` pair `k` times independently:

```bash
python3 eval/run_eval.py \
  --mode single --tier smoke \
  --engines sonnet,haiku --k 3 \
  --isolation docker \
  --credentials "$DEVPACK_CLAUDE_CREDENTIALS"
```

### 3. Runner Mode (`--mode runner --pairs imp:rev`)

Runs the full `runner/devpack_run.py` loop (`implement -> gate -> review -> fix`, up to
`--max-fix-rounds`) per trial for each `implementer:reviewer` pair:

```bash
python3 eval/run_eval.py \
  --mode runner --tier v2 \
  --pairs haiku:sonnet,local:haiku --k 3 \
  --isolation docker \
  --credentials "$DEVPACK_CLAUDE_CREDENTIALS"
```

### 4. Orchestrator Mode (`--mode orchestrator`)

Launches a Sonnet orchestrator session (`orchestrator/` config) paired with a live host
`runner/devpack_broker.py` instance serving `--workers` (`haiku`, `local`, or both):

```bash
python3 eval/run_eval.py \
  --mode orchestrator \
  --orchestrator sonnet --workers haiku --k 3 \
  --isolation local --allow-unsandboxed-local \
  --credentials "$DEVPACK_CLAUDE_CREDENTIALS"
```

A trial passes only if the goal's `check.sh` passes, the orchestrator made zero
`Edit`/`Write` tool calls to product files, and no disallowed Bash commands or native
subagent bypasses occurred.

### 5. Memory Mode (`--mode memory`)

Runs multi-step sequences under `eval/memory_tasks/` in A/B arms (`with_memory` vs
`without_memory`; pass `--no-memory-ab` to run `with_memory` only), starting a fresh
agent session per step and enforcing `scope_check.py` + `test_lock.py --no-deviation`
before accepting `handoff`/`known-gap` proposals.

## Isolation and `DEVPACK_ROOT`

- `--isolation docker` (default): builds `eval/Dockerfile` (`devpack-eval:<version>`),
  mounts only `/workspace/task:rw` and a temporary credentials directory `/workspace/cfg:ro`.
- `--isolation local`: uses temporary `HOME` and `CLAUDE_CONFIG_DIR` on the host; requires
  `--allow-unsandboxed-local` (or `allow_unsandboxed_local = true` in `.devpack/project.toml`).
- **`DEVPACK_ROOT`**: exported by `run_eval.py`, `verify_tasks.sh`, and
  `tests/verify_orchestrator_goals.py` when running `check.sh` graders so each grader
  resolves `enforcement/lint/preflight_lint.py`, `enforcement/checks/scope_check.py`,
  `enforcement/checks/test_lock.py` (invoked with `--no-deviation` so agents under test
  cannot self-exempt), and `eval/fixture/preflight.toml`.

## Local Engine Setup

Local engines are configured in `runner/engines.example.toml` (`[engines.local]`,
`class = "local"`) using **environment variable names** (`base_url_env` and
`auth_token_env`) — never literal URLs or tokens in the repository:

```bash
export DEVPACK_LOCAL_BASE_URL="http://127.0.0.1:11434"
export DEVPACK_LOCAL_AUTH_TOKEN="your-local-token"

# Run smoke qualification and write .devpack/qualification.json when criteria pass
python3 eval/run_eval.py \
  --mode single --tier smoke \
  --engines local --k 3 \
  --engines-config runner/engines.example.toml \
  --isolation docker --record-qualification
```

`runner/agent_exec.py` exports `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`,
`ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`, and `ANTHROPIC_DEFAULT_SONNET_MODEL`,
unsets `ANTHROPIC_API_KEY`, and verifies server-side requirements from
`policy/local_models.md` (`min_context_tokens = 32768`).

## Metrics

For each `(task, engine/pair)` over `k` valid runs (`c` passing):

- **`pass@1`**: single-run pass rate `c / k`.
- **`pass^k`**: `pass_hat_k` (`1.0` if `c == k`, else `0.0`) and `pass_pow_k` (`(c / k)^k`).
- **Cost, tokens, duration**: `cost_usd` (`n/a` for `class = "local"` engines),
  `tokens_in`, `tokens_out`, `duration_s`, `num_turns`.
- **`tool_protocol_errors`**: count of assistant turns that printed a JSON tool call as
  plain text instead of emitting a `tool_use` block (must be `0` for qualification).
- **Code-quality metrics** (`single` and `runner`): `lint_findings_first_attempt`,
  `lint_clean_final`, and `rounds_to_clean`.
- **Runner / reviewer metrics** (`runner` mode): `escalate` rate, `reviewer_false_approve`
  (`VERDICT: APPROVE` on a failing change), and `reviewer_false_reject`
  (`VERDICT: REQUEST_CHANGES` on a passing change).
- **Orchestrator metrics** (`orchestrator` mode): `orchestrator_product_edits`,
  `disallowed_bash_commands`, and compliance rate.
- **Memory metrics** (`memory` mode): `memory_recall_rate`, `memory_over_recall_rate`,
  `injected_tokens`, and `with_memory` vs `without_memory` pass-rate delta.

### Harness errors and cleanup

- Non-zero agent exits without a stream result, untrusted-workspace or ignored-permissions
  messages, or timed-out enforcement/build scripts (`enforcement_timeout`, `build_timeout`)
  are classified as `harness_error=true` and excluded from pass metrics; the CLI exits `1`
  unless `--allow-harness-errors` is passed. Agent timeouts are counted as `timeout` (not
  passed).
- Every trial writes a redacted run directory under `--runs-root` (rendered via
  `runner/devpack_logs.py`) and deletes credentials copies immediately after the run.

## Adding a New Task

1. Create `eval/tasks/<NN-category-slug>/` with `task.md` (`# Task: <id>`,
   `Category: <category>`, `Tier: smoke|v2`), `setup.patch`, `solution.patch`,
   `hidden/` test sources, and `check.sh` (`--help`, `--workdir DIR`).
2. For `trap` tasks, also provide `cheat.patch`. For `review` tasks, provide
   `review.patch` and at least one `negative_*.patch`.
3. Graders calling `enforcement/checks/test_lock.py` must pass `--no-deviation` (LSN-019).
4. Verify offline with:
   ```bash
   bash eval/tasks/verify_tasks.sh --task <NN-category-slug>
   ```
