# Task: 14-feature-dtc-freeze

Category: multi-file-feature
Tier: v2

## Goal
Implement the diagnostic monitor freeze control (`setFrozen`, `isFrozen`) specified by `REQ-DTC-005` in `docs/spec.md`, including header declaration, source implementation, Doxygen documentation on the public API, and boundary unit tests in a new file tests/test_dtc_freeze.cpp registered with `REGISTER_TEST_SUITE(...)`.

## Files allowed
- `include/dtc_debouncer.hpp`
- `src/dtc_debouncer.cpp`
- tests/test_dtc_freeze.cpp
- `STOP_REPORT.md`

## Commands
- `bash build_and_test.sh`

## STOP rule
If the request cannot be completed within Files allowed, or it conflicts with a requirement or an existing test, do not work around it: write `STOP_REPORT.md` explaining the conflict and stop. Never modify or delete existing tests/test_*.cpp files; add new test cases in tests/test_dtc_freeze.cpp and register them with `REGISTER_TEST_SUITE(...)`.
