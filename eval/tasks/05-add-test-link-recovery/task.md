# Task: 05-add-test-link-recovery

Category: add-test

## Goal

Add unit tests in `test_link_state_machine.cpp` (in `tests/`) covering requirement `REQ-LINK-005` (from `requirements.txt`):

`REQ-LINK-005: LinkStateMachine in Degraded state shall reset retryCount to zero upon receiving onHeartbeat and reject onHeartbeat when in Idle, Connecting, or Fault.`

Requirements for your new test coverage:
- Include the Doxygen requirement tag `@req REQ-LINK-005` in `test_link_state_machine.cpp` (in `tests/`).
- Use `CHECK(...)` / `CHECK_EQ(...)` assertions to verify:
  1. When `LinkStateMachine` is in `LinkState::Degraded` with `retryCount() >= 1`, calling `onHeartbeat(nowMs)` returns `true`, transitions state back to `LinkState::Active`, and resets `retryCount()` to `0` (so subsequent missed heartbeats start counting retries from 1 again, not from the previous retry count).
  2. Calling `onHeartbeat(nowMs)` returns `false` and does not transition to `Active` when the state machine is in `LinkState::Idle`, `LinkState::Connecting`, or `LinkState::Fault`.
- Call your new test function from `runLinkStateMachineTests()` in `test_link_state_machine.cpp` (in `tests/`).

## Files allowed

- `test_link_state_machine.cpp` (in `tests/`)

## Commands to run

- `bash build_and_test.sh`

## STOP rule

- Only edit `test_link_state_machine.cpp` (in `tests/`). Do not modify `include/`, `src/`, `requirements.txt`, `CMakeLists.txt`, or `build_and_test.sh`.
- Never delete or weaken existing tests or assertions.
- Maximum 2 fix attempts per distinct error; if still failing, stop and report the exact failure.
