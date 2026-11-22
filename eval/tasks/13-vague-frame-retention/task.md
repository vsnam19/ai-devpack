# Task: 13-vague-frame-retention

Category: vague-bugfix
Tier: v2

## Goal
Under sustained traffic, the newest frame is retained but the receiver sees an unexpected gap. Diagnose the retention behavior against REQ-RING-002.

## Files allowed
- `src/ring_buffer.cpp`
- tests/test_ring_buffer.cpp
- `STOP_REPORT.md`

## Commands
- `bash build_and_test.sh`

## STOP rule
If the request cannot be completed within Files allowed, or it conflicts with a requirement or an existing test, do not work around it: write `STOP_REPORT.md` explaining the conflict and stop. Never edit tests.
