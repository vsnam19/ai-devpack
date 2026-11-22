# Task: 08-doxygen-naming-codec

Category: doxygen/naming

## Goal

Fix Doxygen documentation defects and identifier naming violations in `include/can_codec.hpp` and `src/can_codec.cpp`:

1. **Doxygen compliance (`include/can_codec.hpp`)**:
   - Every function declaration in `include/can_codec.hpp` (`isValidSignal`, `packRaw`, `unpackRaw`, `signExtend`, `encodePhysical`, `decodePhysical`) must have a Doxygen comment block containing `@brief`, `@return`, and explicit direction tags `@param[in]`, `@param[out]`, or `@param[in,out]` for every parameter.
   - Bare `@param` tags without `[in]`, `[out]`, or `[in,out]` are forbidden.
   - `@param` names in `include/can_codec.hpp` must match the declared parameter names.
2. **Identifier naming compliance (`include/can_codec.hpp` and `src/can_codec.cpp`)**:
   - Replace all forbidden non-meaningful variable and parameter names (`buf`, `val`, `tmp`, `ptr`, `dat`, `var`, `foo`, `bar`) with descriptive `camelBack` domain names (e.g., `frameBytes`, `rawValue`, `rawOut`, `physicalValue`, `physicalOut`, `extractedBits`).
   - Preserve exact runtime behavior of all functions.

## Files allowed

- `include/can_codec.hpp`
- `src/can_codec.cpp`

## Commands to run

- `bash build_and_test.sh`

## STOP rule

- Only edit `include/can_codec.hpp` and `src/can_codec.cpp`.
- Do not change function names, parameter types, return types, or runtime behavior.
- Never modify `tests/`, `CMakeLists.txt`, `requirements.txt`, or `build_and_test.sh`.
- Maximum 2 fix attempts per distinct error; if still failing, stop and report the exact failure.
