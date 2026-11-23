# Requirements Findings: DTC Debouncer Specification

Document: `spec.md`
Requirements inventoried: `REQ-DTC-010`, `REQ-DTC-011`, `REQ-DTC-012`, `REQ-DTC-013` (no temporary IDs needed).

## Findings table

| ID | defect type | quote | why it matters | question to stakeholder | proposed rewrite |
|---|---|---|---|---|---|
| REQ-DTC-011 | Set-level conflict with REQ-DTC-012 [high, candidate] | "WHEN `reportSample(true)` is called while `isConfirmed()` is `false` and `counter()` reaches `100`, the `DtcDebouncer` SHALL transition to `DtcState::Confirmed` and set `isConfirmed()` to `true`." | Both `REQ-DTC-011` and `REQ-DTC-012` trace to `SYS-DIAG-02` with identical triggers but conflicting confirmation thresholds (`100` vs `50`), making ASIL B diagnostic confirmation ambiguous. | Which confirmation threshold (`100`, `50`, or another project-configured value) is authoritative for `SYS-DIAG-02`, and should the obsolete requirement be removed? | WHEN `reportSample(true)` is called while `isConfirmed()` is `false` and `counter()` reaches `<TBD: confirm_threshold>`, the `DtcDebouncer` SHALL transition to `DtcState::Confirmed` and set `isConfirmed()` to `true`. |
| REQ-DTC-012 | Set-level conflict with REQ-DTC-011 [high, candidate] | "WHEN `reportSample(true)` is called while `isConfirmed()` is `false` and `counter()` reaches `50`, the `DtcDebouncer` SHALL transition to `DtcState::Confirmed` and set `isConfirmed()` to `true`." | Directly contradicts `REQ-DTC-011` (`50` vs `100`) under the same precondition and trace parent `SYS-DIAG-02`. | Should `REQ-DTC-012` be retired or scoped to a different operating mode or DTC fault class than `REQ-DTC-011`? | WHILE `<TBD: distinct_mode_or_fault_class>`, WHEN `reportSample(true)` is called while `isConfirmed()` is `false` and `counter()` reaches `<TBD: confirm_threshold>`, the `DtcDebouncer` SHALL transition to `DtcState::Confirmed` and set `isConfirmed()` to `true`. |
| REQ-DTC-013 | Ambiguous wording + non-conforming syntax + missing error path [high, candidate] | "The `DtcDebouncer` shall process incoming fault samples appropriately and update the diagnostic status." | "appropriately" is untestable; step size (`failStep`/`passStep`), counter clamping, invalid-config behaviour, and pass-sample handling are all missing. | What are the fail/pass counter step sizes, clamping bounds, `reportSample(false)` transitions, and behaviour when configuration is invalid? | WHEN `reportSample(<TBD: fault_flag>)` is called while `<TBD: config_valid_guard>`, the `DtcDebouncer` SHALL update `counter()` by `<TBD: step_rule>` and set `state()` according to `<TBD: state_rule>`. |

## Set-level gaps and conflicts

- **Conflict (`C-01`):** `REQ-DTC-011` (`100`) vs `REQ-DTC-012` (`50`) specify different confirmation thresholds for the identical trigger and `SYS-DIAG-02` parent.
- **Error/degraded gap:** No requirement defines behaviour when configuration is invalid or when `reportSample(false)` is called while unconfirmed or healing.
- **Traceability gap:** `REQ-DTC-011`, `REQ-DTC-012`, and `REQ-DTC-013` lack downstream test references.

## Open questions

1. (`REQ-DTC-011` / `REQ-DTC-012`) Which threshold (`100` or `50`) governs fault confirmation under `SYS-DIAG-02`?
2. (`REQ-DTC-013`) What are `failStep`, `passStep`, saturation bounds, and invalid-config error handling?

## Derived test ideas

- `REQ-DTC-010` (ready):
  - State transition: call `reset()` from `DtcState::Pending`, `DtcState::Confirmed`, and `DtcState::Healing`; assert `state() == DtcState::Inactive`, `counter() == 0`, `healingCounter() == 0`, and `isConfirmed() == false`.
- `REQ-DTC-011`: blocked — waiting on `REQ-DTC-011` / `REQ-DTC-012` conflict resolution.
- `REQ-DTC-012`: blocked — waiting on `REQ-DTC-011` / `REQ-DTC-012` conflict resolution.
- `REQ-DTC-013`: blocked — waiting on `REQ-DTC-013` stakeholder answers.
