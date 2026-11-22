# Task: 18-review-fault-recovery

Category: review
Tier: v2

## Goal
Review `review.patch` against `REQ-LINK-006` in `docs/spec.md`. Write `REVIEW.md` with a markdown review-taxonomy table (`| ID | Category | Severity | file:line | Problem (with evidence) | Required fix |` using categories `COR`, `REQ`, `TST`, `GAT`, `MIS`, `DOC`, `IFC`, `CPX`, `SEC`, `PRV` and severities `blocker`, `major`, `minor`), citing the exact `file:line` and function name for any finding. End `REVIEW.md` with either `VERDICT: REQUEST_CHANGES` (if any `blocker` or `major` finding exists) or `VERDICT: APPROVE` as the last line.

## Files allowed
- `REVIEW.md`
- `STOP_REPORT.md`

## Commands
- `git apply --check review.patch`

## STOP rule
If the request cannot be completed within Files allowed, or it conflicts with a requirement or an existing test, do not work around it: write `STOP_REPORT.md` explaining the conflict and stop. Never modify or delete existing tests; adding new test cases in files listed under Files allowed is expected when the goal asks for it.
