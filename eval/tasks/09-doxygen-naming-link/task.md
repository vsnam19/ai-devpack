# Task: 09-doxygen-naming-link

Category: doxygen/naming
Tier: smoke

## Goal

Fix Doxygen documentation defects and member/variable naming violations in `include/link_state_machine.hpp` and `src/link_state_machine.cpp`:

1. **Doxygen compliance (`include/link_state_machine.hpp`)**:
   - Every public method and constructor on `LinkStateMachine` must have a Doxygen comment block with `@brief`.
   - Every parameter in a Doxygen comment must specify `@param[in]`, `@param[out]`, or `@param[in,out]` (bare `@param` without direction is forbidden) and match the declared parameter name.
   - Every non-void method must have a `@return` tag.
2. **Naming compliance (`include/link_state_machine.hpp` and `src/link_state_machine.cpp`)**:
   - Private member variables of `LinkStateMachine` must use the `m_` prefix followed by `camelBack` (`m_config`, `m_state`, `m_lastTimestampMs`, `m_retryCount`) instead of trailing-underscore names (`cfg_`, `st_`, `ts_`, `retries_`).
   - Replace forbidden non-meaningful local variable/parameter names (`tmp`, `val`, `var`) with descriptive `camelBack` domain names (such as `nowMs`, `elapsedMs`).
   - Preserve exact runtime behavior of all methods.

## Files allowed

- `include/link_state_machine.hpp`
- `src/link_state_machine.cpp`

## Commands to run

- `bash build_and_test.sh`

## STOP rule

- Only edit `include/link_state_machine.hpp` and `src/link_state_machine.cpp`.
- Do not change public class/method names, parameter types, return types, or runtime behavior.
- Never modify `tests/`, `CMakeLists.txt`, `requirements.txt`, or `build_and_test.sh`.
- Maximum 2 fix attempts per distinct error; if still failing, stop and report the exact failure.
