# Task: 11-vague-link-recovery

Category: vague-bugfix
Tier: v2

## Goal
After a supervised link faults out due to exhausted retries, incoming peer heartbeats unexpectedly report the link as active again instead of remaining faulted until an explicit reset. Investigate and correct the supervision behavior against REQ-LINK-005.

## Files allowed
- `include/link_state_machine.hpp`
- `src/link_state_machine.cpp`
- tests/test_vague_link_recovery.cpp
- `STOP_REPORT.md`

## Commands
- `bash build_and_test.sh`

## STOP rule
If the request cannot be completed within Files allowed, or it conflicts with a requirement or an existing test, do not work around it: write `STOP_REPORT.md` explaining the conflict and stop. Never modify or delete existing tests/test_*.cpp files; add new test cases in tests/test_vague_link_recovery.cpp and register them with `REGISTER_TEST_SUITE(...)`.
