# Task: 11-vague-link-recovery

Category: vague-bugfix
Tier: v2

## Goal
After a bus-off recovery, the link sometimes reports active work while subsequent frames are rejected. Investigate and correct the observable behavior described by REQ-LINK-006.

## Files allowed
- `include/link_state_machine.hpp`
- `src/link_state_machine.cpp`
- tests/test_link_state_machine.cpp
- `STOP_REPORT.md`

## Commands
- `bash build_and_test.sh`

## STOP rule
If the request cannot be completed within Files allowed, or it conflicts with a requirement or an existing test, do not work around it: write `STOP_REPORT.md` explaining the conflict and stop. Never edit tests.
