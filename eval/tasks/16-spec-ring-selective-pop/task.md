# Task: 16-spec-ring-selective-pop

Category: spec-driven
Tier: v2

## Goal
Implement REQ-RING-005. Add tests carrying `@req REQ-RING-005` trace tags for non-match, first-match, and wrap-around cases.

## Files allowed
- `include/ring_buffer.hpp`
- `src/ring_buffer.cpp`
- `tests/test_ring_buffer.cpp`

## Commands
- `bash build_and_test.sh`

## STOP rule
- Preserve FIFO order and do not alter unrelated tests.
