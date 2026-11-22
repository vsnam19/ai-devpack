# Goal: 01-link-fault-recovery

Category: orchestrator-goal
Tier: v2

## Goal
Using `runner/devpack_dispatch.py` for every worker job (never edit product or test files directly as the orchestrator), coordinate two sub-tasks on the repository:

1. **Sub-task 1 — Feature implementation (`include/link_state_machine.hpp`, `src/link_state_machine.cpp`):**
   Implement `REQ-LINK-006` (`faultCount()` and `recoverFromFault(std::uint64_t nowMs)`) from `docs/spec.md` on `can_fixture::LinkStateMachine`, preserving all existing behavior and meeting Doxygen and MISRA C++17 rules (`preflight_lint.py`).
2. **Sub-task 2 — Unit test coverage (tests/test_link_state_machine.cpp):**
   Add unit tests covering `REQ-LINK-006` (initial `faultCount() == 0U`, increment on handshake and heartbeat fault transitions, `recoverFromFault(nowMs)` succeeding only in `LinkState::Fault` with valid config and `nowMs >= lastTimestampMs()` while preserving `faultCount()`, and `reset()` clearing `faultCount()` to `0U`) without modifying or weakening any existing test cases.

## Constraints
- Orchestrator writes briefs under `briefs/` only and dispatches every worker via `python3 runner/devpack_dispatch.py`.
- All fixture tests (`bash build_and_test.sh --no-cmake`) and `preflight_lint.py` must pass.
