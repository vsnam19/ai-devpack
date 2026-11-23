# CAN Watchdog and Signal Supervision Specification

## REQ-CAN-020: Buffer and Length Guard
WHEN `packRaw()` is called with `frameBytes == nullptr` or `frameLength < 8` bytes, the `CanCodec` SHALL return `false` without modifying any caller-provided buffer. (Traces to `SYS-CAN-01`; Status: `approved`; ASIL: `QM`; Verification: unit test `TC-CAN-020` passing `nullptr` and `frameLength` values `0` and `7`.)

## REQ-CAN-021: Temperature Frame Decoding
WHEN a temperature frame is received on CAN ID `0x210`, the `CanCodec` SHALL decode the payload into a physical temperature in `degC`. (Traces to `SYS-CAN-02`; Status: `draft`; ASIL: `A`.)

## REQ-CAN-022: Corrupted Frame Handling
The `CanCodec` shall drop corrupted frames and notify the supervisor as soon as possible, if practical. (Traces to `SYS-CAN-02`; Status: `draft`; ASIL: `A`.)

## Unnumbered Requirement: Startup Initialization
The `CanCodec` shall initialize all signal slots to their default values on startup.
