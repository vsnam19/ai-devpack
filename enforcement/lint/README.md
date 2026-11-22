# C/C++ Preflight Linter (`preflight_lint.py`)

`enforcement/lint/preflight_lint.py` is a single, versioned, project-neutral C/C++ preflight linter designed for coding agents and CI gates. It requires only the Python 3.10+ standard library and `clang`/`clang++` (with optional `clang-tidy` execution when installed).

---

## Quickstart

```bash
# Lint source and header files (fail-closed if clang/AST tools are missing)
python3 enforcement/lint/preflight_lint.py src/ include/

# Lint using a project configuration file and compile database
python3 enforcement/lint/preflight_lint.py \
  --config preflight.toml \
  --compile-commands build/compile_commands.json \
  src/

# Emit machine-readable JSON output
python3 enforcement/lint/preflight_lint.py --json src/
```

---

## Rules List and IDs

| Rule ID | Category | Default Severity | Description |
|---|---|---|---|
| `MISRA-8.2.2` | AST / Lexical (C++) | `error` | C-style casts `(T)expr` are forbidden (except explicit `(void)expr`); use `static_cast`, `const_cast`, or `reinterpret_cast`. |
| `MISRA-21.6.1` | AST / Lexical (C++) | `error` | Dynamic heap allocation (`new`, `delete`, `delete[]`, `malloc`, `calloc`, `realloc`, `free`) is forbidden outside configured allowed paths (`allowed_heap_paths` / `--allow-new-paths` / `--allow-new-in-init`). Placement `new` (`new (buf) T(...)`) and `= delete` declarations are permitted. |
| `MISRA-C-21.3` | Lexical (C) | `error` | C dynamic memory allocation (`malloc`, `calloc`, `realloc`, `free`) is forbidden outside configured allowed paths. |
| `MISRA-LAMBDA-CAPTURE` | AST / Lexical (C++) | `error` | Implicit default lambda captures (`[=]` or `[&]`, including `[=, ...]` and `[&, ...]`) are forbidden; list all captured variables explicitly. |
| `MISRA-15.1.3` | AST / Lexical (C++) | `error` | Constructors callable with a single argument (1 parameter, or multiple parameters where parameters 2..N have default values) must be marked `explicit` (copy and move constructors are exempt). |
| `MISRA-13.3.1` | AST / Compiler / Lexical (C++) | `error` | Member functions overriding a base class `virtual` function must be explicitly annotated with `override` or `final`. |
| `MISRA-9.4.2` | AST / Lexical (C++) | `error` | Every `switch` statement in C++ must contain a `default:` clause. |
| `MISRA-C-16.4` | AST / Lexical (C) | `error` | Every `switch` statement in C must contain a `default:` clause. |
| `MISRA-9.3.1` | AST / Lexical (C++) | `error` | Bodies of `if`, `else`, `for`, `while`, and `do` statements in C++ must be enclosed in compound statement braces `{}` (`else if` is permitted). |
| `MISRA-C-15.6` | AST / Lexical (C) | `error` | Bodies of `if`, `else`, `for`, `while`, and `do` statements in C must be enclosed in compound statement braces `{}`. |
| `MISRA-9.6.1` | AST / Lexical (C++) | `error` | The `goto` statement is forbidden in C++. |
| `MISRA-C-15.1` | AST / Lexical (C) | `error` | The `goto` statement is forbidden in C. |
| `MISRA-5.7.2` | Lexical | `error` | Commented-out code in `//` comments is forbidden. |
| `MISRA-COMMENT-REDUNDANT` | Lexical | `error` | Empty `//` comments are forbidden. |
| `MISRA-18.5.2` | Lexical (C++) | `error` | Process-terminating and shell execution calls (`abort`, `exit`, `_Exit`, `quick_exit`, `system`) are forbidden. |
| `MISRA-C-21.8` | Lexical (C) | `error` | Process-terminating and shell execution calls (`abort`, `exit`, `_Exit`, `quick_exit`, `system`) are forbidden in C. |
| `MISRA-21.10.2` | Lexical (C++) | `error` | `<setjmp.h>`, `<csetjmp>`, `setjmp`, and `longjmp` are forbidden. |
| `MISRA-C-21.4` | Lexical (C) | `error` | `<setjmp.h>`, `setjmp`, and `longjmp` are forbidden in C. |
| `MISRA-STR-BOUND` | Lexical (C++) | `error` | Unbounded C string functions (`strcpy`, `strcat`, `sprintf`, `vsprintf`, `gets`) are forbidden; use bounded alternatives such as `snprintf`. |
| `MISRA-C-21.17` | Lexical (C) | `error` | Unbounded C string functions (`strcpy`, `strcat`, `sprintf`, `vsprintf`, `gets`) are forbidden in C. |
| `MISRA-C-20.4` | Lexical | `error` | Preprocessor macros (`#define`) shall not redefine a C/C++ keyword. |
| `MISRA-C-18.8` | AST | `error` | Variable Length Arrays (VLAs) are forbidden. |
| `NO-EXCEPTIONS` | AST / Lexical (C++) | `error` | C++ exception constructs (`throw`, `try`, `catch`) are forbidden when `no_exceptions = true` (default). |
| `NO-RTTI` | AST / Lexical (C++) | `error` | Run-Time Type Information constructs (`dynamic_cast`, `typeid`) are forbidden when `no_rtti = true` (default). |
| `NO-IMPL-IN-HEADER` | AST (Headers) | `error` | Non-template, non-constexpr function or method definitions with bodies `{ ... }` are forbidden in header files (`.h`, `.hh`, `.hpp`) when `no_impl_in_header = true` (default). |
| `DOXYGEN-FILE` | Doxygen | `error` | Every source and header file must contain a Doxygen file block with `@file` and `@brief` (or `\file` and `\brief`). |
| `DOXYGEN-PARAM-DIR` | Doxygen | `error` | Every `@param` tag must include an explicit direction: `@param[in]`, `@param[out]`, or `@param[in,out]`. |
| `NAMING-MEANINGFUL` | Naming | `error` | Disallows single-character variable and parameter names (except configured loop indices `i`, `j`, `k`) and generic placeholder names (`tmp`, `temp`, `val`, `var`, `foo`, `bar`, `baz`, `obj`, `dat`, `buf`, `ptr`). |
| `NAMING-CONVENTION` | Naming | `error` | Validates identifier names (classes/structs/enums/type aliases, functions/methods, variables, parameters, member fields, constants, namespaces) against configurable regex patterns. |
| `HIS-PAR` | Complexity | `error` | Functions, methods, and constructors may take at most `max_params` parameters (default `5`). |
| `CLANG-COMPILE` | Compiler | `error` | Compiler syntax or warning-as-error diagnostic from `clang` / `clang++`. |
| `CLANG-TIDY` | External Tool | `error` | Static analysis diagnostic reported by optional `clang-tidy`. |
| `TOOL-MISSING` | Tooling / Gate | `error` (or `warning` with `--allow-missing-tools`) | Reported when the required compiler/AST tool (`clang`/`clang++`) is missing, AST generation fails, or requested `compile_commands.json` information is missing. Causes a non-zero exit (`1`) by default (fail-closed) unless `--allow-missing-tools` is passed. |

