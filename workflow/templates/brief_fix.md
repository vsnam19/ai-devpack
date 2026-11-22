# Fix brief: {{TASK_TITLE}}  ({{TASK_ID}}), fix round {{ROUND}} of 2

Role: fixer. Same rules and allowed files as the original brief `{{BRIEF_FILE}}`.
Review to address: `{{REVIEW_FILE}}`. Gate receipt of the failing run: `{{GATE_RECEIPT}}`.

## Instructions
1. Handle EVERY blocker and major finding, and cheap minors.
2. For each finding, do exactly one of:
   - **fixed**: change the code; cite the commit SHA;
   - **rejected**: give evidence (file:line, command output) why the finding is wrong.
   "Not needed" without evidence is not a rejection.
3. Never weaken, delete or skip a test or check to resolve a finding.
4. Do not widen scope. A finding needing files outside the allowed list: STOP and escalate.
5. Verify only what you changed first (targeted checks), then the gate runs in CI.
6. Commit with `fix(review): ...`. No tool trailers.
7. This is round {{ROUND}}. After round 2, any remaining failure goes to the human
   (`policy/failure_handling.md`).

## Output table (final message)
| Finding ID | Result (fixed / rejected) | Commit SHA or evidence |
|---|---|---|

Then the standard handoff headings from the implement brief.
