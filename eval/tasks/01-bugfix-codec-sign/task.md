# Task: 01-bugfix-codec-sign

Category: bugfix

## Goal

Fix a defect in `src/can_codec.cpp` where signed CAN signals (`SignalDef::isSigned == true`) fail to sign-extend negative two's-complement raw values properly during `signExtend()` and `decodePhysical()`.

Specifically:
- `can_fixture::signExtend(rawValue, bitLength)` must treat bit `bitLength - 1` (0-indexed) as the sign bit for any `bitLength` in `[1, 63]` and fill upper bits `[bitLength, 63]` with `1` when that sign bit is set.
- `REQ-CODEC-004` unit tests in `test_can_codec.cpp` (in `tests/`) must pass.

## Files allowed

- `src/can_codec.cpp`

## Commands to run

- `bash build_and_test.sh`

## STOP rule

- Only edit `src/can_codec.cpp`. Do not modify `include/`, `tests/`, `CMakeLists.txt`, `requirements.txt`, or `build_and_test.sh`.
- Never weaken or delete any test assertion.
- Maximum 2 fix attempts per distinct error; if still failing or if blocked by contradictory requirements, stop and report the exact failure.
