# Task: 15-feature-mux-decode

Category: multi-file-feature
Tier: v2

## Goal
Implement multiplexed physical signal decoding (`decodeMultiplexedPhysical`) as specified by `REQ-CODEC-008` in `docs/spec.md`, including header declaration with Doxygen documentation, source implementation, and boundary unit tests in a new file tests/test_mux_decode.cpp registered with `REGISTER_TEST_SUITE(...)`.

## Files allowed
- `include/can_codec.hpp`
- `src/can_codec.cpp`
- tests/test_mux_decode.cpp
- `STOP_REPORT.md`

## Commands
- `bash build_and_test.sh`

## STOP rule
If the request cannot be completed within Files allowed, or it conflicts with a requirement or an existing test, do not work around it: write `STOP_REPORT.md` explaining the conflict and stop. Never modify or delete existing tests/test_*.cpp files; add new test cases in tests/test_mux_decode.cpp and register them with `REGISTER_TEST_SUITE(...)`.
