# Task: 18-review-fault-recovery

Category: review
Tier: v2
Status: rebuilding

## Goal
Review `review.patch` against REQ-LINK-006. Write `REVIEW.md` with a review-taxonomy table; identify the seeded defect by function name. The final line must be `VERDICT: REQUEST_CHANGES`.

## Files allowed
- `REVIEW.md`
- `STOP_REPORT.md`

## Commands
- `git apply --check review.patch`

## STOP rule
If the request cannot be completed within Files allowed, or it conflicts with a requirement or an existing test, do not work around it: write `STOP_REPORT.md` explaining the conflict and stop. Never modify or delete existing tests; adding new test cases in files listed under Files allowed is expected when the goal asks for it.
