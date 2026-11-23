# Task: 16-spec-ring-selective-pop

Category: spec-driven
Tier: v2

## Goal
Implement `REQ-RING-005` (`FrameRingBuffer::popByCanId`) as defined in `docs/spec.md`. Add unit tests in a new file tests/test_ring_selective_pop.cpp carrying `@req REQ-RING-005` trace tags and registered with `REGISTER_TEST_SUITE(...)`, covering null/empty rejection, non-match, duplicate-ID FIFO order, and circular wrap-around compaction.

## Files allowed
- `include/ring_buffer.hpp`
- `src/ring_buffer.cpp`
- tests/test_ring_selective_pop.cpp
- `STOP_REPORT.md`

## Commands
- `bash build_and_test.sh`

## STOP rule
If the request cannot be completed within Files allowed, or it conflicts with a requirement or an existing test, do not work around it: write `STOP_REPORT.md` explaining the conflict and stop. Never modify or delete existing tests/test_*.cpp files; add new test cases in tests/test_ring_selective_pop.cpp and register them with `REGISTER_TEST_SUITE(...)`.
