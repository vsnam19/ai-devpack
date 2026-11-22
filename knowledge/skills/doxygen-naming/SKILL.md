---
name: doxygen-naming
description: Doxygen comment and naming rules for C/C++ files; use when writing or reviewing declarations.
---

# Doxygen and naming

The adopting project may override the naming table in its own context file; the preflight
lint reads the project configuration. Defaults below.

## Doxygen

1. Every file has a header block with `@file` and `@brief`.
2. Every class, struct, enum, free function and method has a block with `@brief`.
3. Every parameter has `@param[in]`, `@param[out]` or `@param[in,out]` with the **correct
   direction** and a meaning, in declaration order. Names must match the signature.
4. Non-void functions have `@return` describing every distinguishable value.
5. Document preconditions, units and ownership where relevant (`@pre`, `@note`).
6. Comments describe behaviour, not history. No commented-out code.

```cpp
/**
 * @brief Encode a signal value into a frame buffer.
 * @param[in]     value   Physical value in the signal unit.
 * @param[in,out] frame   Frame buffer; bits of this signal are overwritten.
 * @param[out]    status  Set to the encoding status.
 * @return true if the value was in range and written.
 */
bool encodeSignal(double value, FrameBuffer& frame, Status& status);
```

## Naming (default convention)

| Item | Style | Example |
|---|---|---|
| Types, enums, classes | CamelCase | `RingBuffer` |
| Functions, methods, variables | camelBack | `encodeSignal` |
| Private/protected members | `m_` prefix | `m_head` |
| constexpr and global constants | `k` + CamelCase | `kMaxFrames` |
| Macros | UPPER_SNAKE, avoid macros | `PROJECT_ASSERT` |
| Files | project rule; one scheme per project, checked by the lint | `ringBuffer.hpp` |

## Parameter count

At most 5 parameters per function (default). Over 5: group related values in a struct,
or split the function. Do not rename to evade the check.

## Renames

A rename-only task must not change behaviour: the diff touches names and comments only,
and all tests stay unchanged and green.
