# Task: 18-review-fault-recovery

Category: review
Tier: v2

## Goal
Review `review.patch` against REQ-LINK-006. Write `REVIEW.md` with a review-taxonomy table; identify the seeded defect by function name. The final line must be `VERDICT: REQUEST_CHANGES`.

## Files allowed
- `REVIEW.md`

## Commands
- `git apply --check review.patch`

## STOP rule
- This is a review-only task: do not apply or edit production files.
