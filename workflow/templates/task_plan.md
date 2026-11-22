# Task plan: {{PLAN_TITLE}}  ({{PLAN_ID}})

Owner: {{TECH_LEAD_ROLE}}. Drafted by: {{DRAFTER}} (draft only). Approved on: {{DATE}}.

## 0. Common conventions
- Variables: `{{BUILD_CMD}}`, `{{TEST_CMD}}`, `{{GATE_CMD}}`, `{{PRIV_CMD}}`.
- Rules: pointers to `AGENTS.md` and policy files; no personal data in files or commits.
- Failure handling: classify; max 2 fix attempts per distinct error; environment failure
  waits once then retries once; then STOP (`policy/failure_handling.md`).
- Review: one exhaustive pass, max 2 fix rounds, then the tech lead.
- Interface ownership: provider keeps the interface file; consumers reference it.
- Size limit default: {{DEFAULT_MAX_CHANGED_LINES}} changed lines per task.

## Task list
| ID | Title | Depends on | Size limit | Requirement IDs |
|---|---|---|---|---|
| {{TASK_ID}} | {{TITLE}} | {{DEPENDS_ON}} | {{LIMIT}} | {{REQ_IDS}} |

## Task block (repeat per task)

### Task {{TASK_ID}}: {{TITLE}}
- Goal: {{GOAL}}
- Requirement IDs: {{REQ_IDS}} (every requirement ends with at least one test tag)
- Files allowed: {{ALLOWED_GLOBS}}
- Files read-only: {{READONLY_FILES}}
- Size limit: {{LIMIT}} changed lines
- RED: write test `{{TEST_NAME}}` first; command `{{RED_CMD}}`; expected: fails because {{REASON}}.
- GREEN: implement minimal change; command `{{GREEN_CMD}}`; expected: pass.
- Commands: {{COMMANDS}} with expected results.
- STOP if: {{STOP_CONDITIONS}}
- Commit subject: `{{COMMIT_SUBJECT}}`
- Human decision needed before start: {{OPEN_DECISIONS_OR_NONE}}
