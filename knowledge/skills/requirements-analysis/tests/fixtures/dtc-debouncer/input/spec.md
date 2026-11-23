# Diagnostic Trouble Code (DTC) Debouncer Specification

## REQ-DTC-010: Debouncer Reset
WHEN `reset()` is called from any state, the `DtcDebouncer` SHALL set `state()` to `DtcState::Inactive`, `counter()` to `0`, `healingCounter()` to `0`, and `isConfirmed()` to `false` before returning. (Traces to `SYS-DIAG-01`; Status: `approved`; ASIL: `B`; Verification: unit test `TC-DTC-010` calling `reset()` from `Pending`, `Confirmed`, and `Healing`.)

## REQ-DTC-011: Fault Confirmation Threshold A
WHEN `reportSample(true)` is called while `isConfirmed()` is `false` and `counter()` reaches `100`, the `DtcDebouncer` SHALL transition to `DtcState::Confirmed` and set `isConfirmed()` to `true`. (Traces to `SYS-DIAG-02`; Status: `draft`; ASIL: `B`.)

## REQ-DTC-012: Fault Confirmation Threshold B
WHEN `reportSample(true)` is called while `isConfirmed()` is `false` and `counter()` reaches `50`, the `DtcDebouncer` SHALL transition to `DtcState::Confirmed` and set `isConfirmed()` to `true`. (Traces to `SYS-DIAG-02`; Status: `draft`; ASIL: `B`.)

## REQ-DTC-013: Sample Processing
The `DtcDebouncer` shall process incoming fault samples appropriately and update the diagnostic status. (Traces to `SYS-DIAG-03`; Status: `draft`; ASIL: `B`.)

## REQ-DTC-014: Invalid Configuration Guard
WHILE `isConfigValid()` is `false`, WHEN `reportSample(true)` or `reportSample(false)` is called, the `DtcDebouncer` SHALL leave `state()`, `counter()`, `healingCounter()`, and `isConfirmed()` unchanged before returning. (Traces to `SYS-DIAG-04`; Status: `approved`; ASIL: `B`; Verification: unit test `TC-DTC-014` calling `reportSample()` with invalid `DtcConfig`.)
