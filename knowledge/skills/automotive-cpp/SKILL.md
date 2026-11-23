---
name: automotive-cpp
description: Write and review safety-related automotive C++17 code (MISRA C++:2023 themes, ISO 26262-6 unit design, Doxygen with param directions, meaningful names, non-exception error handling, preflight lint). Use when implementing, refactoring, documenting, or reviewing automotive C/C++ headers, sources, or unit tests.
---

# Automotive C++17 Safety Coding

## When to use

Use when implementing, refactoring, documenting, or reviewing automotive C++17 headers, sources, or unit tests.
- Check the safety class of the target module first; if unknown or ASIL C-D, stop and escalate (`policy/asil_boundaries.md`).
- Never claim code "is MISRA compliant". The project static analyser and CI gate decide compliance.

## Workflow

1. Read the brief, requirement IDs, and existing public headers. Preserve public signatures unless asked to change them.
2. Design without dynamic heap allocation, exceptions, or RTTI (`-fno-exceptions -fno-rtti`). Report failures via a status enum, `bool` + `[out]` parameter, or status+value struct (`std::optional`-like type).
3. Implement using the enforced patterns below.
4. Add Doxygen (`@file`, `@brief`, `@param[in|out|in,out]`, `@return`) and follow naming rules.
5. Run preflight lint (`python3 enforcement/lint/preflight_lint.py <changed files>`) and compile with `-std=c++17 -Wall -Wextra -Wpedantic -Werror -fno-exceptions -fno-rtti`. Fix every finding before moving on.
6. Run the **Reviewer checks (not machine-checked)** below and execute the unit tests.
7. Report any unavoidable rule conflict with a formal deviation ID (`DEV-<nnn>`, `policy/deviations.md`). Propose deviations in an escalation note; never add silent suppressions.

## Enforced rules (checked by `preflight_lint.py`)

- **No dynamic memory (`MISRA-21.6.1`, `MISRA-C-21.3`):** Never use `new`, `delete`, `delete[]`, `malloc`, `calloc`, `realloc`, or `free` outside configured allowed paths (placement `new` and `= delete` are allowed).
- **No exceptions (`NO-EXCEPTIONS`) or RTTI (`NO-RTTI`):** Never use `throw`, `try`, `catch`, `dynamic_cast`, or `typeid`.
- **No C-style casts (`MISRA-8.2.2`):** Never use `(T)expr` casts (except `(void)expr`); use `static_cast<T>(expr)`.
- **Explicit lambda captures (`MISRA-LAMBDA-CAPTURE`):** Never use `[=]` or `[&]` default captures; list every captured variable (and `this`) explicitly.
- **Explicit constructors and overrides (`MISRA-15.1.3`, `MISRA-13.3.1`):** Mark single-argument constructors (or multi-arg with defaults from parameter 2) `explicit`; mark overridden virtual methods `override` or `final`.
- **Control flow (`MISRA-9.4.2`, `MISRA-C-16.4`, `MISRA-9.3.1`, `MISRA-C-15.6`, `MISRA-9.6.1`, `MISRA-C-15.1`):** Include `default:` in every `switch`; wrap every `if`/`else`/`for`/`while`/`do` body in `{}` braces; never use `goto`.
- **Forbidden calls and macros (`MISRA-18.5.2`, `MISRA-C-21.8`, `MISRA-21.10.2`, `MISRA-C-21.4`, `MISRA-STR-BOUND`, `MISRA-C-21.17`, `MISRA-C-20.4`, `MISRA-C-18.8`):** Never use `abort`/`exit`/`_Exit`/`quick_exit`/`system`, `setjmp`/`longjmp`, `strcpy`/`strcat`/`sprintf`/`vsprintf`/`gets`, keyword `#define` macros, or VLAs.
- **Header hygiene (`NO-IMPL-IN-HEADER`):** Keep non-template, non-constexpr function bodies out of `.h`/`.hpp` files.
- **Comments (`MISRA-5.7.2`, `MISRA-COMMENT-REDUNDANT`):** Remove commented-out code and empty `//` comments.
- **Parameter count (`HIS-PAR`):** Keep functions at `<= 5` parameters; group related parameters in a `struct`.
- **Doxygen (`DOXYGEN-FILE`, `DOXYGEN-FILE-BLOCK`, `DOXYGEN-PARAM-DIR`, `DOXYGEN-PARAM-DIR-MISMATCH`):** Start every file with a Doxygen block (`/**`, `/*!`, `///`, or `//!`) containing `@file` and `@brief`. Document every parameter with `@param[in]`, `@param[out]`, or `@param[in,out]` matching mutability (`const`/value params cannot be `[out]`/`[in,out]`).
- **Naming (`NAMING-MEANINGFUL`, `NAMING-CONVENTION`):** Avoid single-char names (except loop indices `i`, `j`, `k`) and placeholders (`tmp`, `temp`, `val`, `var`, `foo`, `bar`, `baz`, `obj`, `dat`, `buf`, `ptr`). Use `CamelCase` types/enums, `camelBack` functions/vars/params, `m_` private members, `kCamelCase` constants, `snake_case` namespaces.