---

## Configuration File Format (`preflight.toml`)

`preflight_lint.py` automatically loads `preflight.toml` from the repository root if present, or from an explicit path via `--config path/to/preflight.toml`. Command-line flags override values from `preflight.toml`.

```toml
# preflight.toml — Configuration for preflight_lint.py

[general]
std = "c++17"                       # C++ language standard: "c++14", "c++17", "c++20"
c_std = "c11"                       # C language standard: "c99", "c11", "c17"
max_params = 5                      # Maximum function parameters (HIS-PAR)
allow_missing_tools = false         # Fail-closed by default when AST/compiler info is missing
skip_clang_tidy = false             # Skip clang-tidy even if installed
no_exceptions = true                # Enforce NO-EXCEPTIONS (-fno-exceptions)
no_rtti = true                      # Enforce NO-RTTI (-fno-rtti)
no_impl_in_header = true            # Enforce NO-IMPL-IN-HEADER
allow_new_in_init = false           # Allow dynamic heap allocation in *init* files
allowed_heap_paths = [              # Globs where new/delete/malloc/free are allowed
  "src/bootstrap/*",
  "tests/*"
]
skip_dirs = [                       # Directories skipped during recursive directory traversal
  ".git",
  "build",
  "_deps",
  "third_party",
  "generated"
]
exclude_globs = []                  # Repository-relative file globs excluded from linting
disabled_rules = []                 # Rule IDs to disable (e.g. ["NO-IMPL-IN-HEADER"])
exempt_param_prefixes = []          # Function name prefixes exempt from HIS-PAR

[doxygen]
require_file_doxygen = true         # Require @file and @brief in every file (DOXYGEN-FILE)
require_param_direction = true      # Require @param[in|out|in,out] (DOXYGEN-PARAM-DIR)

[naming]
check_meaningful = true             # Enforce NAMING-MEANINGFUL
disallowed_names = [
  "tmp", "temp", "val", "var", "foo", "bar", "baz", "obj", "dat", "buf", "ptr"
]
allowed_loop_indices = ["i", "j", "k"]

# Configurable regex patterns for NAMING-CONVENTION (set to "" to disable a category):
class_regex = "^[A-Z][A-Za-z0-9]*$"
function_regex = "^[a-z][A-Za-z0-9]*$"
variable_regex = "^(?:k[A-Z][A-Za-z0-9]*|m_[a-z][A-Za-z0-9]*|[a-z][A-Za-z0-9]*)$"
parameter_regex = "^[a-z][A-Za-z0-9]*$"
member_regex = "^(?:m_)?[a-z][A-Za-z0-9]*$"
private_member_regex = "^m_[a-z][A-Za-z0-9]*$"
constant_regex = "^k[A-Z][A-Za-z0-9]*$"
namespace_regex = "^[a-z][a-z0-9_]*$"

[plugins]
paths = []                          # Optional paths to custom Python plugin modules
```

