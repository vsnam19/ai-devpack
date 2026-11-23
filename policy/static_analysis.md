# Static analysis (`cppcheck` vs Coverity)

Company CI runs **Coverity** as the authoritative static analyzer for release and safety
gates. Coverity is not available inside the local pack or isolated worker sandboxes. To
catch defects early during implementation and evaluation, `ai-devpack` runs
`enforcement/static/cppcheck_check.py` (wrapping `cppcheck`) everywhere preflight lint
runs:

- Worker dispatch (`runner/devpack_dispatch.py`)
- Task runner gate and fix loop (`runner/devpack_run.py`)
- Evaluation graders (`eval/tasks/v2_check.sh`) and metrics (`eval/run_eval.py`)
- The pack's own gate (`enforcement/gate/gate.example.toml`, check `cppcheck-fixture`)

## Coverage comparison

| Capability | `cppcheck` (local early warning) | Coverity (company CI, authoritative) |
|---|---|---|
| Null-pointer dereference, uninitialized variables, bounds/overflow, leaks | Covered within single translation units and local call chains | Covered with deeper path-sensitive and whole-program analysis |
| Style, performance, portability diagnostics | Reported as non-blocking by default (`fail_on = ["error", "warning"]`) | Project checker profile enforced on CI |
| Deep interprocedural taint / cross-TU dataflow | **Not covered** (limited cross-TU analysis) | **Covered** |
| Concurrency / lock-order / MISRA commercial addon | **Not covered** (no commercial MISRA C++ addon; `enforcement/lint/preflight_lint.py` checks core MISRA-style lexical/AST rules separately) | **Covered** per CI stream configuration |
| Authority | Early warning; local gate fail on blocking severities | **Authoritative** for merge/release acceptance |

Passing `cppcheck` locally does **not** override or waive Coverity findings on company CI.

## Fail-closed behaviour and configuration

`enforcement/static/cppcheck_check.py` reads the `[cppcheck]` table in the project's
`preflight.toml` (or `.devpack/project.toml`):

```toml
[cppcheck]
enable = ["warning", "style", "performance", "portability"]
std = "c++17"
include_dirs = ["include"]
suppressions_file = ""
extra_args = ["--inline-suppr"]
fail_on = ["error", "warning"]
```

- Findings whose severity is in `fail_on` (default `error`, `warning`; must be non-empty
  and contain only known cppcheck severities) fail the check (exit `1`) and are pasted
  verbatim into the fixer prompt alongside `preflight_lint.py` findings.
- `style`, `performance`, and `portability` findings are reported in output and JSON
  (`"blocking": false`) but do not block unless added to `fail_on`.
- `[cppcheck].extra_args` and `--extra-arg` are validated against a strict allowlist
  (`--inline-suppr`, `--check-level=normal|exhaustive`, `--suppress=<id>[:path]`,
  `--std=c++NN`, `-D<NAME>[=value]`, `-U<NAME>`, `--platform=<name>`,
  `--max-ctu-depth=<n>`); disallowed flags (such as `--addon*`, `--rule-file*`,
  `--output-file*`, `--dump`, `--project*`, `--library=`, or escaping
  `--suppressions-list`) fail with exit code `2` before launching `cppcheck`.
- When invoked by `runner/devpack_dispatch.py` or `runner/devpack_run.py`, gate config is
  snapshotted from the base commit (`git show <base>:preflight.toml`) or pack/broker
  project config outside the worker tree, never read from the post-run working tree;
  worker edits to `preflight.toml`, `.devpack/**`, or `[cppcheck]`/lint config files are
  rejected as `gate-touched` scope violations.
- Missing `cppcheck` on `PATH` fails closed with exit code `3` (`TOOL_MISSING`), never
  silently passing. In `eval/tasks/v2_check.sh`, `TOOL_MISSING` maps to
  `GRADER_MISCONFIGURED` (exit `2`).

## Suppressions and deviations

Every suppression is a deviation (`policy/deviations.md`) and requires an approved
Deviation ID (`DEV-<nnn>`). Agents may propose a suppression in an escalation note, but
must never invent a Deviation ID.

Inline suppression format:

```cpp
// cppcheck-suppress <id> -- DEV-012: <brief justification and expiry>
```

Project-wide suppressions may be placed in the file configured by
`[cppcheck].suppressions_file`, with each line documented by its `DEV-<nnn>` reference.

## Baselines for legacy code

For pre-existing code with legacy findings that cannot be fixed in the current task,
create a path-relative JSON baseline (never containing absolute paths) and track it as a
deviation per `policy/deviations.md`:

```bash
# Generate or update a baseline manifest from legacy directories
python3 enforcement/static/cppcheck_check.py \
  --repo-root . \
  --config preflight.toml \
  --baseline cppcheck_baseline.json \
  --baseline-update \
  src/

# Check changed files against the baseline (only new or excess findings block)
python3 enforcement/static/cppcheck_check.py \
  --repo-root . \
  --config preflight.toml \
  --baseline cppcheck_baseline.json \
  --base HEAD
```
