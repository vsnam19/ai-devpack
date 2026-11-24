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
fail_on = ["error", "warning"]
```

- Findings whose severity is in `fail_on` (default `error`, `warning`; must be non-empty
  and contain only known cppcheck severities) fail the check (exit `1`) and are pasted
  verbatim into the fixer prompt alongside `preflight_lint.py` findings.
- `style`, `performance`, and `portability` findings are reported in output and JSON
  (`"blocking": false`) but do not block unless added to `fail_on`.
- `--inline-suppr` is **disabled by default** in both `enforcement/static/cppcheck_check.py`
  and `runner/devpack_dispatch.py`. Even if `--inline-suppr` is explicitly enabled via
  trusted config, `cppcheck-suppress` without a matching `Deviation: DEV-<n>` verified in
  `DEVPACK_DEVIATIONS` fails as `SUPPRESSION-WITHOUT-DEVIATION`.
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
- Every preprocessor, syntax, AST, `clang-tidy`, and `cppcheck` subprocess on worker
  sources runs under a timeout and an address-space limit (`resource.setrlimit`
  `RLIMIT_AS`, default `2 GiB`, configurable). Timeout or memory exhaustion fails closed on
  the worker with `LINT-RESOURCE-LIMIT` (exit `1`, never falling back silently to a
  weaker lexical pass and never reporting a harness error).
- Any `#include` naming an absolute path or `/dev/*` device path is rejected before
  compiler or `cppcheck` invocation as `ABSOLUTE-INCLUDE` (exit `1`).
- `INLINE-ASM` (`enforcement/lint/preflight_lint.py`) rejects any `asm`, `__asm`, or
  `__asm__` token in the preprocessed token stream as an error (MISRA C++:2023 [verify]).
  `TEST-FRAMEWORK-TAMPER` is also evaluated over backslash-joined directives and the
  preprocessed token stream so multi-line namespaces and spliced symbol overrides fail.

## Suppressions and deviations

Every inline or file suppression (`cppcheck-suppress`, `NOLINT`, `NOLINTNEXTLINE`,
`#pragma GCC/clang diagnostic`, `_Pragma(...diagnostic...)`) is a deviation
(`policy/deviations.md`) and requires an approved Deviation ID (`DEV-<nnn>`) registered
in `DEVPACK_DEVIATIONS` naming both the affected file and the rule
(`SUPPRESSION-WITHOUT-DEVIATION`). Agents may propose a suppression in an escalation note,
but must never invent a Deviation ID. Any worker-added suppression without `Deviation: DEV-<n>`
verified in `DEVPACK_DEVIATIONS` is blocked by `preflight_lint.py` and `cppcheck_check.py`
as `SUPPRESSION-WITHOUT-DEVIATION`.

Inline suppression format:

```cpp
// cppcheck-suppress <id> -- Deviation: DEV-012 (<brief justification and expiry>)
```

Project-wide suppressions may be placed in the file configured by
`[cppcheck].suppressions_file`, with each line documented by its `DEV-<nnn>` reference.

## Test execution proof (`--check tests`) and symbol allowlist

Before executing any test binary in `eval/tasks/v2_check.sh`, `eval/fixture/build_and_test.sh`,
`eval/lib/check_memory_step.sh`, `eval/orchestrator_goals/`, or
`enforcement/gate/project_checks.py --check tests`, `enforcement/checks/symbol_check.py` runs
`nm -C --defined-only` and `nm -u` on objects compiled from worker-changed sources (and all
component translation units whenever any header in the component changes). It rejects any object
that defines a symbol in the test-framework namespace, collides with a framework-defined symbol, or
references a denied primitive (`__gcov_*`, `_exit`, `_Exit`, `exit`, `quick_exit`, `abort`, `fork`,
`vfork`, `exec*`, `posix_spawn*`, `kill`, `raise`, `signal`, `sigaction`, `dlopen`, `dlsym`,
`syscall`, `ptrace`, `setjmp`/`longjmp`, `std::set_terminate`, `atexit`, `at_quick_exit`, `freopen`,
`dup2`):

- **`eval` import profile (`--import-profile eval`, default for all evaluation graders):** worker
  objects may reference **only** undefined symbols matching the minimal pure-computation allowlist in
  `enforcement/checks/symbol_allowlist_eval.txt` (`memcpy`, `memset`, `memmove`, `memcmp`, `strlen`,
  `round`, `__stack_chk_fail`, and `can_fixture::*`; worker-authored test TUs additionally permit
  the `std::cerr` primitives emitted by `test_framework.hpp`). `operator new`/`delete`, file I/O,
  `std::cout`/`std::basic_streambuf`/`rdbuf`, `/proc`, `getenv`, `mmap`, `dl_iterate_phdr`, and
  any other external import fail closed with `SYMBOL-NOT-ALLOWED`.
- **`production` import profile (default for `project_checks.py --check tests`):** keeps the
  denylist above so normal application/SDK dependencies continue to build, and optionally enforces
  a per-project or per-component allowlist configured in `.devpack/project.toml` via
  `symbol_allowlist = ["^memcpy$", ...]` or `symbol_allowlist_file = "path/to/allowlist.txt"`
  under `[workspace]`, `[symbols]`, or `[[component]]`.

`enforcement/gate/project_checks.py --check tests` refuses to trust plain exit code `0` as
test proof:

- For projects using the pack test framework (`test_framework.hpp` / `CHECK*`), `printSummaryAndExitCode`
  tracks `passedChecks` independently on the success path (`DEVPACK_SUCCESS_PATH`) and returns
  failure unless `passedChecks == totalChecks`, `failedChecks == 0`, and `totalChecks > 0`. The
  gate requires out-of-process `gcov` proof (`enforcement/checks/test_proof.py`) confirming every
  `CHECK*` assertion line in pristine test sources executed at least once, every dedicated
  failure-path line (`DEVPACK_FAILURE_PATH`) executed `0` times, the `DEVPACK_SUCCESS_PATH` gcov
  execution count equals the framework `CHECK` execution count and `DEVPACK_TESTS_COMPLETED` tally,
  `.gcda` files were written after the run started (any missing `.gcda` fails with exit `1`, not
  harness error), zero failure lines appeared in output, and the test exited `0`.
- For non-pack / third-party test runners (e.g. GoogleTest, Catch2, CTest), each component
  must declare `test_xml` (relative to its `build_dir` or `compile_commands` directory;
  duplicate `test_xml` paths across components are a configuration error). The gate deletes
  the declared file before running `test_cmd`, then requires it to exist afterward, be newer
  than the run start, and contain `>= 1` `<testcase>`/`<Test>` and `0` failures/errors.
  Note: XML proof for non-pack runners is **weaker** than pack out-of-process `gcov`
  assertion proof.
- The project gate template (`enforcement/gate/gate.project.example.toml`) also runs
  `enforcement/gate/project_checks.py --check test_lock` (`enforcement/checks/test_lock.py`)
  across every component to enforce `policy/asil_boundaries.md`.

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
