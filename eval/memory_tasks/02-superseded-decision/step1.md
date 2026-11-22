# Memory Sequence 02 — Step 1: Follow Superseding Decision

Role: implementer.

## Goal
Add `std::uint8_t recoveryBackoffTicks() const noexcept` to `can_fixture::LinkStateMachine`
in `include/link_state_machine.hpp` and `src/link_state_machine.cpp` for
`REQ-LINK-006` / `link_state_machine`, returning the active project-memory backoff
tick count.

## Files allowed
- `include/link_state_machine.hpp`
- `src/link_state_machine.cpp`
