# Task: 04-add-test-codec-bounds

Category: add-test
Tier: smoke

## Goal

Add unit tests in `test_can_codec.cpp` (in `tests/`) covering requirement `REQ-CODEC-006` (from `requirements.txt`):

`REQ-CODEC-006: Signal packing and unpacking shall reject null pointers, frames shorter than 8 bytes, and signals crossing the 64-bit frame boundary.`

Requirements for your new test coverage:
- Include the Doxygen requirement tag `@req REQ-CODEC-006` in `test_can_codec.cpp` (in `tests/`).
- Use `CHECK(...)` / `CHECK_EQ(...)` assertions to verify:
  1. `isValidSignal`, `packRaw`, and `unpackRaw` return `false` when `startBit + bitLength > 64` (e.g., `startBit = 57, bitLength = 8` or `startBit = 64, bitLength = 1`), and return `true` when `startBit + bitLength == 64`.
  2. `packRaw` and `unpackRaw` return `false` when `frameLength < 8` (e.g., `frameLength = 7` or `0`).
  3. `packRaw` returns `false` when `frameBytes == nullptr`, and `unpackRaw` returns `false` when `frameBytes == nullptr` or `rawOut == nullptr`.
- Call your new test function from `runCanCodecTests()` in `test_can_codec.cpp` (in `tests/`).

## Files allowed

- `test_can_codec.cpp` (in `tests/`)

## Commands to run

- `bash build_and_test.sh`

## STOP rule

- Only edit `test_can_codec.cpp` (in `tests/`). Do not modify `include/`, `src/`, `requirements.txt`, `CMakeLists.txt`, or `build_and_test.sh`.
- Never delete or weaken existing tests or assertions.
- Maximum 2 fix attempts per distinct error; if still failing, stop and report the exact failure.
