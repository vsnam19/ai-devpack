# Requirements Findings: Link Handshake Timing Specification

Document: `spec.md`
Requirements inventoried: `REQ-COM-001`, `REQ-COM-002`, `REQ-COM-003`, `REQ-COM-004` (no temporary IDs needed).

## Findings table

| ID | defect type | quote | why it matters | question to stakeholder | proposed rewrite |
|---|---|---|---|---|---|
| REQ-COM-002 | Ambiguous wording + missing timing/error path [high, candidate] | "The `LinkManager` shall respond quickly after receiving a handshake request." | "quickly" is unverifiable; response deadline, tolerance, and invalid-request behaviour are unspecified. | What is the maximum handshake response latency in ms, its tolerance, and the action if the request is malformed or received outside `LinkState::Idle`? | WHEN a valid handshake request is received while `state()` is `<TBD: valid_state>`, the `LinkManager` SHALL send the handshake response within `<TBD: response_deadline_ms>` ms. |
| REQ-COM-003 | Incomplete specification: missing unit + missing escalation path [high, candidate] | "IF no heartbeat is received within `50`, THEN the `LinkManager` shall enter `LinkState::Degraded`." | `50` has no unit (ms vs ticks), no tolerance, no starting state, and no escalation path if heartbeats remain absent in `LinkState::Degraded`. | What is the unit and tolerance for `50` (e.g., ms or ticks), which source states apply, and what transition occurs if heartbeats remain absent in `LinkState::Degraded`? | WHILE `state()` is `<TBD: active_state>`, IF no heartbeat is received within `50` `<TBD: timeout_unit>` (+/- `<TBD: tolerance>`), THEN the `LinkManager` SHALL transition to `LinkState::Degraded`. |
| REQ-COM-004 | Open placeholder (`TBD`) + missing fault recovery [high, candidate] | "The `LinkManager` shall allow `TBD` consecutive handshake retries before entering `LinkState::Fault`." | Open `TBD` retry count blocks implementation and testing; behaviour on timeout expiry and recovery from `LinkState::Fault` are not stated. | What is the maximum consecutive handshake retry count, the per-retry timeout duration in ms, and the exit/recovery condition from `LinkState::Fault`? | WHILE `state()` is `LinkState::Connecting`, IF a handshake attempt times out and `retryCount < <TBD: max_retries>`, THEN the `LinkManager` SHALL increment `retryCount` and retry; otherwise it SHALL enter `LinkState::Fault`. |

## Set-level gaps and conflicts

- **State/error coverage gap:** No requirement specifies recovery out of `LinkState::Fault` or handling when `isConfigValid()` is `false` on `startConnect()`.
- **Traceability gap:** `REQ-COM-002`, `REQ-COM-003`, and `REQ-COM-004` have no downstream verification criteria (`TC-*`).

## Open questions

1. (`REQ-COM-002`) What is the maximum handshake response time (ms + tolerance) and invalid-state response?
2. (`REQ-COM-003`) What is the time unit and tolerance for `50`, and how does `LinkState::Degraded` escalate or recover?
3. (`REQ-COM-004`) What is the integer value for `max_retries`, the retry timeout (ms), and the `LinkState::Fault` recovery rule?

## Derived test ideas

- `REQ-COM-001` (ready):
  - Equivalence/transition: call `startConnect(nowMs)` in `LinkState::Idle` with `isConfigValid() == true`; verify `state() == LinkState::Connecting`, `retryCount() == 0`, `lastTimestampMs() == nowMs`, and completion within `1` cycle (`<= 10` ms).
  - Boundary/invalid: call `startConnect(nowMs)` with `isConfigValid() == false` or from non-`Idle` states (flag expected rejection behaviour with stakeholder if unspecified).
- `REQ-COM-002`: blocked — waiting on `REQ-COM-002` stakeholder answers.
- `REQ-COM-003`: blocked — waiting on `REQ-COM-003` stakeholder answers.
- `REQ-COM-004`: blocked — waiting on `REQ-COM-004` stakeholder answers.
