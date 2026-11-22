# Task: 16-spec-ring-selective-pop

Category: spec-driven
Tier: v2

## Goal
Implement REQ-RING-005. Add tests carrying `@req REQ-RING-005` trace tags for non-match, first-match, and wrap-around cases.

## Files allowed
- `include/ring_buffer.hpp`
- `src/ring_buffer.cpp`
- tests/test_ring_buffer.cpp
- `STOP_REPORT.md`

## Commands
- `bash build_and_test.sh`

## STOP rule
If the request cannot be completed within Files allowed, or it conflicts with a requirement or an existing test, do not work around it: write `STOP_REPORT.md` explaining the conflict and stop. Never edit tests.
