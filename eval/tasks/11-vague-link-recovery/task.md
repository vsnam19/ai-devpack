# Task: 11-vague-link-recovery

Category: vague-bugfix
Tier: v2

## Goal
After a supervised link faults out due to exhausted retries, incoming peer heartbeats unexpectedly report the link as active again instead of remaining faulted until an explicit reset. Investigate and correct the supervision behavior against REQ-LINK-005.

## Files allowed
- `include/link_state_machine.hpp`
- `src/link_state_machine.cpp`
- tests/test_link_state_machine.cpp
- `STOP_REPORT.md`

## Commands
- `bash build_and_test.sh`

## STOP rule
If the request cannot be completed within Files allowed, or it conflicts with a requirement or an existing test, do not work around it: write `STOP_REPORT.md` explaining the conflict and stop. Never modify or delete existing tests; adding new test cases in files listed under Files allowed is expected when the goal asks for it.