---

## Baseline Usage and Portable Fingerprints

Baseline manifests record SHA-256 fingerprints of accepted legacy findings so CI gates block new or excess findings while tolerating existing debt.

### Key Properties
- **Path-relative and machine-portable**: All file paths and diagnostic messages are normalized relative to the repository root (`--repo-root` or auto-detected `.git`/`preflight.toml`/`CMakeLists.txt`). Absolute paths are never stored in baseline manifests and are rejected during manifest validation.
- **Reflow-stable**: Fingerprints are computed from normalized C/C++ statement/declaration tokens, enclosing symbol scope, normalized diagnostic path, rule ID, and severity—Not physical line numbers—so `clang-format` reflows or unrelated line insertions do not invalidate existing baseline entries.
- **Versioned policy (`fingerprint_policy_version`)**: Every baseline JSON manifest records `schema_version` and `fingerprint_policy_version` (currently `2`). If a manifest has a mismatched `fingerprint_policy_version`, `preflight_lint.py` exits with code `2`, prints the expected vs actual policy version, and provides an explicit migration command.

### Recording / Updating a Baseline
```bash
# Record or update the default baseline for a target tree
python3 enforcement/lint/preflight_lint.py \
  --baseline lint_baseline.json \
  --baseline-update \
  src/ include/

# Record a named scope and profile
python3 enforcement/lint/preflight_lint.py \
  --baseline lint_baseline.json \
  --baseline-scope can_codec \
  --baseline-profile release \
  --baseline-update \
  src/can_codec
```

