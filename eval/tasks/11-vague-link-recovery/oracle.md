## Oracle Design & Implementation Guide

### Root Cause & Design
- **Module**: `LinkStateMachine` in `src/link_state_machine.cpp` (declared in `include/link_state_machine.hpp`).
- **Defect**: `LinkStateMachine::onHeartbeat(std::uint64_t nowMs)` currently only rejects `LinkState::Idle` and `LinkState::Connecting`, allowing heartbeats received while in `LinkState::Fault` to transition the state machine back to `LinkState::Active`.
- **Expected behavior (`REQ-LINK-005`)**: Heartbeats are valid ONLY when the supervised link is already established (`LinkState::Active` or `LinkState::Degraded`) and the timestamp is monotonic (`nowMs >= m_lastTimestampMs`). Any call in `LinkState::Idle`, `LinkState::Connecting`, or `LinkState::Fault` must leave state, `retryCount()`, and `lastTimestampMs()` unchanged and return `false`. Once in `LinkState::Fault`, only `reset()` may leave the faulted state.

### Interfaces
- `bool LinkStateMachine::onHeartbeat(std::uint64_t nowMs) noexcept` (`include/link_state_machine.hpp`, `src/link_state_machine.cpp`)

### Files to Touch
- `src/link_state_machine.cpp`: tighten the state guard in `onHeartbeat` to require `LinkState::Active` or `LinkState::Degraded`.
- tests/test_link_state_machine.cpp: add a unit test (`@req REQ-LINK-005`) covering heartbeat rejection in `Idle`, `Connecting`, and `Fault` (both after handshake-retry exhaustion and after heartbeat-retry exhaustion), plus stale timestamp rejection and recovery only via `reset()`.

### Edge Cases
- Equal timestamp (`nowMs == lastTimestampMs()`) in `Active` or `Degraded` must still succeed.
- Strictly older timestamp (`nowMs < lastTimestampMs()`) in `Active` or `Degraded` must return `false` without mutating state.
- Preserving all existing test assertions in tests/test_link_state_machine.cpp is mandatory.
