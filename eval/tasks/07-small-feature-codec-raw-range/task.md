# Task: 07-small-feature-codec-raw-range

Category: small-feature
Tier: smoke

## Goal

Implement a new validation function `can_fixture::isRawInRange` satisfying requirement `REQ-CODEC-007` (from `requirements.txt`):

`REQ-CODEC-007: isRawInRange shall verify whether a signed or unsigned integer value fits within a signal definition bitLength and signedness.`

Specifically:
1. In `include/can_codec.hpp`, declare in namespace `can_fixture`:
   ```cpp
   bool isRawInRange(const SignalDef& signal, std::int64_t candidateValue) noexcept;
   ```
   with full Doxygen documentation (`@brief`, `@param[in] signal`, `@param[in] candidateValue`, `@return`).
2. In `src/can_codec.cpp`, implement `isRawInRange(const SignalDef& signal, std::int64_t candidateValue) noexcept`:
   - If `!isValidSignal(signal)`, return `false`.
   - If `!signal.isSigned` (unsigned signal):
     - Return `false` if `candidateValue < 0`.
     - If `signal.bitLength >= 63U`, return `true` (all non-negative `std::int64_t` values fit in 63 or 64 unsigned bits).
     - Otherwise, return `static_cast<std::uint64_t>(candidateValue) <= ((1ULL << signal.bitLength) - 1ULL)`.
   - If `signal.isSigned` (signed two's-complement signal):
     - If `signal.bitLength >= 64U`, return `true` (all `std::int64_t` values fit in 64 signed bits).
     - Otherwise, compute `minSigned = -(1LL << (signal.bitLength - 1U))` and `maxSigned = (1LL << (signal.bitLength - 1U)) - 1LL`, and return `candidateValue >= minSigned && candidateValue <= maxSigned`.
3. In a new test file tests/test_codec_raw_range.cpp (do not modify existing protected tests/test_*.cpp files), add unit tests tagged `@req REQ-CODEC-007` and register the test function via `REGISTER_TEST_SUITE(...)` from `test_framework.hpp`.

## Files allowed

- `include/can_codec.hpp`
- `src/can_codec.cpp`
- tests/test_codec_raw_range.cpp

## Commands to run

- `bash build_and_test.sh`

## STOP rule

- Only edit `include/can_codec.hpp` and `src/can_codec.cpp`, and create tests/test_codec_raw_range.cpp. Do not modify existing tests/test_*.cpp.
- Follow all Doxygen (`@param[in]`) and naming rules (no non-meaningful names such as `tmp`, `val`, `buf`).
- Never delete or weaken existing tests or assertions.
- Maximum 2 fix attempts per distinct error; if still failing, stop and report the exact failure.
