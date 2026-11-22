---
name: misra-cpp-2023
description: Generic MISRA C++:2023 coding guidance and pre-flight verification for agents writing automotive C++17 code.
---

# MISRA C++:2023 guidance for agents

> Not verified against the standard text. This is a working digest to avoid common
> violations; the licensed standard and the project's static analyser are authoritative.
> Rule numbers are deliberately not cited here; use `references/rules_digest.md` themes.

## When to use

When implementing or reviewing C++17 code in the project. The gate and the static analyser
decide compliance; do not state that code "is MISRA compliant".

## Core habits

1. **No dynamic allocation after initialisation** in runtime paths (`new`, `delete`, `malloc`, `free`);
   use fixed-size containers or pre-allocated pools.
2. **No C-style casts.** Use `static_cast`; avoid `reinterpret_cast`; never cast away const.
3. **No exceptions or RTTI** if the project compiles with `-fno-exceptions -fno-rtti`.
4. **Every `switch`** covers all enumerators, has a `default` branch where the project requires it, and no implicit fall-through.
5. **Initialise every object** at declaration; no uninitialised reads.
6. **Check every return value** that can signal an error; use `[[nodiscard]]` on such functions.
7. **Single-argument constructors are `explicit`**; overriding functions use `override`.
8. **Fixed-width integers** for data crossing interfaces; no implicit narrowing; use unsigned types for bit operations.
9. **No recursion** in runtime paths. Loops have visible bounds.
10. **Keep functions small and simple:** low cyclomatic complexity, at most 5 parameters (default).
11. **No lambda default captures** (`[&]`, `[=]`) in code that crosses threads.
12. **Avoid macros**; use `constexpr` and `inline`.
13. **Do not suppress** warnings or analyser findings with comments; an unavoidable violation needs a documented deviation (`policy/deviations.md`).

## Compiler contract (example, project may differ)

`-std=c++17 -Wall -Wextra -Wpedantic -Werror` plus `-Wswitch-enum` for state machines.

## Workflow

1. Before editing: read the brief and `references/rules_digest.md`.
2. After editing: `python3 enforcement/lint/preflight_lint.py <changed files>`.
3. Fix findings in your own change. Never edit the lint config to make it pass.
4. If a rule cannot be met: STOP and escalate; do not add a silent suppression.

## Background (external, verify)

- MISRA C++:2023 contains many AUTOSAR C++14 guidelines and targets C++17
  (https://www.parasoft.com/blog/breaking-down-the-autosar-c14-coding-guidelines-for-adaptive-autosar/).
- LLM output often violates MISRA; no model produced fully compliant code in one study
  (https://arxiv.org/abs/2506.23535).
