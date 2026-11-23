# Task: 12-vague-dtc-healing

Category: vague-bugfix
Tier: v2

## Goal
Under some valid aging configurations, a confirmed diagnostic remains latched after completing the required number of clean operation cycles at the clear floor instead of returning to inactive. Determine the intended aging behavior from REQ-DTC-003 and correct it.

## Files allowed
- `include/dtc_debouncer.hpp`
- `src/dtc_debouncer.cpp`
- tests/test_vague_dtc_healing.cpp
- `STOP_REPORT.md`

## Commands
- `bash build_and_test.sh`

## STOP rule
If the request cannot be completed within Files allowed, or it conflicts with a requirement or an existing test, do not work around it: write `STOP_REPORT.md` explaining the conflict and stop. Never modify or delete existing tests/test_*.cpp files; add new test cases in tests/test_vague_dtc_healing.cpp and register them with `REGISTER_TEST_SUITE(...)`.
