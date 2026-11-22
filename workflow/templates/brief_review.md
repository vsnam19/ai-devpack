# Review brief: {{TASK_TITLE}}  ({{TASK_ID}})

Role: reviewer. READ-ONLY. Do not edit, build, commit or run containers.
Input: `git diff {{BASE_COMMIT}}..{{HEAD_COMMIT}}`; the task brief `{{BRIEF_FILE}}`; the gate receipt `{{GATE_RECEIPT}}`.
Rules: `AGENTS.md`, `knowledge/skills/review-taxonomy/SKILL.md`.

## Scope
In scope: {{IN_SCOPE}}
Out of scope (record as notes only, never blocker/major): {{OUT_OF_SCOPE}}

## Instructions
1. **EXHAUSTIVE, single pass.** Review the WHOLE change set now. List EVERY finding you can
   find across all files, all error paths, parse/format/buffer/truncation paths and
   retry/timeout paths. Do not hold findings back for a later round. A later round only
   checks the fixes.
2. Check: task fully done (nothing faked: no stubs, always-OK code, placeholders, TODO in
   production paths); correctness; tests really exercise the change and were not weakened;
   gate honesty (no skip or baseline hiding failures, config unchanged); coding standard;
   Doxygen/naming; interface ownership; privacy (no personal data, secrets, host paths);
   commit message claims vs code.
3. Use only the fixed taxonomy (COR, REQ, TST, GAT, MIS, DOC, IFC, CPX, SEC, PRV).
4. Every finding cites `file:line` you read. No evidence = drop it.
5. Do not trust claims in commit messages or the author's report; check the receipt.

## Output (max {{MAX_WORDS}} words)
| ID | Category | Severity (blocker/major/minor) | file:line | Problem | Required fix |
|---|---|---|---|---|---|

Then notes (out-of-scope observations), then the LAST line, exactly one of:

VERDICT: APPROVE
VERDICT: REQUEST_CHANGES

APPROVE only if there is no blocker and no major finding. Nothing may follow the verdict line.
