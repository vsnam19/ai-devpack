# Task: 03-bugfix-link-retry

Category: bugfix

## Goal

Fix a timeout handling defect in `src/link_state_machine.cpp` inside `LinkStateMachine::onTick(std::uint64_t nowMs)`.

In `LinkState::Connecting`:
- A handshake timeout occurs when `elapsedMs >= m_config.handshakeTimeoutMs` (inclusive boundary).
- When `m_retryCount < m_config.maxRetries`, `onTick` must increment `m_retryCount`, update `m_lastTimestampMs = nowMs` to start the new retry window, and remain in `LinkState::Connecting` (`REQ-LINK-002`).
- When `m_retryCount >= m_config.maxRetries` and another timeout elapses, `onTick` must transition to `LinkState::Fault` and update `m_lastTimestampMs = nowMs`.

## Files allowed

- `src/link_state_machine.cpp`

## Commands to run

- `bash build_and_test.sh`

## STOP rule

- Only edit `src/link_state_machine.cpp`. Do not modify `include/`, `tests/`, `CMakeLists.txt`, `requirements.txt`, or `build_and_test.sh`.
- Never weaken or delete any test assertion.
- Maximum 2 fix attempts per distinct error; if still failing or if blocked by contradictory requirements, stop and report the exact failure.
