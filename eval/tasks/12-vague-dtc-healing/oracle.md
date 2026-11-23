## Oracle Design & Implementation Guide

### Root Cause & Design
- **Module**: `DtcDebouncer` in `src/dtc_debouncer.cpp` (declared in `include/dtc_debouncer.hpp`).
- **Defect**: In `DtcDebouncer::onOperationCycleEnd()`, the check that heals the fault to `DtcState::Inactive` requires `m_state == DtcState::Healing` in addition to `m_healingCounter >= m_config.agingThreshold`. When `m_config.agingThreshold == 1U`, the debouncer is still in `DtcState::Confirmed` on the first operation cycle end at `clearThreshold`, so the fault remains latched and transitions to `DtcState::Healing` with `healingCounter == 1` instead of clearing to `DtcState::Inactive`.
- **Expected behavior (`REQ-DTC-003`)**: Whenever `isConfigValid()`, `isConfirmed()`, and `counter() == clearThreshold`, `onOperationCycleEnd()` increments `m_healingCounter` and transitions immediately to `DtcState::Inactive` (clearing `m_isLatched = false`, `m_healingCounter = 0U`, and resetting `m_counter = 0`) as soon as `m_healingCounter >= m_config.agingThreshold`, regardless of whether `m_state` was `Confirmed` or `Healing`.

### Interfaces
- `bool DtcDebouncer::onOperationCycleEnd() noexcept` (`include/dtc_debouncer.hpp`, `src/dtc_debouncer.cpp`)

### Files to Touch
- `src/dtc_debouncer.cpp`: remove the extra `m_state == DtcState::Healing` condition when evaluating `m_healingCounter >= m_config.agingThreshold`.
- tests/test_dtc_debouncer.cpp: add a boundary unit test (`@req REQ-DTC-003`) for `agingThreshold == 1U` verifying immediate transition from `Confirmed` at `clearThreshold` to `Inactive` with `counter() == 0` (so subsequent fault samples count up from `0`, not `clearThreshold`).

### Edge Cases
- `agingThreshold > 1U` must still enter `DtcState::Healing` on cycles `1 .. agingThreshold - 1` and clear to `Inactive` on cycle `agingThreshold`.
- Calling `onOperationCycleEnd()` while `!isConfirmed()` or `counter() > clearThreshold` must return `false` without changing counters or state.
- Any `reportSample(true)` while in `DtcState::Healing` must reset `healingCounter()` to `0U` and return state to `DtcState::Confirmed`.
