# Memory Sequence 01 — Step 2: Implement Sentinel API from Recalled Memory

Role: implementer (fresh session, no conversation carry-over; repo reset to clean baseline).

## Goal
Add both methods below to `can_fixture::DtcDebouncer` in `include/dtc_debouncer.hpp`
and `src/dtc_debouncer.cpp` (`REQ-DTC-005`, `dtc_debouncer`):
1. `std::uint8_t freezeSentinelCode() const noexcept`: return the active freeze
   sentinel code recorded in project memory from Step 1 when `isConfirmed()` is true,
   and `0x00U` otherwise.
2. `bool isFreezeSentinelMatch(std::uint8_t candidateCode) const noexcept`: return `true`
   if and only if `candidateCode` equals the active freeze sentinel code recorded in
   project memory from Step 1.

## Files allowed
- `include/dtc_debouncer.hpp`
- `src/dtc_debouncer.cpp`
