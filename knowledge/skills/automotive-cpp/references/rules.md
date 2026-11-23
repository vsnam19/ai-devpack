# Automotive C++17 Rules Reference

> Standard wording is NOT reproduced here. Rules marked `review` trace to secondary sources in [sources.md](sources.md) and carry `verify` until confirmed against licensed MISRA C++:2023 and ISO 26262-6:2018 texts. Rules enforced by `enforcement/lint/preflight_lint.py` list their exact linter rule ID.

---

## 1. Machine-enforced rules (`preflight_lint.py`)

### Dynamic heap allocation
- **Statement:** Do not use `new`, `delete`, `delete[]`, `malloc`, `calloc`, `realloc`, or `free` outside configured allowed init/test paths. Placement `new` (`new (buf) T(...)`) and `= delete` declarations are permitted.
- **Why:** Deterministic memory bounds and absence of runtime allocation failures or fragmentation `[src: MathWorks MISRA C++:2023 theme 21.6.1 — verify]`.
- **Enforced by:** `MISRA-21.6.1` (C++), `MISRA-C-21.3` (C).
```cpp
// Bad
int* samples = new int[8];
// Good
std::array<std::int32_t, 8U> samples{};
```

### Exceptions and RTTI
- **Statement:** Do not use `throw`, `try`, `catch`, `dynamic_cast`, or `typeid` when exceptions and RTTI are disabled (`-fno-exceptions -fno-rtti`).
- **Why:** Predictable control flow and compatibility with embedded safety toolchains `[src: MathWorks MISRA C++:2023 sections 8.2 / 18 — verify]`.
- **Enforced by:** `NO-EXCEPTIONS`, `NO-RTTI`.
```cpp
// Bad
if (len == 0U) { throw std::invalid_argument("empty"); }
// Good
if (len == 0U) { return DecodeStatus::InvalidLength; }
```

### C-style casts
- **Statement:** Do not use C-style casts `(T)expr` (except explicit `(void)expr`); use `static_cast<T>(expr)` (avoid `const_cast` and `reinterpret_cast` unless justified).
- **Why:** C-style casts can silently combine `const_cast`, `static_cast`, and `reinterpret_cast` `[src: MathWorks MISRA C++:2023 8.2.2 — verify]`.
- **Enforced by:** `MISRA-8.2.2`.
```cpp
// Bad
std::int32_t avg = (std::int32_t)(sum / count);
// Good
std::int32_t avg = static_cast<std::int32_t>(sum / count);
```

### Lambda captures
- **Statement:** Do not use implicit default lambda captures (`[=]` or `[&]`, including `[=, ...]` or `[&, ...]`). List every captured variable (and `this`) explicitly.
- **Why:** Prevents unintended lifetime dangling or implicit `this` capture `[src: MathWorks MISRA C++:2023 8.1.1 / 8.1.2 — verify]`.
- **Enforced by:** `MISRA-LAMBDA-CAPTURE`.
```cpp
// Bad
auto fn = [&]() { return m_limit; };
// Good
auto fn = [this]() { return m_limit; };
```

### Constructors and overrides
- **Statement:** Mark constructors callable with a single argument `explicit` (copy/move constructors exempt). Annotate overriding virtual functions with `override` or `final`.
- **Why:** Prevents accidental implicit conversions and silent signature mismatches in inheritance hierarchies.
- **Enforced by:** `MISRA-15.1.3`, `MISRA-13.3.1`.
```cpp
// Bad
class Counter { public: Counter(std::int32_t initial); };
// Good
class Counter { public: explicit Counter(std::int32_t initial) noexcept; };
```

### Control-flow braces, `switch` defaults, and `goto`
- **Statement:** Enclose every `if`, `else`, `for`, `while`, and `do` body in `{}` braces (`else if` permitted). Every `switch` must have a `default:` branch. Never use `goto`.
- **Why:** Prevents dangling-else/statement bugs, unhandled enum/integer cases, and unstructured jumps `[src: MathWorks MISRA C++:2023 9.6.1; clang-tidy avoid-goto — verify]`.
- **Enforced by:** `MISRA-9.3.1`, `MISRA-C-15.6`, `MISRA-9.4.2`, `MISRA-C-16.4`, `MISRA-9.6.1`, `MISRA-C-15.1`.
```cpp
// Bad
if (ready) count++;
// Good
if (ready) {
    ++count;
}
```

