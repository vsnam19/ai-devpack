# Brief: Fix link recovery

## Goal
Investigate fault recovery in `LinkStateMachine` and fix the supervision bug.

## Input
- `docs/spec.md`

## Expected output
- Update the link state machine implementation.

## Files allowed
- `src/link_state_machine.cpp`

## Constraints
- Only edit `src/link_state_machine.cpp`.

## Gate
- `bash build_and_test.sh`