### Comparing Against a Baseline
```bash
# Verify no new or excess findings exist relative to the baseline
python3 enforcement/lint/preflight_lint.py \
  --baseline lint_baseline.json \
  src/ include/
```
During baseline comparison, `preflight_lint.py` logs status lines to `stderr`:
- `BASELINE_NEW <path>:<line> [<rule>] count=<n> fp=<hash>` — New unbaselined finding (causes exit code `1`).
- `BASELINE_EXCESS <path>:<line> [<rule>] observed=<n> allowed=<m> fp=<hash>` — Finding count exceeds baseline allowance (causes exit code `1`).
- `BASELINE_FIXED <path> [<rule>] observed=<n> allowed=<m> fp=<hash>` — Finding count decreased in a checked file (informational; exits `0`).

### Migrating on `fingerprint_policy_version` Mismatch
If `fingerprint_policy_version` changes in a newer version of `preflight_lint.py`, validation fails closed with exit code `2` and prints:
```text
error: fingerprint_policy_version mismatch in lint_baseline.json: expected 2, got 1. Migration hint: regenerate the baseline manifest by running 'python3 enforcement/lint/preflight_lint.py --baseline lint_baseline.json --baseline-update <targets>'.
```

---

## Tool Availability and Fail-Closed Policy

- **Compiler / AST / Compile Database (`TOOL-MISSING`)**:
  By default, `preflight_lint.py` is **fail-closed**: if `clang`/`clang++` is missing, if the Clang AST dump fails, or if `--compile-commands` is missing an entry for a checked source file, a `TOOL-MISSING` finding is emitted and the linter exits with code `1`.
  Pass `--allow-missing-tools` (or set `allow_missing_tools = true` in `preflight.toml`) to allow lexical-only fallback runs to exit `0` when no other findings exist (the `TOOL-MISSING` finding is still reported in the output).
- **Optional `clang-tidy`**:
  When `clang-tidy` is installed on `PATH`, it is executed automatically on C++ source files (unless `--skip-clang-tidy` is passed). When `clang-tidy` is absent, `preflight_lint.py` prints an explicit notice to `stderr` (`notice: clang-tidy not found; skipping optional clang-tidy checks`) and never skips silently.

---

## Plugin Hook Interface

Platform- or framework-specific rules are excluded from the core linter. Teams needing custom file extensions or domain-specific checks can load Python plugin modules via `--plugin path/to/plugin.py` or `[plugins] paths = [...]` in `preflight.toml`. No platform plugin is shipped.

A plugin module may define any subset of the following attributes:

```python
"""Example custom preflight lint plugin."""

import pathlib
from typing import Any
import preflight_lint

# Optional: additional file extensions to collect when scanning directories
SUPPORTED_EXTENSIONS = {".idl"}


def check_file(
    path: pathlib.Path,
    raw_text: str,
    stripped_text: str,
    config: preflight_lint.LintConfig,
) -> list[preflight_lint.Finding]:
    """Run custom file-level or lexical checks."""
    findings: list[preflight_lint.Finding] = []
    for line_num, line in enumerate(stripped_text.splitlines(), start=1):
        if "FORBIDDEN_API(" in line:
            findings.append(
                preflight_lint.Finding(
                    file=str(path),
                    line=line_num,
                    rule="CUSTOM-FORBIDDEN-API",
                    severity="error",
                    message="FORBIDDEN_API is not permitted",
                )
            )
    return findings


def check_ast_node(
    node: dict[str, Any],
    path: pathlib.Path,
    line: int,
    raw_text: str,
    config: preflight_lint.LintConfig,
) -> list[preflight_lint.Finding]:
    """Run custom Clang AST node checks."""
    return []
```

---

## Exit Codes

| Exit Code | Meaning |
|---|---|
| `0` | Clean run: no findings (or all findings are within baseline allowances, or only `TOOL-MISSING` with `--allow-missing-tools`). |
| `1` | Lint check failed: one or more active findings (including `TOOL-MISSING` in default fail-closed mode) or baseline regression (`BASELINE_NEW` / `BASELINE_EXCESS`). |
| `2` | Usage, configuration, or execution error: invalid CLI flags, malformed `preflight.toml`, missing target files, baseline `schema_version` / `fingerprint_policy_version` mismatch, or subprocess crash. |