### Forbidden C/runtime constructs and comments
- **Statement:** Never call `abort`, `exit`, `_Exit`, `quick_exit`, `system`, `setjmp`, `longjmp`, or unbounded C string functions (`strcpy`, `strcat`, `sprintf`, `vsprintf`, `gets`). Never `#define` keywords or use VLAs. Remove commented-out `//` code and empty `//` lines.
- **Why:** Prevents abrupt process termination, stack unwinding bypass, buffer overflows, and dead-code clutter `[src: MathWorks MISRA C++:2023 sections 18 / 21 — verify]`.
- **Enforced by:** `MISRA-18.5.2`, `MISRA-C-21.8`, `MISRA-21.10.2`, `MISRA-C-21.4`, `MISRA-STR-BOUND`, `MISRA-C-21.17`, `MISRA-C-20.4`, `MISRA-C-18.8`, `MISRA-5.7.2`, `MISRA-COMMENT-REDUNDANT`.

### Header hygiene and parameter count
- **Statement:** Keep non-template, non-constexpr function bodies out of `.h`/`.hpp` headers. Keep functions at `<= 5` parameters (`HIS-PAR` default).
- **Why:** Preserves clean interface/implementation separation and limits interface complexity.
- **Enforced by:** `NO-IMPL-IN-HEADER`, `HIS-PAR`.

### Doxygen and naming
- **Statement:** Every file must have a Doxygen comment (`/**`, `/*!`, `///`, `//!`) with `@file` and `@brief`. Every `@param` must specify `[in]`, `[out]`, or `[in,out]` matching parameter mutability (`const`/value parameters cannot be `[out]` or `[in,out]`). Avoid single-char names (except `i`, `j`, `k` loop indices) and generic placeholders (`tmp`, `temp`, `val`, `var`, `foo`, `bar`, `baz`, `obj`, `dat`, `buf`, `ptr`). Follow configured regex conventions (`CamelCase` types, `camelBack` functions/vars/params, `m_` private members, `kCamelCase` constants, `snake_case` namespaces).
- **Why:** Makes interfaces and ownership unambiguous for humans and automated checks `[src: Doxygen manual — verify]`.
- **Enforced by:** `DOXYGEN-FILE`, `DOXYGEN-FILE-BLOCK`, `DOXYGEN-PARAM-DIR`, `DOXYGEN-PARAM-DIR-MISMATCH`, `NAMING-MEANINGFUL`, `NAMING-CONVENTION`.
```cpp
// Bad
/** @brief Update. @param[in] frame Buffer. @param[out] val Input. */
void update(std::uint16_t& frame, std::uint16_t val);

// Good
/**
 * @brief Update frame with scaled sample.
 * @param[in,out] frame  Target frame updated in place.
 * @param[in]     sample Input sample value.
 */
void update(std::uint16_t& frame, std::uint16_t sample) noexcept;
```

---

## 2. Reviewer checks (not machine-checked)

> `preflight_lint.py` does NOT check the items below. Reviewers and self-reviewing agents must verify them directly.

### No recursion and bounded loops
- **Statement:** Functions shall not call themselves directly or indirectly; every loop shall have a visible upper bound.
- **Why:** Ensures bounded stack depth and deterministic execution time `[src: MathWorks MISRA C++:2023 8.2.10; ISO 26262-6 Table 6 — verify]`.
- **Enforced by:** `review`.
```cpp
// Bad
std::uint32_t factorial(std::uint32_t n) { return (n <= 1U) ? 1U : (n * factorial(n - 1U)); }
// Good
std::uint32_t factorial(const std::uint32_t limit) noexcept {
    std::uint32_t result{1U};
    for (std::uint32_t idx{2U}; idx <= limit; ++idx) { result *= idx; }
    return result;
}
```

### No `union`
- **Statement:** Do not use the `union` keyword.
- **Why:** Inactive union member reads cause type-punning undefined behavior `[src: MathWorks MISRA C++:2023 12.3.1 — verify]`.
- **Enforced by:** `review`.

