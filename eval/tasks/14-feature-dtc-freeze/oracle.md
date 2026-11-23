## Oracle Design & Implementation Guide

### Design
- Add diagnostic monitor freeze control (`REQ-DTC-005`) to `DtcDebouncer`.
- Store a private `bool m_isFrozen` member in `DtcDebouncer`, initialized to `false` in the constructor.
- When `m_isFrozen` is `true`, both `reportSample(bool)` and `onOperationCycleEnd()` become no-ops on internal state (`m_counter`, `m_healingCounter`, `m_state`, `m_isLatched`), returning `m_state` and `false` respectively.
- Calling `reset()` clears `m_isFrozen` back to `false` alongside resetting counters, state, and latch status.

### Interfaces
Declare on `can_fixture::DtcDebouncer` in `include/dtc_debouncer.hpp` with full Doxygen `@brief` and `@param`/`@return` tags:
- `void setFrozen(bool frozen) noexcept;`
- `bool isFrozen() const noexcept;`

### Files to Touch
- `include/dtc_debouncer.hpp`: add private `bool m_isFrozen` and public `setFrozen` / `isFrozen` declarations with Doxygen comments.
- `src/dtc_debouncer.cpp`: initialize `m_isFrozen(false)` in constructor, implement `setFrozen` and `isFrozen`, guard `reportSample` and `onOperationCycleEnd` when `m_isFrozen` is true, and reset `m_isFrozen = false` in `reset()`.
- tests/test_dtc_debouncer.cpp: add unit tests (`@req REQ-DTC-005`) verifying initial `false` state, frozen `reportSample` and `onOperationCycleEnd` no-op behavior in `Inactive`/`Confirmed`/`Healing`, resumption upon `setFrozen(false)`, and clearing via `reset()`.

### Edge Cases
- Freezing while `Confirmed` at `clearThreshold` or during `Healing` must preserve `counter()`, `healingCounter()`, and `isConfirmed()` across both `reportSample(true/false)` and `onOperationCycleEnd()`.
- Follow MISRA/Doxygen rules in `preflight.toml` (Doxygen on all public declarations, `noexcept`, no C-style casts, `U` suffix on unsigned literals).