## Reviewer checks (not machine-checked)

Verify manually during self-review and code review (sourced rules not checked by `preflight_lint.py`; see [rules](references/rules.md)):
- **No recursion and bounded loops:** Never call functions recursively (directly or indirectly); give every loop an explicit upper bound.
- **No `union`:** Replace `union` with explicit typed structs or safe serialization helpers.
- **Variables and types:** Initialize every variable at declaration; do not reuse or shadow names across nested scopes; use `nullptr` (not `0`/`NULL`) and fixed-width integers (`std::uint8_t`, `std::int32_t`).
- **Implicit conversions:** Avoid bool/integer conversions and signedness or width narrowing; use checked explicit conversions.
- **Standard library limits:** Avoid `atoi`/`atof`, `memcpy`/`memmove`/`memcmp`, `<cstdarg>`, `<csignal>`, `<cctype>`, `<cstdio>` I/O, and raw array decay (prefer `std::array` + explicit length).
- **Error handling without exceptions:** Mark non-throwing functions `noexcept` and status returns `[[nodiscard]]`; check every returned error; validate pointers, sizes, and arithmetic bounds before use; verify included SDK headers and APIs exist.
- **Single exit and global state:** Prefer one return point per function where practical and avoid global mutable state (note ASIL rating conflicts in [ISO 26262-6 unit design](references/iso26262-unit-design.md)).
- **Doxygen completeness:** Add `@brief` on every class/struct/enum/function, list `@param` in declaration order, add `@return` on every non-void function, and document `@pre`/`@post` contracts.
- **Tooling limits:** `clang-tidy` has no `misra-*` or `autosar-*` prefix (enable explicit `cppcoreguidelines-*`, `cert-*`, `bugprone-*`, `misc-*`); `cppcheck` MISRA covers MISRA C 2012 only; undecidable rules require human review.

## Worked example

Header (`sensor_limiter.hpp`) and implementation (`sensor_limiter.cpp`):

```cpp
/** @file sensor_limiter.hpp @brief Bounded sensor reading clamping interface. */
#ifndef SENSOR_LIMITER_HPP
#define SENSOR_LIMITER_HPP
#include <cstdint>

/** @brief Status returned by SensorLimiter operations. */
enum class LimitStatus : std::uint8_t { Ok = 0U, OutOfRange = 1U };

/** @brief Clamps raw sensor readings to a fixed safe range. */
class SensorLimiter {
public:
    /** @brief Construct limiter with upper bound. @param[in] maxSample Maximum allowed reading. */
    explicit SensorLimiter(std::int32_t maxSample) noexcept;

    /**
     * @brief Clamp a sample in place and report whether clamping occurred.
     * @param[in]     minSample  Minimum allowed reading.
     * @param[in,out] sample     Reading validated and clamped on return.
     * @param[out]    wasClamped Set true when sample exceeded bounds.
     * @return LimitStatus::Ok if in range, LimitStatus::OutOfRange if clamped.
     */
    [[nodiscard]] LimitStatus clampSample(
        std::int32_t minSample, std::int32_t& sample, bool& wasClamped) const noexcept;
private:
    std::int32_t m_maxSample;
};
#endif
```

```cpp
/** @file sensor_limiter.cpp @brief Bounded sensor reading clamping implementation. */
#include "sensor_limiter.hpp"

SensorLimiter::SensorLimiter(const std::int32_t maxSample) noexcept : m_maxSample(maxSample) {}

LimitStatus SensorLimiter::clampSample(
    const std::int32_t minSample, std::int32_t& sample, bool& wasClamped) const noexcept
{
    LimitStatus status{LimitStatus::Ok};
    wasClamped = false;
    if (sample < minSample) {
        sample = minSample;
        wasClamped = true;
        status = LimitStatus::OutOfRange;
    } else if (sample > m_maxSample) {
        sample = m_maxSample;
        wasClamped = true;
        status = LimitStatus::OutOfRange;
    }
    return status;
}
```

## References

- [Rules reference](references/rules.md)
- [ISO 26262-6 unit design](references/iso26262-unit-design.md)
- [Sources and open items](references/sources.md)
