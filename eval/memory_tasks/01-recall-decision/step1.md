# Memory Sequence 01 — Step 1: Record Freeze Sentinel Handoff

Role: implementer.

## Goal
Add `std::uint8_t freezeSentinelCode() const noexcept` to `can_fixture::DtcDebouncer`
in `include/dtc_debouncer.hpp` and `src/dtc_debouncer.cpp` for `REQ-DTC-005`:
return `0xA5U` when `isConfirmed()` is true, and `0x00U` otherwise.

In your final response, include a `## Memory proposals` block with a `handoff`
entry scoped to `task:01-recall-decision` and tagged
`REQ-DTC-005, dtc_debouncer, freezeSentinelCode, isFreezeSentinelMatch`
recording that the active freeze sentinel code is `0xA5U`.

## Files allowed
- `include/dtc_debouncer.hpp`
- `src/dtc_debouncer.cpp`
