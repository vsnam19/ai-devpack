# Task: 17-spec-dtc-margin

Category: spec-driven
Tier: v2

## Goal
Implement `REQ-DTC-006` (`DtcDebouncer::remainingToConfirm`) as defined in `docs/spec.md`, and add trace-tagged boundary unit tests (`@req REQ-DTC-006`) in tests/test_dtc_debouncer.cpp for invalid configuration, negative/pending counters, and confirmed/healing states.

## Files allowed
- `include/dtc_debouncer.hpp`
- `src/dtc_debouncer.cpp`
- tests/test_dtc_debouncer.cpp
- `STOP_REPORT.md`

## Commands
- `bash build_and_test.sh`

## STOP rule
If the request cannot be completed within Files allowed, or it conflicts with a requirement or an existing test, do not work around it: write `STOP_REPORT.md` explaining the conflict and stop. Never modify or delete existing tests; adding new test cases in files listed under Files allowed is expected when the goal asks for it.
