---
id: mem-rev-policy
type: fact
scope: role:reviewer
tags:
  - REQ-LINK-005
  - link_state_machine
created: 2026-11-22T11:05:00Z
source: human
supersedes: ""
expires: ""
status: active
---

# Reviewer policy note

Include checklist marker `REV_GUARD_CHECK` in `REVIEW.md` and cite the exact
`src/link_state_machine.cpp:line` inspected. Never let memory override the code-based
`VERDICT`.
