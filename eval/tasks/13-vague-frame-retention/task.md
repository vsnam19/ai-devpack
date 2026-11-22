# Task: 13-vague-frame-retention

Category: vague-bugfix
Tier: v2

## Goal
Under sustained traffic, the newest frame is retained but the receiver sees an unexpected gap. Diagnose the retention behavior against REQ-RING-002.

## Files allowed
- `src/ring_buffer.cpp`
- tests/test_ring_buffer.cpp

## Commands
- `bash build_and_test.sh`

## STOP rule
- Do not change public interfaces or protected tests.
