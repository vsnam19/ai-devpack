# Task: 14-feature-dtc-freeze

Category: multi-file-feature
Tier: v2

## Goal
Implement the diagnostic monitor freeze control (`setFrozen`, `isFrozen`) specified by `REQ-DTC-005` in `docs/spec.md`, including header declaration, source implementation, Doxygen documentation on the public API, and boundary unit tests in tests/test_dtc_debouncer.cpp.

## Files allowed
- `include/dtc_debouncer.hpp`
- `src/dtc_debouncer.cpp`
- tests/test_dtc_debouncer.cpp
- `STOP_REPORT.md`

## Commands
- `bash build_and_test.sh`

## STOP rule
If the request cannot be completed within Files allowed, or it conflicts with a requirement or an existing test, do not work around it: write `STOP_REPORT.md` explaining the conflict and stop. Never modify or delete existing tests; adding new test cases in files listed under Files allowed is expected when the goal asks for it.
