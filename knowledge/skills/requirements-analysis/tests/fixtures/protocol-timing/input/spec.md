# Link Handshake Timing Specification

## REQ-COM-001: Handshake Initialization
WHEN `startConnect(nowMs)` is called while `state()` is `LinkState::Idle` and `isConfigValid()` is `true`, the `LinkManager` SHALL transition to `LinkState::Connecting`, set `retryCount` to `0`, and record `lastTimestampMs = nowMs` within `1` processing cycle (`<= 10` ms). (Traces to `SYS-COM-01`; Status: `approved`; ASIL: `QM`; Verification: unit test `TC-COM-001` verifying state transition, `retryCount() == 0`, and execution time `<= 10` ms.)

## REQ-COM-002: Handshake Response Latency
The `LinkManager` shall respond quickly after receiving a handshake request. (Traces to `SYS-COM-02`; Status: `draft`; ASIL: `QM`.)

## REQ-COM-003: Heartbeat Timeout
IF no heartbeat is received within `50`, THEN the `LinkManager` shall enter `LinkState::Degraded`. (Traces to `SYS-COM-03`; Status: `draft`; ASIL: `QM`.)

## REQ-COM-004: Handshake Retry Limit
The `LinkManager` shall allow `TBD` consecutive handshake retries before entering `LinkState::Fault`. (Traces to `SYS-COM-04`; Status: `draft`; ASIL: `QM`.)

## REQ-COM-005: Link Reset Behaviour
WHEN `reset()` is called from any `LinkState`, the `LinkManager` SHALL transition to `LinkState::Idle`, set `retryCount` to `0`, and set `lastTimestampMs` to `0` ms within `1` processing cycle (`<= 10` ms). (Traces to `SYS-COM-05`; Status: `approved`; ASIL: `QM`; Verification: unit test `TC-COM-005` calling `reset()` from `Connecting`, `Active`, `Degraded`, and `Fault`.)
