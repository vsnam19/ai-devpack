# Brief: Implement Fault Counter & Recovery (REQ-LINK-006)

Role: implementer.

## Goal
Implement `faultCount()` and `recoverFromFault(std::uint64_t nowMs)` on `can_fixture::LinkStateMachine` per `docs/spec.md` section `REQ-LINK-006`, without altering existing state transitions for `Idle`, `Connecting`, `Active`, or `Degraded`.

## Input
- `docs/spec.md` (`REQ-LINK-006`)
- `include/link_state_machine.hpp`
- `src/link_state_machine.cpp`

## Expected output
- Declare `recoverFromFault(std::uint64_t nowMs) noexcept` and `faultCount() const noexcept` with full Doxygen comments (`@brief`, `@param[in]`, `@return`) in `include/link_state_machine.hpp`.
- Increment `m_faultCount` on every transition into `LinkState::Fault` inside `onTick(nowMs)`, preserve `m_faultCount` across `recoverFromFault(nowMs)`, and reset `m_faultCount` to `0U` in `reset()`.

## Files allowed
- `include/link_state_machine.hpp`
- `src/link_state_machine.cpp`

## Commands
- `bash build_and_test.sh --no-cmake`

## STOP conditions
Stop and write `STOP_REPORT.md` if the requirement conflicts with existing tests or requires editing files outside `Files allowed`. Never weaken or delete existing tests.
