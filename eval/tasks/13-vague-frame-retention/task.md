# Task: 13-vague-frame-retention

Category: vague-bugfix
Tier: v2

## Goal
Under sustained traffic, the ring buffer retains the newest frames with the expected drop count, yet an upper-nibble counter co-packed into the same payload byte as a lower-nibble status signal reads back as zero from retained frames. Diagnose the underlying defect against REQ-CODEC-002 and REQ-RING-002.

## Files allowed
- `src/can_codec.cpp`
- `src/ring_buffer.cpp`
- tests/test_vague_frame_retention.cpp
- `STOP_REPORT.md`

## Commands
- `bash build_and_test.sh`

## STOP rule
If the request cannot be completed within Files allowed, or it conflicts with a requirement or an existing test, do not work around it: write `STOP_REPORT.md` explaining the conflict and stop. Never modify or delete existing tests/test_*.cpp files; add any new test cases in tests/test_vague_frame_retention.cpp and register them with `REGISTER_TEST_SUITE(...)`.
