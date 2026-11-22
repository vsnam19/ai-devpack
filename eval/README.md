# Evaluation Harness (`eval/`)

This directory contains the isolated agent evaluation suite for `ai-devpack` v0.1.
It measures agent capability and consistency (`pass@1` and `pass^k`) across a
self-contained C++17 automotive fixture and 10 graded tasks without leaking host
machine memory, user plugins, MCP servers, or global instructions.

## Directory Layout

- `eval/fixture/`: Self-contained C++17 automotive library (CAN signal codec,
  fixed-capacity frame ring buffer, and supervised link state machine) with a
  header-only unit test framework and requirement traceability file.
- `eval/tasks/<id>/`: 10 graded evaluation tasks across 5 categories:
  - `bugfix` (3 tasks)
  - `add-test` (2 tasks, verified with mutation testing)
  - `small-feature` (2 tasks)
  - `doxygen/naming` (2 tasks)
  - `trap` (1 canary task with an impossible contradictory test assertion where
    passing the test implies test tampering; the agent must stop and write
    `STOP_REPORT.md`)
- `eval/tasks/verify_tasks.sh`: Verifies every task in an isolated temporary
  directory (`check.sh` fails after `setup.patch` and passes after `solution.patch`).
- `eval/run_eval.py`: Runs `tasks x models x k` in an isolated workspace, runs
  each task's deterministic `check.sh` grader, and writes `results.json` and
  `summary.md`.
- `eval/Dockerfile`: Container image definition (`devpack-eval:0.1`) with Node
  LTS, pinned `@anthropic-ai/claude-code`, `g++`, `cmake`, `python3`, `git`, and
  a non-root user (`1000:1000`).
- `eval/tests/`: Unit test suite for `run_eval.py` using a mocked agent script.

## Running the Evaluation Suite

### 1. Dry-run (validate tasks and print execution plan)

```bash
python3 eval/run_eval.py --dry-run --tasks all --models sonnet,haiku --k 5
```

`--dry-run` validates the fixture directory and all selected task directories
(`task.md`, `check.sh`, `setup.patch`, `solution.patch`, `hidden/`) and prints
the planned trials without calling any model or requiring credentials.

### 2. Docker Isolation Mode (`--isolation docker`)

Docker mode is the primary isolation mechanism:

```bash
python3 eval/run_eval.py \
  --isolation docker \
  --models sonnet,haiku \
  --k 5 \
  --tasks all \
  --credentials "$DEVPACK_CLAUDE_CREDENTIALS" \
  --out eval/results
```

In Docker mode:
- `eval/Dockerfile` is built once with tag `devpack-eval:0.1`.
- Each trial runs in a fresh container as non-root (`--user 1000:1000`) with
  `--network default` (required for API access).
- Only two host paths are mounted into the container:
  1. The per-trial task workspace (`/workspace/task:rw`).
  2. A temporary directory containing only a copy of the credentials file
     (`/workspace/cfg:ro`, mounted read-only).
- If `docker` is missing or the image build fails, `run_eval.py` exits with an
  explicit error and never silently falls back to `local` mode.

### 3. Local Isolation Mode (`--isolation local`)

When Docker is unavailable on a developer workstation, `--isolation local`
isolates the CLI configuration environment:

```bash
python3 eval/run_eval.py \
  --isolation local \
  --models haiku \
  --k 3 \
  --tasks 01-bugfix-codec-sign \
  --credentials "$DEVPACK_CLAUDE_CREDENTIALS" \
  --out eval/results
```

In local mode:
- Each trial creates a fresh directory under `/tmp/devpack-eval/<run-id>/`
  (overridable via `--work-root`).
- `HOME` points to `<trial-dir>/home` and `CLAUDE_CONFIG_DIR` points to
  `<trial-dir>/cfg`, which contains only a copy of the credentials file.
- The agent is invoked with:
  ```bash
  HOME=<trial>/home CLAUDE_CONFIG_DIR=<trial>/cfg \
    claude -p --model <model> \
    --setting-sources project \
    --strict-mcp-config \
    --no-session-persistence \
    --disable-slash-commands \
    --output-format json \
    "<prompt>"
  ```
- A project settings file (`.claude/settings.json`) is written into the task
  workspace allowing `Read`, `Edit`, `Write`, and `Bash` in the working
  directory and denying `WebFetch` and `WebSearch`.
- **Security limitation**: In `--isolation local` mode, commands executed via
   the `Bash` tool still run on the host kernel and can read host files outside
  the task directory. Always prefer `--isolation docker` for untrusted code or
  strict benchmark runs.

## Metrics (`pass@1` and `pass^k`)

For each `(task, model)` pair evaluated over `k` independent runs with `c`
passing runs (`0 <= c <= k`):

- **`pass@1`**: Empirical single-run pass rate `p = c / k`.
- **`pass^k` (all `k` runs pass)**: Measures agent reliability and consistency
  across repeated runs rather than best-of-`k` luck:
  - `pass_hat_k` (strict all-`k` pass indicator): `1.0` if `c == k` (every one
    of the `k` trials passed), else `0.0`.
  - `pass_pow_k` (Bernoulli consistency estimate): `p^k = (c / k)^k` (for
    example, a 75% single-run pass rate yields `0.75^3 = 42.2%` at `k = 3`).
- **Per-category summary**: Aggregates `pass@1`, `pass^k`, mean cost (USD),
  mean turns, and mean duration (seconds) across all tasks in each category
  (`bugfix`, `add-test`, `small-feature`, `doxygen/naming`, `trap`).

All outputs in `results.json` and `summary.md` automatically redact credential
contents, credential paths, and host home directory paths (`$HOME`).

## Adding a New Task

1. Create a directory `eval/tasks/<NN-category-slug>/`.
2. Add `task.md` starting with `# Task: <id>` and `Category: <category>`,
   followed by `## Goal`, `## Files allowed`, `## Commands to run`, and
   `## STOP rule`.
3. Create `setup.patch` (applied on top of `eval/fixture/` to introduce the bug
   or task starting state) and `solution.patch` (the reference solution).
4. Add `hidden/hidden_test.cpp` containing independent tests that grade the
   requirement without being visible to the agent during the trial.
5. Add `check.sh` supporting `--help` and `--workdir DIR` that verifies file
   scope against post-setup SHA-256 hashes, runs `build_and_test.sh`, compiles
   and runs `hidden/hidden_test.cpp`, and exits `0` on pass or `1` on fail.
6. Verify the task with:
   ```bash
   bash eval/tasks/verify_tasks.sh --task <NN-category-slug>
   ```
