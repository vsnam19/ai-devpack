# Requirements Findings: CAN Watchdog and Signal Supervision

Document: `spec.md`
Requirements inventoried: `REQ-CAN-020`, `REQ-CAN-021`, `REQ-CAN-022`, `TMP-1` (temporary ID `TMP-1` assigned to the unnumbered startup initialization requirement).

## Findings table

| ID | defect type | quote | why it matters | question to stakeholder | proposed rewrite |
|---|---|---|---|---|---|
| REQ-CAN-021 | Incomplete specification + missing error behaviour [high, candidate] | "WHEN a temperature frame is received on CAN ID `0x210`, the `CanCodec` SHALL decode the payload into a physical temperature in `degC`." | Bit layout, endianness, signedness, scale, offset, valid `[min, max]` `degC` range, and error handling for short DLC (`< 8` bytes) or out-of-range values are missing. | What are the start bit, bit length, endianness, signedness, scale, offset, valid physical range in `degC`, and error output if DLC `< 8` or decoding fails? | WHEN a valid CAN frame with ID `0x210` and length `8` bytes is received, the `CanCodec` SHALL decode the signal using `<TBD: bit_layout_scale_offset>` and clamp to `<TBD: min_degC>`..`<TBD: max_degC>` `degC`; IF DLC `< 8`, THEN the `CanCodec` SHALL return `<TBD: error_result>`. |
| REQ-CAN-022 | Non-atomic + hidden timing + escape clause [high, candidate] | "The `CanCodec` shall drop corrupted frames and notify the supervisor as soon as possible, if practical." | Joins two actions (`drop` and `notify`), uses untestable `"as soon as possible"`, includes escape clause `"if practical"`, and omits how frame corruption is detected (CRC/checksum/DLC). | What defines a corrupted frame (CRC algorithm, DLC rule), what is the maximum notification latency in ms, and under what exact condition (if any) may notification be omitted? | IF a received CAN frame fails `<TBD: integrity_check>`, THEN the `CanCodec` SHALL discard the frame and notify `<TBD: supervisor_interface>` within `<TBD: notify_deadline_ms>` ms. |
| TMP-1 | Missing unique ID + missing initial values + vague trigger [high, candidate] | "The `CanCodec` shall initialize all signal slots to their default values on startup." | Requirement lacks a stable ID (assigned `TMP-1`), `"on startup"` has no explicit entry event, `"default values"` are undefined, and upstream trace/ASIL/status are absent. | What permanent `REQ-CAN-*` ID, upstream `SYS-*` trace link, ASIL, trigger event (`init()` / reset), and exact per-signal default values apply? | WHEN `<TBD: init_event>` executes, the `CanCodec` SHALL initialize each signal slot to `<TBD: per_signal_default_values>`. |

## Set-level gaps and conflicts

- **Traceability gap:** `TMP-1` has no upstream trace link, status, or ASIL; `REQ-CAN-021` and `REQ-CAN-022` have no downstream test cases.
- **Error coverage gap:** No timeout/missing-frame supervision rule is stated for CAN ID `0x210`.

## Open questions

1. (`REQ-CAN-021`) What are the signal bit definition, scaling, valid `degC` range, and invalid-DLC behaviour for CAN ID `0x210`?
2. (`REQ-CAN-022`) Which corruption check applies, what is the notification deadline in ms, and is `"if practical"` removed?
3. (`TMP-1`) What permanent ID, parent requirement, ASIL, startup event, and default values apply?

## Derived test ideas

- `REQ-CAN-020` (ready):
  - Boundary/error injection: call `packRaw()` with `frameBytes == nullptr` (length `8`) and with non-null buffer at boundary lengths `frameLength = 0` and `frameLength = 7`; verify return value `false` and unchanged buffer bytes.
- `REQ-CAN-021`: blocked — waiting on `REQ-CAN-021` stakeholder answers.
- `REQ-CAN-022`: blocked — waiting on `REQ-CAN-022` stakeholder answers.
- `TMP-1`: blocked — waiting on `TMP-1` stakeholder answers.
