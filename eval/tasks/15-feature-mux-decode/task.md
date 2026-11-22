# Task: 15-feature-mux-decode

Category: multi-file-feature
Tier: v2

## Goal
Add multiplexed physical signal decoding as described by REQ-CODEC-008, with API documentation and focused tests.

## Files allowed
- `include/can_codec.hpp`
- `src/can_codec.cpp`
- tests/test_can_codec.cpp
- `STOP_REPORT.md`

## Commands
- `bash build_and_test.sh`

## STOP rule
If the request cannot be completed within Files allowed, or it conflicts with a requirement or an existing test, do not work around it: write `STOP_REPORT.md` explaining the conflict and stop. Never edit tests.