### Variable initialization and no name reuse
- **Statement:** Initialize every variable at declaration; do not shadow or reuse variable names across nested or overlapping scopes.
- **Why:** Prevents uninitialized reads and accidental shadowing bugs `[src: ISO 26262-6 Table 6 via NVIDIA spark-process — verify]`.
- **Enforced by:** `review`.
```cpp
// Bad
std::int32_t reading;
if (hasInput) { std::int32_t reading = rawInput; }
// Good
std::int32_t reading{0};
if (hasInput) { reading = rawInput; }
```

### Implicit conversions, `nullptr`, and fixed-width integers
- **Statement:** Avoid bool/integer conversions, mixed-signedness comparisons, and narrowing conversions. Use `nullptr` instead of `0` or `NULL`, and fixed-width integers (`std::uint8_t`, `std::int32_t`, etc.) at interfaces.
- **Why:** Prevents sign extension, silent truncation, and overload ambiguity `[src: MathWorks MISRA C++:2023 section 7 — verify]`.
- **Enforced by:** `review` (and compiler `-Wconversion` when enabled).

### Restricted standard library and array decay
- **Statement:** Avoid `atof`/`atoi`/`atol`/`atoll`, `<cstdarg>`, `<csignal>`, `<cctype>`/`<cwctype>`, `memcpy`/`memmove`/`memcmp`, `offsetof`, `setlocale`, and `<cstdio>` C I/O in production paths. Avoid raw array-to-pointer decay; prefer `std::array` or explicit `(ptr, length)` pairs.
- **Why:** Avoids undefined behavior on invalid strings/overlapping buffers and preserves array bounds `[src: MathWorks MISRA C++:2023 sections 7.11.2, 21, 24, 25, 30 — verify]`.
- **Enforced by:** `review`.

### Error handling without exceptions
- **Statement:** Mark non-throwing functions `noexcept`; mark status-returning functions `[[nodiscard]]`; report failures through a status enum, `bool` + `[out]` parameter, or status+value struct; validate pointers, lengths, and arithmetic bounds before use; verify SDK headers and APIs exist.
- **Why:** Common LLM failures include ignored error paths, missing bounds checks leading to buffer/integer overflows, and hallucinated headers `[src: Pavel et al. arXiv 2508.11257; Waterloo thesis — verify]`.
- **Enforced by:** `review`.
```cpp
// Bad (throws or drops error)
std::int32_t decode(const std::uint8_t* bytes, std::size_t len);
// Good (status + value)
[[nodiscard]] DecodeResult decode(const std::uint8_t* bytes, std::size_t len) noexcept;
```

### Single exit point and global mutable state
- **Statement:** Prefer a single return point per function where practical, avoid dead code, and avoid global mutable state (justify any unavoidable global).
- **Why:** Simplifies control/data flow inspection `[src: ISO 26262-6 Table 6 — verify; ASIL B/C/D rating conflict documented in iso26262-unit-design.md]`.
- **Enforced by:** `review`.

### Complete Doxygen contracts
- **Statement:** Ensure every class, struct, enum, and function has `@brief`, `@param` entries appear in declaration order, every non-void function has `@return` describing every outcome, and preconditions/postconditions use `@pre`/`@post` where relevant.
- **Why:** `preflight_lint.py` checks `@file`/`@brief` and `@param` directions, not `@return` or `@pre`/`@post` completeness `[src: Doxygen manual — verify]`.
- **Enforced by:** `review`.

---

## 3. Static-analysis tooling notes

- **`clang-tidy`:** Official LLVM checks have no `misra-*` or `autosar-*` prefix; enable explicit `cppcoreguidelines-*`, `cert-*`, `bugprone-*`, and `misc-*` checks `[src: LLVM clang-tidy list — verify]`.
- **`cppcheck`:** The `cppcheck` MISRA addon targets MISRA C 2012 only, not MISRA C++:2023 `[src: cppcheck.sourceforge.io — verify]`.
- **Undecidable rules:** Static analyzers cannot prove undecidable properties in all cases and can over- or under-report; manual review is required `[src: Qt blog on MISRA undecidability — verify]`.
