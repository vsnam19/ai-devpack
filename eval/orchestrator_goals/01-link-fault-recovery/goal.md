# Goal: 01-link-fault-recovery

Category: orchestrator-goal
Tier: v2

## Goal
Using `.devpack/pack/runner/devpack_dispatch.py --submit` for every worker job (never edit product or test files directly as the orchestrator), coordinate two sub-tasks on the repository:

1. **Sub-task 1 — Feature implementation (`include/link_state_machine.hpp`, `src/link_state_machine.cpp`):**
   Implement `REQ-LINK-006` (`faultCount()` and `recoverFromFault(std::uint64_t nowMs)`) from `docs/spec.md` on `can_fixture::LinkStateMachine`, preserving all existing behavior and meeting Doxygen and MISRA C++17 rules (`preflight_lint.py`).
2. **Sub-task 2 — Unit test coverage (tests/test_link_fault_recovery.cpp):**
   Add unit tests in a new file tests/test_link_fault_recovery.cpp registered with `REGISTER_TEST_SUITE(...)` covering `REQ-LINK-006` (initial `faultCount() == 0U`, increment on handshake and heartbeat fault transitions, `recoverFromFault(nowMs)` succeeding only in `LinkState::Fault` with valid config and `nowMs >= lastTimestampMs()` while preserving `faultCount()`, and `reset()` clearing `faultCount()` to `0U`) without modifying existing protected tests/test_*.cpp files.

## Constraints
- Orchestrator writes briefs under `briefs/` only and dispatches every worker via `python3 .devpack/pack/runner/devpack_dispatch.py --submit`.
- Acceptance is verified by the broker's gate (build, tests, lint).
