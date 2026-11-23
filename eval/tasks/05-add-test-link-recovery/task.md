# Task: 05-add-test-link-recovery

Category: add-test
Tier: smoke

## Goal

Add unit tests in a new file tests/test_link_recovery.cpp covering requirement `REQ-LINK-005` (from `requirements.txt`):

`REQ-LINK-005: LinkStateMachine in Degraded state shall reset retryCount to zero upon receiving onHeartbeat and reject onHeartbeat when in Idle, Connecting, or Fault.`

Requirements for your new test coverage:
- Create tests/test_link_recovery.cpp (do not modify existing protected tests/test_*.cpp files).
- Include `"link_state_machine.hpp"` and `"test_framework.hpp"`, and include the Doxygen requirement tag `@req REQ-LINK-005`.
- Use `CHECK(...)` / `CHECK_EQ(...)` assertions to verify:
  1. When `LinkStateMachine` is in `LinkState::Degraded` with `retryCount() >= 1`, calling `onHeartbeat(nowMs)` returns `true`, transitions state back to `LinkState::Active`, and resets `retryCount()` to `0` (so subsequent missed heartbeats start counting retries from 1 again, not from the previous retry count).
  2. Calling `onHeartbeat(nowMs)` returns `false` and does not transition to `Active` when the state machine is in `LinkState::Idle`, `LinkState::Connecting`, or `LinkState::Fault`.
- Register your test suite function using `REGISTER_TEST_SUITE(<fn>);` in tests/test_link_recovery.cpp so `test_main.cpp` invokes it automatically.

## Files allowed

- tests/test_link_recovery.cpp

## Commands to run

- `bash build_and_test.sh`

## STOP rule

- Only create tests/test_link_recovery.cpp. Do not modify existing tests/test_*.cpp, `include/`, `src/`, `requirements.txt`, `CMakeLists.txt`, or `build_and_test.sh`.
- Never delete or weaken existing tests or assertions.
- Maximum 2 fix attempts per distinct error; if still failing, stop and report the exact failure.
