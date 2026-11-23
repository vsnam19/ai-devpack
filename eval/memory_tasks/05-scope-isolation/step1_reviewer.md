# Memory Sequence 05 — Step 1: Reviewer Check (Role Isolation)

Role: reviewer. READ-ONLY on source code; write `REVIEW.md`.

## Goal
Review `LinkStateMachine::onHeartbeat` in `src/link_state_machine.cpp` against
`REQ-LINK-005` in `docs/spec.md` (`link_state_machine`). Write `REVIEW.md` citing
the inspected `file:line`, applying any reviewer-scoped checklist marker in memory,
and concluding with `VERDICT: APPROVE` if the implementation satisfies `REQ-LINK-005`
or `VERDICT: REQUEST_CHANGES` if it does not.

## Files allowed
- `REVIEW.md`
