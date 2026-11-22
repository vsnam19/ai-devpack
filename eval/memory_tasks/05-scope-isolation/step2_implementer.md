# Memory Sequence 05 — Step 2: Implementer Guard Constant (Role & Engine Isolation)

Role: implementer.

## Goal
Add `std::uint8_t heartbeatGuardCode() const noexcept` to `can_fixture::LinkStateMachine`
in `include/link_state_machine.hpp` and `src/link_state_machine.cpp` for
`REQ-LINK-005` / `link_state_machine`, returning the implementer-scoped guard
constant from project memory.

## Files allowed
- `include/link_state_machine.hpp`
- `src/link_state_machine.cpp`
